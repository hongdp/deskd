"""Authenticated, idempotent command dispatcher for isolated agents."""

from __future__ import annotations

import hashlib
import json
import re
import datetime as dt
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import mailbox, meetings, orchestration, transaction, workspaces
from ..config import CONFIG, __version__
from .auth import ControlAuthError, Principal
from . import artifacts, store

__all__ = [
    "CommandError", "CommandConflict", "CommandContext", "HostCommand",
    "allowed_verbs", "execute", "self_projection", "verbs",
]


class CommandError(ValueError):
    pass


class CommandConflict(CommandError):
    pass


@dataclass(frozen=True)
class CommandContext:
    principal: Principal
    request_id: str
    db_path: Path


@dataclass(frozen=True)
class HostCommand:
    """One host-owned verb.

    ``transactional=True`` means the callback is in-process and every deskd DB
    write reuses the ambient command transaction.  It must not spawn a process,
    perform network I/O or open the DB independently.

    ``transactional=False`` is a durable job.  It runs outside a DB write
    transaction and a lost result is marked ``indeterminate`` on retry; it is
    never blindly executed twice.  The callback must return a JSON-compatible
    value and should persist its own domain job/result if it needs reconciliation.
    """

    verb: str
    callback: Callable[[CommandContext, dict], Any]
    scope: str = "agent"
    transactional: bool = True


_REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_VERB_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MOUNT_TICKET_RE = re.compile(r"^mount_[0-9a-f]{32}$")
_FORBIDDEN_ACTOR_PARAMS = frozenset({
    "actor", "actor_role", "role", "by", "sender", "caller", "called_by",
    "owner_role", "created_by", "principal", "supervisor",
})


