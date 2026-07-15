"""Bounded multi-agent meetings layered on the durable mailbox.

A meeting is a *bounded* conversation: it has an agenda, an invited attendee
list, a message budget, an idle deadline, and a state machine that always
terminates. The point of the bounds is that autonomous agents cannot turn a
conversation into an unproductive loop — every path leads to `closed`, either
through the termination handshake, an escalation, or the mailbox's own budget.

State machine
-------------
    waiting              invited, quorum not yet met
    active               quorum met, normal discussion
    consensus            near the message budget; only positions/decisions
    termination_pending  someone proposed an end; awaiting confirmations
    paused / escalated   parked for a human
    closed               terminal

Roles are NOT hardcoded. The `agent_registry` table (owned by the
orchestration layer) is the source of truth for which agent roles exist; this
module reads it via `_known_roles()` and binds every role literal in SQL as a
placeholder. There is deliberately no agent-facing path to act as the
supervisor (`CONFIG.supervisor_role`): supervisor actions enter only through
the authenticated web adapter in `deskd.auth`, either as a short-lived Ed25519
assertion (trusted-device mode) or a simple access-code gate (trusted local
mode). Every supervisor mutation carries a one-shot nonce that is burned before
the action runs, and a supervisor *message* is only visible once it has a
matching auth row — an unauthenticated row written straight into the mailbox
can never speak as the supervisor.

Layering: mailbox -> meetings -> orchestration. Never import orchestration here.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Sequence

from . import auth, mailbox
from .config import CONFIG, PROJECT_NAME

MEETING_TYPES = {"live", "review", "ad-hoc"}
MEETING_STATES = {
    "waiting", "active", "consensus", "termination_pending", "paused",
    "escalated", "closed",
}
UPDATE_KINDS = {"evidence", "question", "answer", "proposal", "decision"}

#: Fields each supervisor action must carry INSIDE the signed payload, passed to
#: the auth verifier along with the allowlist below. `deskd.auth` knows no verbs
#: — this module owns them, so a new privileged verb cannot widen the trust
#: boundary without being declared here. Requiring the fields up front means a
#: handler can trust they exist rather than KeyError-ing mid-mutation; keep this
#: map in sync when adding an action. Every action except `call` (which creates
#: the meeting) targets an existing meeting.
REQUIRED_SIGNED_FIELDS: dict[str, tuple[str, ...]] = {
    "call": ("agenda", "attendees"),
    "join": ("meeting_id",),
    "leave": ("meeting_id", "reason"),
    "check_in": ("meeting_id",),
    "read": ("meeting_id",),
    "send": ("meeting_id", "body"),
    "position": ("meeting_id", "body"),
    "propose_end": ("meeting_id", "resolution"),
    "confirm_end": ("meeting_id",),
    "reject_end": ("meeting_id",),
    "resume": ("meeting_id",),
    "force_close": ("meeting_id", "reason"),
}

#: Meeting actions a verified supervisor assertion may carry. Derived from the
#: map above so the two can never drift apart.
SUPERVISOR_ACTIONS = frozenset(REQUIRED_SIGNED_FIELDS)

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

#: The terminal escalation channel: "delivery" is the ledger row itself, which
#: the console renders. Always available, so an escalation is never lost.
OUTBOX_CHANNEL = "outbox"

#: The mailbox's "every participant" recipient token. Re-exported from the
#: module that owns it rather than re-spelled: it is part of the on-disk
#: mailbox_messages contract, and a second literal here would be a second source
#: of truth that could silently drift.
BROADCAST = mailbox.BROADCAST

MEETING_SCHEMA = """
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
    sent_at               TEXT
);

CREATE TABLE IF NOT EXISTS meeting_wake_requests (
    thread_id             TEXT NOT NULL REFERENCES meetings(thread_id) ON DELETE CASCADE,
    role                  TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('pending', 'acknowledged')),
    created_at            TEXT NOT NULL,
    acknowledged_at       TEXT,
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


@contextmanager
def connect(db_path: Path | str | None = None, *, write: bool = False):
    """Open the shared DB with mailbox + auth + meeting schema.

    The auth schema is applied first: the meeting tables foreign-key
    `supervisor_nonces`, so the nonce ledger must already exist before any
    supervisor-authenticated row can be written.
    """
    with mailbox.connect(db_path) as conn:
        conn.executescript(auth.SCHEMA)
        conn.executescript(MEETING_SCHEMA)
        conn.commit()
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn


# --- roles ------------------------------------------------------------------

def _known_roles(conn) -> set[str]:
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


def _meeting_roles(conn) -> set[str]:
    """Everyone who may sit in a meeting: agents plus the supervisor."""
    return _known_roles(conn) | {CONFIG.supervisor_role}


def _agent_role(conn, role: str) -> str:
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


def _visible_message_sql(conn, alias: str = "mm") -> tuple[str, list[str]]:
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

def _active_roles(conn, thread_id: str) -> list[str]:
    return [r["role"] for r in conn.execute(
        """SELECT role FROM meeting_attendees
           WHERE thread_id=? AND checked_in_at IS NOT NULL AND stopped_at IS NULL
           ORDER BY role""",
        (thread_id,),
    ).fetchall()]


def _mode(conn, thread_id: str) -> str:
    """Discussion mode, derived purely from who is currently present.

    one_to_one imposes strict turn-taking (every message owes a reply); multi
    does not, because a broadcast cannot sensibly obligate everyone.
    """
    count = len(_active_roles(conn, thread_id))
    if count < 2:
        return "waiting"
    return "one_to_one" if count == 2 else "multi"


def _resolve_obligations(conn, thread_id: str, role: str, *,
                         resolution: str, reply_message_id: int | None = None) -> int:
    now = _iso()
    cursor = conn.execute(
        """UPDATE meeting_response_obligations
           SET status='resolved',resolved_at=?,resolution=?,resolved_by_message_id=?
           WHERE thread_id=? AND owed_by=? AND status='pending'""",
        (now, resolution, reply_message_id, thread_id, role),
    )
    return int(cursor.rowcount)


def _waive_pending_obligations(conn, thread_id: str, reason: str) -> int:
    now = _iso()
    cursor = conn.execute(
        """UPDATE meeting_response_obligations
           SET status='waived',resolved_at=?,resolution=?
           WHERE thread_id=? AND status='pending'""",
        (now, reason, thread_id),
    )
    return int(cursor.rowcount)


def _event(conn, thread_id: str, event: str, actor: str, detail: str,
           auth_nonce: str | None = None) -> int:
    return int(conn.execute(
        """INSERT INTO meeting_events(thread_id,event,actor,detail,auth_nonce,created_at)
           VALUES (?,?,?,?,?,?)""",
        (thread_id, event, actor, detail, auth_nonce, _iso()),
    ).lastrowid)


def _supervisor_claim(conn, auth_nonce: str | None, actions: set[str], *,
                      thread_id: str | None = None) -> dict:
    """Fetch the verified claim behind a nonce and re-check its binding.

    Verification already happened in `deskd.auth`; this re-reads the *ledger* so
    the action and the meeting it names are checked against what was actually
    signed, not against what the caller passes now. `auth.claim` raises
    `AuthError`, which subclasses ValueError — so every caller's rejection
    handling is unchanged.
    """
    return auth.claim(conn, auth_nonce, actions, meeting_id=thread_id)


