"""Control-plane receipts and the durable committed-change event log."""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

from ..config import CONFIG
from .. import orchestration, workspaces

EVENT_RETENTION = 10_000

CONTROL_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS control_meta (
    key                   TEXT PRIMARY KEY,
    value                 TEXT NOT NULL
);

INSERT OR IGNORE INTO control_meta(key,value)
VALUES ('server_id', lower(hex(randomblob(8))));

CREATE TABLE IF NOT EXISTS control_commands (
    principal_id          TEXT NOT NULL,
    request_id            TEXT NOT NULL,
    verb                  TEXT NOT NULL,
    request_fingerprint   TEXT NOT NULL,
    status                TEXT NOT NULL
                          CHECK (status IN ('accepted','running','completed',
                                            'failed','indeterminate')),
    response_json         TEXT,
    error                 TEXT,
    execution_token       TEXT,
    lease_expires_at      TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    PRIMARY KEY (principal_id, request_id)
);

CREATE TABLE IF NOT EXISTS control_wake_claims (
    claim_id              TEXT PRIMARY KEY,
    role                  TEXT NOT NULL,
    state                 TEXT NOT NULL
                          CHECK (state IN ('claimed','landed','failed',
                                           'indeterminate','reconciled')),
    channel               TEXT NOT NULL,
    mode                  TEXT NOT NULL CHECK (mode IN ('spawn','resume')),
    attempt_ids_json      TEXT NOT NULL,
    inbox_ids_json        TEXT NOT NULL,
    reasons_json          TEXT NOT NULL,
    prompt                TEXT NOT NULL,
    claimed_at            TEXT NOT NULL,
    landed_at             TEXT,
    session_id            TEXT,
    error                 TEXT,
    resolution            TEXT,
    reconciled_at         TEXT,
    reconciled_by         TEXT,
    reconciliation_note   TEXT,
    reason_kind           TEXT NOT NULL DEFAULT 'wake',
    resume_session_id     TEXT,
    rollover_request_id   TEXT,
    attempt_number        INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_control_one_live_wake_claim
ON control_wake_claims(role) WHERE state='claimed';

CREATE TABLE IF NOT EXISTS control_wake_claim_attempts (
    attempt_id            INTEGER PRIMARY KEY,
    claim_id              TEXT NOT NULL REFERENCES control_wake_claims(claim_id)
                          ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS control_rollover_requests (
    request_id            TEXT PRIMARY KEY,
    role                  TEXT NOT NULL,
    resume_session_id     TEXT NOT NULL,
    from_day              TEXT NOT NULL,
    to_day                TEXT NOT NULL,
    prompt                TEXT NOT NULL,
    state                 TEXT NOT NULL
                          CHECK (state IN ('pending','claimed','completed',
                                           'escalated','indeterminate','cancelled')),
    attempt_count         INTEGER NOT NULL DEFAULT 0,
    max_attempts          INTEGER NOT NULL,
    claim_id              TEXT,
    last_error            TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    completed_at          TEXT,
    UNIQUE (role,resume_session_id,from_day,to_day)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_control_one_live_rollover
ON control_rollover_requests(role) WHERE state IN ('pending','claimed','indeterminate');

CREATE TABLE IF NOT EXISTS control_mount_tickets (
    ticket_id             TEXT PRIMARY KEY,
    lease_id              TEXT NOT NULL REFERENCES workspace_leases(lease_id),
    launcher_subject      TEXT NOT NULL,
    owner_role            TEXT NOT NULL,
    workspace_version     INTEGER NOT NULL,
    host_path             TEXT NOT NULL,
    container_path        TEXT NOT NULL,
    expected_device       INTEGER NOT NULL,
    expected_inode        INTEGER NOT NULL,
    image_digest          TEXT,
    build_revision        TEXT,
    config_version        TEXT,
    prompt_version        TEXT,
    state                 TEXT NOT NULL
                          CHECK (state IN ('issued','started','landed',
                                           'indeterminate','expired')),
    expires_at            TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    started_at            TEXT,
    landed_at             TEXT,
    error                 TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_control_one_live_mount_ticket
ON control_mount_tickets(lease_id)
WHERE state IN ('issued','started','indeterminate');

CREATE TABLE IF NOT EXISTS control_session_provenance (
    role                  TEXT PRIMARY KEY,
    session_id            TEXT NOT NULL,
    provisional_session_id TEXT NOT NULL,
    mode                  TEXT NOT NULL CHECK (mode IN ('spawn','resume')),
    provider              TEXT NOT NULL,
    model                 TEXT,
    reasoning             TEXT,
    image_digest          TEXT,
    build_revision        TEXT,
    config_version        TEXT,
    prompt_version        TEXT,
    workspace_lease_id    TEXT,
    started_at            TEXT NOT NULL,
    bound_at              TEXT
);

CREATE TABLE IF NOT EXISTS control_review_artifacts (
    thread_id             TEXT NOT NULL,
    role                  TEXT NOT NULL,
    stage                 TEXT NOT NULL,
    name                  TEXT NOT NULL,
    sha256                TEXT NOT NULL,
    size_bytes            INTEGER NOT NULL,
    stored_path           TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    PRIMARY KEY (thread_id, role, stage)
);

CREATE TABLE IF NOT EXISTS control_events (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    resource              TEXT NOT NULL,
    role                  TEXT,
    resource_id           TEXT,
    operation             TEXT NOT NULL,
    at                    TEXT NOT NULL,
    payload               TEXT
);

CREATE TRIGGER IF NOT EXISTS trg_control_events_prune
AFTER INSERT ON control_events
BEGIN
    DELETE FROM control_events WHERE id <= NEW.id - {EVENT_RETENTION};
END;
"""

# Table -> (resource, role column or None, resource-id column).  Triggers make
# events part of the exact transaction that changed the state, including a
# legacy local CLI write after the control schema has been installed.
_EVENT_TABLES = {
    "agent_registry": ("registry", "role", "role"),
    "agent_sessions": ("presence", "role", "role"),
    "session_feed": ("feed", "role", "session_id"),
    "session_todos": ("todos", "role", "role"),
    "agent_tasks": ("task", "assignee_role", "id"),
    "agent_inbox": ("inbox", "target_role", "id"),
    "wake_hooks": ("hook", "owner_role", "id"),
    "wake_attempts": ("wake", "role", "id"),
    "wake_escalations": ("wake_escalation", "role", "id"),
    "unroutable_demands": ("unroutable", None, "id"),
    "mailbox_threads": ("thread", None, "id"),
    # A direct message is private to its recipient.  The sender receives the
    # command-completed event for its own write; using sender here would make a
    # recipient wait forever while revealing another role's traffic pattern.
    "mailbox_messages": ("message", "recipient", "id"),
    "mailbox_receipts": ("receipt", "role", "message_id"),
    "mailbox_notifications": ("notification", "role", "message_id"),
    "message_delivery": ("delivery", "recipient_role", "message_id"),
    "meetings": ("meeting", "called_by", "thread_id"),
    "meeting_attendees": ("attendance", "role", "thread_id"),
    "meeting_wake_requests": ("meeting_wake", "role", "thread_id"),
    "meeting_events": ("meeting_event", None, "thread_id"),
    "meeting_response_obligations": ("obligation", "owed_by", "message_id"),
    "meeting_escalations": ("meeting_escalation", "requested_by", "id"),
    "meeting_terminations": ("termination", "proposer", "id"),
    "meeting_termination_votes": ("termination_vote", "role", "proposal_id"),
    "control_review_artifacts": ("review_artifact", "role", "thread_id"),
    "control_rollover_requests": ("rollover", "role", "request_id"),
    "workspace_leases": ("workspace", "owner_role", "lease_id"),
}

_CURSOR_RE = re.compile(r"^evt_([0-9a-f]{16})_([0-9a-f]{16})$")


class CursorExpired(ValueError):
    def __init__(self, oldest: str, current: str):
        super().__init__("event cursor expired; reload /api/snapshot")
        self.oldest = oldest
        self.current = current


class CursorAhead(ValueError):
    def __init__(self, current: str):
        super().__init__("event cursor is ahead of this server; reload /api/snapshot")
        self.current = current


class CursorWrongServer(ValueError):
    def __init__(self, current: str):
        super().__init__("event cursor belongs to another server; reload /api/snapshot")
        self.current = current


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _server_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM control_meta WHERE key='server_id'").fetchone()
    if row is None or not re.fullmatch(r"[0-9a-f]{16}", row["value"]):
        raise RuntimeError("control server id is unavailable")
    return row["value"]


def cursor_for(event_id: int, server_id: str) -> str:
    return f"evt_{server_id}_{max(0, int(event_id)):016x}"


def cursor_id(cursor: str | None, server_id: str) -> int:
    if not cursor or cursor == "0":
        return 0
    match = _CURSOR_RE.fullmatch(cursor.strip().lower())
    if not match:
        raise ValueError("invalid event cursor")
    if match.group(1) != server_id:
        raise CursorWrongServer(cursor_for(0, server_id))
    return int(match.group(2), 16)


def _sql_ref(prefix: str, column: str | None) -> str:
    return "NULL" if column is None else f"CAST({prefix}.{column} AS TEXT)"


def _ensure_triggers(conn: sqlite3.Connection) -> None:
    existing = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    timestamp = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
    for table, (resource, role_col, id_col) in _EVENT_TABLES.items():
        if table not in existing:
            continue
        for operation, prefix in (("insert", "NEW"), ("update", "NEW"),
                                  ("delete", "OLD")):
            name = f"trg_control_{table}_{operation}"
            resource_sql = "'" + resource.replace("'", "''") + "'"
            operation_sql = "'" + operation.replace("'", "''") + "'"
            conn.execute(f"""
                CREATE TRIGGER IF NOT EXISTS {name}
                AFTER {operation.upper()} ON {table}
                BEGIN
                  INSERT INTO control_events
                    (resource,role,resource_id,operation,at,payload)
                  VALUES ({resource_sql},{_sql_ref(prefix, role_col)},
                          {_sql_ref(prefix, id_col)},{operation_sql},{timestamp},NULL);
                END
            """)


def ensure_schema(db_path: Path | str | None = None) -> None:
    # Workspace tables must exist before their triggers are installed.
    workspaces.ensure_schema(db_path)
    with orchestration.connect(db_path) as conn:
        conn.executescript(CONTROL_SCHEMA)
        columns = {r["name"] for r in conn.execute(
            "PRAGMA table_info(control_commands)")}
        if "execution_token" not in columns:
            conn.execute("ALTER TABLE control_commands ADD COLUMN execution_token TEXT")
        if "lease_expires_at" not in columns:
            conn.execute("ALTER TABLE control_commands ADD COLUMN lease_expires_at TEXT")
        provenance_columns = {r["name"] for r in conn.execute(
            "PRAGMA table_info(control_session_provenance)")}
        if "image_digest" not in provenance_columns:
            conn.execute(
                "ALTER TABLE control_session_provenance ADD COLUMN image_digest TEXT")
        if "build_revision" not in provenance_columns:
            conn.execute(
                "ALTER TABLE control_session_provenance ADD COLUMN build_revision TEXT")
        claim_columns = {r["name"] for r in conn.execute(
            "PRAGMA table_info(control_wake_claims)")}
        for name in ("resolution", "reconciled_at", "reconciled_by",
                     "reconciliation_note"):
            if name not in claim_columns:
                conn.execute(
                    f"ALTER TABLE control_wake_claims ADD COLUMN {name} TEXT")
        for name, declaration in (
                ("reason_kind", "TEXT NOT NULL DEFAULT 'wake'"),
                ("resume_session_id", "TEXT"),
                ("rollover_request_id", "TEXT"),
                ("attempt_number", "INTEGER")):
            if name not in claim_columns:
                conn.execute(
                    f"ALTER TABLE control_wake_claims ADD COLUMN {name} {declaration}")
        _ensure_triggers(conn)


def current_cursor(conn: sqlite3.Connection | None = None,
                   db_path: Path | str | None = None) -> str:
    if conn is not None:
        row = conn.execute("SELECT COALESCE(MAX(id),0) AS n FROM control_events").fetchone()
        return cursor_for(row["n"], _server_id(conn))
    ensure_schema(db_path)
    with orchestration.connect(db_path) as opened:
        return current_cursor(opened)


def event_bounds(db_path: Path | str | None = None) -> tuple[int, int]:
    ensure_schema(db_path)
    with orchestration.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(MIN(id),0) AS lo, COALESCE(MAX(id),0) AS hi "
            "FROM control_events").fetchone()
        return int(row["lo"]), int(row["hi"])


def events_after(cursor: str | None, *, limit: int = 500,
                 db_path: Path | str | None = None) -> list[dict]:
    ensure_schema(db_path)
    with orchestration.connect(db_path) as conn:
        server_id = _server_id(conn)
        after = cursor_id(cursor, server_id)
        bounds = conn.execute(
            "SELECT COALESCE(MIN(id),0) AS lo, COALESCE(MAX(id),0) AS hi "
            "FROM control_events").fetchone()
        lo, hi = int(bounds["lo"]), int(bounds["hi"])
        if after > hi:
            raise CursorAhead(cursor_for(hi, server_id))
        if lo and after < lo - 1:
            raise CursorExpired(cursor_for(lo - 1, server_id),
                                cursor_for(hi, server_id))
        rows = conn.execute(
            "SELECT * FROM control_events WHERE id>? ORDER BY id LIMIT ?",
            (after, max(1, min(int(limit), 1000))),).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["cursor"] = cursor_for(item.pop("id"), server_id)
        if item.get("payload"):
            try:
                item["payload"] = json.loads(item["payload"])
            except ValueError:
                pass
        out.append(item)
    return out


def append_event(conn: sqlite3.Connection, resource: str, operation: str, *,
                 role: str | None = None, resource_id: str | None = None,
                 payload: dict | None = None) -> str:
    cur = conn.execute(
        "INSERT INTO control_events(resource,role,resource_id,operation,at,payload) "
        "VALUES (?,?,?,?,?,?)",
        (resource, role, resource_id, operation, now_iso(),
         json.dumps(payload, sort_keys=True, separators=(",", ":"))
         if payload is not None else None))
    return cursor_for(int(cur.lastrowid), _server_id(conn))


def command_row(conn: sqlite3.Connection, principal_id: str,
                request_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM control_commands WHERE principal_id=? AND request_id=?",
        (principal_id, request_id)).fetchone()


def insert_command(conn: sqlite3.Connection, principal_id: str,
                   request_id: str, verb: str, fingerprint: str,
                   status: str = "running", *, execution_token: str | None = None,
                   lease_expires_at: str | None = None) -> None:
    now = now_iso()
    conn.execute(
        """INSERT INTO control_commands
           (principal_id,request_id,verb,request_fingerprint,status,
            execution_token,lease_expires_at,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (principal_id, request_id, verb, fingerprint, status, execution_token,
         lease_expires_at, now, now))


def finish_command(conn: sqlite3.Connection, principal_id: str, request_id: str,
                   status: str, *, response: dict | None = None,
                   error: str | None = None,
                   execution_token: str | None = None) -> bool:
    token_clause = " AND execution_token=?" if execution_token else ""
    params = [status, json.dumps(response, sort_keys=True, separators=(",", ":"))
              if response is not None else None, error, now_iso(),
              principal_id, request_id]
    if execution_token:
        params.append(execution_token)
    cur = conn.execute(
        "UPDATE control_commands SET status=?,response_json=?,error=?,updated_at=? "
        f"WHERE principal_id=? AND request_id=?{token_clause}", params)
    return cur.rowcount == 1


def command_jobs(*, principal_id: str | None = None, limit: int = 100,
                 db_path: Path | str | None = None) -> list[dict]:
    ensure_schema(db_path)
    where, params = "", []
    if principal_id:
        where, params = "WHERE principal_id=?", [principal_id]
    with orchestration.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT principal_id,request_id,verb,status,response_json,error,"
            f"lease_expires_at,created_at,updated_at FROM control_commands {where} "
            "ORDER BY created_at DESC LIMIT ?", [*params, min(limit, 1000)]).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        if item["response_json"]:
            item["response"] = json.loads(item.pop("response_json"))
        else:
            item.pop("response_json")
        out.append(item)
    return out
