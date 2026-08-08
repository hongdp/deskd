"""Per-role runtime tuning: which provider, model, and reasoning tier a
role's NEXT session launches with.

These are operator decisions that live on the registry row's authority dict
under the canonical keys ``provider`` / ``model`` / ``reasoning``. They are
runtime STATE, not declaration — a host that re-seeds its registry from a
spec should preserve them (they were set by an operator, not a deploy).
Everything here is per-role on purpose: differing per role is the feature,
so there is no "all" fan-out for model/reasoning (provider keeps one for
migration convenience).

The engine stores and surfaces; the driver (deskd agent run, or a host's
own) reads :func:`role_runtime` and hands the values to the provider. A
session that already exists always resumes under the provider that created
it — see agent_run's harness guard — so every setting here takes effect on
the next NEW session only.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..providers import REASONING_TIERS, get_provider
from .store import _agent_role, _log_event, connect

#: The authority keys this module owns.
RUNTIME_KEYS = ("provider", "model", "reasoning")


def _authority(conn: sqlite3.Connection, role: str) -> dict:
    row = conn.execute(
        "SELECT authority FROM agent_registry WHERE role=?", (role,)).fetchone()
    if row is None:
        raise ValueError(f"agent role is not registered: {role}")
    return json.loads(row["authority"]) or {}


def role_runtime(role: str, db_path: Path | str | None = None) -> dict:
    """{'provider', 'model', 'reasoning'} for one role, defaults resolved.

    ``provider`` falls back to ``CONFIG.default_provider``; model/reasoning
    stay None when unset (the provider then applies its own defaults)."""
    from ..config import CONFIG
    with connect(db_path) as conn:
        role = _agent_role(conn, role)
        authority = _authority(conn, role)
    provider = authority.get("provider") or CONFIG.default_provider
    reasoning = authority.get("reasoning")
    return {
        "provider": provider,
        "model": authority.get("model") or None,
        "reasoning": reasoning if reasoning in REASONING_TIERS else None,
        "allowed_tools": tuple(authority.get("allowed_tools") or ()) or None,
    }


def set_role_runtime(role: str, key: str, value: str | None, *,
                     actor: str = "operator",
                     db_path: Path | str | None = None) -> dict:
    """Set (or with None clear) one runtime key for one role.

    Validation is the point of routing writes through here: an unknown
    provider name or reasoning tier must fail at SET time, in the operator's
    face — not at three in the morning when the driver tries to launch with
    it."""
    if key not in RUNTIME_KEYS:
        raise ValueError(f"unknown runtime key {key!r}; have {RUNTIME_KEYS}")
    if value is not None:
        value = value.strip()
        if not value:
            raise ValueError(f"{key} must be a non-empty string or None to clear")
        if key == "provider":
            get_provider(value)                 # raises on unknown, lists known
        if key == "reasoning" and value not in REASONING_TIERS:
            raise ValueError(
                f"unsupported reasoning tier {value!r}; choose one of "
                f"{REASONING_TIERS} (or clear)")
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        authority = _authority(conn, role)
        previous = authority.get(key)
        if value is None:
            authority.pop(key, None)
        else:
            authority[key] = value
        conn.execute("UPDATE agent_registry SET authority=? WHERE role=?",
                     (json.dumps(authority), role))
        _log_event(conn, actor, f"runtime_{key}_set", role,
                   {"previous": previous, "value": value,
                    "takes_effect": "next new session"})
    return {"role": role, "key": key, "previous": previous, "value": value}


def runtime_overview(db_path: Path | str | None = None) -> list[dict]:
    """Every role's resolved runtime tuning, for `deskd runtime show`."""
    from ..config import CONFIG
    out = []
    for name in CONFIG.role_names():
        rt = role_runtime(name, db_path=db_path)
        out.append({"role": name, **{k: rt[k] for k in RUNTIME_KEYS}})
    return out