def _meeting(conn, thread_id: str):
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
                  t.max_messages, t.message_count,
                  (t.max_messages-t.message_count) AS messages_remaining
           FROM meetings m JOIN mailbox_threads t ON t.id=m.thread_id
           WHERE m.thread_id=?""",
        (thread_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"unknown meeting: {thread_id}")
    return row


def _attendee(conn, thread_id: str, role: str, *, checked_in: bool = False):
    row = conn.execute(
        "SELECT * FROM meeting_attendees WHERE thread_id=? AND role=?",
        (thread_id, role),
    ).fetchone()
    if not row:
        raise ValueError(f"{role} is not invited to meeting {thread_id}")
    if checked_in and not row["checked_in_at"]:
        raise ValueError(f"{role} has not checked in")
    if checked_in and row["stopped_at"]:
        raise ValueError(f"{role} has left the meeting")
    return row


def _has_supervisor(conn, thread_id: str) -> bool:
    row = conn.execute(
        """SELECT 1 FROM meeting_attendees
           WHERE thread_id=? AND role=? AND checked_in_at IS NOT NULL
             AND stopped_at IS NULL""",
        (thread_id, CONFIG.supervisor_role),
    ).fetchone()
    return bool(row)


def _thread_last_activity(conn, thread_id: str) -> dt.datetime:
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


def _stamp_notifications(conn, role: str) -> None:
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


# --- escalation channels ----------------------------------------------------

class EscalationChannel:
    """A destination an escalation can be delivered to.

    The engine ships with no network code and no channel implementations: it
    knows nothing about anyone's Discord, SMTP, or pager. A host registers what
    it has (see `register_channel` / `CallableChannel`); `outbox` is always
    available as the terminal fallback, so an escalation is never silently
    dropped just because nothing is configured.
    """

    #: Unique channel name, as stored in meeting_escalations.channel.
    name: str = ""

    def available(self) -> bool:
        """Is this channel currently usable? `auto` dispatch picks every
        channel that says yes. An unconfigured channel should say no rather
        than fail at send time."""
        return True

    def send(self, subject: str, text: str) -> None:
        """Deliver, or raise. Raising marks this channel failed for this
        escalation; other channels still get their turn."""
        raise NotImplementedError


class CallableChannel(EscalationChannel):
    """Adapter so a host can register a channel with a plain function.

        deskd.meetings.register_channel(CallableChannel(
            "discord", send=lambda subject, text: post(text),
            available=lambda: bool(webhook_url),
        ))
    """

    def __init__(self, name: str, send: Callable[[str, str], None],
                 available: Callable[[], bool] | None = None) -> None:
        self.name = _clean(name, "channel name")
        if self.name in {"auto", OUTBOX_CHANNEL}:
            raise ValueError(f"{self.name!r} is a reserved channel name")
        self._send = send
        self._available = available

    def available(self) -> bool:
        return True if self._available is None else bool(self._available())

    def send(self, subject: str, text: str) -> None:
        self._send(subject, text)


_CHANNELS: dict[str, EscalationChannel] = {}


def register_channel(channel: EscalationChannel) -> None:
    """Register (or replace) an escalation channel. Call at host startup."""
    name = _clean(channel.name, "channel name")
    if name in {"auto", OUTBOX_CHANNEL}:
        raise ValueError(f"{name!r} is a reserved channel name")
    _CHANNELS[name] = channel


def unregister_channel(name: str) -> None:
    _CHANNELS.pop(name, None)


def registered_channels() -> tuple[str, ...]:
    return tuple(sorted(_CHANNELS))


def _channel_available(channel: EscalationChannel) -> bool:
    # A broken availability probe must not take down the dispatcher; treat it
    # as unavailable and let the outbox fallback catch the escalation.
    try:
        return bool(channel.available())
    except Exception:
        return False


def _auto_channels() -> list[str]:
    names = [n for n, c in _CHANNELS.items() if _channel_available(c)]
    return sorted(names) or [OUTBOX_CHANNEL]


def _queue_escalation(conn, thread_id: str, requested_by: str, reason: str,
                      channel: str = "auto") -> int:
    cursor = conn.execute(
        """INSERT INTO meeting_escalations
           (thread_id,requested_by,reason,channel,status,created_at)
           VALUES (?,?,?,?, 'queued', ?)""",
        (thread_id, requested_by, reason, channel, _iso()),
    )
    escalation_id = int(cursor.lastrowid)
    _event(conn, thread_id, "escalation_queued", requested_by,
           f"#{escalation_id}: {reason}")
    return escalation_id


def dispatch_escalation(escalation_id: int, *,
                        db_path: Path | str | None = None) -> dict:
    """Deliver a queued escalation. Always called *after* the transaction that
    queued it has committed, so a slow or hanging channel can never hold a write
    lock on the meeting."""
    with connect(db_path) as conn:
        row = conn.execute(
            """SELECT e.*,m.agenda FROM meeting_escalations e
               JOIN meetings m ON m.thread_id=e.thread_id WHERE e.id=?""",
            (escalation_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"unknown escalation: {escalation_id}")
    channels = _auto_channels() if row["channel"] == "auto" else [row["channel"]]
    subject = f"{PROJECT_NAME} meeting: {row['agenda']}"
    text = (f"{PROJECT_NAME} meeting escalation [{row['thread_id']}]\n"
            f"Agenda: {row['agenda']}\nReason: {row['reason']}")
    results = []
    for name in channels:
        try:
            if name == OUTBOX_CHANNEL:
                # The ledger row IS the delivery; the console surfaces it.
                results.append({"channel": name, "status": "queued"})
                continue
            channel = _CHANNELS.get(name)
            if channel is None:
                raise RuntimeError(f"no such escalation channel: {name}")
            channel.send(subject, text)
            results.append({"channel": name, "status": "sent"})
        except Exception as exc:
            results.append({"channel": name, "status": "failed", "error": str(exc)})
    sent = any(r["status"] == "sent" for r in results)
    queued = any(r["status"] == "queued" for r in results)
    status = "sent" if sent else ("queued" if queued else "failed")
    with connect(db_path, write=True) as conn:
        conn.execute(
            "UPDATE meeting_escalations SET status=?,details=?,sent_at=? WHERE id=?",
            (status, json.dumps(results, ensure_ascii=False),
             _iso() if sent else None, escalation_id),
        )
    return {"id": escalation_id, "status": status, "results": results}


def list_escalations(thread_id: str | None = None, *,
                     db_path: Path | str | None = None) -> list[dict]:
    with connect(db_path) as conn:
        sql = "SELECT * FROM meeting_escalations"
        params: tuple = ()
        if thread_id:
            sql += " WHERE thread_id=?"
            params = (thread_id,)
        sql += " ORDER BY id DESC"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# --- SLA sweep --------------------------------------------------------------

def _sweep_timeouts(db_path: Path | str | None = None) -> list[int]:
    """Escalate overdue attendance/replies without blocking the calling agent.

    Every read path runs this, so an agent that merely looks at a meeting also
    advances the clock for everyone. Dispatch happens after the write
    transaction closes.
    """
    escalation_ids: list[int] = []
    now = _now()
    now_iso = _iso(now)
    with connect(db_path, write=True) as conn:
        agent_roles = _known_roles(conn)
        # 1. Attendance: a `waiting` meeting whose required attendees never
        #    showed up. Escalate exactly once (waiting_escalated_at).
        waiting = conn.execute(
            """SELECT * FROM meetings
               WHERE state='waiting' AND waiting_escalated_at IS NULL"""
        ).fetchall()
        for meeting in waiting:
            created = _parse_time(meeting["created_at"])
            if created + dt.timedelta(seconds=meeting["wait_timeout_seconds"]) > now:
                continue
            missing = [r["role"] for r in conn.execute(
                """SELECT role FROM meeting_attendees
                   WHERE thread_id=? AND required=1 AND checked_in_at IS NULL
                     AND stopped_at IS NULL""",
                (meeting["thread_id"],),
            ).fetchall()]
            # Only agents can be woken; a missing supervisor is a human problem
            # and rides out on the escalation instead.
            for role in sorted(set(missing) & agent_roles):
                conn.execute(
                    """INSERT OR IGNORE INTO meeting_wake_requests
                       (thread_id,role,status,created_at) VALUES (?,?,'pending',?)""",
                    (meeting["thread_id"], role, now_iso),
                )
            escalation_ids.append(_queue_escalation(
                conn, meeting["thread_id"], "system",
                f"attendance timeout after {meeting['wait_timeout_seconds']}s; "
                f"missing: {', '.join(missing) or 'active counterpart'}",
                "auto",
            ))
            conn.execute(
                "UPDATE meetings SET waiting_escalated_at=? WHERE thread_id=?",
                (now_iso, meeting["thread_id"]),
            )
        # 2. Response obligations: an owed one-to-one reply past its due date.
        overdue = conn.execute(
            """SELECT o.message_id,o.thread_id,o.owed_by FROM meeting_response_obligations o
               JOIN meetings m ON m.thread_id=o.thread_id
               WHERE o.status='pending' AND o.escalated_at IS NULL AND o.due_at<=?
                 AND m.state IN ('active','consensus')""",
            (now_iso,),
        ).fetchall()
        for obligation in overdue:
            escalation_ids.append(_queue_escalation(
                conn, obligation["thread_id"], "system",
                f"one-to-one response timeout: {obligation['owed_by']} owes a "
                f"reply to message #{obligation['message_id']}",
                "auto",
            ))
            conn.execute(
                "UPDATE meeting_response_obligations SET escalated_at=? WHERE message_id=?",
                (now_iso, obligation["message_id"]),
            )
        # 3. Stale attendees: checked in, but sitting on unread messages past
        #    the SLA. Re-arm a wake request only when the previous ack predates
        #    the oldest unread message, so an ack cannot silence new traffic.
        role_in, role_params = _in_clause("a.role", sorted(agent_roles))
        visible_sql, visible_params = _visible_message_sql(conn, "mm")
        stale = conn.execute(
            f"""SELECT a.thread_id, a.role, m.wait_timeout_seconds,
                       MIN(mm.created_at) AS oldest_unread
                FROM meeting_attendees a
                JOIN meetings m ON m.thread_id=a.thread_id
                JOIN mailbox_messages mm ON mm.thread_id=a.thread_id
                     AND mm.recipient IN (a.role, ?) AND mm.sender!=a.role
                LEFT JOIN mailbox_receipts r ON r.message_id=mm.id AND r.role=a.role
                WHERE m.state IN ('active','consensus')
                  AND {role_in}
                  AND a.checked_in_at IS NOT NULL AND a.stopped_at IS NULL
                  AND r.message_id IS NULL
                  AND {visible_sql}
                GROUP BY a.thread_id, a.role""",
            (BROADCAST, *role_params, *visible_params),
        ).fetchall()
        for row in stale:
            oldest = _parse_time(row["oldest_unread"])
            if oldest + dt.timedelta(seconds=row["wait_timeout_seconds"]) > now:
                continue
            cursor = conn.execute(
                """INSERT INTO meeting_wake_requests(thread_id,role,status,created_at)
                   VALUES (?,?,'pending',?)
                   ON CONFLICT(thread_id,role) DO UPDATE
                   SET status='pending',created_at=excluded.created_at,
                       acknowledged_at=NULL
                   WHERE meeting_wake_requests.status='acknowledged'
                     AND meeting_wake_requests.acknowledged_at<?""",
                (row["thread_id"], row["role"], now_iso, row["oldest_unread"]),
            )
            if cursor.rowcount:
                escalation_ids.append(_queue_escalation(
                    conn, row["thread_id"], "system",
                    f"stale attendee: {row['role']} left meeting messages unread "
                    f"past the {row['wait_timeout_seconds']}-second SLA",
                    "auto",
                ))
    for escalation_id in escalation_ids:
        dispatch_escalation(escalation_id, db_path=db_path)
    return escalation_ids


# --- calling a meeting ------------------------------------------------------

def _call_meeting(*, agenda: str, called_by: str, attendees: list[str],
                  meeting_type: str = "ad-hoc", priority: str = "normal",
                  idle_minutes: int = DEFAULT_IDLE_MINUTES,
                  max_messages: int | None = None,
                  consensus_threshold: int = DEFAULT_CONSENSUS_THRESHOLD,
                  wait_timeout_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS,
                  auth_nonce: str | None = None,
                  db_path: Path | str | None = None) -> dict:
    agenda = _clean(agenda, "agenda")
    supervisor = CONFIG.supervisor_role
    if meeting_type not in MEETING_TYPES:
        raise ValueError(f"invalid meeting type: {meeting_type}")
    if priority not in {"normal", "urgent"}:
        raise ValueError(f"invalid priority: {priority}")
    if wait_timeout_seconds < MIN_WAIT_TIMEOUT_SECONDS:
        raise ValueError(
            f"wait timeout must be at least {MIN_WAIT_TIMEOUT_SECONDS} seconds")
    if consensus_threshold < MIN_CONSENSUS_THRESHOLD:
        raise ValueError(
            f"consensus threshold must be at least {MIN_CONSENSUS_THRESHOLD}")
    with connect(db_path) as conn:
        meeting_roles = _meeting_roles(conn)
        if called_by not in meeting_roles:
            raise ValueError(f"invalid caller: {called_by}")
        roles = set(attendees) | {called_by}
        if not roles <= meeting_roles or len(roles) < 2:
            raise ValueError("a meeting needs at least two valid attendees")
        if called_by != supervisor and supervisor in roles:
            raise ValueError(
                f"an agent cannot invite or represent {supervisor}; escalate instead")
        if called_by == supervisor:
            if not auth_nonce:
                raise ValueError("supervisor meeting call lacks a verified assertion")
            # The whole request must be what was signed: an assertion for a
            # two-person meeting must not be replayed into a five-person one.
            claim = _supervisor_claim(conn, auth_nonce, {"call"})
            if "attendees" not in claim:
                raise ValueError("supervisor call assertion must name its attendees")
            claimed_roles = set(claim["attendees"]) | {supervisor}
            expected = {
                "agenda": agenda,
                "meeting_type": meeting_type,
                "priority": priority,
                "idle_minutes": idle_minutes,
                "max_messages": max_messages,
                "consensus_threshold": consensus_threshold,
                "wait_timeout_seconds": wait_timeout_seconds,
            }
            actual = {
                "agenda": claim.get("agenda"),
                "meeting_type": claim.get("meeting_type", "ad-hoc"),
                "priority": claim.get("priority", "urgent"),
                "idle_minutes": int(claim.get("idle_minutes", DEFAULT_IDLE_MINUTES)),
                "max_messages": claim.get("max_messages"),
                "consensus_threshold": int(
                    claim.get("consensus_threshold", DEFAULT_CONSENSUS_THRESHOLD)),
                "wait_timeout_seconds": int(
                    claim.get("wait_timeout_seconds", DEFAULT_WAIT_TIMEOUT_SECONDS)),
            }
            if claimed_roles != roles or actual != expected:
                raise ValueError(
                    "supervisor call assertion does not match the complete meeting request")
    max_messages = max_messages or (
        DEFAULT_REVIEW_MAX_MESSAGES if meeting_type == "review" else DEFAULT_MAX_MESSAGES)
    kind = "review" if meeting_type == "review" else "live"
    subject = f"meeting/{meeting_type}: {agenda}"
    thread = mailbox.open_thread(
        subject, kind=kind, idle_minutes=idle_minutes,
        max_messages=max_messages, max_discussion=max(6, consensus_threshold + 2),
        db_path=db_path,
    )
    escalation_id = None
    with connect(db_path, write=True) as conn:
        agent_roles = _known_roles(conn)
        existing = conn.execute(
            "SELECT 1 FROM meetings WHERE thread_id=?", (thread["id"],)
        ).fetchone()
        if not existing:
            if called_by == supervisor:
                _supervisor_claim(conn, auth_nonce, {"call"})
            now = _iso()
            conn.execute(
                """INSERT INTO meetings
                   (thread_id,meeting_type,agenda,called_by,supervisor_auth_nonce,priority,
                    state,consensus_threshold,wait_timeout_seconds,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,'waiting',?,?,?,?)""",
                (thread["id"], meeting_type, agenda, called_by, auth_nonce,
                 priority, consensus_threshold, wait_timeout_seconds, now, now),
            )
            for role in sorted(roles):
                conn.execute(
                    """INSERT INTO meeting_attendees
                       (thread_id,role,required,invited_at,checked_in_at,checkin_auth_nonce)
                       VALUES (?,?,1,?,?,?)""",
                    (thread["id"], role, now, now if role == called_by else None,
                     auth_nonce if role == supervisor and role == called_by else None),
                )
            _event(conn, thread["id"], "called", called_by, agenda, auth_nonce)
            if priority == "urgent":
                for role in sorted((roles & agent_roles) - {called_by}):
                    conn.execute(
                        """INSERT OR IGNORE INTO meeting_wake_requests
                           (thread_id,role,status,created_at) VALUES (?,?,'pending',?)""",
                        (thread["id"], role, now),
                    )
                escalation_id = _queue_escalation(
                    conn, thread["id"], called_by,
                    "urgent meeting requires off-hours wake", "auto",
                )
            missing = conn.execute(
                """SELECT COUNT(*) AS n FROM meeting_attendees
                   WHERE thread_id=? AND required=1 AND checked_in_at IS NULL""",
                (thread["id"],),
            ).fetchone()["n"]
            if not missing:
                conn.execute(
                    "UPDATE meetings SET state='active',updated_at=? WHERE thread_id=?",
                    (now, thread["id"]),
                )
                _event(conn, thread["id"], "quorum", "system", "all attendees checked in")
    if escalation_id:
        dispatch_escalation(escalation_id, db_path=db_path)
    return meeting_status(thread["id"], db_path=db_path)


def call_meeting(*, agenda: str, called_by: str, attendees: list[str] | None = None,
                 meeting_type: str = "ad-hoc", priority: str = "normal",
                 idle_minutes: int = DEFAULT_IDLE_MINUTES,
                 max_messages: int | None = None,
                 consensus_threshold: int = DEFAULT_CONSENSUS_THRESHOLD,
                 wait_timeout_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS,
                 db_path: Path | str | None = None) -> dict:
    """Agent-facing meeting call. `attendees` defaults to every enabled role."""
    supervisor = CONFIG.supervisor_role
    with connect(db_path) as conn:
        called_by = _agent_role(conn, called_by)
        roles = list(attendees) if attendees else sorted(_known_roles(conn))
    if supervisor in roles:
        raise ValueError(
            f"agents cannot add {supervisor}; use an escalation or a signed "
            f"supervisor call")
    return _call_meeting(
        agenda=agenda, called_by=called_by, attendees=roles,
        meeting_type=meeting_type, priority=priority, idle_minutes=idle_minutes,
        max_messages=max_messages, consensus_threshold=consensus_threshold,
        wait_timeout_seconds=wait_timeout_seconds, db_path=db_path,
    )


def discover(role: str, *, include_closed: bool = False,
             db_path: Path | str | None = None) -> list[dict]:
    """Every meeting `role` is invited to, with unread counts."""
    _sweep_timeouts(db_path)
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        _stamp_notifications(conn, role)
        # Refresh before the join: discovery is how a woken agent learns a meeting
        # went idle, so the deadline must be applied before thread_status is read
        # — the same rule _meeting enforces, and mailbox.list_threads before it.
        for r in conn.execute(
            """SELECT m.thread_id FROM meetings m
               JOIN meeting_attendees a ON a.thread_id=m.thread_id
               WHERE a.role=?""",
            (role,),
        ).fetchall():
            mailbox._refresh_thread(conn, r["thread_id"])
        state_filter = "" if include_closed else "AND m.state!='closed'"
        visible_sql, visible_params = _visible_message_sql(conn, "mm")
        rows = conn.execute(
            f"""SELECT m.*, a.checked_in_at, a.stopped_at, t.status AS thread_status,
                       t.max_messages-t.message_count AS messages_remaining,
                       (SELECT COUNT(*) FROM mailbox_messages mm
                        LEFT JOIN mailbox_receipts r
                          ON r.message_id=mm.id AND r.role=?
                        WHERE mm.thread_id=m.thread_id
                          AND mm.recipient IN (?, ?) AND r.message_id IS NULL
                          AND {visible_sql}) AS unread_messages,
                       (SELECT COUNT(*) FROM meeting_events e
                        WHERE e.thread_id=m.thread_id
                          AND e.id>a.last_seen_event_id) AS unread_events
                FROM meetings m JOIN meeting_attendees a ON a.thread_id=m.thread_id
                JOIN mailbox_threads t ON t.id=m.thread_id
                WHERE a.role=? {state_filter}
                ORDER BY (m.priority='urgent') DESC,m.created_at""",
            (role, role, BROADCAST, *visible_params, role),
        ).fetchall()
        return [dict(r) for r in rows]


# --- check-in / join / leave ------------------------------------------------

def _check_in(conn, thread_id: str, role: str, auth_nonce: str | None = None) -> None:
    supervisor = CONFIG.supervisor_role
    meeting = _meeting(conn, thread_id)
    if meeting["state"] in {"closed", "paused", "escalated"}:
        raise ValueError(f"cannot check in while meeting is {meeting['state']}")
    attendee = _attendee(conn, thread_id, role)
    if attendee["checked_in_at"] and not attendee["stopped_at"]:
        return
    if role == supervisor and not auth_nonce:
        raise ValueError("supervisor check-in lacks a verified assertion")
    if role == supervisor:
        _supervisor_claim(conn, auth_nonce, {"check_in"}, thread_id=thread_id)
    previous_mode = _mode(conn, thread_id)
    now = _iso()
    conn.execute(
        """UPDATE meeting_attendees SET checked_in_at=?,checkin_auth_nonce=?,stopped_at=NULL
           WHERE thread_id=? AND role=?""",
        (now, auth_nonce if role == supervisor else None, thread_id, role),
    )
    conn.execute(
        """UPDATE meeting_wake_requests SET status='acknowledged',acknowledged_at=?
           WHERE thread_id=? AND role=?""",
        (now, thread_id, role),
    )
    _event(conn, thread_id, "rejoin" if attendee["stopped_at"] else "check_in",
           role, "attendee present", auth_nonce)
    missing = conn.execute(
        """SELECT COUNT(*) AS n FROM meeting_attendees
           WHERE thread_id=? AND required=1 AND checked_in_at IS NULL
             AND stopped_at IS NULL""",
        (thread_id,),
    ).fetchone()["n"]
    if not missing and len(_active_roles(conn, thread_id)) >= 2:
        refreshed = _meeting(conn, thread_id)
        next_state = ("consensus" if refreshed["messages_remaining"] <=
                      refreshed["consensus_threshold"] else "active")
        conn.execute(
            "UPDATE meetings SET state=?,updated_at=? WHERE thread_id=?",
            (next_state, now, thread_id),
        )
        _event(conn, thread_id, "quorum", "system", "all attendees checked in")
    new_mode = _mode(conn, thread_id)
    if new_mode == "multi" and previous_mode != "multi":
        _waive_pending_obligations(conn, thread_id, "meeting changed to multi-party mode")
    if new_mode != previous_mode:
        _event(conn, thread_id, "mode_changed", "system", new_mode)


def check_in(thread_id: str, *, role: str,
             db_path: Path | str | None = None) -> dict:
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        _check_in(conn, thread_id, role)
    return meeting_status(thread_id, db_path=db_path)


def _supervisor_join(conn, thread_id: str, auth_nonce: str) -> None:
    """The supervisor may drop into a meeting it was never invited to."""
    supervisor = CONFIG.supervisor_role
    meeting = _meeting(conn, thread_id)
    if meeting["state"] in {"closed", "paused", "escalated"}:
        raise ValueError(f"cannot join while meeting is {meeting['state']}")
    _supervisor_claim(conn, auth_nonce, {"join"}, thread_id=thread_id)
    previous_mode = _mode(conn, thread_id)
    now = _iso()
    attendee = conn.execute(
        "SELECT * FROM meeting_attendees WHERE thread_id=? AND role=?",
        (thread_id, supervisor),
    ).fetchone()
    if attendee and attendee["checked_in_at"] and not attendee["stopped_at"]:
        return
    if attendee:
        conn.execute(
            """UPDATE meeting_attendees SET required=1,checked_in_at=?,
               checkin_auth_nonce=?,stopped_at=NULL WHERE thread_id=? AND role=?""",
            (now, auth_nonce, thread_id, supervisor),
        )
    else:
        conn.execute(
            """INSERT INTO meeting_attendees
               (thread_id,role,required,invited_at,checked_in_at,checkin_auth_nonce)
               VALUES (?,?,1,?,?,?)""",
            (thread_id, supervisor, now, now, auth_nonce),
        )
    _event(conn, thread_id, "join", supervisor, "supervisor joined meeting", auth_nonce)
    new_mode = _mode(conn, thread_id)
    if new_mode == "multi" and previous_mode != "multi":
        _waive_pending_obligations(
            conn, thread_id, "supervisor joined; multi-party replies are optional")
    if meeting["state"] == "waiting" and len(_active_roles(conn, thread_id)) >= 2:
        missing = conn.execute(
            """SELECT COUNT(*) AS n FROM meeting_attendees
               WHERE thread_id=? AND required=1 AND checked_in_at IS NULL
                 AND stopped_at IS NULL""",
            (thread_id,),
        ).fetchone()["n"]
        if not missing:
            conn.execute(
                "UPDATE meetings SET state='active',updated_at=? WHERE thread_id=?",
                (now, thread_id),
            )
    if new_mode != previous_mode:
        _event(conn, thread_id, "mode_changed", "system", new_mode)


def _leave(conn, thread_id: str, role: str, reason: str,
           auth_nonce: str | None = None) -> None:
    supervisor = CONFIG.supervisor_role
    meeting = _meeting(conn, thread_id)
    if meeting["state"] in {"closed", "paused", "escalated"}:
        raise ValueError(f"cannot leave while meeting is {meeting['state']}")
    _attendee(conn, thread_id, role, checked_in=True)
    if role == supervisor:
        claim = _supervisor_claim(conn, auth_nonce, {"leave"}, thread_id=thread_id)
        if claim.get("reason") != reason:
            raise ValueError("supervisor assertion leave reason mismatch")
    else:
        # Agents may not abandon a meeting the supervisor convened or is sitting
        # in, and may only leave an otherwise-quiet meeting once the whole
        # thread has gone idle (no message for its SLA window). A live meeting
        # is ended through the propose-end / confirm handshake or escalated —
        # never walked out of.
        if (meeting["called_by"] == supervisor or meeting["supervisor_auth_nonce"]
                or _has_supervisor(conn, thread_id)):
            raise ValueError(
                "cannot leave a supervisor-convened or supervisor-attended "
                "meeting; propose end or escalate instead"
            )
        idle_cutoff = _now() - dt.timedelta(seconds=meeting["wait_timeout_seconds"])
        if _thread_last_activity(conn, thread_id) > idle_cutoff:
            raise ValueError(
                "meeting thread is still active; leaving is only allowed once "
                "the whole thread is idle — propose end or escalate instead"
            )
    previous_mode = _mode(conn, thread_id)
    now = _iso()
    # Remove the attendee first so every quorum / vote tally excludes them.
    conn.execute(
        "UPDATE meeting_attendees SET stopped_at=? WHERE thread_id=? AND role=?",
        (now, thread_id, role),
    )
    _waive_pending_obligations(conn, thread_id, f"participant left: {role}")
    _event(conn, thread_id, "leave", role, _clean(reason, "reason"), auth_nonce)
    # A leaver must never stall an open termination vote. Re-tally over the
    # attendees who are still present: if they now unanimously confirm, close;
    # otherwise keep the proposal open so they can finish voting. (Rejecting the
    # proposal on any leave would force a needless re-proposal and let a
    # since-departed attendee block the decision.)
    pending = _pending_termination(conn, thread_id)
    if pending:
        if _finalize_if_unanimous(conn, thread_id, pending, role, auth_nonce):
            return
        next_state = "termination_pending"
    elif len(_active_roles(conn, thread_id)) < 2:
        next_state = "waiting"
    else:
        refreshed = _meeting(conn, thread_id)
        next_state = ("consensus" if refreshed["messages_remaining"] <=
                      refreshed["consensus_threshold"] else "active")
    conn.execute(
        "UPDATE meetings SET state=?,updated_at=? WHERE thread_id=?",
        (next_state, now, thread_id),
    )
    new_mode = _mode(conn, thread_id)
    if new_mode != previous_mode:
        _event(conn, thread_id, "mode_changed", "system", new_mode)


def leave_meeting(thread_id: str, *, role: str, reason: str,
                  db_path: Path | str | None = None) -> dict:
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        _leave(conn, thread_id, role, reason)
    return meeting_status(thread_id, db_path=db_path)


# --- reading ----------------------------------------------------------------

def _meeting_updates(thread_id: str, *, role: str, mark_read: bool = False,
                     auth_nonce: str | None = None,
                     db_path: Path | str | None = None) -> dict:
    _sweep_timeouts(db_path)
    with connect(db_path, write=mark_read) as conn:
        if role not in _meeting_roles(conn):
            raise ValueError(f"invalid meeting role: {role}")
        if role == CONFIG.supervisor_role:
            _supervisor_claim(conn, auth_nonce, {"read"}, thread_id=thread_id)
        attendee = _attendee(conn, thread_id, role, checked_in=True)
        visible_sql, visible_params = _visible_message_sql(conn, "mm")
        messages = conn.execute(
            f"""SELECT mm.* FROM mailbox_messages mm
                LEFT JOIN mailbox_receipts r ON r.message_id=mm.id AND r.role=?
                WHERE mm.thread_id=? AND mm.recipient IN (?, ?)
                  AND r.message_id IS NULL
                  AND {visible_sql}
                ORDER BY mm.id""",
            (role, thread_id, role, BROADCAST, *visible_params),
        ).fetchall()
        events = conn.execute(
            """SELECT * FROM meeting_events
               WHERE thread_id=? AND id>? ORDER BY id""",
            (thread_id, attendee["last_seen_event_id"]),
        ).fetchall()
        if mark_read:
            now = _iso()
            conn.executemany(
                """INSERT OR IGNORE INTO mailbox_receipts(message_id,role,read_at)
                   VALUES (?,?,?)""",
                [(m["id"], role, now) for m in messages],
            )
            max_event = max([e["id"] for e in events],
                            default=attendee["last_seen_event_id"])
            conn.execute(
                """UPDATE meeting_attendees SET last_seen_event_id=?
                   WHERE thread_id=? AND role=?""",
                (max_event, thread_id, role),
            )
        return {
            "meeting": dict(_meeting(conn, thread_id)),
            "messages": [dict(m) for m in messages],
            "events": [dict(e) for e in events],
        }


def meeting_updates(thread_id: str, *, role: str, mark_read: bool = False,
                    db_path: Path | str | None = None) -> dict:
    with connect(db_path) as conn:
        role = _agent_role(conn, role)
    return _meeting_updates(
        thread_id, role=role, mark_read=mark_read, db_path=db_path,
    )


def wait_for_updates(thread_id: str, *, role: str, wait_seconds: int = 0,
                     mark_read: bool = False,
                     db_path: Path | str | None = None) -> dict:
    """Short bounded poll. Deliberately capped: an agent that wants to wait
    longer should end its turn and let the orchestrator wake it."""
    import time
    if not 0 <= wait_seconds <= MAX_WAIT_SECONDS:
        raise ValueError(
            f"wait_seconds must be between 0 and {MAX_WAIT_SECONDS}; "
            f"continue unrelated work")
    deadline = time.monotonic() + wait_seconds
    while True:
        out = meeting_updates(thread_id, role=role, mark_read=mark_read, db_path=db_path)
        if out["messages"] or out["events"] or time.monotonic() >= deadline:
            return out
        time.sleep(min(2, max(0, deadline - time.monotonic())))


# --- speaking ---------------------------------------------------------------

def _send_update(conn, thread_id: str, role: str, body: str, kind: str,
                 auth_nonce: str | None = None,
                 reply_to: int | None = None) -> tuple[int, int | None]:
    supervisor = CONFIG.supervisor_role
    meeting = _meeting(conn, thread_id)
    _attendee(conn, thread_id, role, checked_in=True)
    if role == supervisor and not auth_nonce:
        raise ValueError("supervisor update lacks a verified assertion")
    if role == supervisor:
        # Bind the assertion to this exact message: body, reply target and kind
        # must all be what was signed, so a captured assertion cannot be reused
        # to say something else.
        expected_action = "position" if kind == "position" else "send"
        claim = _supervisor_claim(conn, auth_nonce, {expected_action}, thread_id=thread_id)
        if claim.get("body") != body:
            raise ValueError("supervisor assertion body mismatch")
        if claim.get("reply_to") != reply_to:
            raise ValueError("supervisor assertion reply target mismatch")
        if expected_action == "send" and claim.get("kind", "decision") != kind:
            raise ValueError("supervisor assertion message kind mismatch")
    active_roles = _active_roles(conn, thread_id)
    mode = _mode(conn, thread_id)
    # The supervisor may seed context before the meeting formally starts (still
    # `waiting`, quorum not yet met). Such a preamble is a broadcast every later
    # joiner reads on check-in; agents still cannot open discussion early.
    preamble = role == supervisor and meeting["state"] == "waiting"
    if not preamble:
        if len(active_roles) < 2:
            raise ValueError("meeting needs at least two active attendees before discussion")
        if meeting["state"] not in {"active", "consensus"}:
            raise ValueError(f"meeting does not accept updates while {meeting['state']}")
        if meeting["state"] == "consensus" and kind not in {"position", "decision"}:
            raise ValueError(
                "consensus mode accepts only one position per attendee or a decision")
    if kind == "position":
        prior = conn.execute(
            """SELECT 1 FROM mailbox_messages
               WHERE thread_id=? AND sender=? AND kind='position'""",
            (thread_id, role),
        ).fetchone()
        if prior:
            raise ValueError(f"{role} already submitted its consensus position")
    elif kind not in UPDATE_KINDS:
        raise ValueError(f"invalid meeting update kind: {kind}")
    if reply_to is not None:
        original = conn.execute(
            "SELECT * FROM mailbox_messages WHERE id=? AND thread_id=?",
            (reply_to, thread_id),
        ).fetchone()
        if not original or original["sender"] == role:
            raise ValueError("reply target must be another attendee's meeting message")
        if original["recipient"] not in {role, BROADCAST}:
            raise ValueError("reply target was not addressed to this attendee")
        recipient = original["sender"]
    elif preamble:
        recipient = BROADCAST
    elif mode == "one_to_one":
        # Strict turn-taking: an outstanding obligation must be discharged
        # before anyone speaks again, so neither side can talk past the other.
        pending = conn.execute(
            """SELECT o.*,mm.sender FROM meeting_response_obligations o
               JOIN mailbox_messages mm ON mm.id=o.message_id
               WHERE o.thread_id=? AND o.status='pending' ORDER BY o.message_id LIMIT 1""",
            (thread_id,),
        ).fetchone()
        if pending:
            if pending["owed_by"] == role:
                raise ValueError(
                    f"one-to-one meeting requires a reply to message #{pending['message_id']}"
                )
            raise ValueError(
                f"await the required response to message #{pending['message_id']} "
                f"before sending again"
            )
        recipient = next(r for r in active_roles if r != role)
    else:
        recipient = BROADCAST
    # _insert_message documents that callers "must ... have refreshed `thread`";
    # handing it a raw row is what let a meeting write past its idle deadline.
    thread = mailbox._refresh_thread(conn, thread_id)
    message_id = mailbox._insert_message(
        conn, thread, sender=role, recipient=recipient, kind=kind,
        body=_clean(body, "message"), reply_to=reply_to,
        allow_authenticated_supervisor=(role == supervisor),
        allow_reference_reply=(reply_to is not None),
    )
    now = _iso()
    conn.execute(
        "INSERT OR IGNORE INTO mailbox_receipts(message_id,role,read_at) VALUES (?,?,?)",
        (message_id, role, now),
    )
    if role == supervisor:
        # This row is what makes the message readable at all — see
        # _visible_message_sql.
        conn.execute(
            "INSERT INTO meeting_message_auth(message_id,auth_nonce) VALUES (?,?)",
            (message_id, auth_nonce),
        )
    if reply_to is not None:
        conn.execute(
            """UPDATE meeting_response_obligations
               SET status='resolved',resolved_at=?,resolution='explicit reply',
                   resolved_by_message_id=?
               WHERE message_id=? AND owed_by=? AND status='pending'""",
            (now, message_id, reply_to, role),
        )
    elif mode == "one_to_one" and not preamble:
        due_at = _iso(_now() + dt.timedelta(seconds=meeting["wait_timeout_seconds"]))
        conn.execute(
            """INSERT INTO meeting_response_obligations
               (message_id,thread_id,owed_by,status,due_at,created_at)
               VALUES (?,?,?,'pending',?,?)""",
            (message_id, thread_id, recipient, due_at, now),
        )
    current = _meeting(conn, thread_id)
    escalation_id = None
    if (meeting["state"] == "active" and
            current["messages_remaining"] <= meeting["consensus_threshold"]):
        conn.execute(
            "UPDATE meetings SET state='consensus',updated_at=? WHERE thread_id=?",
            (now, thread_id),
        )
        _event(conn, thread_id, "consensus_mode", "system",
               f"{current['messages_remaining']} normal messages remain")
        if not _has_supervisor(conn, thread_id):
            escalation_id = _queue_escalation(
                conn, thread_id, "system",
                "meeting entered consensus mode with the supervisor absent", "auto",
            )
            conn.execute(
                "UPDATE meetings SET auto_escalated_at=? WHERE thread_id=?",
                (now, thread_id),
            )
    return message_id, escalation_id


def send_update(thread_id: str, *, role: str, body: str, kind: str = "evidence",
                reply_to: int | None = None,
                db_path: Path | str | None = None) -> dict:
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        message_id, escalation_id = _send_update(
            conn, thread_id, role, body, kind, reply_to=reply_to,
        )
    if escalation_id:
        dispatch_escalation(escalation_id, db_path=db_path)
    status = meeting_status(thread_id, db_path=db_path)
    out = {"message_id": message_id, "meeting": status}
    if status["meeting"]["state"] in {"active", "consensus"}:
        out["next"] = (
            f"meeting still open: run `{PROJECT_NAME} meeting updates ... "
            f"--mark-read --wait-seconds {MAX_WAIT_SECONDS}` once more before "
            f"ending this session; unread messages past the SLA trigger a wake "
            f"request + escalation"
        )
    return out


def submit_position(thread_id: str, *, role: str, body: str,
                    reply_to: int | None = None,
                    db_path: Path | str | None = None) -> dict:
    return send_update(
        thread_id, role=role, body=body, kind="position",
        reply_to=reply_to, db_path=db_path,
    )


# --- termination handshake --------------------------------------------------

def _pending_termination(conn, thread_id: str):
    return conn.execute(
        """SELECT * FROM meeting_terminations
           WHERE thread_id=? AND status='pending' ORDER BY id DESC LIMIT 1""",
        (thread_id,),
    ).fetchone()


def _propose_end(conn, thread_id: str, role: str, resolution: str,
                 auth_nonce: str | None = None) -> int:
    supervisor = CONFIG.supervisor_role
    meeting = _meeting(conn, thread_id)
    _attendee(conn, thread_id, role, checked_in=True)
    if meeting["state"] not in {"active", "consensus"}:
        raise ValueError(f"cannot propose termination while meeting is {meeting['state']}")
    if role == supervisor and not auth_nonce:
        raise ValueError("supervisor proposal lacks a verified assertion")
    if role == supervisor:
        claim = _supervisor_claim(conn, auth_nonce, {"propose_end"}, thread_id=thread_id)
        if claim.get("resolution") != resolution:
            raise ValueError("supervisor assertion resolution mismatch")
    _resolve_obligations(
        conn, thread_id, role, resolution="termination proposal answered pending update",
    )
    now = _iso()
    cursor = conn.execute(
        """INSERT INTO meeting_terminations
           (thread_id,proposer,resolution,status,auth_nonce,created_at)
           VALUES (?,?,?,'pending',?,?)""",
        (thread_id, role, _clean(resolution, "resolution"), auth_nonce, now),
    )
    proposal_id = int(cursor.lastrowid)
    # The proposer implicitly confirms its own proposal; requiring it to vote
    # again would just be ceremony.
    conn.execute(
        """INSERT INTO meeting_termination_votes
           (proposal_id,role,vote,auth_nonce,voted_at) VALUES (?,?,'confirm',?,?)""",
        (proposal_id, role, auth_nonce if role == supervisor else None, now),
    )
    conn.execute(
        "UPDATE meetings SET state='termination_pending',updated_at=? WHERE thread_id=?",
        (now, thread_id),
    )
    _event(conn, thread_id, "termination_proposed", role,
           f"proposal #{proposal_id}: {resolution}", auth_nonce)
    return proposal_id


def propose_end(thread_id: str, *, role: str, resolution: str,
                db_path: Path | str | None = None) -> dict:
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        proposal_id = _propose_end(conn, thread_id, role, resolution)
    return {"proposal_id": proposal_id,
            "meeting": meeting_status(thread_id, db_path=db_path)}


def _close_meeting(conn, thread_id: str, resolution: str, actor: str,
                   auth_nonce: str | None = None) -> None:
    now = _iso()
    conn.execute(
        "UPDATE meetings SET state='closed',updated_at=? WHERE thread_id=?",
        (now, thread_id),
    )
    conn.execute(
        "UPDATE meeting_attendees SET stopped_at=? WHERE thread_id=?",
        (now, thread_id),
    )
    conn.execute(
        """UPDATE mailbox_threads SET status='closed',stop_reason=?,stopped_by=?,
           updated_at=? WHERE id=?""",
        (resolution, actor, now, thread_id),
    )
    _event(conn, thread_id, "closed", actor, resolution, auth_nonce)


def _finalize_if_unanimous(conn, thread_id: str, proposal, actor: str,
                           auth_nonce: str | None = None) -> bool:
    """Close the meeting if every ACTIVE required attendee has confirmed the
    pending termination. Attendees who have left (stopped_at set) are excluded
    from both the numerator and denominator, so a departed participant never
    blocks the remaining ones' decision. Re-run this whenever the active set
    changes (a vote is cast, or someone leaves)."""
    required = conn.execute(
        """SELECT COUNT(*) AS n FROM meeting_attendees
           WHERE thread_id=? AND required=1 AND checked_in_at IS NOT NULL
             AND stopped_at IS NULL""",
        (thread_id,),
    ).fetchone()["n"]
    confirms = conn.execute(
        """SELECT COUNT(*) AS n FROM meeting_termination_votes
           WHERE proposal_id=? AND vote='confirm'
             AND role IN (SELECT role FROM meeting_attendees
                          WHERE thread_id=? AND required=1
                            AND checked_in_at IS NOT NULL AND stopped_at IS NULL)""",
        (proposal["id"], thread_id),
    ).fetchone()["n"]
    if required >= 1 and confirms == required:
        conn.execute(
            "UPDATE meeting_terminations SET status='accepted',resolved_at=? WHERE id=?",
            (_iso(), proposal["id"]),
        )
        _close_meeting(conn, thread_id, proposal["resolution"], actor, auth_nonce)
        return True
    return False


def _vote_end(conn, thread_id: str, role: str, vote: str, reason: str | None,
              auth_nonce: str | None = None) -> bool:
    supervisor = CONFIG.supervisor_role
    _attendee(conn, thread_id, role, checked_in=True)
    proposal = _pending_termination(conn, thread_id)
    if not proposal:
        raise ValueError("meeting has no pending termination proposal")
    if role == supervisor and not auth_nonce:
        raise ValueError("supervisor vote lacks a verified assertion")
    if role == supervisor:
        expected_action = "confirm_end" if vote == "confirm" else "reject_end"
        claim = _supervisor_claim(conn, auth_nonce, {expected_action}, thread_id=thread_id)
        if claim.get("proposal_id") != proposal["id"]:
            raise ValueError(
                "supervisor assertion is bound to a different termination proposal")
        if expected_action == "reject_end" and claim.get("reason") != reason:
            raise ValueError("supervisor assertion rejection reason mismatch")
    _resolve_obligations(
        conn, thread_id, role, resolution=f"termination {vote} answered pending update",
    )
    now = _iso()
    conn.execute(
        """INSERT OR REPLACE INTO meeting_termination_votes
           (proposal_id,role,vote,reason,auth_nonce,voted_at) VALUES (?,?,?,?,?,?)""",
        (proposal["id"], role, vote, reason,
         auth_nonce if role == supervisor else None, now),
    )
    _event(conn, thread_id, f"termination_{vote}", role,
           reason or f"proposal #{proposal['id']}", auth_nonce)
    if vote == "reject":
        conn.execute(
            "UPDATE meeting_terminations SET status='rejected',resolved_at=? WHERE id=?",
            (now, proposal["id"]),
        )
        meeting = _meeting(conn, thread_id)
        next_state = ("consensus" if meeting["messages_remaining"] <=
                      meeting["consensus_threshold"] else "active")
        conn.execute(
            "UPDATE meetings SET state=?,updated_at=? WHERE thread_id=?",
            (next_state, now, thread_id),
        )
        if next_state == "consensus" and not _has_supervisor(conn, thread_id):
            # Left queued rather than dispatched: a rejected end near the budget
            # is a standing condition for the console, not a page.
            _queue_escalation(
                conn, thread_id, "system",
                f"termination proposal #{proposal['id']} rejected near message limit",
                "auto",
            )
        return False
    return _finalize_if_unanimous(conn, thread_id, proposal, role, auth_nonce)


def confirm_end(thread_id: str, *, role: str,
                db_path: Path | str | None = None) -> dict:
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        closed = _vote_end(conn, thread_id, role, "confirm", None)
    return {"closed": closed, "meeting": meeting_status(thread_id, db_path=db_path)}


def reject_end(thread_id: str, *, role: str, reason: str,
               db_path: Path | str | None = None) -> dict:
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        _vote_end(conn, thread_id, role, "reject", _clean(reason, "reason"))
    return {"closed": False, "meeting": meeting_status(thread_id, db_path=db_path)}


# --- pause / escalate -------------------------------------------------------

def pause_meeting(thread_id: str, *, role: str, reason: str,
                  db_path: Path | str | None = None) -> dict:
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        _attendee(conn, thread_id, role, checked_in=True)
        now = _iso()
        conn.execute(
            "UPDATE meetings SET state='paused',updated_at=? WHERE thread_id=?",
            (now, thread_id),
        )
        conn.execute(
            """UPDATE mailbox_threads SET status='paused',stop_reason=?,stopped_by=?,
               updated_at=? WHERE id=?""",
            (_clean(reason, "reason"), role, now, thread_id),
        )
        _event(conn, thread_id, "paused", role, reason)
    return meeting_status(thread_id, db_path=db_path)


def escalate_meeting(thread_id: str, *, role: str, reason: str,
                     channel: str = "auto", pause: bool = True,
                     db_path: Path | str | None = None) -> dict:
    """Hand the meeting to a human. `channel` is `auto` (every available
    registered channel), `outbox`, or a channel the host registered."""
    valid = {"auto", OUTBOX_CHANNEL} | set(_CHANNELS)
    if channel not in valid:
        raise ValueError(
            f"invalid escalation channel: {channel} (known: {', '.join(sorted(valid))})")
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        _attendee(conn, thread_id, role, checked_in=True)
        escalation_id = _queue_escalation(
            conn, thread_id, role, _clean(reason, "reason"), channel,
        )
        if pause:
            now = _iso()
            conn.execute(
                "UPDATE meetings SET state='escalated',updated_at=? WHERE thread_id=?",
                (now, thread_id),
            )
            conn.execute(
                """UPDATE mailbox_threads SET status='escalated',stop_reason=?,
                   stopped_by=?,updated_at=? WHERE id=?""",
                (reason, role, now, thread_id),
            )
    dispatched = dispatch_escalation(escalation_id, db_path=db_path)
    return {"escalation": dispatched,
            "meeting": meeting_status(thread_id, db_path=db_path)}


# --- wake requests ----------------------------------------------------------

def wake_requests(role: str, *, db_path: Path | str | None = None) -> list[dict]:
    """Pending meeting-driven wakes for `role`. The wake driver reads this."""
    with connect(db_path) as conn:
        role = _agent_role(conn, role)
        return [dict(r) for r in conn.execute(
            """SELECT w.*,m.agenda,m.priority FROM meeting_wake_requests w
               JOIN meetings m ON m.thread_id=w.thread_id
               WHERE w.role=? AND w.status='pending' ORDER BY w.created_at""",
            (role,),
        ).fetchall()]


def acknowledge_wake(thread_id: str, *, role: str,
                     db_path: Path | str | None = None) -> dict:
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        cursor = conn.execute(
            """UPDATE meeting_wake_requests SET status='acknowledged',acknowledged_at=?
               WHERE thread_id=? AND role=? AND status='pending'""",
            (_iso(), thread_id, role),
        )
        if not cursor.rowcount:
            raise ValueError("no pending wake request for this role/meeting")
    return {"thread_id": thread_id, "role": role, "status": "acknowledged"}


# --- views ------------------------------------------------------------------

def meeting_status(thread_id: str, *, db_path: Path | str | None = None,
                   sweep: bool = True) -> dict:
    if sweep:
        _sweep_timeouts(db_path)
    with connect(db_path) as conn:
        meeting = dict(_meeting(conn, thread_id))
        attendees = [dict(r) for r in conn.execute(
            """SELECT role,required,invited_at,checked_in_at,last_seen_event_id,stopped_at
               FROM meeting_attendees WHERE thread_id=? ORDER BY role""",
            (thread_id,),
        ).fetchall()]
        proposal = _pending_termination(conn, thread_id)
        votes = []
        if proposal:
            votes = [dict(r) for r in conn.execute(
                """SELECT role,vote,reason,voted_at FROM meeting_termination_votes
                   WHERE proposal_id=? ORDER BY role""",
                (proposal["id"],),
            ).fetchall()]
        obligations = [dict(r) for r in conn.execute(
            """SELECT o.*,mm.sender,mm.kind,mm.body FROM meeting_response_obligations o
               JOIN mailbox_messages mm ON mm.id=o.message_id
               WHERE o.thread_id=? ORDER BY o.message_id""",
            (thread_id,),
        ).fetchall()]
        return {"meeting": meeting, "attendees": attendees,
                "mode": _mode(conn, thread_id),
                "termination": dict(proposal) if proposal else None, "votes": votes,
                "response_obligations": obligations}


def list_meetings(*, include_closed: bool = False,
                  db_path: Path | str | None = None) -> list[dict]:
    _sweep_timeouts(db_path)
    with connect(db_path) as conn:
        sql = "SELECT thread_id FROM meetings"
        if not include_closed:
            sql += " WHERE state!='closed'"
        sql += " ORDER BY (priority='urgent') DESC,created_at DESC"
        ids = [r["thread_id"] for r in conn.execute(sql).fetchall()]
    return [meeting_status(i, db_path=db_path, sweep=False) for i in ids]


def meeting_transcript(thread_id: str, *,
                       db_path: Path | str | None = None) -> dict:
    """Read-only audit view; it does not claim a role or mark receipts."""
    _sweep_timeouts(db_path)
    with connect(db_path) as conn:
        _meeting(conn, thread_id)
        # Deliberately laxer than _visible_message_sql: an audit view shows
        # everything an agent said, including messages sent before a later
        # check-in bookkeeping change. The supervisor still needs its auth row —
        # an unauthenticated row must never appear to speak as the supervisor,
        # not even in the transcript.
        sender_in, sender_params = _in_clause("mm.sender", sorted(_known_roles(conn)))
        messages = [dict(r) for r in conn.execute(
            f"""SELECT mm.* FROM mailbox_messages mm
                WHERE mm.thread_id=?
                  AND ({sender_in}
                       OR (mm.sender=? AND EXISTS
                           (SELECT 1 FROM meeting_message_auth ma
                            WHERE ma.message_id=mm.id)))
                ORDER BY mm.id""",
            (thread_id, *sender_params, CONFIG.supervisor_role),
        ).fetchall()]
        # Per-message delivery state so the console can show notified-but-unread
        # apart from read. read supersedes notified for the same (message, role).
        read_state: dict[int, dict[str, dict]] = {}
        for n in conn.execute(
            """SELECT n.message_id, n.role, n.notified_at FROM mailbox_notifications n
               JOIN mailbox_messages mm ON mm.id=n.message_id
               WHERE mm.thread_id=?""",
            (thread_id,),
        ).fetchall():
            read_state.setdefault(n["message_id"], {})[n["role"]] = {
                "state": "notified", "at": n["notified_at"]}
        for rc in conn.execute(
            """SELECT rc.message_id, rc.role, rc.read_at FROM mailbox_receipts rc
               JOIN mailbox_messages mm ON mm.id=rc.message_id
               WHERE mm.thread_id=?""",
            (thread_id,),
        ).fetchall():
            read_state.setdefault(rc["message_id"], {})[rc["role"]] = {
                "state": "read", "at": rc["read_at"]}
        for m in messages:
            m["read_state"] = read_state.get(m["id"], {})
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM meeting_events WHERE thread_id=? ORDER BY id",
            (thread_id,),
        ).fetchall()]
        escalations = [dict(r) for r in conn.execute(
            "SELECT * FROM meeting_escalations WHERE thread_id=? ORDER BY id DESC",
            (thread_id,),
        ).fetchall()]
    return {
        "status": meeting_status(thread_id, db_path=db_path, sweep=False),
        "messages": messages,
        "events": events,
        "escalations": escalations,
    }


# --- supervisor adapter -----------------------------------------------------
# Verification lives in deskd.auth; this is only the dispatch from a *verified*
# claim to the meeting operation it names.

def _apply_supervisor_payload(verified: auth.VerifiedAssertion, *,
                              db_path: Path | str | None = None) -> dict:
    supervisor = CONFIG.supervisor_role
    # Burn the nonce in its own committed transaction BEFORE running the action.
    # If the action then fails, the nonce stays spent: a rejected assertion must
    # never become replayable by failing on purpose.
    with connect(db_path, write=True) as conn:
        auth.consume_nonce(verified, conn=conn)
    payload = verified.payload
    action = verified.action
    nonce = verified.nonce
    if action == "call":
        if "attendees" not in payload:
            raise ValueError("supervisor call assertion must name its attendees")
        return _call_meeting(
            agenda=payload["agenda"], called_by=supervisor,
            attendees=list(payload["attendees"]),
            meeting_type=payload.get("meeting_type", "ad-hoc"),
            priority=payload.get("priority", "urgent"),
            idle_minutes=int(payload.get("idle_minutes", DEFAULT_IDLE_MINUTES)),
            max_messages=payload.get("max_messages"),
            consensus_threshold=int(
                payload.get("consensus_threshold", DEFAULT_CONSENSUS_THRESHOLD)),
            wait_timeout_seconds=int(
                payload.get("wait_timeout_seconds", DEFAULT_WAIT_TIMEOUT_SECONDS)),
            auth_nonce=nonce, db_path=db_path,
        )
    thread_id = payload["meeting_id"]
    if action == "read":
        return _meeting_updates(
            thread_id, role=supervisor, mark_read=bool(payload.get("mark_read", True)),
            auth_nonce=nonce, db_path=db_path,
        )
    with connect(db_path, write=True) as conn:
        if action == "join":
            _supervisor_join(conn, thread_id, nonce)
            result = {"joined": True}
        elif action == "leave":
            _leave(conn, thread_id, supervisor, _clean(payload["reason"], "reason"), nonce)
            result = {"left": True}
        elif action == "check_in":
            _check_in(conn, thread_id, supervisor, nonce)
            result = {"checked_in": True}
        elif action in {"send", "position"}:
            kind = "position" if action == "position" else payload.get("kind", "decision")
            message_id, escalation_id = _send_update(
                conn, thread_id, supervisor, payload["body"], kind, nonce,
                reply_to=payload.get("reply_to"),
            )
            result = {"message_id": message_id, "escalation_id": escalation_id}
        elif action == "propose_end":
            result = {"proposal_id": _propose_end(
                conn, thread_id, supervisor, payload["resolution"], nonce,
            )}
        elif action in {"confirm_end", "reject_end"}:
            vote = "confirm" if action == "confirm_end" else "reject"
            result = {"closed": _vote_end(
                conn, thread_id, supervisor, vote, payload.get("reason"), nonce,
            )}
        elif action == "resume":
            meeting = _meeting(conn, thread_id)
            next_state = ("consensus" if meeting["messages_remaining"] <=
                          meeting["consensus_threshold"] else "active")
            now = _iso()
            conn.execute(
                "UPDATE meetings SET state=?,updated_at=? WHERE thread_id=?",
                (next_state, now, thread_id),
            )
            # Reopening must also grant a fresh idle window, as every mailbox
            # status write does. A thread paused ON the deadline still carries an
            # expires_at in the past, so leaving it would have the next read
            # retire the meeting again immediately — resumption would be a no-op
            # and the supervisor's override decorative.
            thread = mailbox._refresh_thread(conn, thread_id)
            conn.execute(
                """UPDATE mailbox_threads SET status='open',stop_reason=NULL,
                   stopped_by=NULL,updated_at=?,expires_at=? WHERE id=?""",
                (now, mailbox._deadline(_now(), thread["idle_minutes"]), thread_id),
            )
            _event(conn, thread_id, "resumed", supervisor,
                   payload.get("reason", "supervisor resumed"), nonce)
            result = {"resumed": True}
        elif action == "force_close":
            _close_meeting(conn, thread_id, _clean(payload["reason"], "reason"),
                           supervisor, nonce)
            result = {"closed": True}
        else:
            raise ValueError(f"unimplemented supervisor action: {action}")
    result["meeting"] = meeting_status(thread_id, db_path=db_path)
    return result


def apply_supervisor_assertion_bytes(assertion: bytes, signature_input: bytes, *,
                                     db_path: Path | str | None = None) -> dict:
    """Verify a signed supervisor assertion and apply the meeting action it
    names. Strict (trusted-device) mode."""
    verified = auth.verify_bytes(
        assertion, signature_input, actions=SUPERVISOR_ACTIONS,
        required_fields=REQUIRED_SIGNED_FIELDS)
    return _apply_supervisor_payload(verified, db_path=db_path)


def apply_supervisor_assertion(assertion_path: str | Path, signature_path: str | Path,
                               *, db_path: Path | str | None = None) -> dict:
    return apply_supervisor_assertion_bytes(
        Path(assertion_path).read_bytes(), Path(signature_path).read_bytes(),
        db_path=db_path,
    )


def apply_simple_supervisor_action(action_payload: dict, *,
                                   db_path: Path | str | None = None) -> dict:
    """Apply a web-authenticated supervisor action in explicit simplified mode.

    The caller (the web adapter) has already gated on the access code; auth
    mints the short-lived, single-use claim so the ledger, the nonce burn, and
    every assertion binding check behave exactly as in strict mode.
    """
    verified = auth.mint_simple(
        action_payload, actions=SUPERVISOR_ACTIONS,
        required_fields=REQUIRED_SIGNED_FIELDS)
    return _apply_supervisor_payload(verified, db_path=db_path)