def _fingerprint(verb: str, params: dict) -> str:
    raw = json.dumps({"verb": verb, "params": params}, sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _params(raw: dict, *, allowed: set[str],
            required: frozenset[str] | set[str] = frozenset()) -> dict:
    if not isinstance(raw, dict):
        raise CommandError("params must be an object")
    forbidden = sorted(set(raw) & _FORBIDDEN_ACTOR_PARAMS)
    if forbidden:
        raise CommandError(
            f"actor identity is credential-derived; forbidden params: {forbidden}")
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise CommandError(f"unknown command params: {unknown}")
    missing = sorted(k for k in required if raw.get(k) is None)
    if missing:
        raise CommandError(f"missing command params: {missing}")
    return dict(raw)


def _agent(principal: Principal) -> str:
    principal.require("agent")
    if not principal.role:
        raise ControlAuthError("command requires a role-scoped token")
    return principal.role


def _service(principal: Principal, scope: str) -> None:
    principal.require(scope)
    if principal.role is not None:
        raise ControlAuthError(f"{scope} requires a service principal")


def _task_row(task_id: int) -> dict | None:
    with orchestration.connect() as conn:
        row = conn.execute(
            "SELECT * FROM agent_tasks WHERE id=?", (int(task_id),)).fetchone()
        return dict(row) if row else None


def _owned_task(principal: Principal, task_id: int) -> dict:
    row = _task_row(task_id)
    if row is None:
        raise CommandError("task not found")
    if principal.role is not None and row["assignee_role"] != principal.role:
        raise ControlAuthError("task belongs to another role")
    if principal.role is None:
        principal.require("directive")
    return row


def _owned_hook(role: str, hook_id: int) -> None:
    with orchestration.connect() as conn:
        row = conn.execute(
            "SELECT owner_role FROM wake_hooks WHERE id=?", (int(hook_id),)).fetchone()
    if row is None:
        raise CommandError("hook not found")
    if row["owner_role"] != role:
        raise ControlAuthError("hook belongs to another role")


def _verify_inbox_ids(role: str, ids: list[int]) -> list[int]:
    clean = [int(i) for i in ids]
    if not clean:
        return []
    with orchestration.connect() as conn:
        marks = ",".join("?" * len(clean))
        rows = conn.execute(
            f"SELECT id,target_role FROM agent_inbox WHERE id IN ({marks})", clean,
        ).fetchall()
    if len(rows) != len(set(clean)) or any(r["target_role"] != role for r in rows):
        raise ControlAuthError("inbox ids must all belong to the token role")
    return clean


def _self_projection(role: str) -> dict:
    detail = orchestration.agent_detail(role)
    runtime = orchestration.role_runtime(role)
    leases = workspaces.leases(owner_role=role)
    with orchestration.connect() as conn:
        provenance = conn.execute(
            "SELECT * FROM control_session_provenance WHERE role=?", (role,)
        ).fetchone()
        quarantines = [dict(row) for row in conn.execute(
            "SELECT claim_id,state,channel,mode,claimed_at,error FROM "
            "control_wake_claims WHERE role=? AND state='indeterminate' "
            "ORDER BY claimed_at", (role,)).fetchall()]
        reconciliations = [dict(row) for row in conn.execute(
            "SELECT claim_id,state,resolution,reconciled_at,reconciled_by,"
            "reconciliation_note FROM control_wake_claims WHERE role=? "
            "AND state='reconciled' ORDER BY reconciled_at DESC LIMIT 20",
            (role,)).fetchall()]
        rollovers = [dict(row) for row in conn.execute(
            """SELECT request_id,role,resume_session_id,from_day,to_day,state,
                      attempt_count,max_attempts,claim_id,last_error,created_at,
                      updated_at,completed_at
               FROM control_rollover_requests WHERE role=?
               ORDER BY created_at DESC LIMIT 20""", (role,)).fetchall()]
    return {
        "role": role,
        "runtime": runtime,
        "presence": detail.get("presence"),
        "authority": (detail.get("profile") or {}).get("authority", {}),
        "capabilities": (detail.get("profile") or {}).get("capabilities", []),
        "session_id": (detail.get("presence") or {}).get("session_id"),
        "session_provider": ((dict(provenance).get("provider") if provenance else None)
                             or runtime.get("provider")),
        "session_provenance": dict(provenance) if provenance else None,
        "config_version": CONFIG.config_version,
        "prompt_version": CONFIG.prompt_version,
        "agent_image": CONFIG.agent_image,
        "image_digest": CONFIG.image_digest,
        "build_revision": CONFIG.build_revision,
        "build_pins": {
            "image_digest": CONFIG.image_digest,
            "build_revision": CONFIG.build_revision,
            "config_version": CONFIG.config_version,
            "prompt_version": CONFIG.prompt_version,
        },
        "workspace_leases": leases,
        "wake_quarantine": quarantines,
        "wake_reconciliations": reconciliations,
        "rollover_requests": rollovers,
    }


def self_projection(role: str) -> dict:
    """Read-only role projection used by authenticated GET /api/self."""
    store.ensure_schema()
    return _self_projection(role)


def _session_id(value: object, label: str) -> str:
    value = str(value or "").strip()
    if not _SESSION_RE.fullmatch(value):
        raise CommandError(f"{label} must be a safe 1-256 character id")
    return value


def _live_session(role: str):
    with orchestration.connect() as conn:
        return conn.execute(
            "SELECT * FROM agent_sessions WHERE role=?", (role,)).fetchone()


def _validate_workspace_session(role: str, lease_id: str | None) -> dict | None:
    if not lease_id:
        return None
    try:
        uuid.UUID(lease_id)
    except ValueError as exc:
        raise CommandError("workspace_lease_id is invalid") from exc
    with orchestration.connect() as conn:
        row = conn.execute(
            "SELECT owner_role,state,expires_at,provider,model,prompt_version,"
            "image,image_digest,build_revision,config_version,agent_version "
            "FROM workspace_leases "
            "WHERE lease_id=?", (lease_id,)).fetchone()
    if row is None or row["owner_role"] != role:
        raise ControlAuthError("workspace lease does not belong to the token role")
    if row["state"] != "active" or row["expires_at"] <= store.now_iso():
        raise CommandError("workspace lease is not active")
    return dict(row)


def _wake_claim_view(row) -> dict:
    out = dict(row)
    out["attempt_ids"] = json.loads(out.pop("attempt_ids_json"))
    out["inbox_ids"] = json.loads(out.pop("inbox_ids_json"))
    out["reasons"] = json.loads(out.pop("reasons_json"))
    return out


def _record_rollovers(plan: dict) -> list[dict]:
    """Persist scheduler rollover actions in the command's ambient transaction."""
    max_attempts = int(CONFIG.rollover_max_attempts)
    if not 1 <= max_attempts <= 100:
        raise CommandError("rollover_max_attempts must be between 1 and 100")
    recorded: list[dict] = []
    with orchestration.connect(write=True) as conn:
        for action in plan.get("rollovers", []):
            stable = json.dumps({
                "role": action["role"], "session_id": action["session_id"],
                "from_day": action["from_day"], "to_day": action["to_day"],
            }, sort_keys=True, separators=(",", ":")).encode()
            request_id = "rollover_" + hashlib.sha256(stable).hexdigest()[:32]
            now = store.now_iso()
            conn.execute(
                """INSERT OR IGNORE INTO control_rollover_requests
                   (request_id,role,resume_session_id,from_day,to_day,prompt,state,
                    attempt_count,max_attempts,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,'pending',0,?,?,?)""",
                (request_id, action["role"], action["session_id"],
                 action["from_day"], action["to_day"], action["prompt"],
                 max_attempts, now, now))
            row = conn.execute(
                "SELECT * FROM control_rollover_requests WHERE request_id=?",
                (request_id,)).fetchone()
            recorded.append(dict(row))
    return recorded


def _mount_ticket_view(row, *, include_paths: bool = False,
                       recovered: bool = False) -> dict:
    out = {
        "ticket_id": row["ticket_id"],
        "lease_id": row["lease_id"],
        "owner_role": row["owner_role"],
        "workspace_version": int(row["workspace_version"]),
        "expires_at": row["expires_at"],
        "expected_device": int(row["expected_device"]),
        "expected_inode": int(row["expected_inode"]),
        "state": row["state"],
        "build_pins": {
            "image_digest": row["image_digest"],
            "build_revision": row["build_revision"],
            "config_version": row["config_version"],
            "prompt_version": row["prompt_version"],
        },
        "recovered": recovered,
    }
    if include_paths:
        out["host_path"] = row["host_path"]
        out["container_path"] = row["container_path"]
    if row["error"]:
        out["error"] = row["error"]
    return out


def _lease_launch_descriptor(row) -> dict:
    """Revalidate one active lease and derive its two host-owned paths."""
    host_path = workspaces.launch_path(
        row["lease_id"], owner_role=row["owner_role"])
    spec = next((candidate for candidate in CONFIG.repositories
                 if candidate.name == row["repo"]), None)
    if spec is None:
        raise CommandConflict("workspace repository configuration disappeared")
    try:
        relative = host_path.resolve(strict=True).relative_to(
            spec.worktree_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise CommandConflict("workspace launch path escaped its configured root") from exc
    if row["worktree_device"] is None or row["worktree_inode"] is None:
        raise CommandConflict("workspace lease lacks an inode launch pin")
    return {
        "host_path": str(host_path),
        "container_path": str(spec.container_worktree_root / relative),
        "expected_device": int(row["worktree_device"]),
        "expected_inode": int(row["worktree_inode"]),
        "build_pins": {
            "image_digest": row["image_digest"],
            "build_revision": row["build_revision"],
            "config_version": row["config_version"],
            "prompt_version": row["prompt_version"],
        },
    }


def _mount_ticket_row(conn, principal: Principal, ticket_id: str):
    if not _MOUNT_TICKET_RE.fullmatch(str(ticket_id or "")):
        raise CommandError("invalid mount ticket_id")
    row = conn.execute(
        "SELECT * FROM control_mount_tickets WHERE ticket_id=?", (ticket_id,)
    ).fetchone()
    if row is None or row["launcher_subject"] != principal.subject:
        # Do not disclose whether another launcher owns a guessed ticket id.
        raise ControlAuthError("mount ticket does not belong to this launcher")
    return row


def _mount_service(principal: Principal, *, allow_operator: bool = False) -> str:
    """Require a non-role launcher (or, where explicit, operator) principal."""
    if principal.role is not None:
        raise ControlAuthError("mount control requires a service principal")
    if allow_operator and "operator" in principal.scopes:
        return "operator"
    if "launcher" in principal.scopes:
        return "launcher"
    required = "launcher or operator" if allow_operator else "launcher"
    raise ControlAuthError(f"principal lacks required scope: {required}")


def _claim_mount_ticket(principal: Principal, lease_id: str,
                        ttl_seconds: object | None) -> dict:
    _mount_service(principal)
    try:
        default_ttl = int(CONFIG.launcher_ticket_ttl_seconds)
        max_ttl = int(CONFIG.launcher_ticket_max_seconds)
        ttl = int(ttl_seconds if ttl_seconds is not None else default_ttl)
    except (TypeError, ValueError) as exc:
        raise CommandError("ttl_seconds must be an integer") from exc
    if not 5 <= default_ttl <= max_ttl <= 600 or not 5 <= ttl <= max_ttl:
        raise CommandError(
            f"ttl_seconds must be between 5 and {max_ttl} seconds")
    now_dt = dt.datetime.now(dt.timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    with orchestration.connect(write=True) as conn:
        lease = conn.execute(
            "SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        existing = conn.execute(
            """SELECT * FROM control_mount_tickets WHERE lease_id=?
               AND state IN ('issued','started','indeterminate')""",
            (lease_id,)).fetchone()
        if existing is not None:
            if existing["launcher_subject"] != principal.subject:
                raise ControlAuthError(
                    "workspace mount is owned by another launcher service")
            if existing["state"] in {"started", "indeterminate"}:
                # Recovery must not depend on a lease that expired, drifted or
                # disappeared after the mount operation began.  The opaque id
                # is precisely what lets the launcher stop/reconcile it.
                return _mount_ticket_view(existing, recovered=True)
        if (lease is None or lease["state"] != "active"
                or lease["expires_at"] <= now
                or (lease["last_error"] or "").startswith(
                    "release-in-progress:")):
            raise CommandConflict("mount ticket requires an active workspace lease")
        descriptor = _lease_launch_descriptor(lease)
        if existing is not None:
            still_exact = (
                existing["state"] == "issued"
                and existing["expires_at"] > now
                and int(existing["workspace_version"])
                    == int(lease["workspace_version"])
                and existing["host_path"] == descriptor["host_path"]
                and existing["container_path"] == descriptor["container_path"]
                and int(existing["expected_device"])
                    == descriptor["expected_device"]
                and int(existing["expected_inode"])
                    == descriptor["expected_inode"]
            )
            if still_exact:
                return _mount_ticket_view(
                    existing, include_paths=True, recovered=True)
            conn.execute(
                "UPDATE control_mount_tickets SET state='expired' WHERE ticket_id=?",
                (existing["ticket_id"],))

        lease_expiry = dt.datetime.fromisoformat(lease["expires_at"])
        expires = min(now_dt + dt.timedelta(seconds=ttl), lease_expiry).isoformat(
            timespec="seconds")
        if expires <= now:
            raise CommandConflict("workspace lease expires too soon to issue a ticket")
        ticket_id = f"mount_{uuid.uuid4().hex}"
        pins = descriptor["build_pins"]
        conn.execute(
            """INSERT INTO control_mount_tickets
               (ticket_id,lease_id,launcher_subject,owner_role,workspace_version,
                host_path,container_path,expected_device,expected_inode,
                image_digest,build_revision,config_version,prompt_version,
                state,expires_at,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'issued',?,?)""",
            (ticket_id, lease_id, principal.subject, lease["owner_role"],
             int(lease["workspace_version"]), descriptor["host_path"],
             descriptor["container_path"], descriptor["expected_device"],
             descriptor["expected_inode"], pins["image_digest"],
             pins["build_revision"], pins["config_version"],
             pins["prompt_version"], expires, now))
        issued = conn.execute(
            "SELECT * FROM control_mount_tickets WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
    return _mount_ticket_view(issued, include_paths=True)


def _start_mount_ticket(principal: Principal, ticket_id: str) -> dict:
    _mount_service(principal)
    now = store.now_iso()
    with orchestration.connect(write=True) as conn:
        ticket = _mount_ticket_row(conn, principal, ticket_id)
        if ticket["state"] == "started":
            return _mount_ticket_view(ticket, recovered=True)
        if ticket["state"] != "issued":
            raise CommandConflict(f"mount ticket is {ticket['state']}, not issued")
        if ticket["expires_at"] <= now:
            raise CommandConflict("mount ticket expired before start")
        lease = conn.execute(
            "SELECT * FROM workspace_leases WHERE lease_id=?",
            (ticket["lease_id"],)).fetchone()
        if (lease is None or lease["state"] != "active"
                or lease["expires_at"] <= now
                or (lease["last_error"] or "").startswith(
                    "release-in-progress:")
                or int(lease["workspace_version"])
                    != int(ticket["workspace_version"])):
            raise CommandConflict("mount ticket was invalidated by its workspace lease")
        descriptor = _lease_launch_descriptor(lease)
        exact = (
            descriptor["host_path"] == ticket["host_path"]
            and descriptor["container_path"] == ticket["container_path"]
            and descriptor["expected_device"] == int(ticket["expected_device"])
            and descriptor["expected_inode"] == int(ticket["expected_inode"])
            and descriptor["build_pins"] == {
                "image_digest": ticket["image_digest"],
                "build_revision": ticket["build_revision"],
                "config_version": ticket["config_version"],
                "prompt_version": ticket["prompt_version"],
            }
        )
        if not exact:
            raise CommandConflict("mount ticket launch assertions changed")
        conn.execute(
            "UPDATE control_mount_tickets SET state='started',started_at=? "
            "WHERE ticket_id=? AND state='issued'", (now, ticket_id))
        started = conn.execute(
            "SELECT * FROM control_mount_tickets WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
    return _mount_ticket_view(started)


def _land_mount_ticket(principal: Principal, ticket_id: str, outcome: str,
                       error: object | None) -> dict:
    _mount_service(principal)
    if outcome not in {"landed", "indeterminate"}:
        raise CommandError("outcome must be landed|indeterminate")
    detail = str(error or "").strip()[:4000] or None
    if outcome == "indeterminate" and detail is None:
        raise CommandError("indeterminate mount landing requires error detail")
    with orchestration.connect(write=True) as conn:
        ticket = _mount_ticket_row(conn, principal, ticket_id)
        if ticket["state"] == outcome:
            return _mount_ticket_view(ticket, recovered=True)
        if ticket["state"] != "started":
            raise CommandConflict(
                f"mount ticket is {ticket['state']}, not started")
        now = store.now_iso()
        conn.execute(
            "UPDATE control_mount_tickets SET state=?,landed_at=?,error=? "
            "WHERE ticket_id=? AND state='started'",
            (outcome, now, detail, ticket_id))
        landed = conn.execute(
            "SELECT * FROM control_mount_tickets WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
    return _mount_ticket_view(landed)


def _inspect_mount_ticket(principal: Principal, lease_id: str) -> dict:
    """Recover a live ticket id by lease without replaying mount authority."""
    authority = _mount_service(principal, allow_operator=True)
    lease_id = str(lease_id or "").strip()
    if not lease_id or len(lease_id) > 128:
        raise CommandError("invalid workspace lease_id")
    with orchestration.connect() as conn:
        ticket = conn.execute(
            """SELECT * FROM control_mount_tickets WHERE lease_id=?
               AND state IN ('issued','started','indeterminate')
               ORDER BY created_at DESC LIMIT 1""", (lease_id,)).fetchone()
        if ticket is None:
            raise CommandConflict("workspace lease has no live mount ticket")
        if (authority != "operator"
                and ticket["launcher_subject"] != principal.subject):
            raise ControlAuthError(
                "workspace mount is owned by another launcher service")
    return _mount_ticket_view(ticket, recovered=True)


def _reconcile_mount_ticket(principal: Principal, ticket_id: str,
                            resolution: str, note: object | None) -> dict:
    """Close a proven-unused or proven-stopped mount quarantine."""
    authority = _mount_service(principal, allow_operator=True)
    if resolution not in {"cancelled_before_start", "orphan_stopped"}:
        raise CommandError(
            "resolution must be cancelled_before_start|orphan_stopped")
    detail = str(note or "").strip()
    if not detail or len(detail) > 4000:
        raise CommandError("mount reconciliation note is required (max 4000 chars)")
    now = store.now_iso()
    with orchestration.connect(write=True) as conn:
        if not _MOUNT_TICKET_RE.fullmatch(str(ticket_id or "")):
            raise CommandError("invalid mount ticket_id")
        ticket = conn.execute(
            "SELECT * FROM control_mount_tickets WHERE ticket_id=?",
            (ticket_id,)).fetchone()
        if ticket is None:
            raise ControlAuthError("mount ticket is unavailable")
        if (authority != "operator"
                and ticket["launcher_subject"] != principal.subject):
            raise ControlAuthError(
                "mount ticket does not belong to this launcher")
        marker = f"reconciled:{resolution}:"
        if (ticket["state"] == "expired"
                and (ticket["error"] or "").startswith(marker)):
            return _mount_ticket_view(ticket, recovered=True)
        allowed_states = ({"issued"} if resolution == "cancelled_before_start"
                          else {"started", "indeterminate"})
        if ticket["state"] not in allowed_states:
            raise CommandConflict(
                f"resolution {resolution} cannot close ticket state "
                f"{ticket['state']}")
        audit = f"{marker}{principal.actor}: {detail}"
        conn.execute(
            "UPDATE control_mount_tickets SET state='expired',landed_at=?,error=? "
            "WHERE ticket_id=? AND state=?",
            (now, audit, ticket_id, ticket["state"]))
        closed = conn.execute(
            "SELECT * FROM control_mount_tickets WHERE ticket_id=?",
            (ticket_id,)).fetchone()
    return _mount_ticket_view(closed)


def _claim_wake(role: str) -> dict:
    """Claim one durable per-role launch batch from planner attempt rows."""
    with orchestration.connect(write=True) as conn:
        quarantined = conn.execute(
            "SELECT * FROM control_wake_claims WHERE role=? "
            "AND state='indeterminate' ORDER BY claimed_at LIMIT 1",
            (role,)).fetchone()
        if quarantined:
            raise CommandConflict(
                f"role wake execution is quarantined at {quarantined['claim_id']}; "
                "operator reconciliation required")
        existing = conn.execute(
            "SELECT * FROM control_wake_claims WHERE role=? AND state='claimed'",
            (role,)).fetchone()
        if existing:
            return {"claim": _wake_claim_view(existing), "recovered": True}
        rollover = conn.execute(
            """SELECT * FROM control_rollover_requests
               WHERE role=? AND state='pending' ORDER BY created_at,request_id LIMIT 1""",
            (role,)).fetchone()
        if rollover is not None:
            presence = conn.execute(
                "SELECT session_id,ended_at FROM agent_sessions WHERE role=?",
                (role,)).fetchone()
            if (presence is None or presence["ended_at"] is not None
                    or presence["session_id"] != rollover["resume_session_id"]):
                terminal = ("completed" if presence is not None
                            and presence["session_id"] == rollover["resume_session_id"]
                            and presence["ended_at"] is not None else "cancelled")
                now = store.now_iso()
                conn.execute(
                    "UPDATE control_rollover_requests SET state=?,updated_at=?,"
                    "completed_at=? WHERE request_id=?",
                    (terminal, now, now, rollover["request_id"]))
            else:
                attempt_number = int(rollover["attempt_count"]) + 1
                reasons = [{
                    "reason_kind": "session_rollover",
                    "source_ref": rollover["request_id"],
                    "label": (f"wind down session {rollover['resume_session_id']} "
                              f"from {rollover['from_day']} for {rollover['to_day']}"),
                    "from_day": rollover["from_day"],
                    "to_day": rollover["to_day"],
                    "resume_session_id": rollover["resume_session_id"],
                }]
                claim_id = f"wake_{uuid.uuid4()}"
                now = store.now_iso()
                conn.execute(
                    """INSERT INTO control_wake_claims
                       (claim_id,role,state,channel,mode,attempt_ids_json,
                        inbox_ids_json,reasons_json,prompt,claimed_at,reason_kind,
                        resume_session_id,rollover_request_id,attempt_number)
                       VALUES (?,?,'claimed','resume','resume','[]','[]',?,?,?,?,?,?,?)""",
                    (claim_id, role, json.dumps(reasons, sort_keys=True),
                     rollover["prompt"], now, "session_rollover",
                     rollover["resume_session_id"], rollover["request_id"],
                     attempt_number))
                conn.execute(
                    """UPDATE control_rollover_requests
                       SET state='claimed',attempt_count=?,claim_id=?,updated_at=?,
                           last_error=NULL WHERE request_id=? AND state='pending'""",
                    (attempt_number, claim_id, now, rollover["request_id"]))
                claimed = conn.execute(
                    "SELECT * FROM control_wake_claims WHERE claim_id=?",
                    (claim_id,)).fetchone()
                return {"claim": _wake_claim_view(claimed), "recovered": False,
                        "resume_session_id": rollover["resume_session_id"]}
        launch_channels = [
            rung.channel for rung in CONFIG.wake_ladder
            if not rung.leaves_machine and rung.channel != "hook"
        ]
        if not launch_channels:
            return {"claim": None}
        marks = ",".join("?" * len(launch_channels))
        rows = conn.execute(
            f"""SELECT w.* FROM wake_attempts w
                 LEFT JOIN control_wake_claim_attempts c ON c.attempt_id=w.id
                 WHERE w.role=? AND w.outcome='pending'
                   AND w.channel IN ({marks}) AND c.attempt_id IS NULL
                 ORDER BY w.level DESC,w.id""",
            [role, *launch_channels]).fetchall()
        if not rows:
            return {"claim": None}
        top = max(rows, key=lambda r: (int(r["level"]), int(r["id"])))
        presence = conn.execute(
            "SELECT session_id,ended_at FROM agent_sessions WHERE role=?", (role,)
        ).fetchone()
        mode = ("resume" if top["channel"] == "resume" and presence
                and presence["session_id"] and not presence["ended_at"] else "spawn")
        reasons = [{"attempt_id": int(r["id"]),
                    "reason_kind": r["reason_kind"],
                    "source_ref": r["source_ref"], "label": r["detail"]}
                   for r in rows]
        inbox_rows = conn.execute(
            "SELECT id,title FROM agent_inbox WHERE target_role=? "
            "AND acked_at IS NULL AND delivered_at IS NULL ORDER BY id", (role,)
        ).fetchall()
        reason_text = "; ".join(
            f"{r['reason_kind']}: {r['label'] or r['source_ref']}" for r in reasons)
        prompt = CONFIG.prompt_builder.wake(
            role, reason_text or "planned wake", [r["title"] for r in inbox_rows])
        claim_id = f"wake_{uuid.uuid4()}"
        attempt_ids = [int(r["id"]) for r in rows]
        inbox_ids = [int(r["id"]) for r in inbox_rows]
        now = store.now_iso()
        conn.execute(
            """INSERT INTO control_wake_claims
               (claim_id,role,state,channel,mode,attempt_ids_json,inbox_ids_json,
                reasons_json,prompt,claimed_at,reason_kind,resume_session_id)
               VALUES (?,?,'claimed',?,?,?,?,?,?,?,'wake',?)""",
            (claim_id, role, top["channel"], mode,
             json.dumps(attempt_ids), json.dumps(inbox_ids),
             json.dumps(reasons, sort_keys=True), prompt, now,
             presence["session_id"] if mode == "resume" and presence else None))
        conn.executemany(
            "INSERT INTO control_wake_claim_attempts(attempt_id,claim_id) VALUES (?,?)",
            [(attempt_id, claim_id) for attempt_id in attempt_ids])
        row = conn.execute(
            "SELECT * FROM control_wake_claims WHERE claim_id=?", (claim_id,)
        ).fetchone()
        return {"claim": _wake_claim_view(row), "recovered": False,
                "resume_session_id": (presence["session_id"]
                                      if mode == "resume" and presence else None)}


def _land_wake(role: str, claim_id: str, outcome: str,
               session_id: str | None, error: str | None) -> dict:
    if outcome not in {"landed", "failed", "indeterminate"}:
        raise CommandError("outcome must be landed|failed|indeterminate")
    with orchestration.connect(write=True) as conn:
        row = conn.execute(
            "SELECT * FROM control_wake_claims WHERE claim_id=?", (claim_id,)
        ).fetchone()
        if row is None:
            raise CommandError("wake claim not found")
        if row["role"] != role:
            raise ControlAuthError("wake claim belongs to another role")
        if row["state"] != "claimed":
            if row["state"] == outcome:
                return _wake_claim_view(row)
            raise CommandConflict(f"wake claim is already {row['state']}")
        now = store.now_iso()
        rollover = None
        if row["rollover_request_id"]:
            rollover = conn.execute(
                "SELECT * FROM control_rollover_requests WHERE request_id=?",
                (row["rollover_request_id"],)).fetchone()
            if rollover is None:
                raise CommandConflict("rollover request disappeared")
            if outcome == "landed":
                presence = conn.execute(
                    "SELECT session_id,ended_at FROM agent_sessions WHERE role=?",
                    (role,)).fetchone()
                if (presence is None
                        or presence["session_id"] != row["resume_session_id"]
                        or presence["ended_at"] is None):
                    raise CommandConflict(
                        "rollover may land only after its resumed session is stopped")
        conn.execute(
            "UPDATE control_wake_claims SET state=?,landed_at=?,session_id=?,error=? "
            "WHERE claim_id=?", (outcome, now, session_id, error, claim_id))
        attempt_ids = json.loads(row["attempt_ids_json"])
        if outcome == "landed":
            inbox_ids = json.loads(row["inbox_ids_json"])
            if inbox_ids:
                marks = ",".join("?" * len(inbox_ids))
                conn.execute(
                    f"UPDATE agent_inbox SET delivered_at=? WHERE target_role=? "
                    f"AND id IN ({marks}) AND delivered_at IS NULL",
                    [now, role, *inbox_ids])
        elif outcome == "failed" and attempt_ids:
            marks = ",".join("?" * len(attempt_ids))
            conn.execute(
                f"UPDATE wake_attempts SET outcome='failed',resolved_at=? "
                f"WHERE id IN ({marks}) AND role=? AND outcome='pending'",
                [now, *attempt_ids, role])
        if rollover is not None:
            if outcome == "landed":
                conn.execute(
                    """UPDATE control_rollover_requests
                       SET state='completed',updated_at=?,completed_at=?,last_error=NULL
                       WHERE request_id=? AND claim_id=?""",
                    (now, now, rollover["request_id"], claim_id))
            elif outcome == "indeterminate":
                conn.execute(
                    """UPDATE control_rollover_requests
                       SET state='indeterminate',updated_at=?,last_error=?
                       WHERE request_id=? AND claim_id=?""",
                    (now, error, rollover["request_id"], claim_id))
            else:
                next_state = ("pending" if int(rollover["attempt_count"])
                              < int(rollover["max_attempts"]) else "escalated")
                conn.execute(
                    """UPDATE control_rollover_requests
                       SET state=?,claim_id=NULL,updated_at=?,last_error=?
                       WHERE request_id=? AND claim_id=?""",
                    (next_state, now, error or "rollover worker did not complete",
                     rollover["request_id"], claim_id))
        return _wake_claim_view(conn.execute(
            "SELECT * FROM control_wake_claims WHERE claim_id=?", (claim_id,)
        ).fetchone())


def _reconcile_wake(principal: Principal, claim_id: str, resolution: str,
                    note: str) -> dict:
    _service(principal, "operator")
    if resolution not in {"retry", "landed"}:
        raise CommandError("resolution must be retry|landed")
    note = str(note or "").strip()
    if not note or len(note) > 4000:
        raise CommandError("reconciliation note is required (max 4000 chars)")
    with orchestration.connect(write=True) as conn:
        row = conn.execute(
            "SELECT * FROM control_wake_claims WHERE claim_id=?", (claim_id,)
        ).fetchone()
        if row is None:
            raise CommandError("wake claim not found")
        if row["state"] == "reconciled":
            if row["resolution"] != resolution:
                raise CommandConflict("wake claim was reconciled differently")
            return _wake_claim_view(row)
        if row["state"] != "indeterminate":
            raise CommandConflict(f"wake claim is {row['state']}, not indeterminate")
        now = store.now_iso()
        attempts = json.loads(row["attempt_ids_json"])
        if attempts:
            marks = ",".join("?" * len(attempts))
            attempt_outcome = "failed" if resolution == "retry" else "acked"
            conn.execute(
                f"UPDATE wake_attempts SET outcome=?,resolved_at=? "
                f"WHERE id IN ({marks}) AND outcome='pending'",
                [attempt_outcome, now, *attempts])
        if resolution == "landed":
            inbox_ids = json.loads(row["inbox_ids_json"])
            if inbox_ids:
                marks = ",".join("?" * len(inbox_ids))
                conn.execute(
                    f"UPDATE agent_inbox SET delivered_at=? WHERE target_role=? "
                    f"AND id IN ({marks}) AND delivered_at IS NULL",
                    [now, row["role"], *inbox_ids])
        if row["rollover_request_id"]:
            request = conn.execute(
                "SELECT * FROM control_rollover_requests WHERE request_id=?",
                (row["rollover_request_id"],)).fetchone()
            if request is None:
                raise CommandConflict("rollover request disappeared")
            if resolution == "landed":
                presence = conn.execute(
                    "SELECT session_id,ended_at FROM agent_sessions WHERE role=?",
                    (row["role"],)).fetchone()
                if (presence is None
                        or presence["session_id"] != row["resume_session_id"]
                        or presence["ended_at"] is None):
                    raise CommandConflict(
                        "rollover reconciliation requires the old session stopped")
                conn.execute(
                    """UPDATE control_rollover_requests
                       SET state='completed',updated_at=?,completed_at=?,last_error=NULL
                       WHERE request_id=?""",
                    (now, now, request["request_id"]))
            else:
                # An operator has proved the indeterminate attempt did not land;
                # authorize exactly one further bounded attempt if necessary.
                retry_ceiling = max(int(request["max_attempts"]),
                                    int(request["attempt_count"]) + 1)
                conn.execute(
                    """UPDATE control_rollover_requests
                       SET state='pending',claim_id=NULL,max_attempts=?,updated_at=?,
                           last_error=? WHERE request_id=?""",
                    (retry_ceiling, now, f"operator retry: {note}",
                     request["request_id"]))
        conn.execute(
            "UPDATE control_wake_claims SET state='reconciled',resolution=?,"
            "reconciled_at=?,reconciled_by=?,reconciliation_note=? "
            "WHERE claim_id=?",
            (resolution, now, principal.actor, note, claim_id))
        return _wake_claim_view(conn.execute(
            "SELECT * FROM control_wake_claims WHERE claim_id=?", (claim_id,)
        ).fetchone())


def _owned_mail_thread(role: str, thread_id: str) -> dict:
    with orchestration.connect() as conn:
        row = conn.execute(
            """SELECT t.* FROM mailbox_threads t WHERE t.id=?
               AND NOT EXISTS (SELECT 1 FROM meetings m WHERE m.thread_id=t.id)
               AND (t.owner_role=? OR EXISTS (
                 SELECT 1 FROM mailbox_messages mm WHERE mm.thread_id=t.id
                   AND (mm.sender=? OR mm.recipient IN (?,?))))""",
            (thread_id, role, role, role, mailbox.BROADCAST)).fetchone()
    if row is None:
        raise ControlAuthError(
            "mail thread is not a token-role direct-message thread")
    return dict(row)


def _mail_targets(sender: str, recipient: str) -> tuple[str, list[str]]:
    recipient = str(recipient or "").strip()
    if recipient in mailbox.BROADCAST_ALIASES:
        with orchestration.connect() as conn:
            targets = [row["role"] for row in conn.execute(
                "SELECT role FROM agent_registry WHERE enabled=1 AND role!=? "
                "ORDER BY role", (sender,)).fetchall()]
        if not targets:
            raise CommandError("broadcast has no other enabled recipients")
        return mailbox.BROADCAST, targets
    return recipient, [recipient]


def _send_mail(role: str, thread: dict, *, recipient: str, kind: str,
               body: str, requires_reply: bool = False,
               reply_to: int | None = None, priority: str = "normal") -> dict:
    wire_recipient, targets = _mail_targets(role, recipient)
    message = mailbox.send_message(
        thread["id"], sender=role, recipient=wire_recipient,
        kind=kind, body=body, requires_reply=requires_reply,
        reply_to=reply_to)
    inbox_ids: dict[str, int | None] = {}
    for target in targets:
        inbox_ids[target] = orchestration.inbox_enqueue(
            target, "system", thread["subject"],
            body=f"Mailbox message from {role}: {body}",
            ref=f"{thread['id']}:{message['id']}", priority=priority,
            dedup_key=f"mailbox-message:{message['id']}:{target}")
    return {
        "thread": thread, "message": message, "inbox_ids": inbox_ids,
        "recipients": targets, "priority": priority,
    }


def _review_attendee(role: str, thread_id: str, *, checked_in: bool = False) -> dict:
    with orchestration.connect() as conn:
        row = conn.execute(
            """SELECT m.meeting_type,m.state,a.checked_in_at,a.stopped_at
               FROM meetings m JOIN meeting_attendees a ON a.thread_id=m.thread_id
               WHERE m.thread_id=? AND a.role=?""",
            (thread_id, role)).fetchone()
    if row is None or row["meeting_type"] != "review":
        raise ControlAuthError("review is not visible to the token role")
    if checked_in and not row["checked_in_at"]:
        raise CommandConflict("role has not checked in to the review meeting")
    return dict(row)


def _review_artifacts(thread_id: str) -> list[dict]:
    """Review artifact projection that never exposes a host filesystem path."""
    with orchestration.connect() as conn:
        rows = conn.execute(
            """SELECT ra.role,ra.stage,ra.submitted_at,ca.name,ca.sha256,
                      ca.size_bytes
               FROM review_artifacts ra
               LEFT JOIN control_review_artifacts ca
                 ON ca.thread_id=ra.thread_id AND ca.role=ra.role
                AND ca.stage=ra.stage
               WHERE ra.thread_id=?
               ORDER BY CASE ra.stage WHEN 'report' THEN 1
                         WHEN 'review' THEN 2 ELSE 3 END,ra.role""",
            (thread_id,)).fetchall()
    return [{**dict(row), "managed": row["sha256"] is not None} for row in rows]


def _safe_meeting_result(result: dict) -> dict:
    """Remove broker-private artifact paths from agent-facing meeting rows."""
    for message in result.get("messages", []):
        message.pop("artifact_path", None)
    return result


def _dispatch_builtin(principal: Principal, verb: str, raw: dict) -> Any:
    # --- session and self -------------------------------------------------
    if verb == "agent.self":
        _params(raw, allowed=set())
        return _self_projection(_agent(principal))
    if verb == "agent.session.start":
        role = _agent(principal)
        p = _params(raw, allowed={
            "session_id", "mode", "provider", "model", "reasoning", "state",
            "activity", "image_digest", "build_revision", "config_version",
            "prompt_version",
            "workspace_lease_id",
        }, required={"session_id", "mode", "provider", "image_digest",
                     "build_revision", "config_version", "prompt_version"})
        session_id = _session_id(p["session_id"], "session_id")
        if p["mode"] not in {"spawn", "resume"}:
            raise CommandError("mode must be spawn|resume")
        runtime = orchestration.role_runtime(role)
        if p["provider"] != runtime["provider"]:
            raise CommandConflict("provider does not match the role runtime")
        if p.get("model") is not None and runtime.get("model") != p["model"]:
            raise CommandConflict("model does not match the role runtime")
        if (p.get("reasoning") is not None
                and runtime.get("reasoning") != p["reasoning"]):
            raise CommandConflict("reasoning does not match the role runtime")
        pinned = {
            "image_digest": CONFIG.image_digest,
            "build_revision": CONFIG.build_revision,
            "config_version": CONFIG.config_version,
            "prompt_version": CONFIG.prompt_version,
        }
        for key, expected in pinned.items():
            if p.get(key) is not None and p[key] != expected:
                raise CommandConflict(f"{key} does not match host-pinned provenance")
        lease = _validate_workspace_session(role, p.get("workspace_lease_id"))
        if lease:
            lease_expected = {
                "provider": runtime["provider"], "model": runtime.get("model"),
                "prompt_version": CONFIG.prompt_version,
                "image_digest": CONFIG.image_digest,
                "build_revision": CONFIG.build_revision,
                "config_version": CONFIG.config_version,
                "agent_version": __version__,
            }
            for key, expected in lease_expected.items():
                if lease.get(key) != expected:
                    raise CommandConflict(
                        f"workspace lease {key} provenance no longer matches")
        current = _live_session(role)
        if (current is not None and current["ended_at"] is None
                and current["session_id"] not in {None, session_id}):
            raise CommandConflict("another session already owns this role")
        result = orchestration.set_status(
            role, state=p.get("state") or "booting",
            activity=p.get("activity"), session_id=session_id,
            harness=f"deskd-container#{p['provider']}")
        with orchestration.connect(write=True) as conn:
            conn.execute(
                """INSERT INTO control_session_provenance
                   (role,session_id,provisional_session_id,mode,provider,model,
                   reasoning,image_digest,build_revision,config_version,prompt_version,
                    workspace_lease_id,started_at,bound_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                   ON CONFLICT(role) DO UPDATE SET
                     session_id=excluded.session_id,
                     provisional_session_id=excluded.provisional_session_id,
                     mode=excluded.mode,provider=excluded.provider,model=excluded.model,
                     reasoning=excluded.reasoning,image_digest=excluded.image_digest,
                     build_revision=excluded.build_revision,
                     config_version=excluded.config_version,
                     prompt_version=excluded.prompt_version,
                     workspace_lease_id=excluded.workspace_lease_id,
                     started_at=excluded.started_at,bound_at=NULL""",
                (role, session_id, session_id, p["mode"], runtime["provider"],
                 runtime.get("model"), runtime.get("reasoning"),
                 pinned["image_digest"], pinned["build_revision"],
                 pinned["config_version"],
                 pinned["prompt_version"], p.get("workspace_lease_id"),
                 store.now_iso()))
        return {"presence": result, "provenance": _self_projection(role)[
            "session_provenance"]}
    if verb == "agent.session.bind":
        role = _agent(principal)
        p = _params(raw, allowed={"provisional_session_id", "actual_session_id"},
                    required={"provisional_session_id", "actual_session_id"})
        provisional = _session_id(
            p["provisional_session_id"], "provisional_session_id")
        actual = _session_id(p["actual_session_id"], "actual_session_id")
        with orchestration.connect(write=True) as conn:
            provenance = conn.execute(
                "SELECT * FROM control_session_provenance WHERE role=?", (role,)
            ).fetchone()
            if provenance is None or provenance["provisional_session_id"] != provisional:
                raise CommandConflict("provisional session does not match provenance")
            current = conn.execute(
                "SELECT session_id,ended_at FROM agent_sessions WHERE role=?", (role,)
            ).fetchone()
            if current is None or current["ended_at"] is not None:
                raise CommandConflict("role has no live session to bind")
            if current["session_id"] == actual and provenance["session_id"] == actual:
                return _self_projection(role)
            if current["session_id"] != provisional or provenance["session_id"] != provisional:
                raise CommandConflict("session bind compare-and-swap failed")
            collision = conn.execute(
                "SELECT role FROM agent_sessions WHERE session_id=? AND role!=?",
                (actual, role)).fetchone()
            if collision:
                raise CommandConflict("provider session id already belongs to another role")
            conn.execute(
                "UPDATE agent_sessions SET session_id=? WHERE role=? AND session_id=?",
                (actual, role, provisional))
            conn.execute(
                "UPDATE control_session_provenance SET session_id=?,bound_at=? "
                "WHERE role=? AND session_id=?",
                (actual, store.now_iso(), role, provisional))
        return _self_projection(role)
    if verb == "agent.session.heartbeat":
        role = _agent(principal)
        p = _params(raw, allowed={"session_id", "state", "activity"},
                    required={"session_id"})
        session_id = _session_id(p["session_id"], "session_id")
        current = _live_session(role)
        if (current is None or current["ended_at"] is not None
                or current["session_id"] != session_id):
            raise CommandConflict("heartbeat session does not own the token role")
        orchestration.heartbeat(
            role, state=p.get("state"), activity=p.get("activity"),
            session_id=None, harness=None)
        return _self_projection(role)
    if verb == "agent.session.stop":
        role = _agent(principal)
        p = _params(raw, allowed={"session_id"}, required={"session_id"})
        session_id = _session_id(p["session_id"], "session_id")
        current = _live_session(role)
        if current is None or current["session_id"] != session_id:
            raise CommandConflict("stop session does not own the token role")
        # A worker may have committed the stop and lost only the HTTP response.
        # Repeating the same session-scoped transition must be provably safe so
        # the durable rollover claim can still be landed after restart.
        if current["ended_at"] is not None:
            return {"ended": True, "role": role, "recovered": True}
        orchestration.end_session(role)
        return {"ended": True, "role": role, "recovered": False}
    if verb == "agent.session.feed":
        role = _agent(principal)
        p = _params(raw, allowed={"session_id", "kind", "text"},
                    required={"session_id", "kind"})
        with orchestration.connect() as conn:
            row = conn.execute(
                "SELECT session_id FROM agent_sessions WHERE role=? AND ended_at IS NULL",
                (role,)).fetchone()
        if row is None or row["session_id"] != p["session_id"]:
            raise ControlAuthError("feed session is not the token role's live session")
        kind = {"text": "narration", "narration": "narration",
                "thinking": "thinking", "note": "note"}.get(p["kind"])
        if kind is None:
            raise CommandError("feed kind must be text|narration|thinking|note")
        seq = orchestration.feed_append(
            role, p["session_id"], kind, p.get("text") or "")
        if seq is None:
            raise CommandError("session feed write was rejected")
        return {"session_id": p["session_id"], "seq": seq, "kind": kind}

    # --- inbox / directives ----------------------------------------------
    if verb == "directive.send":
        _service(principal, "directive")
        p = _params(raw, allowed={"target_role", "title", "body", "priority"},
                    required={"target_role", "body"})
        title = p.get("title") or str(p["body"]).splitlines()[0][:160]
        item = orchestration.inbox_enqueue(
            p["target_role"], "system", title, body=p["body"],
            priority=p.get("priority") or "normal")
        return {"enqueued": item, "deduped": item is None}
    if verb == "message.send":
        role = _agent(principal)
        p = _params(raw, allowed={
            "target_role", "thread_id", "subject", "body", "kind",
            "requires_reply", "reply_to", "idle_minutes", "max_messages",
            "priority",
        }, required={"target_role", "body"})
        if not p.get("thread_id") and not p.get("subject"):
            raise CommandError("message.send requires thread_id or subject")
        if p.get("thread_id"):
            thread = _owned_mail_thread(role, p["thread_id"])
        else:
            thread = mailbox.open_thread(
                p["subject"], kind="live", owner_role=role,
                idle_minutes=int(p.get("idle_minutes") or 45),
                max_messages=int(p.get("max_messages") or 12))
        return _send_mail(
            role, thread, recipient=p["target_role"],
            kind=p.get("kind") or "note", body=p["body"],
            requires_reply=bool(p.get("requires_reply")),
            reply_to=p.get("reply_to"), priority=p.get("priority") or "normal")
    if verb == "mailbox.open":
        role = _agent(principal)
        p = _params(raw, allowed={
            "subject", "idle_minutes", "max_messages"}, required={"subject"})
        return mailbox.open_thread(
            p["subject"], kind="live", owner_role=role,
            idle_minutes=int(p.get("idle_minutes") or 45),
            max_messages=int(p.get("max_messages") or 12))
    if verb == "mailbox.inbox":
        role = _agent(principal)
        p = _params(raw, allowed={"thread_id", "mark_read", "include_control"})
        if p.get("thread_id"):
            _owned_mail_thread(role, p["thread_id"])
        return {
            "messages": mailbox.inbox(
                role, thread_id=p.get("thread_id"),
                mark_read=bool(p.get("mark_read")),
                include_control=bool(p.get("include_control", True))),
            "marked_read": bool(p.get("mark_read")),
        }
    if verb == "mailbox.send":
        role = _agent(principal)
        p = _params(raw, allowed={
            "thread_id", "target_role", "body", "kind", "requires_reply",
            "reply_to", "priority"},
            required={"thread_id", "target_role", "body"})
        thread = _owned_mail_thread(role, p["thread_id"])
        return _send_mail(
            role, thread, recipient=p["target_role"],
            kind=p.get("kind") or "note", body=p["body"],
            requires_reply=bool(p.get("requires_reply")),
            reply_to=p.get("reply_to"), priority=p.get("priority") or "normal")
    if verb == "mailbox.ack":
        role = _agent(principal)
        p = _params(raw, allowed={"message_id"}, required={"message_id"})
        mailbox.acknowledge(int(p["message_id"]), role=role)
        return {"message_id": int(p["message_id"]), "acked": True}
    if verb == "mailbox.status":
        role = _agent(principal)
        p = _params(raw, allowed={"thread_id"}, required={"thread_id"})
        _owned_mail_thread(role, p["thread_id"])
        return {"thread": mailbox.get_thread(p["thread_id"]),
                "unread": mailbox.inbox(role, thread_id=p["thread_id"])}
    if verb == "mailbox.stop":
        role = _agent(principal)
        p = _params(raw, allowed={"thread_id", "action", "reason"},
                    required={"thread_id", "action", "reason"})
        _owned_mail_thread(role, p["thread_id"])
        return mailbox.stop_thread(
            p["thread_id"], action=p["action"], actor=role, reason=p["reason"])

    # --- formal review ---------------------------------------------------
    if verb.startswith("review."):
        role = _agent(principal)
        if verb == "review.start":
            p = _params(raw, allowed={
                "subject", "attendees", "priority", "idle_minutes",
                "max_messages", "consensus_threshold", "wait_timeout_seconds",
            }, required={"subject"})
            result = meetings.call_meeting(
                agenda=p["subject"], called_by=role, attendees=p.get("attendees"),
                meeting_type="review", priority=p.get("priority") or "normal",
                idle_minutes=int(p.get("idle_minutes") or 1440),
                max_messages=(int(p["max_messages"])
                              if p.get("max_messages") is not None else None),
                consensus_threshold=int(p.get("consensus_threshold") or 4),
                wait_timeout_seconds=int(p.get("wait_timeout_seconds") or 300))
            thread_id = result["meeting"]["thread_id"]
            if result["meeting"]["called_by"] != role:
                raise CommandConflict(
                    "an existing review with this subject has another caller")
            # The finalizer is not a client-supplied actor claim: it is always
            # the credential-derived caller that opened this control review.
            with orchestration.connect(write=True) as conn:
                conn.execute(
                    "UPDATE mailbox_threads SET owner_role=? WHERE id=?",
                    (role, thread_id))
            result["thread"] = mailbox.get_thread(thread_id)
            return result
        if verb == "review.status":
            p = _params(raw, allowed={"thread_id"}, required={"thread_id"})
            _review_attendee(role, p["thread_id"])
            return {
                "thread": mailbox.get_thread(p["thread_id"]),
                "meeting": meetings.meeting_status(p["thread_id"], sweep=False),
                "artifacts": _review_artifacts(p["thread_id"]),
            }
        if verb in {"review.submit", "review.report", "review.review",
                    "review.finalize"}:
            allowed = {"thread_id", "name", "content", "sha256"}
            required = {"thread_id", "name", "content", "sha256"}
            if verb == "review.submit":
                allowed.add("stage")
                required.add("stage")
            p = _params(raw, allowed=allowed,
                        required=required)
            _review_attendee(role, p["thread_id"], checked_in=True)
            stage = (p.get("stage") if verb == "review.submit" else {
                "review.report": "report", "review.review": "review",
                "review.finalize": "final",
            }[verb])
            if stage == "finalize":
                stage = "final"
            if stage not in mailbox.REVIEW_STAGES:
                raise CommandError("review stage must be report|review|final")
            stored = artifacts.store_text(
                name=p["name"], content=p["content"], sha256=p["sha256"])
            result = mailbox.submit_review_artifact(
                p["thread_id"], role=role, stage=stage, path=stored.path,
                artifact_label=f"{stored.name} (sha256:{stored.sha256})")
            with orchestration.connect(write=True) as conn:
                conn.execute(
                    """INSERT INTO control_review_artifacts
                       (thread_id,role,stage,name,sha256,size_bytes,stored_path,created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (p["thread_id"], role, stage, stored.name, stored.sha256,
                     stored.size_bytes, str(stored.path), store.now_iso()))
            return {**result, "artifact": stored.public(), "stage": stage}
        p = _params(raw, allowed={"thread_id", "body", "reason"},
                    required={"thread_id"})
        _review_attendee(role, p["thread_id"], checked_in=True)
        if verb in {"review.discuss", "review.agree"}:
            if p.get("body") is None:
                raise CommandError(f"{verb} requires body")
            return mailbox.review_discuss(
                p["thread_id"], role=role, body=p["body"],
                agree=verb == "review.agree")
        if verb == "review.conclude":
            if p.get("reason") is None:
                raise CommandError("review.conclude requires reason")
            return mailbox.conclude_review(
                p["thread_id"], role=role, reason=p["reason"])
    if verb == "inbox.enqueue":
        _service(principal, "directive")
        p = _params(raw, allowed={"target_role", "source_kind", "title", "body",
                                         "ref", "priority", "dedup_key", "expires_at"},
                    required={"target_role", "source_kind", "title"})
        item = orchestration.inbox_enqueue(
            p["target_role"], p["source_kind"], p["title"], body=p.get("body"),
            ref=p.get("ref"), priority=p.get("priority") or "normal",
            dedup_key=p.get("dedup_key"), expires_at=p.get("expires_at"))
        return {"enqueued": item, "deduped": item is None}
    if verb == "inbox.read":
        role = _agent(principal)
        p = _params(raw, allowed={"include_delivered"})
        listed_at = orchestration.inbox_note_listed(role)
        return {"listed_at": listed_at, "items": orchestration.inbox_pending(
            role, include_delivered=bool(p.get("include_delivered", True)))}
    if verb == "inbox.ack":
        role = _agent(principal)
        p = _params(raw, allowed={"ids"})
        ids = _verify_inbox_ids(role, p.get("ids") or [])
        return {"acked": orchestration.inbox_ack(role if not ids else None,
                                                  ids=ids or None)}

    # --- tasks ------------------------------------------------------------
    if verb == "task.list":
        p = _params(raw, allowed={"include_closed", "status"})
        role = principal.role
        if role is None:
            principal.require("read")
        return orchestration.tasks(
            assignee_role=role, status=p.get("status"),
            include_closed=bool(p.get("include_closed", False)))
    if verb == "task.add":
        p = _params(raw, allowed={"assignee_role", "title", "detail", "priority",
                                         "due_at", "source_kind", "source_ref"},
                    required={"title"})
        if principal.role is not None:
            principal.require("agent")
            assignee = p.get("assignee_role") or principal.role
            if (assignee != principal.role
                    and (principal.role, assignee) not in CONFIG.task_delegations):
                raise ControlAuthError(
                    "cross-role task delegation is not allowed by host policy")
            if p.get("source_kind") not in {None, "self"}:
                raise ControlAuthError(
                    "agent task provenance is credential-derived as self")
            source = "self"
        else:
            principal.require("directive")
            assignee = p.get("assignee_role")
            if not assignee:
                raise CommandError("service task.add requires assignee_role")
            source = p.get("source_kind") or "system"
        task_id = orchestration.task_add(
            p["title"], assignee_role=assignee, detail=p.get("detail"),
            priority=p.get("priority") or "normal", source_kind=source,
            source_ref=p.get("source_ref"), due_at=p.get("due_at"),
            created_by=principal.actor)
        return {"task_id": task_id, "assignee_role": assignee}
    if verb == "task.update":
        p = _params(raw, allowed={"task_id", "status", "priority", "due_at",
                                         "detail", "title", "result_note",
                                         "assignee_role", "blocked_on"},
                    required={"task_id"})
        _owned_task(principal, int(p.pop("task_id")))
        task_id = int(raw["task_id"])
        if (principal.role is not None and p.get("assignee_role") is not None
                and p["assignee_role"] != principal.role
                and (principal.role, p["assignee_role"])
                not in CONFIG.task_delegations):
            raise ControlAuthError(
                "cross-role task delegation is not allowed by host policy")
        changed = orchestration.task_update(
            task_id, actor=principal.actor, **p)
        return {"task_id": task_id, "updated": changed}
    if verb == "task.done":
        p = _params(raw, allowed={"task_id", "note"}, required={"task_id"})
        task_id = int(p["task_id"])
        _owned_task(principal, task_id)
        return {"task_id": task_id, "done": orchestration.task_close(
            task_id, note=p.get("note"), actor=principal.actor)}

    # --- waking and hooks -------------------------------------------------
    if verb == "wake.sources":
        _params(raw, allowed=set())
        return orchestration.wake_sources(_agent(principal))
    if verb == "agent.wake.claim":
        _params(raw, allowed=set())
        return _claim_wake(_agent(principal))
    if verb == "agent.wake.land":
        role = _agent(principal)
        p = _params(raw, allowed={"claim_id", "outcome", "session_id", "error"},
                    required={"claim_id", "outcome"})
        session_id = (_session_id(p["session_id"], "session_id")
                      if p.get("session_id") else None)
        if p["outcome"] == "landed" and not session_id:
            raise CommandError("landed wake requires session_id")
        return _land_wake(role, p["claim_id"], p["outcome"], session_id,
                          str(p.get("error") or "")[:4000] or None)
    if verb == "wake.reconcile":
        p = _params(raw, allowed={"claim_id", "resolution", "note"},
                    required={"claim_id", "resolution", "note"})
        return _reconcile_wake(
            principal, p["claim_id"], p["resolution"], p["note"])
    if verb == "wake.ack":
        role = _agent(principal)
        p = _params(raw, allowed={"meeting_id"}, required={"meeting_id"})
        return meetings.acknowledge_wake(p["meeting_id"], role=role)
    if verb == "hook.list":
        role = _agent(principal)
        p = _params(raw, allowed={"include_closed"})
        return orchestration.hooks(role, include_closed=bool(p.get("include_closed")))
    if verb == "hook.add":
        role = _agent(principal)
        p = _params(raw, allowed={"title", "at", "every", "cron", "tz", "until",
                                         "body", "priority", "callable_path"},
                    required={"title"})
        return orchestration.hook_add(
            role, p["title"], at=p.get("at"), every=p.get("every"),
            callable_path=p.get("callable_path"), cron=p.get("cron"),
            tz=p.get("tz"), until=p.get("until"), body=p.get("body"),
            priority=p.get("priority") or "normal")
    if verb == "hook.cancel":
        role = _agent(principal)
        p = _params(raw, allowed={"hook_id"}, required={"hook_id"})
        _owned_hook(role, int(p["hook_id"]))
        return {"hook_id": int(p["hook_id"]), "cancelled":
                orchestration.hook_cancel(int(p["hook_id"]), actor=role)}

    # --- meetings ---------------------------------------------------------
    if verb.startswith("meeting."):
        role = _agent(principal)
        if verb == "meeting.call":
            p = _params(raw, allowed={"agenda", "attendees", "meeting_type",
                                             "priority", "idle_minutes", "max_messages",
                                             "consensus_threshold", "wait_timeout_seconds"},
                        required={"agenda"})
            return meetings.call_meeting(
                agenda=p["agenda"], called_by=role, attendees=p.get("attendees"),
                meeting_type=p.get("meeting_type") or "ad-hoc",
                priority=p.get("priority") or "normal",
                idle_minutes=int(p.get("idle_minutes") or meetings.DEFAULT_IDLE_MINUTES),
                max_messages=p.get("max_messages"),
                consensus_threshold=int(p.get("consensus_threshold") or
                                        meetings.DEFAULT_CONSENSUS_THRESHOLD),
                wait_timeout_seconds=int(p.get("wait_timeout_seconds") or
                                         meetings.DEFAULT_WAIT_TIMEOUT_SECONDS))
        if verb in {"meeting.discover", "meeting.wake_list"}:
            p = _params(raw, allowed={"include_closed"} if verb.endswith("discover") else set())
            return (meetings.discover(role, include_closed=bool(p.get("include_closed")))
                    if verb.endswith("discover") else meetings.wake_requests(role))
        p = _params(raw, allowed={
            "meeting_id", "body", "kind", "reply_to", "resolves", "message_ids",
            "covered_by", "reason", "resolution", "channel", "pause", "mark_read",
        }, required={"meeting_id"})
        mid = p["meeting_id"]
        if verb == "meeting.check_in":
            return meetings.check_in(mid, role=role)
        if verb == "meeting.updates":
            return _safe_meeting_result(meetings.meeting_updates(
                mid, role=role, mark_read=bool(p.get("mark_read"))))
        if verb == "meeting.send":
            if p.get("body") is None:
                raise CommandError("meeting.send requires body")
            return meetings.send_update(
                mid, role=role, body=p["body"], kind=p.get("kind") or "evidence",
                reply_to=p.get("reply_to"), resolves=p.get("resolves"))
        if verb == "meeting.resolve":
            if p.get("message_ids") is None or p.get("covered_by") is None:
                raise CommandError("meeting.resolve requires message_ids and covered_by")
            return meetings.resolve_obligations(
                mid, role=role, message_ids=p["message_ids"],
                covered_by=int(p["covered_by"]))
        if verb == "meeting.position":
            if p.get("body") is None:
                raise CommandError("meeting.position requires body")
            return meetings.submit_position(
                mid, role=role, body=p["body"], reply_to=p.get("reply_to"))
        if verb == "meeting.propose_end":
            if p.get("resolution") is None:
                raise CommandError("meeting.propose_end requires resolution")
            return meetings.propose_end(mid, role=role, resolution=p["resolution"])
        if verb == "meeting.confirm_end":
            return meetings.confirm_end(mid, role=role)
        if verb == "meeting.reject_end":
            if p.get("reason") is None:
                raise CommandError("meeting.reject_end requires reason")
            return meetings.reject_end(mid, role=role, reason=p["reason"])
        if verb == "meeting.leave":
            if p.get("reason") is None:
                raise CommandError("meeting.leave requires reason")
            return meetings.leave_meeting(mid, role=role, reason=p["reason"])
        if verb == "meeting.pause":
            if p.get("reason") is None:
                raise CommandError("meeting.pause requires reason")
            return meetings.pause_meeting(mid, role=role, reason=p["reason"])
        if verb == "meeting.escalate":
            if p.get("reason") is None:
                raise CommandError("meeting.escalate requires reason")
            return meetings.escalate_meeting(
                mid, role=role, reason=p["reason"],
                channel=p.get("channel") or "auto",
                pause=bool(p.get("pause", True)))
        if verb == "meeting.wake_ack":
            return meetings.acknowledge_wake(mid, role=role)

    # --- trusted launcher mount tickets ----------------------------------
    if verb == "launcher.mount.claim":
        p = _params(raw, allowed={"lease_id", "ttl_seconds"},
                    required={"lease_id"})
        return _claim_mount_ticket(
            principal, p["lease_id"], p.get("ttl_seconds"))
    if verb == "launcher.mount.start":
        p = _params(raw, allowed={"ticket_id"}, required={"ticket_id"})
        return _start_mount_ticket(principal, p["ticket_id"])
    if verb == "launcher.mount.land":
        p = _params(raw, allowed={"ticket_id", "outcome", "error"},
                    required={"ticket_id", "outcome"})
        return _land_mount_ticket(
            principal, p["ticket_id"], p["outcome"], p.get("error"))
    if verb == "launcher.mount.inspect":
        p = _params(raw, allowed={"lease_id"}, required={"lease_id"})
        return _inspect_mount_ticket(principal, p["lease_id"])
    if verb == "launcher.mount.reconcile":
        p = _params(raw, allowed={"ticket_id", "resolution", "note"},
                    required={"ticket_id", "resolution", "note"})
        return _reconcile_mount_ticket(
            principal, p["ticket_id"], p["resolution"], p["note"])

    # Deleting a host worktree is runtime authority, never provider-role
    # authority.  The launcher/operator derives ownership from the durable
    # lease row; caller-supplied role identity is forbidden by _params.
    if verb == "workspace.release":
        _mount_service(principal, allow_operator=True)
        p = _params(raw, allowed={"lease_id", "expected_version"},
                    required={"lease_id", "expected_version"})
        with orchestration.connect() as conn:
            lease = conn.execute(
                "SELECT owner_role FROM workspace_leases WHERE lease_id=?",
                (p["lease_id"],)).fetchone()
        if lease is None:
            raise CommandError("workspace lease not found")
        return workspaces.release(
            p["lease_id"], owner_role=lease["owner_role"],
            expected_workspace_version=int(p["expected_version"]))

    # --- workspace broker -------------------------------------------------
    if verb.startswith("workspace."):
        role = _agent(principal)
        if verb == "workspace.acquire":
            p = _params(raw, allowed={"repo", "task_key", "base_ref", "branch",
                                             "expected_base_sha", "lease_seconds"},
                        required={"repo", "task_key", "base_ref"})
            return workspaces.acquire(
                p["repo"], owner_role=role, task_key=p["task_key"],
                base_ref=p["base_ref"], branch=p.get("branch"),
                expected_base_sha=p.get("expected_base_sha"),
                lease_seconds=p.get("lease_seconds"))
        if verb == "workspace.inspect":
            p = _params(raw, allowed={"lease_id"}, required={"lease_id"})
            return workspaces.inspect(p["lease_id"], owner_role=role)
        if verb == "workspace.renew":
            p = _params(raw, allowed={"lease_id", "lease_seconds",
                                      "workspace_version"}, required={"lease_id"})
            return workspaces.renew(
                p["lease_id"], owner_role=role,
                lease_seconds=p.get("lease_seconds"),
                expected_workspace_version=p.get("workspace_version"))
        if verb == "workspace.status":
            p = _params(raw, allowed={"lease_id"}, required={"lease_id"})
            return workspaces.status(p["lease_id"], owner_role=role)
        if verb == "workspace.diff":
            p = _params(raw, allowed={"lease_id", "staged"}, required={"lease_id"})
            return workspaces.diff(
                p["lease_id"], owner_role=role, staged=bool(p.get("staged")))
        if verb == "workspace.commit":
            p = _params(raw, allowed={
                "lease_id", "message", "expected_head", "workspace_version",
                "expected_base_sha"}, required={"lease_id"})
            for key in ("message", "expected_head", "workspace_version"):
                if p.get(key) is None:
                    raise CommandError(f"workspace.commit requires {key}")
            return workspaces.commit(
                p["lease_id"], owner_role=role, message=p["message"],
                expected_head=p["expected_head"],
                expected_workspace_version=int(p["workspace_version"]),
                expected_base_sha=p.get("expected_base_sha"))
    if verb == "rollover.retry":
        _service(principal, "operator")
        p = _params(raw, allowed={"request_id", "note"},
                    required={"request_id", "note"})
        request_id = str(p["request_id"] or "").strip()
        note = str(p["note"] or "").strip()
        if not request_id.startswith("rollover_") or len(request_id) != 41:
            raise CommandError("invalid rollover request_id")
        if not note or len(note) > 4000:
            raise CommandError("operator retry note is required (max 4000 chars)")
        retry_batch = int(CONFIG.rollover_max_attempts)
        if not 1 <= retry_batch <= 100:
            raise CommandError("rollover_max_attempts must be between 1 and 100")
        with orchestration.connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM control_rollover_requests WHERE request_id=?",
                (request_id,)).fetchone()
            if row is None:
                raise CommandError("rollover request not found")
            if row["state"] != "escalated":
                raise CommandConflict(
                    f"rollover request is {row['state']}, not escalated")
            now = store.now_iso()
            ceiling = int(row["attempt_count"]) + retry_batch
            conn.execute(
                """UPDATE control_rollover_requests
                   SET state='pending',claim_id=NULL,max_attempts=?,updated_at=?,
                       last_error=? WHERE request_id=? AND state='escalated'""",
                (ceiling, now, f"operator retry: {note}", request_id))
            updated = conn.execute(
                """SELECT request_id,role,resume_session_id,from_day,to_day,state,
                          attempt_count,max_attempts,claim_id,last_error,created_at,
                          updated_at,completed_at
                   FROM control_rollover_requests WHERE request_id=?""",
                (request_id,)).fetchone()
        return dict(updated)

    # --- service ticks ----------------------------------------------------
    if verb == "orchestrator.tick":
        _service(principal, "orchestrator")
        _params(raw, allowed=set())
        return orchestration.plan_wakes(record=True)
    if verb == "scheduler.tick":
        _service(principal, "scheduler")
        _params(raw, allowed=set())
        # Rollover detection, the draining presence transition and the durable
        # role-claimable request are one commit.  The ordinary wake planner may
        # run host probes, so run it only after that commit; a probe failure or
        # lost scheduler response can no longer strand a draining session.
        with orchestration.connect(write=True) as conn:
            with transaction.bind(conn, CONFIG.db_path):
                rollover = orchestration.rollover_plan(record=True)
                rollover_requests = _record_rollovers(rollover)
        wake = orchestration.plan_wakes(record=True)
        return {"wake": wake,
                "rollover": {**rollover, "requests": rollover_requests}}

    raise CommandError(f"unsupported command verb: {verb}")


def _host_commands() -> dict[str, HostCommand]:
    out: dict[str, HostCommand] = {}
    for handler in CONFIG.command_handlers:
        if not isinstance(handler, HostCommand):
            raise CommandError("CONFIG.command_handlers entries must be HostCommand")
        if not _VERB_RE.fullmatch(handler.verb) or handler.verb in _BUILTIN_VERBS:
            raise CommandError(f"invalid or reserved host command: {handler.verb}")
        if handler.verb in out:
            raise CommandError(f"duplicate host command: {handler.verb}")
        out[handler.verb] = handler
    return out


_BUILTIN_VERBS = frozenset({
    "agent.self", "agent.session.start", "agent.session.heartbeat",
    "agent.session.bind", "agent.session.stop", "agent.session.feed",
    "agent.wake.claim", "agent.wake.land", "directive.send", "message.send",
    "mailbox.open", "mailbox.inbox", "mailbox.send", "mailbox.ack",
    "mailbox.status", "mailbox.stop",
    "review.start", "review.submit", "review.report", "review.review",
    "review.finalize", "review.discuss", "review.agree", "review.conclude",
    "review.status",
    "inbox.enqueue", "inbox.read", "inbox.ack", "task.list", "task.add",
    "task.update", "task.done", "wake.sources", "wake.ack", "hook.list",
    "hook.add", "hook.cancel", "meeting.call", "meeting.discover",
    "meeting.check_in", "meeting.updates", "meeting.send", "meeting.resolve",
    "meeting.position", "meeting.leave", "meeting.pause", "meeting.escalate",
    "meeting.propose_end", "meeting.confirm_end", "meeting.reject_end",
    "meeting.wake_list", "meeting.wake_ack", "workspace.acquire",
    "workspace.inspect", "workspace.renew", "workspace.status", "workspace.diff",
    "workspace.commit", "workspace.release", "orchestrator.tick", "scheduler.tick",
    "wake.reconcile", "rollover.retry", "launcher.mount.claim",
    "launcher.mount.start", "launcher.mount.land", "launcher.mount.inspect",
    "launcher.mount.reconcile",
})

_RECOVERABLE_EXTERNAL = frozenset({
    "workspace.acquire", "workspace.commit", "workspace.release",
})
_NONRECOVERABLE_EXTERNAL = frozenset({"orchestrator.tick", "scheduler.tick"})


def verbs() -> list[str]:
    return sorted(_BUILTIN_VERBS | set(_host_commands()))


def allowed_verbs(principal: Principal) -> list[str]:
    """Exact command vocabulary this credential may attempt."""
    if principal.role is not None:
        role_workspace_verbs = {
            "workspace.acquire", "workspace.inspect", "workspace.renew",
            "workspace.status", "workspace.diff", "workspace.commit",
        }
        builtins = {
            verb for verb in _BUILTIN_VERBS
            if (verb != "wake.reconcile"
                and (verb.startswith(("agent.", "message.", "mailbox.",
                                      "meeting.", "review.",
                                      "hook.", "wake."))
                or verb in role_workspace_verbs
                or verb.startswith("task.")
                or verb in {"inbox.read", "inbox.ack"}))
        }
    else:
        builtins: set[str] = set()
        if "read" in principal.scopes:
            builtins.add("task.list")
        if "directive" in principal.scopes:
            builtins.update({"directive.send", "inbox.enqueue", "task.add",
                             "task.update", "task.done"})
        if "orchestrator" in principal.scopes:
            builtins.add("orchestrator.tick")
        if "scheduler" in principal.scopes:
            builtins.add("scheduler.tick")
        if "operator" in principal.scopes:
            builtins.update({"wake.reconcile", "rollover.retry",
                             "launcher.mount.inspect",
                             "launcher.mount.reconcile", "workspace.release"})
        if "launcher" in principal.scopes:
            builtins.update({"launcher.mount.claim", "launcher.mount.start",
                             "launcher.mount.land", "launcher.mount.inspect",
                             "launcher.mount.reconcile", "workspace.release"})
    for verb, handler in _host_commands().items():
        if handler.scope in principal.scopes:
            builtins.add(verb)
    return sorted(builtins)


def _invoke(principal: Principal, request_id: str, verb: str, params: dict) -> Any:
    handler = _host_commands().get(verb)
    if handler is None:
        return _dispatch_builtin(principal, verb, params)
    principal.require(handler.scope)
    return handler.callback(
        CommandContext(principal, request_id, Path(CONFIG.db_path)), params)


def _stored(row) -> dict:
    if row["status"] == "completed" and row["response_json"]:
        return json.loads(row["response_json"])
    if row["status"] in {"failed", "indeterminate"}:
        raise CommandConflict(
            f"command is {row['status']}: {row['error'] or 'inspect /api/jobs'}")
    raise CommandConflict("command is still running; inspect /api/jobs")


def _execute_atomic(principal: Principal, request_id: str, verb: str,
                    params: dict, fingerprint: str,
                    before_complete: Callable[[], None] | None) -> dict:
    store.ensure_schema()
    path = Path(CONFIG.db_path)
    with orchestration.connect(write=True) as conn:
        with transaction.bind(conn, path):
            old = store.command_row(conn, principal.subject, request_id)
            if old is not None:
                if old["request_fingerprint"] != fingerprint:
                    raise CommandConflict("request_id was reused with different content")
                return _stored(old)
            store.insert_command(
                conn, principal.subject, request_id, verb, fingerprint, "running")
            result = _invoke(principal, request_id, verb, params)
            cursor = store.append_event(
                conn, "command", "completed", role=principal.role,
                resource_id=request_id, payload={"verb": verb})
            response = {"request_id": request_id, "accepted": True,
                        "event_cursor": cursor, "result": result}
            if before_complete:
                before_complete()
            store.finish_command(conn, principal.subject, request_id,
                                 "completed", response=response)
            return response


def _execute_external(principal: Principal, request_id: str, verb: str,
                      params: dict, fingerprint: str,
                      before_complete: Callable[[], None] | None,
                      *, recoverable: bool) -> dict:
    """Durable external-operation saga; never holds SQLite while it runs.

    A token + expiry is an execution lease, not a retry timer.  A duplicate
    inside the live lease receives the existing running job and cannot enter
    the callback.  Only a stale, explicitly recoverable broker verb may take
    over; non-recoverable work becomes indeterminate because its side effect
    cannot honestly be inferred from a missing receipt.
    """
    store.ensure_schema()
    execution_token = uuid.uuid4().hex
    lease_seconds = max(30, int(CONFIG.external_command_lease_seconds))
    now = dt.datetime.now(dt.timezone.utc)
    lease_expires = (now + dt.timedelta(seconds=lease_seconds)).isoformat(
        timespec="seconds")
    claimed = False
    with orchestration.connect(write=True) as conn:
        old = store.command_row(conn, principal.subject, request_id)
        if old is not None:
            if old["request_fingerprint"] != fingerprint:
                raise CommandConflict("request_id was reused with different content")
            if old["status"] == "completed":
                return _stored(old)
            if old["status"] in {"failed", "indeterminate"}:
                return _stored(old)
            live_until = old["lease_expires_at"]
            if live_until and live_until > store.now_iso():
                return {
                    "request_id": request_id, "accepted": True,
                    "event_cursor": store.current_cursor(conn),
                    "job": {"status": old["status"],
                            "lease_expires_at": live_until},
                }
            if not recoverable:
                store.finish_command(
                    conn, principal.subject, request_id, "indeterminate",
                    error="previous external execution lost its result; not retried")
                raise CommandConflict(
                    "external command outcome is indeterminate; not retried")
            conn.execute(
                "UPDATE control_commands SET status='running',execution_token=?,"
                "lease_expires_at=?,updated_at=?,error=NULL WHERE principal_id=? "
                "AND request_id=?",
                (execution_token, lease_expires, store.now_iso(),
                 principal.subject, request_id))
            claimed = True
        else:
            store.insert_command(
                conn, principal.subject, request_id, verb, fingerprint, "running",
                execution_token=execution_token, lease_expires_at=lease_expires)
            claimed = True
    if not claimed:  # defensive: every branch above returns or claims
        raise CommandConflict("external command was not claimed")
    callback_completed = False
    try:
        result = _invoke(principal, request_id, verb, params)
        callback_completed = True
        if before_complete:
            before_complete()
    except Exception as exc:
        recovery_required = recoverable and (
            callback_completed
            or isinstance(exc, workspaces.WorkspaceOutcomeUnknown))
        with orchestration.connect(write=True) as conn:
            if recovery_required:
                changed = conn.execute(
                    "UPDATE control_commands SET status='running',error=?,"
                    "lease_expires_at=?,updated_at=? WHERE principal_id=? "
                    "AND request_id=? AND execution_token=?",
                    (str(exc)[:4000], store.now_iso(), store.now_iso(),
                     principal.subject, request_id, execution_token),
                ).rowcount
                if changed:
                    store.append_event(
                        conn, "command", "recovery_required", role=principal.role,
                        resource_id=request_id, payload={"verb": verb})
            elif not recoverable:
                if store.finish_command(
                        conn, principal.subject, request_id, "indeterminate",
                        error=str(exc)[:4000], execution_token=execution_token):
                    store.append_event(
                        conn, "command", "indeterminate", role=principal.role,
                        resource_id=request_id, payload={"verb": verb})
            elif store.finish_command(
                    conn, principal.subject, request_id, "failed",
                    error=str(exc)[:4000], execution_token=execution_token):
                store.append_event(
                    conn, "command", "failed", role=principal.role,
                    resource_id=request_id, payload={"verb": verb})
        raise
    with orchestration.connect(write=True) as conn:
        current = store.command_row(conn, principal.subject, request_id)
        if current is None or current["execution_token"] != execution_token:
            raise CommandConflict("external execution lease was superseded")
        cursor = store.append_event(conn, "command", "completed",
                                    role=principal.role,
                                    resource_id=request_id,
                                    payload={"verb": verb})
        response = {"request_id": request_id, "accepted": True,
                    "event_cursor": cursor, "result": result}
        if not store.finish_command(
                conn, principal.subject, request_id, "completed",
                response=response, execution_token=execution_token):
            raise CommandConflict("external execution lease was superseded")
    return response


def execute(principal: Principal, request_id: str, verb: str, params: dict, *,
            before_complete: Callable[[], None] | None = None) -> dict:
    """Execute one command with credential-derived identity and idempotency."""
    if not _REQUEST_RE.fullmatch(request_id or ""):
        raise CommandError("request_id must be a stable 8-128 character id")
    if not _VERB_RE.fullmatch(verb or ""):
        raise CommandError("invalid command verb")
    # Reject identity claims even before the verb-specific allowlist sees them.
    forbidden = sorted(set(params or {}) & _FORBIDDEN_ACTOR_PARAMS)
    if forbidden:
        raise CommandError(
            f"actor identity is credential-derived; forbidden params: {forbidden}")
    if verb not in _BUILTIN_VERBS and verb not in _host_commands():
        raise CommandError(f"unsupported command verb: {verb}")
    fingerprint = _fingerprint(verb, params)
    host = _host_commands().get(verb)
    external = (verb in _RECOVERABLE_EXTERNAL
                or verb in _NONRECOVERABLE_EXTERNAL
                or (host is not None and not host.transactional))
    if external:
        return _execute_external(
            principal, request_id, verb, params, fingerprint, before_complete,
            recoverable=verb in _RECOVERABLE_EXTERNAL)
    return _execute_atomic(
        principal, request_id, verb, params, fingerprint, before_complete)
