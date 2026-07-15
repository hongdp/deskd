"""Agent orchestration: presence, tasks, a unified inbox, and wake orchestration.

Layered on the durable mailbox / meeting tables (same SQLite WAL file). This
module tracks *agent* state — who is online, what they are doing, and their
cross-session work items — as distinct from meeting attendance, which is
per-meeting. It is domain-agnostic: it knows nothing about what the agents
actually do. A host application supplies the roles (``CONFIG.roles``), the
notification sources (``CONFIG.inbox_sources``), the probe allowlist, and the
prompt that boots a woken session (``CONFIG.prompt_builder``).

Design invariants worth knowing before you change anything here:

- ``agent_sessions`` is keyed by ROLE, not session id. The system enforces at
  most one live session per role (``config.role_lock_path`` + flock), and the
  supervisor is never an agent role, so a role-keyed row is faithful and lets an
  in-session hook write a heartbeat without solving "which role am I?".
- Task ``priority`` (urgent/normal/low) is the only axis that drives waking.
  ``due_at`` is a *soft* deadline — pure visibility/ordering, never a wake
  trigger. Overdue open tasks sort to the top everywhere.
- The role registry (``agent_registry``) is the single source of truth for which
  roles exist. Nothing in this module hardcodes a role name; every role literal
  that reaches SQL is a bound placeholder built from the registry.
- This layer only ever WAKES agents. It never acts as one, and it never
  executes anything on their behalf. The only code it runs is a host-allowlisted
  probe, which may observe and notify — nothing else.
- ``plan_wakes`` decides, the driver executes. Nothing here spawns or resumes a
  session; the driver holds the per-role lock and does that.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from contextlib import contextmanager
from pathlib import Path

from . import mailbox, meetings
from .config import CONFIG, PROJECT_NAME

SESSION_STATES = {
    "booting", "working", "idle_standby", "in_meeting", "stopping", "dead",
}
TASK_STATUSES = {"pending", "in_progress", "blocked", "done", "cancelled"}
TASK_OPEN_STATUSES = {"pending", "in_progress", "blocked"}
TASK_PRIORITIES = {"urgent", "normal", "low"}

#: Task provenance kinds that are engine-intrinsic. The supervisor role name is
#: added at validation time (see ``_task_sources``) because it is configurable.
TASK_SOURCES_BASE = {"meeting", "self", "system"}

#: Mailbox recipient token meaning "every attendee". Engine-level (defined by
#: the mailbox transport), not a role. Taken from the module that owns it so a
#: second spelling here can never drift from the on-disk contract.
_RECIPIENT_ALL = mailbox.BROADCAST

ORCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_registry (
    role                  TEXT PRIMARY KEY,
    display_name          TEXT NOT NULL,
    capabilities          TEXT NOT NULL DEFAULT '[]',
    authority             TEXT NOT NULL DEFAULT '{}',
    enabled               INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    role                  TEXT PRIMARY KEY,
    session_id            TEXT,
    harness               TEXT,
    state                 TEXT NOT NULL,
    activity              TEXT,
    started_at            TEXT NOT NULL,
    last_heartbeat_at     TEXT NOT NULL,
    ended_at              TEXT,
    session_day           TEXT,   -- local-tz day the session belongs to
    phase                 TEXT     -- active | draining | closed
);

-- No CHECK on source_kind: a host defines its own task provenance kinds and its
-- own supervisor role name, so the enumeration is validated in Python instead.
CREATE TABLE IF NOT EXISTS agent_tasks (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    title                 TEXT NOT NULL,
    detail                TEXT,
    assignee_role         TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','in_progress','blocked','done','cancelled')),
    priority              TEXT NOT NULL DEFAULT 'normal'
                          CHECK (priority IN ('urgent','normal','low')),
    source_kind           TEXT NOT NULL DEFAULT 'self',
    source_ref            TEXT,
    due_at                TEXT,
    created_by            TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    result_note           TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_tasks_assignee
ON agent_tasks(assignee_role, status);

CREATE TABLE IF NOT EXISTS session_todos (
    role                  TEXT PRIMARY KEY,
    snapshot              TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orchestration_events (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at            TEXT NOT NULL,
    actor                 TEXT NOT NULL,
    kind                  TEXT NOT NULL,
    ref                   TEXT,
    payload               TEXT
);

-- One row per (meeting message, intended recipient role). A pure PROJECTION of
-- the durable mailbox tables (messages/notifications/receipts): sync_delivery()
-- (re)derives it idempotently, so a row can never be silently lost — the source
-- message is durable and always re-projects. Rows are never deleted except by
-- FK cascade when the source message is. The time-dependent state (queued/
-- notified/read/overdue/escalated) is computed at read time, never stored.
CREATE TABLE IF NOT EXISTS message_delivery (
    message_id            INTEGER NOT NULL REFERENCES mailbox_messages(id) ON DELETE CASCADE,
    recipient_role        TEXT NOT NULL,
    thread_id             TEXT NOT NULL,
    queued_at             TEXT NOT NULL,
    sla_due_at            TEXT NOT NULL,
    notified_at           TEXT,
    read_at               TEXT,
    first_projected_at    TEXT NOT NULL,
    PRIMARY KEY (message_id, recipient_role)
);

-- Append-only ledger of wake attempts. plan_wakes() records here; the driver
-- executes the returned plan. Every escalation is a new row (the old one marked
-- 'superseded'), so the full wake history of a demand is auditable. This layer
-- only wakes agents — it never acts as an agent.
CREATE TABLE IF NOT EXISTS wake_attempts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    role                  TEXT NOT NULL,
    reason_kind           TEXT NOT NULL,   -- meeting_wake / stuck_delivery / urgent_task / inbox
    source_ref            TEXT NOT NULL,
    channel               TEXT NOT NULL,   -- a CONFIG.wake_ladder rung channel
    level                 INTEGER NOT NULL,
    attempted_at          TEXT NOT NULL,
    outcome               TEXT NOT NULL DEFAULT 'pending'
                          CHECK (outcome IN ('pending','acked','read','timeout','superseded','failed')),
    resolved_at           TEXT,
    latency_seconds       INTEGER,
    detail                TEXT
);

CREATE INDEX IF NOT EXISTS idx_wake_attempts_open
ON wake_attempts(outcome, role, reason_kind, source_ref);

-- Unified agent inbox: every agent-directed notification (host alert, signal,
-- system event, projected meeting message, supervisor note) lands here as ONE
-- queue per role. The wake orchestrator delivers these by resuming the role's
-- session with the queued items as the prompt (batched; urgent = immediate).
-- queued -> delivered -> acked. No CHECK on source_kind: hosts extend the set
-- via CONFIG.inbox_sources, so it is validated in Python.
CREATE TABLE IF NOT EXISTS agent_inbox (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    target_role           TEXT NOT NULL,
    source_kind           TEXT NOT NULL,
    ref                   TEXT,
    priority              TEXT NOT NULL DEFAULT 'normal'
                          CHECK (priority IN ('urgent','normal','low')),
    title                 TEXT NOT NULL,
    body                  TEXT,
    dedup_key             TEXT,
    enqueued_at           TEXT NOT NULL,
    delivered_at          TEXT,
    acked_at              TEXT,
    expires_at            TEXT
);

-- At most one un-acked item per (role, dedup_key): a re-firing notification with
-- the same key is a no-op until the current one is acked, then it can enqueue
-- again. Partial unique index — acked history is unconstrained.
CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_dedup
ON agent_inbox(target_role, dedup_key) WHERE dedup_key IS NOT NULL AND acked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_inbox_open
ON agent_inbox(target_role, acked_at, delivered_at);

-- Agent-registered wake hooks: the self-service API through which an agent asks
-- the orchestrator to wake it later — a one-shot timer ('at'), a recurring timer
-- ('interval'), a calendar schedule ('cron'), or a custom watcher function
-- evaluated per tick ('probe', a dotted callable inside CONFIG.probe_allowlist).
-- Firing enqueues an agent_inbox item, which rides the normal delivery/wake
-- ladder. Agents must use this instead of ANY self-managed waiting/polling.
CREATE TABLE IF NOT EXISTS wake_hooks (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_role            TEXT NOT NULL,
    kind                  TEXT NOT NULL,   -- at | interval | probe | cron (validated in Python)
    title                 TEXT NOT NULL,
    body                  TEXT,
    priority              TEXT NOT NULL DEFAULT 'normal'
                          CHECK (priority IN ('urgent','normal','low')),
    spec                  TEXT NOT NULL,   -- JSON: {at} | {every,until?} | {cron,tz} | {callable,every,until?}
    status                TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active','done','cancelled','error')),
    next_fire_at          TEXT,
    last_fired_at         TEXT,
    fire_count            INTEGER NOT NULL DEFAULT 0,
    error_streak          INTEGER NOT NULL DEFAULT 0,
    last_error            TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wake_hooks_due
ON wake_hooks(status, next_fire_at);
"""


# --- time helpers -----------------------------------------------------------

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime | None = None) -> str:
    return (value or _now()).astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _session_day(value: dt.datetime | None = None) -> str:
    """The local (CONFIG.timezone) calendar date a moment belongs to.

    This is the session-rollover boundary: a session stamped with an earlier day
    is stale and gets wound down by rollover_plan().
    """
    return (value or _now()).astimezone(CONFIG.tzinfo()).date().isoformat()


