"""Agent presence: heartbeats, session state, derived liveness, and
the session-todo mirror.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import store
from ..config import CONFIG
from .store import (LIVE_LIVENESS, RESTING_STATES, SESSION_STATES,
                    _agent_role, _clean, _iso, _load_json, _log_event, _session_day, connect)

# --- presence ---------------------------------------------------------------

def heartbeat(role: str, *, state: str | None = None, activity: str | None = None,
              session_id: str | None = None, harness: str | None = None,
              db_path: Path | str | None = None) -> None:
    """Upsert the role's live session row and refresh its heartbeat timestamp."""
    now_dt = store._now()
    now = _iso(now_dt)
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        if state is not None and state not in SESSION_STATES:
            raise ValueError(f"invalid session state: {state}")
        activity = _clean(activity, "activity", required=False)
        existing = conn.execute(
            "SELECT * FROM agent_sessions WHERE role=?", (role,)).fetchone()
        if existing is None or existing["ended_at"] is not None:
            # Fresh session (or reviving an ended one): reset lifecycle, stamp
            # the day it belongs to (drives cross-day rollover), phase active.
            conn.execute(
                """INSERT INTO agent_sessions
                       (role, session_id, harness, state, activity, started_at,
                        last_heartbeat_at, ended_at, session_day, phase)
                   VALUES (?,?,?,?,?,?,?,NULL,?, 'active')
                   ON CONFLICT(role) DO UPDATE SET
                       session_id=excluded.session_id, harness=excluded.harness,
                       state=excluded.state, activity=excluded.activity,
                       started_at=excluded.started_at,
                       last_heartbeat_at=excluded.last_heartbeat_at, ended_at=NULL,
                       session_day=excluded.session_day, phase='active'""",
                (role, session_id, harness, state or "working", activity, now, now,
                 _session_day(now_dt)),
            )
        else:
            conn.execute(
                """UPDATE agent_sessions SET last_heartbeat_at=?,
                       state=COALESCE(?,state),
                       activity=COALESCE(?,activity),
                       session_id=COALESCE(?,session_id),
                       harness=COALESCE(?,harness)
                   WHERE role=?""",
                (now, state, activity, session_id, harness, role),
            )


def set_status(role: str, *, state: str | None = None, activity: str | None = None,
               session_id: str | None = None, harness: str | None = None,
               db_path: Path | str | None = None) -> dict:
    """Agent-facing status update; heartbeats and logs an event."""
    heartbeat(role, state=state, activity=activity, session_id=session_id,
              harness=harness, db_path=db_path)
    with connect(db_path, write=True) as conn:
        _log_event(conn, role, "status", role,
                   {"state": state, "activity": activity})
        row = conn.execute("SELECT * FROM agent_sessions WHERE role=?", (role,)).fetchone()
        return _presence_row(dict(row), store._now())


def end_session(role: str, *, db_path: Path | str | None = None) -> None:
    now = _iso()
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        conn.execute(
            "UPDATE agent_sessions SET ended_at=?, state='stopping', phase='closed', "
            "last_heartbeat_at=? WHERE role=?",
            (now, now, role),
        )
        # The session's live work breakdown belongs to that session — a fresh
        # session writes its own — so clear it, else the board shows a dead
        # session's in-progress todos as if it were still executing.
        conn.execute("DELETE FROM session_todos WHERE role=?", (role,))
        _log_event(conn, role, "session_end", role, None)


