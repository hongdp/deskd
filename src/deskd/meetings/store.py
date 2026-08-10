"""Shared store: constants, the meeting schema, the meetings clock,
connections/migrations, role gates, and the row helpers every sibling
leans on. The only module in this package that other meetings modules
may all depend on; it talks only to the layers below (auth / mailbox).

The meetings clock is `_now`/`_iso` here; submodules call them through
the module attribute (`store._now()`), never a bound import, so tests
patch this single point.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Sequence

from .. import auth, mailbox
from ..config import CONFIG

MEETING_TYPES = {"live", "review", "ad-hoc"}
MEETING_STATES = {
    "waiting", "active", "consensus", "termination_pending", "paused",
    "escalated", "closed",
}
UPDATE_KINDS = {"evidence", "question", "answer", "proposal", "decision"}

# --- meeting defaults -------------------------------------------------------
# Per-meeting tunables: every one of these is also a keyword argument, so a
# caller may override it per meeting. They are engine defaults, not policy.

#: How long a `waiting` meeting tolerates a missing required attendee, and the
#: SLA for an owed one-to-one reply / an unread meeting message.
DEFAULT_WAIT_TIMEOUT_SECONDS = 300
MIN_WAIT_TIMEOUT_SECONDS = 30
#: Remaining message budget at which a meeting flips into `consensus`.
DEFAULT_CONSENSUS_THRESHOLD = 4
MIN_CONSENSUS_THRESHOLD = 2
DEFAULT_IDLE_MINUTES = 60
DEFAULT_MAX_MESSAGES = 20
DEFAULT_REVIEW_MAX_MESSAGES = 40
#: Upper bound on `wait_for_updates` blocking. Agents must not busy-wait.
MAX_WAIT_SECONDS = 5

#: The mailbox's "every participant" recipient token. Re-exported from the
#: module that owns it rather than re-spelled: it is part of the on-disk
#: mailbox_messages contract, and a second literal here would be a second source
#: of truth that could silently drift.
BROADCAST = mailbox.BROADCAST

MEETING_SCHEMA = """
-- meetings.closed_at: when the meeting actually closed. NOT a synonym for
-- updated_at, even though the two coincide on every row today: closing merely
-- happens to be the last thing that touches most meetings. That is a
-- coincidence, not an invariant — anything writing a closed meeting afterwards
-- turns updated_at into a lie about when it ended, silently, and a console
-- sorting history by "end time" would quietly reorder itself. Written in
-- exactly one place (_close_meeting), which is also the only place that can
-- close a meeting.
CREATE TABLE IF NOT EXISTS meetings (
    thread_id             TEXT PRIMARY KEY REFERENCES mailbox_threads(id) ON DELETE CASCADE,
    meeting_type          TEXT NOT NULL,
    agenda                TEXT NOT NULL,
    called_by             TEXT NOT NULL,
    supervisor_auth_nonce TEXT REFERENCES supervisor_nonces(nonce),
    priority              TEXT NOT NULL CHECK (priority IN ('normal', 'urgent')),
    state                 TEXT NOT NULL,
    consensus_threshold   INTEGER NOT NULL CHECK (consensus_threshold >= 2),
    wait_timeout_seconds  INTEGER NOT NULL DEFAULT 300 CHECK (wait_timeout_seconds >= 30),
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    closed_at             TEXT,
    auto_escalated_at     TEXT,
    waiting_escalated_at  TEXT
);

-- NOTE: `role` columns below carry no CHECK constraint on purpose. Roles are
-- host-defined and live in agent_registry; enumerating them in DDL would bake
-- one host's roster into the engine's schema. Validation happens in Python.
CREATE TABLE IF NOT EXISTS meeting_attendees (
    thread_id             TEXT NOT NULL REFERENCES meetings(thread_id) ON DELETE CASCADE,
    role                  TEXT NOT NULL,
    required              INTEGER NOT NULL DEFAULT 1,
    invited_at            TEXT NOT NULL,
    checked_in_at         TEXT,
    checkin_auth_nonce    TEXT REFERENCES supervisor_nonces(nonce),
    last_seen_event_id    INTEGER NOT NULL DEFAULT 0,
    stopped_at            TEXT,
    PRIMARY KEY (thread_id, role)
);

