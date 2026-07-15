"""Shared test fixtures.

Two things make this suite different from a typical one, and both are
deliberate:

1. **`CONFIG` is process-wide and mutable.** That is the engine's injection
   seam (a host calls `configure()` at startup), so tests must restore it or
   they leak into each other. The `desk` fixture snapshots every field and
   puts it back, unconditionally.

2. **The roles are deliberately nonsense.** `alpha`/`beta`/`gamma`, not
   anything domain-shaped. deskd was extracted from a host that had hardcoded
   its two real roles into the engine, and the bug that fell out — a third
   role silently getting no meeting obligations, no delivery projection, and
   therefore no wakes — was invisible precisely because every test used the
   two blessed names. A test suite that names its roles after real agents
   cannot catch that class of bug. If a test here needs a specific role name
   to pass, the engine has a hardcoded role and the test is right to fail.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deskd import config as cfg_mod  # noqa: E402
from deskd.config import CONFIG, PromptBuilder, RoleSpec  # noqa: E402

#: Three roles, so "works for N roles" is actually exercised. A two-role suite
#: passes happily against a two-role hardcoding.
ROLES = (
    RoleSpec("alpha", "Alpha"),
    RoleSpec("beta", "Beta"),
    RoleSpec("gamma", "Gamma"),
)


class _RecordingPrompts(PromptBuilder):
    """Deterministic prompts, so assertions don't depend on prose."""

    def bootstrap(self, role: str) -> str:
        return f"BOOTSTRAP:{role}"

    # Signature must track config.PromptBuilder.wake: the engine calls it with
    # the inbox titles it wants carried into the woken session's prompt, and a
    # narrower override here fails every wake test with a TypeError rather than
    # anything that points at the real contract.
    def wake(self, role: str, reason: str, inbox_titles: list[str] | None = None) -> str:
        return f"WAKE:{role}:{reason}"


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """A configured engine over a fresh DB. Yields the resolved EngineConfig.

    Restores every CONFIG field afterwards — including on failure — because
    CONFIG is process-wide and pytest runs everything in one process.
    """
    saved = {f.name: getattr(CONFIG, f.name)
             for f in dataclasses.fields(CONFIG)}
    cfg_mod.configure(
        roles=ROLES,
        db_path=tmp_path / "desk.db",
        timezone="America/New_York",   # NOT UTC: catches naive-tz assumptions
        probe_allowlist=(),            # deny-all by default; tests opt in
        prompt_builder=_RecordingPrompts(),
    )
    try:
        yield CONFIG
    finally:
        for k, v in saved.items():
            setattr(CONFIG, k, v)


@pytest.fixture
def conn(desk):
    """A write connection with the schema applied."""
    from deskd import orchestration

    with orchestration.connect(write=True) as c:
        yield c


def iso(offset_seconds: float = 0.0) -> str:
    """A UTC ISO timestamp offset from now — the engine's canonical form."""
    return (dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(seconds=offset_seconds)).isoformat(timespec="seconds")


def rows(conn, sql: str, params=()) -> list[tuple]:
    return conn.execute(sql, params).fetchall()


def scalar(conn, sql: str, params=()):
    r = conn.execute(sql, params).fetchone()
    return r[0] if r else None