def _clean(value: str | None, label: str, *, required: bool = True) -> str | None:
    out = " ".join((value or "").split())
    if not out:
        if required:
            raise ValueError(f"{label} is required")
        return None
    return out


def _normalize_due(value: str | None) -> str | None:
    """Parse a free-form ISO timestamp and re-emit it in canonical UTC.

    All stored/compared timestamps are canonical UTC ('+00:00') so the
    lexicographic string comparisons in _task_sort_key / _task_view are
    chronologically correct. A naive (offset-less) input is interpreted as UTC.
    Invalid input is rejected rather than silently mis-sorted.
    """
    v = _clean(value, "due_at", required=False)
    if v is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(v)
    except ValueError as exc:
        raise ValueError(f"invalid due_at (expected ISO 8601): {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return _iso(parsed)


# --- connection, seeding, migrations ----------------------------------------

def _seed_registry(conn) -> None:
    """Insert CONFIG.roles into agent_registry if absent.

    DO NOTHING on conflict is deliberate: the DB row is authoritative once it
    exists, so runtime changes (e.g. enabled=0) are never clobbered by a config
    reload. An empty CONFIG.roles seeds nothing — the engine assumes no roles.
    """
    now = _iso()
    for spec in CONFIG.roles:
        conn.execute(
            """INSERT INTO agent_registry
                   (role, display_name, capabilities, authority, enabled, created_at)
               VALUES (?,?,?,?,1,?)
               ON CONFLICT(role) DO NOTHING""",
            (spec.name, spec.display_name or spec.name,
             json.dumps(list(spec.capabilities)), json.dumps(spec.authority), now),
        )


def _has_enum_check(conn, table: str, column: str) -> bool:
    """True if `table`.`column` still carries a legacy `CHECK (col IN (...))`.

    Such constraints enumerate role/source literals and must not exist: a host
    defines its own. We detect them from the stored DDL and rebuild the table.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    sql = (row[0] if row else "") or ""
    return bool(re.search(rf"CHECK\s*\(\s*{column}\s+IN\s*\(", sql, re.IGNORECASE))


def _rebuild(conn, table: str, columns_ddl: str, indexes_ddl: str = "") -> None:
    """Rebuild `table` with new DDL, copying every row (column order must match).

    SQLite cannot drop a CHECK constraint in place; the copy-and-rename dance is
    the supported migration. Safe to run inside the caller's transaction.
    """
    conn.executescript(f"""
        CREATE TABLE {table}__new ({columns_ddl});
        INSERT INTO {table}__new SELECT * FROM {table};
        DROP TABLE {table};
        ALTER TABLE {table}__new RENAME TO {table};
        {indexes_ddl}
    """)


_AGENT_TASKS_DDL = """
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, detail TEXT,
    assignee_role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','in_progress','blocked','done','cancelled')),
    priority TEXT NOT NULL DEFAULT 'normal'
        CHECK (priority IN ('urgent','normal','low')),
    source_kind TEXT NOT NULL DEFAULT 'self', source_ref TEXT, due_at TEXT,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    result_note TEXT
"""

_AGENT_INBOX_DDL = """
    id INTEGER PRIMARY KEY AUTOINCREMENT, target_role TEXT NOT NULL,
    source_kind TEXT NOT NULL, ref TEXT,
    priority TEXT NOT NULL DEFAULT 'normal'
        CHECK (priority IN ('urgent','normal','low')),
    title TEXT NOT NULL, body TEXT, dedup_key TEXT, enqueued_at TEXT NOT NULL,
    delivered_at TEXT, acked_at TEXT, expires_at TEXT
"""

_WAKE_HOOKS_DDL = """
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_role TEXT NOT NULL,
    kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT,
    priority TEXT NOT NULL DEFAULT 'normal', spec TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', next_fire_at TEXT,
    last_fired_at TEXT, fire_count INTEGER NOT NULL DEFAULT 0,
    error_streak INTEGER NOT NULL DEFAULT 0, last_error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
"""


def _migrate(conn) -> None:
    """Bring an existing DB up to the current schema. Idempotent."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(agent_sessions)")}
    if "session_day" not in cols:
        conn.execute("ALTER TABLE agent_sessions ADD COLUMN session_day TEXT")
    if "phase" not in cols:
        conn.execute("ALTER TABLE agent_sessions ADD COLUMN phase TEXT")
    # Legacy DBs (including one adopted from a host that predates deskd) may
    # still enumerate roles/sources/kinds in CHECK constraints. Drop them so the
    # host's own vocabulary works; Python validates instead.
    if _has_enum_check(conn, "agent_tasks", "source_kind"):
        _rebuild(conn, "agent_tasks", _AGENT_TASKS_DDL,
                 "CREATE INDEX IF NOT EXISTS idx_agent_tasks_assignee "
                 "ON agent_tasks(assignee_role, status);")
    if _has_enum_check(conn, "agent_inbox", "source_kind"):
        _rebuild(conn, "agent_inbox", _AGENT_INBOX_DDL, """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_dedup
            ON agent_inbox(target_role, dedup_key)
            WHERE dedup_key IS NOT NULL AND acked_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_inbox_open
            ON agent_inbox(target_role, acked_at, delivered_at);
        """)
    if _has_enum_check(conn, "wake_hooks", "kind"):
        _rebuild(conn, "wake_hooks", _WAKE_HOOKS_DDL,
                 "CREATE INDEX IF NOT EXISTS idx_wake_hooks_due "
                 "ON wake_hooks(status, next_fire_at);")


@contextmanager
def connect(db_path: Path | str | None = None, *, write: bool = False):
    """Open the shared DB with mailbox + meeting + orchestration schema."""
    with meetings.connect(db_path) as conn:
        conn.executescript(ORCH_SCHEMA)
        _migrate(conn)
        _seed_registry(conn)
        conn.commit()
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn


# --- roles ------------------------------------------------------------------

def _known_roles(conn) -> set[str]:
    """The registry is the source of truth for which roles exist."""
    return {r["role"] for r in conn.execute(
        "SELECT role FROM agent_registry WHERE enabled=1")}


def _role_params(conn) -> tuple[list[str], str]:
    """(sorted role list, SQL placeholder string) for binding roles into SQL.

    Role literals must NEVER be interpolated into SQL. Sorted for deterministic
    query text (statement-cache friendly).
    """
    roles = sorted(_known_roles(conn))
    return roles, ",".join("?" * len(roles))


def _agent_role(conn, role: str) -> str:
    """Validate an agent-facing role argument.

    The supervisor is a human identity, not an agent: it has no session, no
    heartbeat, and no inbox, and its actions only enter through the
    authenticated Web adapter. Agent-facing APIs reject it outright.
    """
    role = _clean(role, "role")
    if role == CONFIG.supervisor_role:
        raise ValueError(
            f"{CONFIG.supervisor_role} is not an agent role; "
            f"use the authenticated Web adapter")
    if role not in _known_roles(conn):
        raise ValueError(f"unknown or disabled agent role: {role}")
    return role


def _task_sources() -> set[str]:
    return TASK_SOURCES_BASE | {CONFIG.supervisor_role}


# --- presence ---------------------------------------------------------------

def heartbeat(role: str, *, state: str | None = None, activity: str | None = None,
              session_id: str | None = None, harness: str | None = None,
              db_path: Path | str | None = None) -> None:
    """Upsert the role's live session row and refresh its heartbeat timestamp."""
    now_dt = _now()
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
        return _presence_row(dict(row), _now())


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
    """Attach derived liveness to a raw agent_sessions row."""
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


def _presence_list(conn, now: dt.datetime) -> list[dict]:
    """Presence for all enabled roles using an EXISTING connection (so callers
    already inside a write transaction don't open a second, dead-locking one)."""
    reg = conn.execute(
        "SELECT * FROM agent_registry WHERE enabled=1 ORDER BY role").fetchall()
    out = []
    for r in reg:
        sess = conn.execute(
            "SELECT * FROM agent_sessions WHERE role=?", (r["role"],)).fetchone()
        base = {"role": r["role"], "session_id": None, "harness": None,
                "state": None, "activity": None, "started_at": None,
                "last_heartbeat_at": None, "ended_at": None}
        row = _presence_row(dict(sess) if sess else base, now)
        row["display_name"] = r["display_name"]
        row["capabilities"] = _load_json(r["capabilities"])
        row["authority"] = _load_json(r["authority"])
        out.append(row)
    return out


def presence(db_path: Path | str | None = None) -> list[dict]:
    """One entry per enabled registered role, with derived liveness."""
    now = _now()
    with connect(db_path) as conn:
        return _presence_list(conn, now)


# --- tasks ------------------------------------------------------------------

_PRIO_RANK = {"urgent": 0, "normal": 1, "low": 2}