CREATE TABLE IF NOT EXISTS meeting_events (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id             TEXT NOT NULL REFERENCES meetings(thread_id) ON DELETE CASCADE,
    event                 TEXT NOT NULL,
    actor                 TEXT NOT NULL,
    detail                TEXT NOT NULL,
    auth_nonce            TEXT REFERENCES supervisor_nonces(nonce),
    created_at            TEXT NOT NULL
);

-- A supervisor message is only readable once it has a row here: the auth row
-- IS the proof that a verified assertion produced it.
CREATE TABLE IF NOT EXISTS meeting_message_auth (
    message_id            INTEGER PRIMARY KEY REFERENCES mailbox_messages(id) ON DELETE CASCADE,
    auth_nonce            TEXT NOT NULL REFERENCES supervisor_nonces(nonce)
);

CREATE TABLE IF NOT EXISTS meeting_response_obligations (
    message_id            INTEGER PRIMARY KEY REFERENCES mailbox_messages(id) ON DELETE CASCADE,
    thread_id             TEXT NOT NULL REFERENCES meetings(thread_id) ON DELETE CASCADE,
    owed_by               TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('pending', 'resolved', 'waived')),
    due_at                TEXT NOT NULL,
    resolved_by_message_id INTEGER REFERENCES mailbox_messages(id),
    resolution            TEXT,
    created_at            TEXT NOT NULL,
    resolved_at           TEXT,
    escalated_at          TEXT
);

CREATE TABLE IF NOT EXISTS meeting_terminations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id             TEXT NOT NULL REFERENCES meetings(thread_id) ON DELETE CASCADE,
    proposer              TEXT NOT NULL,
    resolution            TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'rejected')),
    auth_nonce            TEXT REFERENCES supervisor_nonces(nonce),
    created_at            TEXT NOT NULL,
    resolved_at           TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_pending_termination
ON meeting_terminations(thread_id) WHERE status='pending';

CREATE TABLE IF NOT EXISTS meeting_termination_votes (
    proposal_id           INTEGER NOT NULL REFERENCES meeting_terminations(id) ON DELETE CASCADE,
    role                  TEXT NOT NULL,
    vote                  TEXT NOT NULL CHECK (vote IN ('confirm', 'reject')),
    reason                TEXT,
    auth_nonce            TEXT REFERENCES supervisor_nonces(nonce),
    voted_at              TEXT NOT NULL,
    PRIMARY KEY (proposal_id, role)
);

CREATE TABLE IF NOT EXISTS meeting_escalations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id             TEXT NOT NULL REFERENCES meetings(thread_id) ON DELETE CASCADE,
    requested_by          TEXT NOT NULL,
    reason                TEXT NOT NULL,
    channel               TEXT NOT NULL,
    status                TEXT NOT NULL,
    details               TEXT,
    created_at            TEXT NOT NULL,
    sent_at               TEXT,
    -- `status` is DELIVERY (did the channel take it). These are DISPOSITION
    -- (did a human deal with it). Two axes, deliberately not merged: a page
    -- that sent successfully and a question nobody answered are different
    -- states, and the queue had only the first for its whole life.
    origin                TEXT NOT NULL DEFAULT 'agent',
    resolved_at           TEXT,
    resolved_by           TEXT,
    resolution            TEXT
);

CREATE TABLE IF NOT EXISTS meeting_wake_requests (
    thread_id             TEXT NOT NULL REFERENCES meetings(thread_id) ON DELETE CASCADE,
    role                  TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('pending', 'acknowledged')),
    created_at            TEXT NOT NULL,
    acknowledged_at       TEXT,
    generation            INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (thread_id, role)
);