def _presence_row(row: dict, now: dt.datetime) -> dict:
    """Attach derived liveness to a raw agent_sessions row.

    `dead` means "it claims to be working and has stopped heartbeating". It must
    NOT mean "no process exists": between wakes there is never a process, and
    this engine's whole premise is that this is normal. A session that parked
    itself in a resting state and then went quiet is doing exactly what it was
    told to, so it reads `idle` — and, crucially, stays RESUMABLE.

    Without that distinction a driver has only one way to stop a finished turn
    looking like a crash: call end_session, which sets ended_at and thereby
    destroys resumability. That trade was being made silently, and it made the
    ladder's `resume` rung dead code — every wake paid a cold start while
    design.md advertised "within a day, resume (context is preserved, cheap)".
    """
    hb = row.get("last_heartbeat_at")
    age = None
    liveness = "never"
    if row.get("ended_at"):
        liveness = "offline"
    elif hb:
        age = (now - dt.datetime.fromisoformat(hb)).total_seconds()
        if age < CONFIG.online_max_seconds:
            liveness = "online"
        elif age < CONFIG.suspect_max_seconds:
            liveness = "suspect"
        elif row.get("state") in RESTING_STATES:
            liveness = "idle"
        else:
            liveness = "dead"
    return {
        "role": row["role"],
        "session_id": row.get("session_id"),
        "harness": row.get("harness"),
        "state": row.get("state"),
        "activity": row.get("activity"),
        "started_at": row.get("started_at"),
        "last_heartbeat_at": hb,
        "ended_at": row.get("ended_at"),
        "heartbeat_age_seconds": None if age is None else int(age),
        "liveness": liveness,
        "session_day": row.get("session_day"),
        "phase": row.get("phase"),
        "stale_day": bool(row.get("session_day") and not row.get("ended_at")
                          and row["session_day"] < _session_day(now)),
    }


def _role_presence(conn, role: str, now: dt.datetime) -> dict:
    """Derived presence for ONE role, session row or not.

    The single place a role with no session row at all is turned into presence.
    Every caller that asks "is this role executing right now?" must get the same
    answer, and there were three hand-rolled copies of this `base` dict before
    the wake path needed to ask it too.
    """
    sess = conn.execute(
        "SELECT * FROM agent_sessions WHERE role=?", (role,)).fetchone()
    base = {"role": role, "session_id": None, "harness": None,
            "state": None, "activity": None, "started_at": None,
            "last_heartbeat_at": None, "ended_at": None}
    return _presence_row(dict(sess) if sess else base, now)


def _is_busy(conn, role: str, now: dt.datetime) -> bool:
    """True if a turn is running that a wake would INTERRUPT.

    Deliberately the same predicate the board uses to decide whether a session's
    work breakdown may be shown as current (LIVE_LIVENESS): "executing right now"
    is one fact, and the surface that renders it and the path that decides whether
    interrupting it is allowed must never disagree about it. Everything else —
    parked (`idle`), crashed (`dead`), ended (`offline`), never started (`never`)
    — has no turn in flight, so there is nothing there to interrupt.
    """
    return _role_presence(conn, role, now)["liveness"] in LIVE_LIVENESS


def _presence_list(conn, now: dt.datetime) -> list[dict]:
    """Presence for all enabled roles using an EXISTING connection (so callers
    already inside a write transaction don't open a second, dead-locking one)."""
    reg = conn.execute(
        "SELECT * FROM agent_registry WHERE enabled=1 ORDER BY role").fetchall()
    out = []
    for r in reg:
        row = _role_presence(conn, r["role"], now)
        row["display_name"] = r["display_name"]
        row["capabilities"] = _load_json(r["capabilities"])
        row["authority"] = _load_json(r["authority"])
        out.append(row)
    return out


def presence(db_path: Path | str | None = None) -> list[dict]:
    """One entry per enabled registered role, with derived liveness."""
    now = store._now()
    with connect(db_path) as conn:
        return _presence_list(conn, now)

# --- session todo mirror ----------------------------------------------------

def record_todos(role: str, snapshot, *, db_path: Path | str | None = None) -> None:
    """Mirror a session's live todo list (display-only, not authoritative)."""
    now = _iso()
    payload = snapshot if isinstance(snapshot, str) else json.dumps(snapshot)
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        conn.execute(
            """INSERT INTO session_todos (role, snapshot, updated_at) VALUES (?,?,?)
               ON CONFLICT(role) DO UPDATE SET snapshot=excluded.snapshot,
                   updated_at=excluded.updated_at""",
            (role, payload, now),
        )