def _task_sort_key(t: dict, now_iso: str):
    closed = t["status"] in ("done", "cancelled")
    overdue = (not closed) and bool(t["due_at"]) and t["due_at"] < now_iso
    return (
        1 if closed else 0,                       # open before closed
        0 if overdue else 1,                      # overdue before on-time
        t["due_at"] if overdue else "",           # most-overdue (earliest due) first
        _PRIO_RANK.get(t["priority"], 1),         # urgent before normal before low
        0 if t["due_at"] else 1,                  # has-due before no-due
        t["due_at"] or "9999-12-31",              # soonest due first
        t["created_at"],                          # stable oldest-first
    )


def _task_view(row: dict, now_iso: str) -> dict:
    out = dict(row)
    out["overdue"] = (out["status"] in TASK_OPEN_STATUSES
                      and bool(out["due_at"]) and out["due_at"] < now_iso)
    return out


def task_add(title: str, *, assignee_role: str, detail: str | None = None,
             priority: str = "normal", source_kind: str = "self",
             source_ref: str | None = None, due_at: str | None = None,
             created_by: str | None = None,
             db_path: Path | str | None = None) -> int:
    """Create a cross-session work item.

    ``due_at`` is a SOFT deadline: it drives ordering and the overdue flag, and
    never wakes anyone. Only priority='urgent' generates a wake demand.
    """
    now = _iso()
    title = _clean(title, "title")
    if priority not in TASK_PRIORITIES:
        raise ValueError(f"invalid priority: {priority}")
    if source_kind not in _task_sources():
        raise ValueError(f"invalid source_kind: {source_kind}")
    due_at = _normalize_due(due_at)
    with connect(db_path, write=True) as conn:
        assignee_role = _agent_role(conn, assignee_role)
        created_by = _clean(created_by, "created_by", required=False) or assignee_role
        cur = conn.execute(
            """INSERT INTO agent_tasks
                   (title, detail, assignee_role, status, priority, source_kind,
                    source_ref, due_at, created_by, created_at, updated_at)
               VALUES (?,?,?,'pending',?,?,?,?,?,?,?)""",
            (title, _clean(detail, "detail", required=False), assignee_role,
             priority, source_kind, source_ref, due_at, created_by, now, now),
        )
        task_id = cur.lastrowid
        _log_event(conn, created_by, "task_add", str(task_id),
                   {"assignee": assignee_role, "priority": priority, "title": title})
        return task_id


def tasks(*, assignee_role: str | None = None, status: str | None = None,
          include_closed: bool = False,
          db_path: Path | str | None = None) -> list[dict]:
    now_iso = _iso()
    clauses, params = [], []
    if assignee_role:
        clauses.append("assignee_role=?")
        params.append(assignee_role)
    if status:
        clauses.append("status=?")
        params.append(status)
    elif not include_closed:
        clauses.append("status NOT IN ('done','cancelled')")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect(db_path) as conn:
        rows = [_task_view(dict(r), now_iso) for r in conn.execute(
            f"SELECT * FROM agent_tasks {where}", params).fetchall()]
    rows.sort(key=lambda t: _task_sort_key(t, now_iso))
    return rows