CREATE TABLE IF NOT EXISTS mailbox_notifications (
    message_id            INTEGER NOT NULL REFERENCES mailbox_messages(id) ON DELETE CASCADE,
    role                  TEXT NOT NULL,
    notified_at           TEXT NOT NULL,
    PRIMARY KEY (message_id, role)
);
"""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime | None = None) -> str:
    """Durable timestamps are always stored UTC-normalised and ISO-8601.

    `CONFIG.timezone` is a *presentation* and scheduling concern (see the
    orchestration layer); persisting local time here would make the ledger
    ambiguous across a DST fold.
    """
    return (value or _now()).astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp is missing a timezone offset: {value!r}")
    return parsed.astimezone(dt.timezone.utc)


def _clean(value: str, label: str) -> str:
    out = " ".join(value.split())
    if not out:
        raise ValueError(f"{label} is required")
    return out


def _clean_prose(value: str, label: str) -> str:
    # Message bodies and resolutions are prose: agents write paragraphs,
    # lists, and tables, and the console renders that structure. Flattening
    # them through _clean destroyed it at ingest, so no renderer downstream
    # could ever get it back. Line structure survives here; only horizontal
    # whitespace inside a line is normalized, and blank runs collapse to one.
    lines = [" ".join(line.split()) for line in (value or "").splitlines()]
    kept: list[str] = []
    for line in lines:
        if not line and (not kept or not kept[-1]):
            continue
        kept.append(line)
    while kept and not kept[-1]:
        kept.pop()
    out = "\n".join(kept)
    if not out:
        raise ValueError(f"{label} is required")
    return out


@contextmanager
def connect(db_path: Path | str | None = None, *,
            write: bool = False) -> Iterator[sqlite3.Connection]:
    """Open the shared DB with mailbox + auth + meeting schema.

    The auth schema is applied first: the meeting tables foreign-key
    `supervisor_nonces`, so the nonce ledger must already exist before any
    supervisor-authenticated row can be written.
    """
    with mailbox.connect(db_path) as conn:
        conn.executescript(auth.SCHEMA)
        conn.executescript(MEETING_SCHEMA)
        _migrate(conn)
        conn.commit()
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing DB's meeting tables up to schema. Idempotent.

    This layer migrates its own tables. `closed_at` was briefly added by
    orchestration's `_migrate` instead, which a meetings-only host never runs —
    so its `list_meetings` raised `no such column: closed_at` against any DB that
    predated the column, while a fresh DB was fine because CREATE TABLE already
    had it. Only the shape nobody exercises breaks that way.
    """
    if "closed_at" not in {r["name"] for r in conn.execute(
            "PRAGMA table_info(meetings)")}:
        conn.execute("ALTER TABLE meetings ADD COLUMN closed_at TEXT")
        # Backfilled from updated_at, which is an APPROXIMATION and the only one
        # available: these rows closed before anything recorded when. It is the
        # tightest true upper bound — a meeting cannot have closed after its last
        # write — and on every row in existence today it is exact, because
        # closing was in fact the last write. New rows get the real thing.
        conn.execute("UPDATE meetings SET closed_at=updated_at "
                     "WHERE state='closed' AND closed_at IS NULL")
    if "generation" not in {r["name"] for r in conn.execute(
            "PRAGMA table_info(meeting_wake_requests)")}:
        # A request can be acknowledged and later re-armed for the same
        # (meeting, role). The wake attempt key is also (meeting, role), so
        # without a generation the planner cannot distinguish the new request
        # from an old attempt that has not yet been reconciled.
        conn.execute(
            "ALTER TABLE meeting_wake_requests "
            "ADD COLUMN generation INTEGER NOT NULL DEFAULT 1")
    escalation_columns = {r["name"] for r in conn.execute(
        "PRAGMA table_info(meeting_escalations)")}
    if "origin" not in escalation_columns:
        # The queue was write-only: rows were created and delivered, never
        # closed. Everything ever raised still read as outstanding, so the
        # handful that genuinely needed a human sat in a list of dozens that
        # did not. Two columns fix two different confusions — what KIND of
        # thing this is, and whether it is DONE.
        conn.execute("ALTER TABLE meeting_escalations "
                     "ADD COLUMN origin TEXT NOT NULL DEFAULT 'agent'")
        for column in ("resolved_at", "resolved_by", "resolution"):
            conn.execute(
                f"ALTER TABLE meeting_escalations ADD COLUMN {column} TEXT")
        # Backfill by reason, because the historical rows carry no other
        # signal: `requested_by` names whoever CALLED the meeting, not who
        # asked for the human, so an auto-escalation on an urgent meeting is
        # filed under the caller — including the supervisor themselves.
        # Taken from the wordings actually present in a two-week-old desk,
        # not from the current call sites: two of these phrasings predate a
        # rename ("five-minute X" became "X after 300s"), and matching only
        # today's text left the older rows filed as questions nobody had
        # answered. Match on the stable middle of each sentence.
        for phrase in ("attendance timeout",
                       "urgent meeting requires off-hours wake",
                       "meeting entered consensus mode with the supervisor absent",
                       "rejected near message limit",
                       "stale attendee:",
                       "one-to-one response timeout"):
            conn.execute(
                "UPDATE meeting_escalations SET origin='engine' "
                "WHERE reason LIKE ?", (f"%{phrase}%",))
        # Engine notes about a conversation that has ended cannot need anything
        # from anyone. Agent questions are NOT swept: one outlived its meeting
        # by four days and was still the live question when it was answered.
        conn.execute(
            """UPDATE meeting_escalations
                  SET resolved_at=COALESCE(
                          (SELECT closed_at FROM meetings m
                            WHERE m.thread_id=meeting_escalations.thread_id),
                          created_at),
                      resolved_by='migration',
                      resolution='engine note about a meeting that has closed'
                WHERE origin='engine' AND resolved_at IS NULL
                  AND thread_id IN (SELECT thread_id FROM meetings
                                     WHERE state='closed')""")


