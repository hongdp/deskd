"""Shared store: schema, migrations, connections, the role registry,
the event log, and the small helpers every sibling leans on. The only
module in this package that talks to the layers below (mailbox /
meetings own the tables this schema joins against).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from contextlib import contextmanager
from pathlib import Path

from .. import mailbox, meetings
from ..config import CONFIG

SESSION_STATES = {
    "booting", "working", "idle_standby", "in_meeting", "stopping", "dead",
}
TASK_STATUSES = {"pending", "in_progress", "blocked", "done", "cancelled"}
TASK_OPEN_STATUSES = {"pending", "in_progress", "blocked"}
TASK_PRIORITIES = {"urgent", "normal", "low"}

#: Liveness values that mean a session is executing RIGHT NOW, so its work
#: breakdown may be shown as current (design.md: the board shows "now executing"
#: only for a live session). Every surface that renders session_todos shares this
#: one predicate: a crashed session never calls end_session to clear the mirror,
#: so the gate — not the cleanup — is what upholds the invariant.
LIVE_LIVENESS = ("online", "suspect")

#: States in which a session has DECLARED it is not working. Going quiet from one
#: of these is expected, so it reads `idle` rather than `dead` — the session is
#: between wakes, which is this engine's normal resting condition, and its id is
#: still resumable. Going quiet from any other state means it said it was working
#: and then stopped proving it: that is `dead`, and it should look alarming.
RESTING_STATES = ("idle_standby", "stopping")

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

-- agent_sessions.session_day: the local-tz day the session belongs to.
-- agent_sessions.phase: active | draining | closed.
CREATE TABLE IF NOT EXISTS agent_sessions (
    role                  TEXT PRIMARY KEY,
    session_id            TEXT,
    harness               TEXT,
    state                 TEXT NOT NULL,
    activity              TEXT,
    started_at            TEXT NOT NULL,
    last_heartbeat_at     TEXT NOT NULL,
    ended_at              TEXT,
    session_day           TEXT,
    phase                 TEXT
);

-- No CHECK on source_kind: a host defines its own task provenance kinds
-- (CONFIG.task_sources) and its own supervisor role name, so the enumeration is
-- validated in Python instead (see _task_sources).
-- agent_tasks.blocked_on: what a 'blocked' task is waiting ON. No column
-- recorded this, so `blocked` was a graveyard: anything could be marked blocked
-- and die there, because nothing could say what it waited for, whether that had
-- happened, or who to ask. Enforced in Python (task_update), not by a CHECK,
-- because legacy rows predate the column and an honest backfill cannot invent
-- their dependency. (Comments live OUTSIDE the CREATE body: older SQLite
-- rewrites DDL textually on ALTER, and inline comments corrupt the rewrite.)
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
    result_note           TEXT,
    blocked_on            TEXT
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
-- wake_attempts.reason_kind: meeting_wake / stuck_delivery / urgent_task /
-- inbox / owed_reply. wake_attempts.channel: a CONFIG.wake_ladder rung channel.
CREATE TABLE IF NOT EXISTS wake_attempts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    role                  TEXT NOT NULL,
    reason_kind           TEXT NOT NULL,
    source_ref            TEXT NOT NULL,
    channel               TEXT NOT NULL,
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
-- wake_hooks.kind: at | interval | probe | cron (validated in Python).
-- wake_hooks.spec: JSON — {at} | {every,until?} | {cron,tz} | {callable,every,until?}.
CREATE TABLE IF NOT EXISTS wake_hooks (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_role            TEXT NOT NULL,
    kind                  TEXT NOT NULL,
    title                 TEXT NOT NULL,
    body                  TEXT,
    priority              TEXT NOT NULL DEFAULT 'normal'
                          CHECK (priority IN ('urgent','normal','low')),
    spec                  TEXT NOT NULL,
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

-- The wake ladder's HUMAN-RUNG ledger: one row per arrival of a demand at a
-- rung that leaves the machine. This is the durable half of the terminal
-- sink — the row exists whether or not any channel is registered, the console
-- renders it, and the channel layer (deskd.channels) only MIRRORS it out.
-- Previously only meeting_wake demands escalating past the machine reached a
-- person (via the driver's meeting-specific branch); every other reason kind
-- arrived at the human rung and reached nobody. wake_escalations.status:
-- queued (outbox only) | sent | failed.
CREATE TABLE IF NOT EXISTS wake_escalations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    role                  TEXT NOT NULL,
    reason_kind           TEXT NOT NULL,
    source_ref            TEXT NOT NULL,
    level                 INTEGER NOT NULL,
    channel               TEXT NOT NULL DEFAULT 'auto',
    reason                TEXT,
    status                TEXT NOT NULL DEFAULT 'queued'
                          CHECK (status IN ('queued','sent','failed')),
    details               TEXT,
    created_at            TEXT NOT NULL,
    sent_at               TEXT
);

-- Capability-addressed demands that NO enabled role can take. The wake ladder's
-- guarantee applied to the authority axis: a demand nobody is allowed to handle
-- must not vanish — it is recorded here, counted red on the board
-- (health.unroutable_demands), and re-routed by plan_wakes the moment a
-- qualifying role exists. Columns mirror agent_inbox because routing moves the
-- row there verbatim.
CREATE TABLE IF NOT EXISTS unroutable_demands (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    require_capability    TEXT NOT NULL,
    source_kind           TEXT NOT NULL,
    ref                   TEXT,
    priority              TEXT NOT NULL DEFAULT 'normal'
                          CHECK (priority IN ('urgent','normal','low')),
    title                 TEXT NOT NULL,
    body                  TEXT,
    dedup_key             TEXT,
    enqueued_at           TEXT NOT NULL,
    expires_at            TEXT,
    routed_at             TEXT,
    routed_to             TEXT
);

-- Same dedup contract as the inbox, on the capability instead of the role: a
-- re-firing unroutable alert must not pile up while nobody can take it.
CREATE UNIQUE INDEX IF NOT EXISTS idx_unroutable_dedup
ON unroutable_demands(require_capability, dedup_key)
WHERE dedup_key IS NOT NULL AND routed_at IS NULL;
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
    result_note TEXT,
    blocked_on TEXT
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
    # NOTE: only agent_* tables belong here. A migration for a lower layer's
    # table must live in that layer — `meetings.closed_at` was briefly added
    # here, which made it unreachable for a host that uses meetings without
    # orchestration (a supported configuration: meetings._known_roles falls back
    # to CONFIG.role_names() precisely so that works). Its list_meetings then
    # raised `no such column: closed_at` on any pre-existing DB, and nothing
    # noticed, because the meetings-only shape is the one nothing exercises.
    # agent_tasks IS this layer's table (ORCH_SCHEMA above creates it, nothing
    # below can read it), so blocked_on belongs here and a meetings-only host
    # never executes this line.
    #
    # The backfill is deliberately NOTHING. A pre-existing 'blocked' row records
    # no dependency and this migration has no evidence of one, so every available
    # value is a lie: a placeholder ('unknown', '') would make an illegal state
    # look legal to every later reader, and flipping the row to 'pending' would
    # overwrite an agent's own judgement with a guess. NULL is the only true
    # value — it says exactly what happened, which is that nobody recorded it.
    # It does not rot: _blocked_unspecified() reports these rows so a human
    # decides, which is what rule "genuinely cannot move -> escalate" asks for,
    # and the first task_update to touch one has to name the dependency or leave.
    #
    # MUST precede the _rebuild calls below: they copy with `SELECT *` into the
    # new DDL, so the live table has to have this column already or the copy
    # fails on a column-count mismatch.
    if "blocked_on" not in {r["name"] for r in
                            conn.execute("PRAGMA table_info(agent_tasks)")}:
        conn.execute("ALTER TABLE agent_tasks ADD COLUMN blocked_on TEXT")
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
    """Valid ``agent_tasks.source_kind`` values.

    Read from CONFIG at call time, never frozen at import: the host owns this
    enumeration (that is why the DDL carries no CHECK on the column). The
    supervisor role is always accepted — it is configurable, so it cannot be one
    of the literals.
    """
    return set(CONFIG.task_sources) | {CONFIG.supervisor_role}

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

def _load_json(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value