def task_update(task_id: int, *, actor: str | None = None,
                db_path: Path | str | None = None, **fields) -> bool:
    """Update mutable task fields. Only status/priority/due_at/detail/title/
    result_note/assignee_role are settable; unknown or None fields are ignored."""
    allowed = {"status", "priority", "due_at", "detail", "title",
               "result_note", "assignee_role"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    if "status" in updates and updates["status"] not in TASK_STATUSES:
        raise ValueError(f"invalid status: {updates['status']}")
    if "priority" in updates and updates["priority"] not in TASK_PRIORITIES:
        raise ValueError(f"invalid priority: {updates['priority']}")
    if "due_at" in updates:
        updates["due_at"] = _normalize_due(updates["due_at"])
    now = _iso()
    with connect(db_path, write=True) as conn:
        if "assignee_role" in updates:
            updates["assignee_role"] = _agent_role(conn, updates["assignee_role"])
        sets = ", ".join(f"{k}=?" for k in updates) + ", updated_at=?"
        params = list(updates.values()) + [now, task_id]
        cur = conn.execute(f"UPDATE agent_tasks SET {sets} WHERE id=?", params)
        if cur.rowcount:
            _log_event(conn, actor or "system", "task_update", str(task_id), updates)
        return bool(cur.rowcount)


def task_close(task_id: int, *, status: str = "done", note: str | None = None,
               actor: str | None = None, db_path: Path | str | None = None) -> bool:
    if status not in ("done", "cancelled"):
        raise ValueError("close status must be done or cancelled")
    return task_update(task_id, status=status, result_note=note, actor=actor,
                       db_path=db_path)


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


# --- events -----------------------------------------------------------------

def _log_event(conn, actor: str, kind: str, ref: str | None, payload) -> None:
    conn.execute(
        "INSERT INTO orchestration_events (created_at, actor, kind, ref, payload) "
        "VALUES (?,?,?,?,?)",
        (_iso(), actor, kind, ref,
         None if payload is None else json.dumps(payload, ensure_ascii=False)),
    )


def recent_events(limit: int = 20, db_path: Path | str | None = None) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM orchestration_events ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("payload"):
            d["payload"] = _load_json(d["payload"])
        out.append(d)
    return out


# --- delivery ledger --------------------------------------------------------

DELIVERY_STATES = {"queued", "notified", "read", "overdue", "escalated"}


def sync_delivery(conn) -> int:
    """Idempotently project the durable mailbox tables into message_delivery.

    Scope mirrors the transcript's authenticity filter: messages sent by a
    registered agent role, or by the supervisor WITH a verified message-auth row
    (an unauthenticated supervisor message is not a real message and must never
    create a delivery obligation). One row per intended agent recipient (an
    attendee, not the sender, whose recipient tag matches). Re-running is safe
    and self-healing — a missing row is recreated because the source message is
    durable. Requires a write transaction.
    """
    now = _iso()
    roles, ph = _role_params(conn)
    if not roles:
        return 0
    pairs = conn.execute(
        f"""SELECT mm.id, mm.thread_id, mm.created_at, a.role,
                   COALESCE(m.wait_timeout_seconds, 300) AS sla
            FROM mailbox_messages mm
            JOIN meetings m ON m.thread_id = mm.thread_id
            JOIN meeting_attendees a ON a.thread_id = mm.thread_id
                 AND a.role != mm.sender AND a.role IN ({ph})
                 AND mm.recipient IN (a.role, ?)
            WHERE mm.sender IN ({ph})
               OR (mm.sender=? AND EXISTS
                   (SELECT 1 FROM meeting_message_auth ma WHERE ma.message_id=mm.id))""",
        (*roles, _RECIPIENT_ALL, *roles, CONFIG.supervisor_role),
    ).fetchall()
    for mm_id, thread_id, created_at, role, sla in pairs:
        sla_due = _iso(dt.datetime.fromisoformat(created_at)
                       + dt.timedelta(seconds=int(sla)))
        notified = conn.execute(
            "SELECT notified_at FROM mailbox_notifications WHERE message_id=? AND role=?",
            (mm_id, role)).fetchone()
        read = conn.execute(
            "SELECT read_at FROM mailbox_receipts WHERE message_id=? AND role=?",
            (mm_id, role)).fetchone()
        conn.execute(
            """INSERT INTO message_delivery
                   (message_id, recipient_role, thread_id, queued_at, sla_due_at,
                    notified_at, read_at, first_projected_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(message_id, recipient_role) DO UPDATE SET
                   notified_at=excluded.notified_at, read_at=excluded.read_at,
                   sla_due_at=excluded.sla_due_at""",
            (mm_id, role, thread_id, created_at, sla_due,
             notified["notified_at"] if notified else None,
             read["read_at"] if read else None, now),
        )
    return len(pairs)


def _wake_keys(conn) -> set:
    """(thread, role) pairs with a wake request — the PER-ROLE signal that the
    system is actively re-driving delivery to that specific role."""
    return {(r["thread_id"], r["role"]) for r in conn.execute(
        "SELECT thread_id, role FROM meeting_wake_requests")}


def _delivery_state(row, now_iso: str, wake: set) -> str:
    if row["read_at"]:
        return "read"
    if row["sla_due_at"] >= now_iso:            # still within SLA
        return "notified" if row["notified_at"] else "queued"
    # Past SLA, still unread: 'escalated' ONLY if THIS role is being re-driven
    # (a per-role wake request). A thread-level escalation raised for one role
    # must never mask another role's genuinely-stuck message — otherwise a single
    # historical escalation freezes the whole thread out of ever producing a
    # stuck_delivery wake demand again.
    if (row["thread_id"], row["recipient_role"]) in wake:
        return "escalated"
    return "overdue"


def delivery_ledger(thread_id: str | None = None,
                    db_path: Path | str | None = None) -> dict:
    """Per-message per-recipient delivery ledger, keyed message_id -> role.

    Syncs the projection first so the source of truth (durable messages) is
    always fully reflected. Returns computed state + every hop timestamp."""
    now_iso = _iso()
    with connect(db_path, write=True) as conn:
        sync_delivery(conn)
        wake = _wake_keys(conn)
        q = "SELECT * FROM message_delivery"
        params: tuple = ()
        if thread_id:
            q += " WHERE thread_id=?"
            params = (thread_id,)
        rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    out: dict = {}
    for r in rows:
        entry = {
            "state": _delivery_state(r, now_iso, wake),
            "queued_at": r["queued_at"], "sla_due_at": r["sla_due_at"],
            "notified_at": r["notified_at"], "read_at": r["read_at"],
        }
        out.setdefault(str(r["message_id"]), {})[r["recipient_role"]] = entry
    return out


# --- wake orchestrator ------------------------------------------------------

WAKE_REASONS = {"meeting_wake", "stuck_delivery", "urgent_task", "inbox"}


def _ladder():
    """The escalation ladder in effect (CONFIG.wake_ladder)."""
    return CONFIG.wake_ladder


def _channel_level(ladder, channel: str, fallback: int) -> int:
    """Index of the rung with `channel`, or a clamped fallback.

    Levels are ladder INDICES, so nothing may assume a fixed number here: a host
    can define its own ladder. Look the channel up by name instead.
    """
    for i, rung in enumerate(ladder):
        if rung.channel == channel:
            return i
    return max(0, min(fallback, len(ladder) - 1))


def _human_level(ladder) -> int:
    """First rung that leaves the machine (a human is being pulled in).

    Used for the 'wakes at human level' health counter — the number that should
    make someone look at the board.
    """
    for i, rung in enumerate(ladder):
        if rung.channel in ("human", "supervisor_badge"):
            return i
    return max(0, len(ladder) - 2)


def collect_wake_demand(conn) -> list[dict]:
    """Unify the DB-derived wake demands.

    Note: task ``due_at`` is deliberately NOT a source — soft deadlines never
    wake; only priority='urgent' pending tasks do.
    """
    now_iso = _iso()
    demands = []
    for r in conn.execute(
            """SELECT w.role, w.thread_id, m.agenda, w.created_at
               FROM meeting_wake_requests w JOIN meetings m ON m.thread_id=w.thread_id
               WHERE w.status='pending'"""):
        demands.append({"role": r["role"], "reason_kind": "meeting_wake",
                        "source_ref": r["thread_id"], "label": r["agenda"],
                        "since_at": r["created_at"]})
    wake = _wake_keys(conn)
    for r in conn.execute("SELECT * FROM message_delivery"):
        if _delivery_state(r, now_iso, wake) == "overdue":
            demands.append({"role": r["recipient_role"], "reason_kind": "stuck_delivery",
                            "source_ref": f'{r["thread_id"]}:{r["message_id"]}',
                            "label": f'msg#{r["message_id"]}', "since_at": r["queued_at"]})
    for r in conn.execute(
            "SELECT id, assignee_role, title, created_at FROM agent_tasks "
            "WHERE priority='urgent' AND status='pending'"):
        demands.append({"role": r["assignee_role"], "reason_kind": "urgent_task",
                        "source_ref": str(r["id"]), "label": r["title"],
                        "since_at": r["created_at"]})
    # Unified inbox — batched: an urgent item wakes now; non-urgent items wake
    # once the oldest has waited CONFIG.inbox_batch_seconds (or ride along with
    # any other demand for the role, since plan_wakes batches per role).
    inbox_by_role: dict = {}
    for r in conn.execute(
            "SELECT target_role, priority, enqueued_at FROM agent_inbox "
            "WHERE acked_at IS NULL AND delivered_at IS NULL"):
        inbox_by_role.setdefault(r["target_role"], []).append(r)
    now = _now()
    for role, items in inbox_by_role.items():
        oldest = min(i["enqueued_at"] for i in items)
        has_urgent = any(i["priority"] == "urgent" for i in items)
        age = (now - dt.datetime.fromisoformat(oldest)).total_seconds()
        if has_urgent or age > CONFIG.inbox_batch_seconds:
            demands.append({"role": role, "reason_kind": "inbox",
                            "source_ref": f"inbox:{role}",
                            "label": f"{len(items)} notification(s)", "since_at": oldest})
    return demands


def _demand_resolved(conn, role: str, reason_kind: str, source_ref: str,
                     now_iso: str) -> tuple[bool, str]:
    """Closed-loop check: has the underlying demand been satisfied?"""
    if reason_kind == "meeting_wake":
        pend = conn.execute(
            "SELECT 1 FROM meeting_wake_requests WHERE thread_id=? AND role=? "
            "AND status='pending'",
            (source_ref, role)).fetchone()
        return (pend is None, "acked")
    if reason_kind == "urgent_task":
        try:
            tid = int(source_ref)
        except ValueError:
            return (True, "acked")
        r = conn.execute("SELECT status, priority FROM agent_tasks WHERE id=?",
                         (tid,)).fetchone()
        # Resolved when the task is gone OR no longer an urgent-pending demand
        # (mirror collect_wake_demand's predicate so a de-prioritized-but-still-
        # pending task retires its attempt instead of orphaning it).
        resolved = r is None or not (r["status"] == "pending" and r["priority"] == "urgent")
        return (resolved, "acked")
    if reason_kind == "stuck_delivery":
        thread, _, msg = source_ref.partition(":")
        try:
            mid = int(msg)
        except ValueError:
            return (True, "read")
        r = conn.execute(
            "SELECT read_at FROM message_delivery WHERE message_id=? AND recipient_role=?",
            (mid, role)).fetchone()
        if r is None or r["read_at"]:
            return (True, "read")
        # Another channel took over (a wake request / escalation now exists).
        if conn.execute("SELECT 1 FROM meeting_wake_requests WHERE thread_id=? AND role=?",
                        (thread, role)).fetchone() or \
           conn.execute("SELECT 1 FROM meeting_escalations WHERE thread_id=?",
                        (thread,)).fetchone():
            return (True, "superseded")
        return (False, "")
    if reason_kind == "inbox":
        # Resolved once the role has no UNDELIVERED items (delivery = the wake
        # put them in front of the agent; acking is the agent's own step).
        n = conn.execute(
            "SELECT COUNT(*) FROM agent_inbox WHERE target_role=? "
            "AND acked_at IS NULL AND delivered_at IS NULL", (role,)).fetchone()[0]
        return (n == 0, "acked")
    return (True, "acked")


def _start_level(p: dict | None, ladder) -> int:
    """Which rung a brand-new demand starts on, given the role's presence."""
    spawn = _channel_level(ladder, "spawn", 2)
    if p is None:
        return spawn                              # unknown role -> spawn
    if p["liveness"] == "online":
        return _channel_level(ladder, "hook", 0)  # in-session hook will deliver
    if p.get("session_id") and not p.get("ended_at"):
        return _channel_level(ladder, "resume", 1)  # resumable session exists
    return spawn                                  # no live/resumable session


def _insert_attempt(conn, d: dict, level: int, now_iso: str, ladder) -> int:
    channel = ladder[level].channel
    cur = conn.execute(
        """INSERT INTO wake_attempts
               (role, reason_kind, source_ref, channel, level, attempted_at, outcome, detail)
           VALUES (?,?,?,?,?,?, 'pending', ?)""",
        (d["role"], d["reason_kind"], d["source_ref"], channel, level, now_iso,
         d.get("label")),
    )
    return cur.lastrowid


def _wake_reasons_text(ds: list[dict]) -> str:
    """Summarize a role's demands into one human-readable clause."""
    by: dict = {}
    for d in ds:
        by.setdefault(d["reason_kind"], []).append(d)
    parts = []
    if by.get("meeting_wake"):
        ags = ", ".join(sorted({str(d["label"]) for d in by["meeting_wake"]}))
        parts.append(f'{len(by["meeting_wake"])} meeting(s) need you ({ags})')
    if by.get("stuck_delivery"):
        parts.append(f'{len(by["stuck_delivery"])} message(s) past their read SLA')
    if by.get("urgent_task"):
        ts = ", ".join(sorted({str(d["label"]) for d in by["urgent_task"]}))
        parts.append(f'{len(by["urgent_task"])} urgent task(s) ({ts})')
    if by.get("inbox"):
        parts.append(f'{len(by["inbox"])} inbox batch(es) queued')
    return "; ".join(parts) or "pending work"


def _wake_prompt(role: str, ds: list[dict], inbox_items: list[dict] | None = None) -> str:
    """Build the prompt that boots/resumes a woken session.

    Delegates to CONFIG.prompt_builder: a cold-spawned session has NO context,
    so only the host knows how to tell it what it is.
    """
    titles = [("[!] " if i["priority"] == "urgent" else "") + i["title"]
              for i in sorted(inbox_items or [], key=_inbox_sort_key)]
    return CONFIG.prompt_builder.wake(role, _wake_reasons_text(ds), titles)


def plan_wakes(db_path: Path | str | None = None, *, record: bool = True) -> dict:
    """The four-step loop as one step: collect demand, close resolved attempts,
    create/escalate the rest, and RETURN a driver plan. Records to wake_attempts
    but never spawns/resumes anything — the driver executes.

    record=False is a truly side-effect-free preview: it makes the SAME decisions
    (using an in-memory resolved set so escalation logic is identical) but writes
    NOTHING — no probe runs, no timer advances, no phantom attempts, and the
    escalation clock does not move.
    """
    now = _now()
    now_iso = _iso(now)
    ladder = _ladder()
    resolved, changed = [], []
    with connect(db_path, write=record) as conn:
        sync_delivery(conn)
        # Agent-registered wake hooks fire first (same txn), so their inbox items
        # are visible to this tick's demand collection. Evaluated only in record
        # mode: a dry preview must not run probes or advance timers.
        hooks_fired = _eval_wake_hooks(conn, now) if record else []
        pres = {p["role"]: p for p in _presence_list(conn, now)}
        demands = collect_wake_demand(conn)
        # 1) close pending attempts whose demand is resolved or has disappeared
        resolved_keys = set()
        for a in conn.execute(
                "SELECT * FROM wake_attempts WHERE outcome='pending'").fetchall():
            done, outcome = _demand_resolved(conn, a["role"], a["reason_kind"],
                                             a["source_ref"], now_iso)
            if done:
                resolved_keys.add((a["role"], a["reason_kind"], a["source_ref"]))
                lat = int((now - dt.datetime.fromisoformat(a["attempted_at"])).total_seconds())
                if record:
                    conn.execute(
                        "UPDATE wake_attempts SET outcome=?, resolved_at=?, "
                        "latency_seconds=? WHERE id=?",
                        (outcome, now_iso, lat, a["id"]))
                    _log_event(conn, "orchestrator", "wake_resolved", a["source_ref"],
                               {"role": a["role"], "reason": a["reason_kind"],
                                "outcome": outcome, "latency_s": lat})
                resolved.append({"role": a["role"], "reason_kind": a["reason_kind"],
                                 "source_ref": a["source_ref"], "outcome": outcome,
                                 "latency_seconds": lat})
        # 2) create new attempts / escalate stale ones. Treat just-resolved
        # attempts as absent so decisions match whether or not we recorded.
        pend = {(a["role"], a["reason_kind"], a["source_ref"]): a
                for a in conn.execute(
                    "SELECT * FROM wake_attempts WHERE outcome='pending'").fetchall()
                if (a["role"], a["reason_kind"], a["source_ref"]) not in resolved_keys}
        for d in demands:
            cur = pend.get((d["role"], d["reason_kind"], d["source_ref"]))
            if cur is None:
                lvl = _start_level(pres.get(d["role"]), ladder)
                if record:
                    _insert_attempt(conn, d, lvl, now_iso, ladder)
                    _log_event(conn, "orchestrator", "wake_attempt", d["source_ref"],
                               {"role": d["role"], "reason": d["reason_kind"],
                                "level": lvl, "channel": ladder[lvl].channel})
                changed.append({**d, "level": lvl, "escalated": False})
            else:
                lvl = min(cur["level"], len(ladder) - 1)
                sla = ladder[lvl].sla_seconds
                age = (now - dt.datetime.fromisoformat(cur["attempted_at"])).total_seconds()
                if sla is not None and age > sla:
                    nl = min(lvl + 1, len(ladder) - 1)
                    # Escalation is APPEND-ONLY: supersede the old row, insert a
                    # new one. The wake history of a demand is never rewritten.
                    if record:
                        conn.execute(
                            "UPDATE wake_attempts SET outcome='superseded', resolved_at=? "
                            "WHERE id=?",
                            (now_iso, cur["id"]))
                        _insert_attempt(conn, d, nl, now_iso, ladder)
                        _log_event(conn, "orchestrator", "wake_escalate", d["source_ref"],
                                   {"role": d["role"], "reason": d["reason_kind"],
                                    "from": lvl, "to": nl, "channel": ladder[nl].channel})
                    changed.append({**d, "level": nl, "escalated": True})
        # 3) build the per-role actionable plan (L0 hook needs no driver action)
        changed_roles = {c["role"] for c in changed}
        actions = []
        for role in sorted(changed_roles):
            role_changes = [c for c in changed if c["role"] == role]
            top = max(role_changes, key=lambda x: x["level"])
            channel = ladder[top["level"]].channel
            if channel == "hook":
                continue
            role_demands = [d for d in demands if d["role"] == role]
            # The resume/spawn prompt CARRIES the role's inbox, but we do NOT
            # mark items delivered here: the plan is speculative — the driver may
            # skip (per-role lock) or the launch may fail. Delivered is stamped
            # only by the in-session hook when the session actually runs, or by
            # the agent's own ack. A failed launch therefore leaves the items
            # undelivered, the demand alive, and the ladder escalating.
            inbox_items = [dict(r) for r in conn.execute(
                "SELECT * FROM agent_inbox WHERE target_role=? "
                "AND acked_at IS NULL AND delivered_at IS NULL", (role,)).fetchall()]
            actions.append({
                "role": role, "level": top["level"], "channel": channel,
                "session_id": (pres.get(role) or {}).get("session_id"),
                "reasons": [{"reason_kind": d["reason_kind"], "source_ref": d["source_ref"],
                             "label": d.get("label")} for d in role_demands],
                "prompt": _wake_prompt(role, role_demands, inbox_items),
            })
    return {"generated_at": now_iso, "actions": actions,
            "resolved": resolved, "changed": changed,
            "hooks_fired": hooks_fired}


def wake_attempts_recent(limit: int = 20,
                         db_path: Path | str | None = None) -> list[dict]:
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM wake_attempts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def wake_sources(role: str, db_path: Path | str | None = None) -> dict:
    """One-shot answer to 'what can currently wake/remind me, and can I change
    it?' — the role's own registered hooks (self-managed via `hook add/cancel`),
    pending meeting wakes, queued inbox notifications, urgent tasks, and any
    in-flight wake attempts."""
    now_iso = _iso()
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        sync_delivery(conn)
        hooks_ = []
        for r in conn.execute(
                "SELECT * FROM wake_hooks WHERE owner_role=? AND status='active' "
                "ORDER BY (next_fire_at IS NULL), next_fire_at", (role,)):
            d = dict(r)
            d["spec"] = _load_json(d["spec"])
            hooks_.append(d)
        meeting_wakes = [dict(r) for r in conn.execute(
            "SELECT w.thread_id, m.agenda, w.created_at FROM meeting_wake_requests w "
            "JOIN meetings m ON m.thread_id=w.thread_id "
            "WHERE w.role=? AND w.status='pending'", (role,)).fetchall()]
        inbox = [dict(r) for r in conn.execute(
            "SELECT id, source_kind, priority, title, delivered_at, enqueued_at "
            "FROM agent_inbox WHERE target_role=? AND acked_at IS NULL", (role,)).fetchall()]
        urgent_tasks = [dict(r) for r in conn.execute(
            "SELECT id, title, due_at FROM agent_tasks "
            "WHERE assignee_role=? AND priority='urgent' AND status='pending'",
            (role,)).fetchall()]
        attempts = [dict(r) for r in conn.execute(
            "SELECT reason_kind, source_ref, channel, level, attempted_at "
            "FROM wake_attempts WHERE role=? AND outcome='pending' ORDER BY id DESC",
            (role,)).fetchall()]
    return {
        "role": role, "as_of": now_iso,
        "self_hooks": hooks_,                # yours — hook add/cancel to change
        "meeting_wakes": meeting_wakes,      # a meeting needs you (wake-ack/check-in)
        "inbox_queued": [i for i in inbox if not i["delivered_at"]],
        "inbox_delivered_unacked": [i for i in inbox if i["delivered_at"]],
        "urgent_tasks": urgent_tasks,
        "pending_wake_attempts": attempts,
        "manage": (f"{PROJECT_NAME} hook add --for {role} "
                   f"(--at|--every|--cron|--probe) / "
                   f"{PROJECT_NAME} hook cancel <id>; "
                   f"{PROJECT_NAME} inbox ack --for {role}"),
    }


# --- session lifecycle (cross-day rollover) ---------------------------------

def _rollover_prompt(role: str, from_day: str, today: str) -> str:
    """Wind-down prompt for a session left over from a previous day.

    SESSION_DONE on its own line is the engine's end-of-session sentinel: the
    driver watches for it to know the drain completed.
    """
    return (f"{CONFIG.prompt_builder.bootstrap(role)} "
            f"New session day {today} ({CONFIG.timezone}). Your session from "
            f"{from_day} must wind down and hand off: finish or park in-flight "
            f"work, write a handoff note recording open items and the first thing "
            f"the next session should do, then output SESSION_DONE on its own line "
            f"to end this session. A fresh session opens for the new day and picks "
            f"up from your handoff note.")


def rollover_plan(db_path: Path | str | None = None, *, record: bool = True) -> dict:
    """Detect sessions left over from a prior day and plan their wind-down: the
    driver resumes the stale session with the wrap-up prompt; when it outputs
    SESSION_DONE / ends, a fresh session opens and reads the handoff note.
    record=True marks newly-detected sessions 'draining' and returns them;
    record=False is a dry preview that mutates nothing."""
    today = _session_day()
    out = []
    with connect(db_path, write=record) as conn:
        rows = conn.execute(
            "SELECT * FROM agent_sessions WHERE ended_at IS NULL "
            "AND session_day IS NOT NULL AND session_day < ?", (today,)).fetchall()
        for r in rows:
            # Mark draining once (for the board); but ALWAYS return the action
            # while the session is stale-and-not-ended so a lock-busy tick
            # retries. The driver ends the session after draining it, which
            # clears it from this list (ended_at set) — bounding the drain to one
            # successful pass per stale session.
            if record and r["phase"] != "draining":
                conn.execute(
                    "UPDATE agent_sessions SET phase='draining', state='stopping' "
                    "WHERE role=?",
                    (r["role"],))
                _log_event(conn, "orchestrator", "session_rollover", r["role"],
                           {"from_day": r["session_day"], "to_day": today})
            out.append({"role": r["role"], "session_id": r["session_id"],
                        "from_day": r["session_day"], "to_day": today,
                        "prompt": _rollover_prompt(r["role"], r["session_day"], today)})
    return {"today": today, "rollovers": out}


# --- unified agent inbox ----------------------------------------------------

def _inbox_insert(conn, target_role: str, source_kind: str, title: str, *,
                  body: str | None = None, ref: str | None = None,
                  priority: str = "normal", dedup_key: str | None = None,
                  expires_at: str | None = None) -> int | None:
    """Same-connection inbox insert (for callers already inside a write txn).
    Returns the new id, or None on a (role, dedup_key) deduped no-op."""
    title = _clean(title, "title")
    if source_kind not in CONFIG.inbox_sources:
        raise ValueError(f"invalid source_kind: {source_kind}")
    if priority not in TASK_PRIORITIES:
        raise ValueError(f"invalid priority: {priority}")
    target_role = _agent_role(conn, target_role)
    cur = conn.execute(
        """INSERT OR IGNORE INTO agent_inbox
               (target_role, source_kind, ref, priority, title, body,
                dedup_key, enqueued_at, expires_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (target_role, source_kind, ref, priority, title,
         _clean(body, "body", required=False), dedup_key, _iso(), expires_at),
    )
    if cur.rowcount:
        _log_event(conn, source_kind, "inbox_enqueue", ref,
                   {"role": target_role, "title": title, "priority": priority})
        return cur.lastrowid
    return None


def inbox_enqueue(target_role: str, source_kind: str, title: str, *,
                  body: str | None = None, ref: str | None = None,
                  priority: str = "normal", dedup_key: str | None = None,
                  expires_at: str | None = None,
                  db_path: Path | str | None = None) -> int | None:
    """Enqueue an agent-directed notification — THE public ingress for hosts.

    This is how a host application injects its own domain events into the engine:
    the engine never reaches into the host to collect them. Returns the new id,
    or None if a same-(role, dedup_key) un-acked item already exists (deduped
    no-op).
    """
    with connect(db_path, write=True) as conn:
        return _inbox_insert(conn, target_role, source_kind, title, body=body,
                             ref=ref, priority=priority, dedup_key=dedup_key,
                             expires_at=expires_at)


_INBOX_RANK = {"urgent": 0, "normal": 1, "low": 2}


def _inbox_sort_key(r: dict):
    return (_INBOX_RANK.get(r["priority"], 1), r["enqueued_at"])


def inbox_pending(target_role: str | None = None, *, include_delivered: bool = True,
                  db_path: Path | str | None = None) -> list[dict]:
    """Un-acked inbox items (the live queue). include_delivered=False returns
    only not-yet-delivered items."""
    clauses = ["acked_at IS NULL"]
    params: list = []
    if target_role:
        clauses.append("target_role=?")
        params.append(target_role)
    if not include_delivered:
        clauses.append("delivered_at IS NULL")
    where = " AND ".join(clauses)
    with connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM agent_inbox WHERE {where}", params).fetchall()]
    rows.sort(key=_inbox_sort_key)
    return rows


def inbox_mark_delivered(ids, db_path: Path | str | None = None) -> int:
    """Stamp items delivered. Called by the in-session hook when the session
    actually runs, never speculatively at plan time."""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    now = _iso()
    with connect(db_path, write=True) as conn:
        q = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE agent_inbox SET delivered_at=? WHERE id IN ({q}) "
            f"AND delivered_at IS NULL",
            [now, *ids])
        return cur.rowcount


def inbox_ack(target_role: str | None = None, ids=None,
              db_path: Path | str | None = None) -> int:
    """Mark items processed. Pass ids to ack specific items, or target_role to
    ack all of a role's delivered-but-unacked items."""
    now = _iso()
    with connect(db_path, write=True) as conn:
        if ids:
            ids = [int(i) for i in ids]
            q = ",".join("?" * len(ids))
            cur = conn.execute(
                f"UPDATE agent_inbox SET acked_at=? WHERE id IN ({q}) AND acked_at IS NULL",
                [now, *ids])
        elif target_role:
            role = _agent_role(conn, target_role)
            # Only DELIVERED items: an item enqueued after this batch was
            # surfaced (still delivered_at NULL) has never been seen by the
            # agent — a blanket ack must not silently drop it.
            cur = conn.execute(
                "UPDATE agent_inbox SET acked_at=? WHERE target_role=? "
                "AND acked_at IS NULL AND delivered_at IS NOT NULL",
                (now, role))
        else:
            raise ValueError("inbox_ack needs ids or target_role")
        if cur.rowcount:
            _log_event(conn, target_role or "agent", "inbox_ack", None,
                       {"count": cur.rowcount})
        return cur.rowcount


def _inbox_view(rows: list[dict]) -> dict:
    """Group a role's un-acked items into queued (not delivered) vs delivered."""
    queued = [r for r in rows if not r["delivered_at"]]
    delivered = [r for r in rows if r["delivered_at"]]
    return {
        "queued": queued, "delivered": delivered,
        "queued_count": len(queued), "delivered_count": len(delivered),
        "urgent_queued": sum(1 for r in queued if r["priority"] == "urgent"),
    }


# --- agent wake hooks (self-service wake API) --------------------------------

WAKE_HOOK_KINDS = {"at", "interval", "probe", "cron"}

#: Shape of a probe path: 'dotted.module:function'. The allowlist decides which
#: dotted prefixes are importable — this only validates the syntax.
_PROBE_SHAPE_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$")


def _cron_field(field: str, lo: int, hi: int) -> set:
    """Expand one 5-field-cron field: supports *, a, a-b, */n, a-b/n, and commas."""
    out = set()
    for part in field.split(","):
        rng, step = part, 1
        if "/" in part:
            rng, s = part.split("/", 1)
            step = int(s)
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            aa, bb = rng.split("-", 1)
            a, b = int(aa), int(bb)
        else:
            a = b = int(rng)
        v = a
        while v <= b:
            if lo <= v <= hi:
                out.add(v)
            v += step
    return out


def _next_cron_fire(expr: str, tzname: str, after: dt.datetime) -> str | None:
    """Next UTC firing time at/after `after` for a 5-field cron in `tzname`
    (min hour dom month dow; dow 0=Sun). AND semantics for dom/dow. Scans
    minute-by-minute up to ~8 days — trivial cost, DST-correct via zoneinfo."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron must have 5 fields (got {expr!r})")
    mins = _cron_field(parts[0], 0, 59)
    hrs = _cron_field(parts[1], 0, 23)
    doms = _cron_field(parts[2], 1, 31)
    months = _cron_field(parts[3], 1, 12)
    dows = _cron_field(parts[4], 0, 6)
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tzname)
    except Exception:
        tz = CONFIG.tzinfo()
    t = after.astimezone(tz).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    for _ in range(8 * 24 * 60):
        if (t.minute in mins and t.hour in hrs and t.day in doms
                and t.month in months and ((t.weekday() + 1) % 7) in dows):
            return _iso(t.astimezone(dt.timezone.utc))
        t += dt.timedelta(minutes=1)
    return None


def _probe_path_ok(path: str) -> bool:
    """True if `path` is syntactically a probe AND inside CONFIG.probe_allowlist.

    An EMPTY allowlist denies everything: the engine only ever imports code the
    host has explicitly opted in. The prefix match is dotted-boundary-aware, so
    an allowlist of 'myapp.watch' never admits 'myapp.watchdog_evil'.
    """
    if not _PROBE_SHAPE_RE.match(path or ""):
        return False
    module = path.partition(":")[0]
    for prefix in CONFIG.probe_allowlist:
        if module == prefix or module.startswith(prefix + "."):
            return True
    return False


def _resolve_probe(path: str):
    """Import a 'module:function' probe, restricted to CONFIG.probe_allowlist.

    A probe may only OBSERVE and NOTIFY: its return value is turned into inbox
    items and nothing else. It is called with no arguments and no engine handle.
    """
    if not _probe_path_ok(path):
        if not CONFIG.probe_allowlist:
            raise ValueError(
                f"probe {path!r} rejected: probes are disabled "
                f"(CONFIG.probe_allowlist is empty)")
        allowed = ", ".join(CONFIG.probe_allowlist)
        raise ValueError(
            f"probe {path!r} is not allowed: expected '<module>:<function>' "
            f"under one of [{allowed}]")
    import importlib
    mod_name, _, func_name = path.partition(":")
    fn = getattr(importlib.import_module(mod_name), func_name, None)
    if not callable(fn):
        raise ValueError(f"probe {path!r} does not resolve to a callable")
    return fn


def hook_add(owner_role: str, title: str, *, at: str | None = None,
             every: int | None = None, callable_path: str | None = None,
             cron: str | None = None, tz: str | None = None,
             until: str | None = None, body: str | None = None,
             priority: str = "normal",
             db_path: Path | str | None = None) -> dict:
    """Register a wake hook. Exactly one shape:

    - at=ISO ts                      -> one-shot timer
    - every=N [until=ISO]            -> recurring timer
    - cron="m h dom mon dow" [tz]    -> calendar schedule (tz defaults to CONFIG.timezone)
    - callable_path=mod:fn [every N] -> custom watcher probe (fires when the
      function returns a truthy dict / list of dicts)

    Validation is fail-fast at registration: a probe outside the allowlist, a
    missing function, or a cron that never matches is rejected here rather than
    silently failing on some future tick.
    """
    title = _clean(title, "title")
    if priority not in TASK_PRIORITIES:
        raise ValueError(f"invalid priority: {priority}")
    now = _now()
    spec: dict = {}
    if callable_path:
        _resolve_probe(callable_path)  # fail fast on disallowed / missing probe
        kind = "probe"
        every = int(every or CONFIG.default_probe_every)
        if every < CONFIG.min_hook_every:
            raise ValueError(f"every must be >= {CONFIG.min_hook_every}s")
        spec = {"callable": callable_path, "every": every}
        next_fire = _iso(now)                       # evaluate on the next tick
    elif cron:
        kind = "cron"
        tzname = tz or CONFIG.timezone
        next_fire = _next_cron_fire(cron, tzname, now)  # validates + schedules
        if next_fire is None:
            raise ValueError(f"cron never matches within 8 days: {cron!r}")
        spec = {"cron": cron, "tz": tzname}
    elif at:
        kind = "at"
        next_fire = _normalize_due(at)
        spec = {"at": next_fire}
    elif every:
        kind = "interval"
        every = int(every)
        if every < CONFIG.min_hook_every:
            raise ValueError(f"every must be >= {CONFIG.min_hook_every}s")
        spec = {"every": every}
        next_fire = _iso(now + dt.timedelta(seconds=every))
    else:
        raise ValueError("hook needs one of: at / every / cron / callable_path")
    if until:
        spec["until"] = _normalize_due(until)
    now_iso = _iso(now)
    with connect(db_path, write=True) as conn:
        owner_role = _agent_role(conn, owner_role)
        cur = conn.execute(
            """INSERT INTO wake_hooks (owner_role, kind, title, body, priority,
                                       spec, status, next_fire_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?, 'active', ?,?,?)""",
            (owner_role, kind, title, _clean(body, "body", required=False),
             priority, json.dumps(spec), next_fire, now_iso, now_iso))
        _log_event(conn, owner_role, "hook_add", str(cur.lastrowid),
                   {"kind": kind, "title": title, "spec": spec})
        return {"hook": cur.lastrowid, "kind": kind, "next_fire_at": next_fire}


def hooks(owner_role: str | None = None, *, include_closed: bool = False,
          db_path: Path | str | None = None) -> list[dict]:
    clauses, params = [], []
    if owner_role:
        clauses.append("owner_role=?")
        params.append(owner_role)
    if not include_closed:
        clauses.append("status='active'")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect(db_path) as conn:
        out = []
        for r in conn.execute(f"SELECT * FROM wake_hooks {where} ORDER BY id", params):
            d = dict(r)
            d["spec"] = _load_json(d["spec"])
            out.append(d)
        return out


def hook_cancel(hook_id: int, *, actor: str | None = None,
                db_path: Path | str | None = None) -> bool:
    with connect(db_path, write=True) as conn:
        cur = conn.execute(
            "UPDATE wake_hooks SET status='cancelled', updated_at=? "
            "WHERE id=? AND status='active'", (_iso(), int(hook_id)))
        if cur.rowcount:
            _log_event(conn, actor or "agent", "hook_cancel", str(hook_id), None)
        return bool(cur.rowcount)


def _eval_wake_hooks(conn, now: dt.datetime) -> list[dict]:
    """Evaluate due hooks inside the caller's write txn; fire -> inbox items.

    A probe that raises CONFIG.max_error_streak times in a row is auto-disabled
    and its owner is notified through the inbox (so a broken watcher can't fail
    silently forever). A probe exception NEVER breaks the tick. Timer hooks
    cannot raise.
    """
    now_iso = _iso(now)
    fired = []
    rows = conn.execute(
        "SELECT * FROM wake_hooks WHERE status='active' "
        "AND next_fire_at IS NOT NULL AND next_fire_at<=?", (now_iso,)).fetchall()
    for h in rows:
        spec = _load_json(h["spec"]) or {}
        until = spec.get("until")
        if until and now_iso > until:
            conn.execute("UPDATE wake_hooks SET status='done', updated_at=? WHERE id=?",
                         (now_iso, h["id"]))
            continue
        items, err = [], None
        if h["kind"] == "probe":
            try:
                res = _resolve_probe(spec["callable"])()
                if res:
                    items = res if isinstance(res, list) else [res]
                    items = [i for i in items if isinstance(i, dict)] or [{}]
            except Exception as exc:  # never let a probe break the tick
                err = f"{type(exc).__name__}: {exc}"[:300]
        else:
            items = [{}]  # timers fire with the hook's own title/body

        if err:
            streak = (h["error_streak"] or 0) + 1
            if streak >= CONFIG.max_error_streak:
                conn.execute(
                    "UPDATE wake_hooks SET status='error', error_streak=?, "
                    "last_error=?, updated_at=? WHERE id=?",
                    (streak, err, now_iso, h["id"]))
                _inbox_insert(conn, h["owner_role"], "system",
                              f"Wake hook #{h['id']} ({h['title']}) disabled "
                              f"after repeated errors",
                              body=err, ref=f"hook:{h['id']}", priority="normal",
                              dedup_key=f"hook-error:{h['id']}")
            else:
                nxt = _iso(now + dt.timedelta(
                    seconds=int(spec.get("every", CONFIG.default_probe_every))))
                conn.execute(
                    "UPDATE wake_hooks SET error_streak=?, last_error=?, "
                    "next_fire_at=?, updated_at=? WHERE id=?",
                    (streak, err, nxt, now_iso, h["id"]))
            continue

        n_enqueued = 0
        for item in items:
            prio = item.get("priority") if item.get("priority") in TASK_PRIORITIES \
                else h["priority"]
            try:
                iid = _inbox_insert(
                    conn, h["owner_role"], "system",
                    item.get("title") or h["title"], body=item.get("body") or h["body"],
                    ref=item.get("ref") or f"hook:{h['id']}", priority=prio,
                    dedup_key=item.get("dedup_key")
                    or f"hook:{h['id']}:{(item.get('title') or h['title'])[:80]}")
            except Exception:
                iid = None
            if iid:
                n_enqueued += 1
        # bookkeeping + reschedule
        if h["kind"] == "at":
            conn.execute(
                "UPDATE wake_hooks SET status='done', last_fired_at=?, "
                "fire_count=fire_count+1, error_streak=0, next_fire_at=NULL, "
                "updated_at=? WHERE id=?",
                (now_iso, now_iso, h["id"]))
        else:
            nxt = (_next_cron_fire(spec["cron"], spec.get("tz") or CONFIG.timezone, now)
                   if h["kind"] == "cron"
                   else _iso(now + dt.timedelta(
                       seconds=int(spec.get("every", CONFIG.default_probe_every)))))
            done = bool(nxt is None or (until and nxt > until))
            conn.execute(
                "UPDATE wake_hooks SET status=?, last_fired_at=?, "
                "fire_count=fire_count + ?, error_streak=0, next_fire_at=?, "
                "updated_at=? WHERE id=?",
                ("done" if done else "active",
                 now_iso if n_enqueued else h["last_fired_at"],
                 1 if n_enqueued else 0,
                 None if done else nxt, now_iso, h["id"]))
        if n_enqueued:
            _log_event(conn, "orchestrator", "hook_fire", str(h["id"]),
                       {"role": h["owner_role"], "title": h["title"], "items": n_enqueued})
            fired.append({"hook": h["id"], "role": h["owner_role"],
                          "title": h["title"], "items": n_enqueued})
    return fired


# --- board aggregate --------------------------------------------------------

def _meeting_load(conn) -> dict:
    """Per-role meeting obligations, from the registry's roles."""
    roles = tuple(sorted(_known_roles(conn)))
    wakes = {r: c for r, c in conn.execute(
        """SELECT role, COUNT(*) FROM meeting_wake_requests
           WHERE status='pending' GROUP BY role""").fetchall()}
    unread = {r: c for r, c in conn.execute(
        """SELECT a.role, COUNT(*) FROM meeting_attendees a
           JOIN meetings m ON m.thread_id=a.thread_id
           JOIN mailbox_messages mm ON mm.thread_id=a.thread_id
                AND mm.recipient IN (a.role, ?) AND mm.sender!=a.role
           LEFT JOIN mailbox_receipts r ON r.message_id=mm.id AND r.role=a.role
           WHERE m.state IN ('active','consensus')
             AND a.checked_in_at IS NOT NULL AND a.stopped_at IS NULL
             AND r.message_id IS NULL
           GROUP BY a.role""", (_RECIPIENT_ALL,)).fetchall()}
    oblig = {}
    for owed, cnt, due in conn.execute(
            """SELECT owed_by, COUNT(*), MIN(due_at) FROM meeting_response_obligations
               WHERE status='pending' GROUP BY owed_by""").fetchall():
        oblig[owed] = {"pending": cnt, "next_due_at": due}
    active_meetings = {}
    for role, thread_id, agenda in conn.execute(
            """SELECT a.role, a.thread_id, m.agenda FROM meeting_attendees a
               JOIN meetings m ON m.thread_id=a.thread_id
               WHERE a.checked_in_at IS NOT NULL AND a.stopped_at IS NULL
                 AND m.state IN ('waiting','active','consensus','termination_pending')""").fetchall():
        active_meetings.setdefault(role, []).append(
            {"thread_id": thread_id, "agenda": agenda})
    out = {}
    for r in roles:
        out[r] = {
            "pending_wakes": wakes.get(r, 0),
            "unread_messages": unread.get(r, 0),
            "response_obligations": oblig.get(r, {"pending": 0, "next_due_at": None}),
            "active_meetings": active_meetings.get(r, []),
        }
    return out


def _delivery_health(conn, now_iso: str) -> dict:
    """Delivery health from the projected ledger. `stuck_deliveries` is the
    invariant-breach surface: past SLA, unread, and NOT yet escalated."""
    wake = _wake_keys(conn)
    rows = conn.execute("SELECT * FROM message_delivery").fetchall()
    oldest_unread = None
    unread = stuck = escalated = 0
    for r in rows:
        st = _delivery_state(r, now_iso, wake)
        if st == "read":
            continue
        unread += 1
        if oldest_unread is None or r["queued_at"] < oldest_unread:
            oldest_unread = r["queued_at"]
        if st == "overdue":
            stuck += 1
        elif st == "escalated":
            escalated += 1
    unsent = conn.execute(
        "SELECT COUNT(*) FROM meeting_escalations WHERE status!='sent'").fetchone()[0]
    age = None
    if oldest_unread:
        age = int((_now() - dt.datetime.fromisoformat(oldest_unread)).total_seconds())
    return {"oldest_unread_at": oldest_unread, "oldest_unread_age_seconds": age,
            "unread_deliveries": unread, "stuck_deliveries": stuck,
            "escalated_deliveries": escalated, "unsent_escalations": unsent}


def board(db_path: Path | str | None = None) -> dict:
    """Everything the agent board needs, in one aggregate call."""
    now_iso = _iso()
    pres = presence(db_path)
    human_lvl = _human_level(_ladder())
    with connect(db_path, write=True) as conn:
        sync_delivery(conn)
        meeting_load = _meeting_load(conn)
        health = _delivery_health(conn, now_iso)
        all_tasks = [_task_view(dict(r), now_iso) for r in conn.execute(
            "SELECT * FROM agent_tasks").fetchall()]
        todos = {r["role"]: {"snapshot": _load_json(r["snapshot"]),
                             "updated_at": r["updated_at"]}
                 for r in conn.execute("SELECT * FROM session_todos").fetchall()}
        # Wake orchestrator state (READ only — board never records attempts;
        # that is the driver/plan_wakes job).
        wake_pending = {r["role"]: {"pending": r["c"], "max_level": r["ml"]}
                        for r in conn.execute(
                            """SELECT role, COUNT(*) c, MAX(level) ml FROM wake_attempts
                               WHERE outcome='pending' GROUP BY role""").fetchall()}
        # Full aggregate, NOT derived from the limited display window below.
        human_level_wakes = conn.execute(
            "SELECT COUNT(*) FROM wake_attempts WHERE outcome='pending' AND level>=?",
            (human_lvl,)).fetchone()[0]
        wake_activity = [dict(x) for x in conn.execute(
            "SELECT * FROM wake_attempts ORDER BY id DESC LIMIT 12").fetchall()]
        inbox_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM agent_inbox WHERE acked_at IS NULL").fetchall()]
        hook_rows = [dict(r) for r in conn.execute(
            "SELECT id, owner_role, kind, title, priority, next_fire_at, "
            "fire_count, last_error FROM wake_hooks WHERE status='active' ORDER BY id"
        ).fetchall()]
    all_tasks.sort(key=lambda t: _task_sort_key(t, now_iso))
    open_tasks = [t for t in all_tasks if t["status"] in TASK_OPEN_STATUSES]

    agents = []
    for p in pres:
        role = p["role"]
        role_tasks = [t for t in open_tasks if t["assignee_role"] == role]
        role_inbox = sorted([r for r in inbox_rows if r["target_role"] == role],
                            key=_inbox_sort_key)
        agents.append({
            **p,
            "meeting": meeting_load.get(role, {}),
            "tasks": role_tasks,
            "task_counts": _count_by_status(all_tasks, role),
            "overdue_count": sum(1 for t in role_tasks if t["overdue"]),
            "session_todos": todos.get(role),
            "wake": wake_pending.get(role, {"pending": 0, "max_level": None}),
            "inbox": _inbox_view(role_inbox),
            "hooks": [h for h in hook_rows if h["owner_role"] == role],
        })
    return {
        "generated_at": now_iso,
        "agents": agents,
        "health": {
            **health,
            "total_overdue": sum(1 for t in open_tasks if t["overdue"]),
            "total_open_tasks": len(open_tasks),
            "pending_wakes": sum(v["pending"] for v in wake_pending.values()),
            "wakes_at_human_level": human_level_wakes,
            "inbox_queued": sum(1 for r in inbox_rows if not r["delivered_at"]),
            "inbox_total": len(inbox_rows),
        },
        "wake_activity": wake_activity,
        "recent_events": recent_events(20, db_path),
    }


def _count_by_status(all_tasks: list[dict], role: str) -> dict:
    counts = {s: 0 for s in TASK_STATUSES}
    for t in all_tasks:
        if t["assignee_role"] == role:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
    return counts


def _load_json(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


# --- per-agent detail (RESTful) ---------------------------------------------

def agent_detail(role: str, db_path: Path | str | None = None) -> dict:
    """Everything about ONE agent for its detail page: profile, live session +
    work, meetings, the full inbox, all tasks, and the execution history
    (wake attempts, delivery ledger, orchestration events, hooks)."""
    now = _now()
    now_iso = _iso(now)
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        sync_delivery(conn)
        reg = conn.execute("SELECT * FROM agent_registry WHERE role=?", (role,)).fetchone()
        sess = conn.execute("SELECT * FROM agent_sessions WHERE role=?", (role,)).fetchone()
        base = {"role": role, "session_id": None, "harness": None, "state": None,
                "activity": None, "started_at": None, "last_heartbeat_at": None,
                "ended_at": None}
        pres = _presence_row(dict(sess) if sess else base, now)
        live = pres["liveness"] in ("online", "suspect")

        td = conn.execute(
            "SELECT snapshot, updated_at FROM session_todos WHERE role=?", (role,)).fetchone()
        session_todos = ({"snapshot": _load_json(td["snapshot"]),
                          "updated_at": td["updated_at"]} if td and live else None)

        tasks_ = [_task_view(dict(r), now_iso) for r in conn.execute(
            "SELECT * FROM agent_tasks WHERE assignee_role=? ORDER BY id DESC",
            (role,)).fetchall()]
        wake_history = [dict(r) for r in conn.execute(
            "SELECT * FROM wake_attempts WHERE role=? ORDER BY id DESC LIMIT 80",
            (role,)).fetchall()]
        inbox = [dict(r) for r in conn.execute(
            "SELECT * FROM agent_inbox WHERE target_role=? ORDER BY id DESC LIMIT 100",
            (role,)).fetchall()]
        hooks_ = []
        for r in conn.execute("SELECT * FROM wake_hooks WHERE owner_role=? ORDER BY id DESC",
                              (role,)):
            d = dict(r)
            d["spec"] = _load_json(d["spec"])
            hooks_.append(d)
        meeting = _meeting_load(conn).get(role, {})
        wake_keys = _wake_keys(conn)
        delivery = []
        for r in conn.execute(
                "SELECT * FROM message_delivery WHERE recipient_role=? "
                "ORDER BY message_id DESC LIMIT 80",
                (role,)):
            d = dict(r)
            d["state"] = _delivery_state(d, now_iso, wake_keys)
            delivery.append(d)
        events = []
        for r in conn.execute(
                "SELECT * FROM orchestration_events WHERE actor=? OR payload LIKE ? "
                "ORDER BY id DESC LIMIT 100", (role, '%"' + role + '"%')):
            d = dict(r)
            if d.get("payload"):
                d["payload"] = _load_json(d["payload"])
            events.append(d)
    return {
        "role": role, "generated_at": now_iso,
        "profile": {
            "display_name": reg["display_name"] if reg else role,
            "capabilities": _load_json(reg["capabilities"]) if reg else [],
            "authority": _load_json(reg["authority"]) if reg else {},
        },
        "presence": pres,
        "session_todos": session_todos,
        "meeting": meeting,
        "inbox": inbox,
        "tasks": tasks_,
        "task_counts": _count_by_status(tasks_, role),
        "wake_history": wake_history,
        "hooks": hooks_,
        "delivery": delivery,
        "events": events,
    }