# --- roles ------------------------------------------------------------------

def _known_roles(conn: sqlite3.Connection) -> set[str]:
    """The enabled agent roles, from the registry the host owns.

    The engine ships with no roster at all. When the registry table has not been
    provisioned yet (meetings used standalone, before the orchestration layer
    has ever opened the DB) we fall back to the roles declared on the config, so
    a host that only wants meetings still works. The registry always wins when
    it exists.
    """
    try:
        rows = conn.execute(
            "SELECT role FROM agent_registry WHERE enabled=1").fetchall()
    except sqlite3.OperationalError:
        return set(CONFIG.role_names())
    return {r["role"] for r in rows}


def _meeting_roles(conn: sqlite3.Connection) -> set[str]:
    """Everyone who may sit in a meeting: agents plus the supervisor."""
    return _known_roles(conn) | {CONFIG.supervisor_role}


def _agent_role(conn: sqlite3.Connection, role: str) -> str:
    """Validate a role an *agent* claims to be. Never the supervisor."""
    role = _clean(role, "role")
    if role == CONFIG.supervisor_role:
        raise ValueError(
            f"{role!r} is not an agent role; supervisor actions require the "
            f"authenticated web adapter"
        )
    if role not in _known_roles(conn):
        raise ValueError(f"unknown or disabled agent role: {role}")
    return role


def _in_clause(column: str, values: Sequence[str]) -> tuple[str, list[str]]:
    """Build `column IN (?,?,...)` with bound placeholders.

    Role names come from the registry and must never be interpolated into SQL.
    An empty set collapses to a constant-false predicate, because `x IN ()` is a
    syntax error in SQLite — and "no roles are known" genuinely means "no
    message matches".
    """
    if not values:
        return "0", []
    return f"{column} IN ({','.join('?' * len(values))})", list(values)


def _visible_message_sql(conn: sqlite3.Connection,
                         alias: str = "mm") -> tuple[str, list[str]]:
    """Predicate: this mailbox row really was *said in the meeting*.

    A row counts only when it is either an agent message whose sender is (or
    was) a checked-in attendee, or a supervisor message carrying a verified auth
    row. Everything else is ignored, so a row written straight into the mailbox
    with a forged sender can never manufacture unread counts, response
    obligations, or escalations. Every unread/SLA query shares this predicate;
    if they ever diverge, an attacker gets a wedge between them.
    """
    roles = sorted(_known_roles(conn))
    sender_in, params = _in_clause(f"{alias}.sender", roles)
    sql = (
        f"(({sender_in} AND EXISTS "
        f"  (SELECT 1 FROM meeting_attendees va "
        f"   WHERE va.thread_id={alias}.thread_id AND va.role={alias}.sender "
        f"     AND va.checked_in_at IS NOT NULL)) "
        f" OR ({alias}.sender=? AND EXISTS "
        f"     (SELECT 1 FROM meeting_message_auth ma "
        f"      WHERE ma.message_id={alias}.id)))"
    )
    return sql, params + [CONFIG.supervisor_role]


# --- attendance primitives --------------------------------------------------

def _active_roles(conn: sqlite3.Connection, thread_id: str) -> list[str]:
    return [r["role"] for r in conn.execute(
        """SELECT role FROM meeting_attendees
           WHERE thread_id=? AND checked_in_at IS NOT NULL AND stopped_at IS NULL
           ORDER BY role""",
        (thread_id,),
    ).fetchall()]


def _mode(conn: sqlite3.Connection, thread_id: str) -> str:
    """Discussion mode, derived purely from who is currently present.

    one_to_one imposes strict turn-taking (every message owes a reply); multi
    does not, because a broadcast cannot sensibly obligate everyone.
    """
    count = len(_active_roles(conn, thread_id))
    if count < 2:
        return "waiting"
    return "one_to_one" if count == 2 else "multi"


# --- row helpers --------------------------------------------------------------

def _event(conn: sqlite3.Connection, thread_id: str, event: str, actor: str,
           detail: str,
           auth_nonce: str | None = None) -> int:
    return int(conn.execute(
        """INSERT INTO meeting_events(thread_id,event,actor,detail,auth_nonce,created_at)
           VALUES (?,?,?,?,?,?)""",
        (thread_id, event, actor, detail, auth_nonce, _iso()),
    ).lastrowid)


def _supervisor_claim(conn: sqlite3.Connection, auth_nonce: str | None,
                      actions: set[str], *,
                      thread_id: str | None = None) -> dict:
    """Fetch the verified claim behind a nonce and re-check its binding.

    Verification already happened in `deskd.auth`; this re-reads the *ledger* so
    the action and the meeting it names are checked against what was actually
    signed, not against what the caller passes now. `auth.claim` raises
    `AuthError`, which subclasses ValueError — so every caller's rejection
    handling is unchanged.
    """
    return auth.claim(conn, auth_nonce, actions, meeting_id=thread_id)


def _meeting(conn: sqlite3.Connection, thread_id: str) -> sqlite3.Row:
    """Read the meeting joined to its thread, retiring the thread if it went idle.

    The refresh is what makes the idle deadline one of the four bounds design.md
    §Meetings claims. The deadline is enforced lazily on read (no daemon owns the
    mailbox), so a raw `SELECT ... FROM mailbox_threads` here would report a stale
    status='open' and let `_insert_message` write to a thread that expired — the
    exact bypass `mailbox._refresh_thread` exists to close ("Every read path goes
    through here so a stale thread can never be written to"). Every meetings read
    of the thread funnels through this helper for that reason; none may go direct.
    """
    try:
        mailbox._refresh_thread(conn, thread_id)
    except ValueError:
        # No thread means no meeting; keep this helper's own error contract.
        raise ValueError(f"unknown meeting: {thread_id}") from None
    row = conn.execute(
        """SELECT m.*, t.status AS thread_status, t.phase AS review_phase,
                  t.stop_reason AS thread_stop_reason,
                  t.stopped_by AS thread_stopped_by,
                  t.expires_at AS thread_expires_at,
                  t.max_messages, t.message_count,
                  (t.max_messages-t.message_count) AS messages_remaining
           FROM meetings m JOIN mailbox_threads t ON t.id=m.thread_id
           WHERE m.thread_id=?""",
        (thread_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"unknown meeting: {thread_id}")
    return row


def _meeting_projection(row: sqlite3.Row | dict) -> dict:
    """Add the operational state without rewriting the protocol state.

    ``meetings.state`` says where the meeting protocol should continue when its
    mailbox is available. ``mailbox_threads.status`` says whether anybody can
    actually speak right now. Idle retirement intentionally changes only the
    latter, so the two may legitimately read ``active`` / ``paused``.

    Keep the raw ``state`` for existing consumers and expose the thread-led
    ``effective_state`` for operator surfaces. A lifecycle stop remains
    authoritative if a damaged/legacy row has an open thread, while a retired
    thread overrides any otherwise-live lifecycle state.
    """
    out = dict(row)
    lifecycle = out["state"]
    thread = out.get("thread_status") or "open"
    stopped = {"paused", "escalated", "closed"}
    if lifecycle in stopped:
        effective = lifecycle
    elif thread in stopped:
        effective = thread
    else:
        effective = lifecycle
    expected_thread = lifecycle if lifecycle in stopped else "open"
    out["effective_state"] = effective
    out["state_in_sync"] = thread == expected_thread
    return out


def _rearm_agent_wakes(conn: sqlite3.Connection, thread_id: str, *,
                       now: str | None = None) -> list[str]:
    """Queue a fresh wake for every enabled, non-departed agent attendee.

    A supervisor resume is an explicit request to put the room back in front
    of its participants. Merely opening the mailbox is insufficient when the
    previous invitation was already acknowledged and no reply obligation is
    outstanding: the agents remain idle forever. This ledger is owned by the
    meetings layer, so re-arming it here preserves the mailbox -> meetings ->
    orchestration dependency direction; the orchestrator will pull the demand.
    """
    known = sorted(_known_roles(conn))
    role_in, role_params = _in_clause("role", known)
    roles = [r["role"] for r in conn.execute(
        f"""SELECT role FROM meeting_attendees
            WHERE thread_id=? AND stopped_at IS NULL AND {role_in}
            ORDER BY role""",
        (thread_id, *role_params),
    ).fetchall()]
    created_at = now or _iso()
    conn.executemany(
        """INSERT INTO meeting_wake_requests(thread_id,role,status,created_at)
           VALUES (?,?,'pending',?)
           ON CONFLICT(thread_id,role) DO UPDATE
           SET status='pending',created_at=excluded.created_at,
               acknowledged_at=NULL,
               generation=meeting_wake_requests.generation+1""",
        [(thread_id, role, created_at) for role in roles],
    )
    return roles


def _attendee(conn: sqlite3.Connection, thread_id: str, role: str, *,
              checked_in: bool = False,
              allow_stopped: bool = False) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM meeting_attendees WHERE thread_id=? AND role=?",
        (thread_id, role),
    ).fetchone()
    if not row:
        raise ValueError(f"{role} is not invited to meeting {thread_id}")
    if checked_in and not row["checked_in_at"]:
        raise ValueError(f"{role} has not checked in")
    if checked_in and row["stopped_at"] and not allow_stopped:
        raise ValueError(f"{role} has left the meeting")
    return row


def _has_supervisor(conn: sqlite3.Connection, thread_id: str) -> bool:
    row = conn.execute(
        """SELECT 1 FROM meeting_attendees
           WHERE thread_id=? AND role=? AND checked_in_at IS NOT NULL
             AND stopped_at IS NULL""",
        (thread_id, CONFIG.supervisor_role),
    ).fetchone()
    return bool(row)


def _thread_last_activity(conn: sqlite3.Connection, thread_id: str) -> dt.datetime:
    """Newest substantive activity on the thread = last message (not events).

    Events (check-in, mode changes, escalations) are bookkeeping, not someone
    speaking, so the leave/idle test looks only at real messages, falling back
    to the meeting's own creation time when nothing has been said yet.
    """
    row = conn.execute(
        "SELECT MAX(created_at) AS last FROM mailbox_messages WHERE thread_id=?",
        (thread_id,),
    ).fetchone()
    if row and row["last"]:
        return _parse_time(row["last"])
    return _parse_time(_meeting(conn, thread_id)["created_at"])


def _stamp_notifications(conn: sqlite3.Connection, role: str) -> None:
    """Record that `role` has been *notified* of its still-unread meeting
    messages (distinct from having *read* them). Purely additive: this never
    touches mailbox_receipts, so it cannot suppress an unread count or a
    stale-unread escalation — it only lets the console show delivered-but-
    unread separately from read.
    """
    visible_sql, visible_params = _visible_message_sql(conn, "mm")
    conn.execute(
        f"""INSERT OR IGNORE INTO mailbox_notifications(message_id, role, notified_at)
            SELECT mm.id, ?, ?
            FROM mailbox_messages mm
            JOIN meeting_attendees a
              ON a.thread_id=mm.thread_id AND a.role=?
            WHERE a.checked_in_at IS NOT NULL AND a.stopped_at IS NULL
              AND mm.recipient IN (?, ?) AND mm.sender != ?
              AND NOT EXISTS (SELECT 1 FROM mailbox_receipts r
                              WHERE r.message_id=mm.id AND r.role=?)
              AND {visible_sql}""",
        (role, _iso(), role, role, BROADCAST, role, role, *visible_params),
    )
