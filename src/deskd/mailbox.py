"""Durable multi-agent mailbox and bounded review workflow.

The mailbox is the engine's transport: it moves threads, messages, receipts and
review artifacts between agent sessions that are scheduled independently and
never share a process. It coordinates agents; it never performs any action on
their behalf. A host application layers its own domain on top.

Three properties drive the design, and every change must preserve them:

*   **Durability across sessions.** Agents are cold-started processes with no
    shared memory. SQLite in WAL mode is the handoff point: a message written by
    a session that has since exited is still there for the next one. Writers
    take ``BEGIN IMMEDIATE`` so two sessions racing on the same thread serialize
    at the database instead of interleaving reads and writes.

*   **Bounded conversation.** An unbounded mailbox is an agent loop: two agents
    politely acknowledging each other forever, burning tokens and producing
    nothing. Every thread therefore carries an *idle deadline* and a *message
    budget*, and reviews additionally carry a *discussion budget*. When a budget
    is exhausted the thread stops itself. Progress must come from the budget
    being spent, never from an agent choosing to stop.

*   **No duplicate work.** ``open_thread`` is idempotent on (kind, subject) and
    message bodies are deduplicated by hash, because a woken agent that cannot
    tell whether it already spoke will speak again.

Role-agnostic by construction
-----------------------------
This module hardcodes no roles. The ``agent_registry`` table (owned by the
orchestration module) is the source of truth for which roles exist; when that
table is absent — a mailbox-only deployment — the roles configured on
``CONFIG`` are used instead. Every role that reaches SQL does so as a bound
placeholder, never interpolated into a statement.

Consequently the review workflow generalizes past the two-party shape it was
extracted from: instead of one boolean column per role, agreement lives in the
``thread_agreements`` table (one row per agreeing role), and a review advances
when *every participant* has submitted — where the participants are the
checked-in attendees of the wrapping meeting, or the whole registry if the
thread stands alone.

The supervisor is not an agent. ``CONFIG.supervisor_role`` is rejected by every
agent-facing entry point here; supervisor-authored messages may only be
inserted by the meetings module, which first verifies an Ed25519 assertion and
records the nonce that authorized them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import CONFIG

#: Recipient token addressing every participant of a thread rather than one
#: role. Stored verbatim in ``mailbox_messages.recipient``, so the word itself
#: is part of the on-disk contract the meetings module reads and writes.
BROADCAST = "all"

#: The two-party spelling this token carried while the engine had exactly two
#: roles. A thread has N participants, so ``both`` was a claim about the host's
#: shape that the review workflow has since outgrown (see the module docstring).
#: Kept as a READ alias only: ``_migrate`` rewrites stored rows to ``BROADCAST``
#: and no code path writes it, so the alias cannot reintroduce the fossil.
_LEGACY_BROADCAST = "both"
BROADCAST_ALIASES = frozenset({BROADCAST, _LEGACY_BROADCAST})

#: Engine-owned vocabularies: closed sets, enforced in Python here and backed by
#: a CHECK in the DDL below. Every literal is a branch in *this module* —
#: ``review`` selects the phase machine in ``open_thread``, and
#: ``report``/``review``/``final`` are literally the keys of ``_STAGE_PHASE`` —
#: so a host word like ``incident`` or ``postmortem`` would name a state the
#: engine has no code for. They are deliberately NOT a host seam; see the note
#: above SCHEMA for why that does not contradict the role rule.
THREAD_KINDS = frozenset({"live", "review"})
THREAD_STATUSES = frozenset({"open", "paused", "closed", "escalated"})
REVIEW_STAGES = ("report", "review", "final")

#: Message kinds. Deliberately generic; a host that needs another verb should
#: prefer reusing one of these over teaching the engine a domain word.
MESSAGE_KINDS = frozenset({
    "note", "evidence", "question", "answer", "proposal", "decision", "alert",
    "report", "review", "discussion", "position", "control",
})

# Phases of a review thread, in order. A review walks: every participant files a
# report -> every participant reviews the others -> bounded discussion ->
# finalize. `phase` is advanced only by this module, never by an agent.
_STAGE_PHASE = {"report": "reports", "review": "cross_review",
                "final": "ready_to_finalize"}
_STAGE_NEXT_PHASE = {"report": "cross_review", "review": "discussion"}

# Which vocabularies may be frozen into a CHECK, stated as a rule rather than as
# a list of what happens not to be here. The previous note claimed only that "no
# CHECK enumerates roles or sources" — true, but silent about the kind/status/
# stage CHECKs directly below it, so it read as a blanket ban that the very next
# lines appeared to break. The scoping *was* the loophole those three came in
# through, so the rule now names both halves:
#
# An ENGINE vocabulary MAY live in a CHECK. `kind`, `status` and `stage` name
# states this module's own code branches on (THREAD_KINDS / THREAD_STATUSES /
# REVIEW_STAGES above). Freezing them into the database file freezes nothing a
# host owns: the engine already refuses these words in Python, where it can say
# which word was wrong, and the CHECK is only the backstop for a writer that
# bypasses this module. Widening one is an engine change with code behind it,
# not configuration.
#
# A HOST vocabulary MUST NOT. No CHECK below names a role, sender, recipient or
# provenance kind — that is orchestration.py's principle ("a CHECK would freeze
# one host's vocabulary into every host's database file"), and it holds because
# those words come from CONFIG and the registry, which no DDL can see. They are
# validated in Python against the registry instead.
#
# Corollary for migrations: orchestration's `_has_enum_check`/`_rebuild` exist to
# STRIP host-vocabulary CHECKs off legacy tables. They must never be pointed at
# mailbox_threads.kind/status or review_artifacts.stage — those CHECKs are the
# intended shape, not legacy damage, and rebuilding them away would delete the
# backstop on the engine's own state machine.
SCHEMA = """
CREATE TABLE IF NOT EXISTS mailbox_threads (
    id                    TEXT PRIMARY KEY,
    kind                  TEXT NOT NULL CHECK (kind IN ('live', 'review')),
    subject               TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('open', 'paused', 'closed', 'escalated')),
    phase                 TEXT NOT NULL,
    owner_role            TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    expires_at            TEXT NOT NULL,
    idle_minutes          INTEGER NOT NULL CHECK (idle_minutes > 0),
    max_messages          INTEGER NOT NULL CHECK (max_messages > 1),
    message_count         INTEGER NOT NULL DEFAULT 0,
    max_discussion        INTEGER NOT NULL DEFAULT 0,
    discussion_count      INTEGER NOT NULL DEFAULT 0,
    stop_reason           TEXT,
    stopped_by            TEXT
);

CREATE TABLE IF NOT EXISTS mailbox_messages (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id             TEXT NOT NULL REFERENCES mailbox_threads(id) ON DELETE CASCADE,
    sender                TEXT NOT NULL,
    recipient             TEXT NOT NULL,
    kind                  TEXT NOT NULL,
    body                  TEXT NOT NULL,
    artifact_path         TEXT,
    body_hash             TEXT NOT NULL,
    requires_reply        INTEGER NOT NULL DEFAULT 0,
    reply_to              INTEGER REFERENCES mailbox_messages(id),
    resolved_at           TEXT,
    created_at            TEXT NOT NULL
);

-- Idempotency backstop for open_thread: at most one open thread per subject,
-- enforced by the database so two sessions racing to open the same
-- conversation cannot both win.
CREATE UNIQUE INDEX IF NOT EXISTS idx_mailbox_one_open_subject
ON mailbox_threads(kind, subject) WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_mailbox_recipient
ON mailbox_messages(thread_id, recipient, id);

CREATE TABLE IF NOT EXISTS mailbox_receipts (
    message_id            INTEGER NOT NULL REFERENCES mailbox_messages(id) ON DELETE CASCADE,
    role                  TEXT NOT NULL,
    read_at               TEXT NOT NULL,
    PRIMARY KEY (message_id, role)
);

-- One row per role that currently agrees with the thread's direction. This
-- replaces the per-role boolean columns of the original two-party design: any
-- number of roles may participate, and the set of agreeing roles is data, not
-- schema. A dissent clears the whole set (see review_discuss).
CREATE TABLE IF NOT EXISTS thread_agreements (
    thread_id             TEXT NOT NULL REFERENCES mailbox_threads(id) ON DELETE CASCADE,
    role                  TEXT NOT NULL,
    agreed_at             TEXT NOT NULL,
    PRIMARY KEY (thread_id, role)
);

CREATE TABLE IF NOT EXISTS review_artifacts (
    thread_id             TEXT NOT NULL REFERENCES mailbox_threads(id) ON DELETE CASCADE,
    role                  TEXT NOT NULL,
    stage                 TEXT NOT NULL CHECK (stage IN ('report', 'review', 'final')),
    path                  TEXT NOT NULL,
    submitted_at          TEXT NOT NULL,
    PRIMARY KEY (thread_id, role, stage)
);
"""


# --- time -------------------------------------------------------------------
# Mailbox timestamps are UTC and comparable as ISO strings; only host-facing
# schedules (session rollover, cron hooks) care about CONFIG.timezone.

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


# --- connection -------------------------------------------------------------

@contextmanager
def connect(db_path: Path | str | None = None):
    """Open the coordination database with the engine's durability settings.

    WAL lets a reader (an agent polling its inbox) proceed while a writer
    commits. ``busy_timeout`` makes concurrent sessions wait for the lock rather
    than fail — sessions are cold-started and cannot retry intelligently. The
    schema is applied on every connect so any entry point can be the first one.
    """
    path = Path(db_path or CONFIG.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    # Commit the migration on its own. sqlite3 opens an implicit transaction for
    # the UPDATE and holds it, which would make the caller's `BEGIN IMMEDIATE`
    # raise "cannot start a transaction within a transaction" — and only on the
    # connect that happens to find legacy rows. A migration is also not part of
    # the caller's unit of work: it must not roll back with it.
    conn.commit()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing coordination database up to the current on-disk contract.

    Idempotent, and applied on every connect for the same reason the schema is:
    sessions are cold-started and any entry point can be the first one, so no
    caller can be trusted to have migrated first.
    """
    # BROADCAST used to be spelled `_LEGACY_BROADCAST`. The token is data, not
    # code, so renaming the constant alone would strand every broadcast already
    # on disk: nothing queries for the old word any more, and inbox() matches
    # recipients by equality, so those rows would durably address nobody while
    # still looking delivered. Probe before writing — connect() is on every read
    # path, and an unconditional UPDATE would take the write lock each time.
    if conn.execute("SELECT 1 FROM mailbox_messages WHERE recipient=? LIMIT 1",
                    (_LEGACY_BROADCAST,)).fetchone():
        conn.execute("UPDATE mailbox_messages SET recipient=? WHERE recipient=?",
                     (BROADCAST, _LEGACY_BROADCAST))


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


# --- roles ------------------------------------------------------------------

def _known_roles(conn: sqlite3.Connection) -> set[str]:
    """The roles that exist, per the registry.

    ``agent_registry`` is owned by the orchestration module. Importing it here
    would close an import cycle (orchestration -> meetings -> mailbox), so the
    table is read directly; when it is absent the mailbox is running standalone
    and the host's configured roles are used. The supervisor is never a role.
    """
    if _has_table(conn, "agent_registry"):
        roles = {r["role"] for r in conn.execute(
            "SELECT role FROM agent_registry WHERE enabled=1")}
    else:
        roles = set(CONFIG.role_names())
    return roles - {CONFIG.supervisor_role}


def _role(conn: sqlite3.Connection, role: str, *, recipient: bool = False) -> str:
    """Validate a role against the registry and return it normalized.

    Agent-facing APIs reject ``CONFIG.supervisor_role`` outright: the supervisor
    is a human authority, and anything it says must arrive through the
    authenticated meetings adapter, never by an agent naming it as a sender.
    """
    if not isinstance(role, str) or not role.strip():
        raise ValueError("role is required")
    role = role.strip()
    if recipient and role in BROADCAST_ALIASES:
        return BROADCAST
    if role == CONFIG.supervisor_role:
        raise ValueError(f"{role} is not an agent role")
    known = _known_roles(conn)
    if role not in known:
        label = ", ".join(sorted(known)) or "<registry is empty>"
        raise ValueError(f"invalid {'recipient' if recipient else 'role'}: "
                         f"{role} (known roles: {label})")
    return role


# --- threads ----------------------------------------------------------------

def _thread_id(kind: str, now: dt.datetime) -> str:
    return f"{kind}-{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _deadline(now: dt.datetime, idle_minutes: int) -> str:
    return _iso(now + dt.timedelta(minutes=idle_minutes))


def _refresh_thread(conn: sqlite3.Connection, thread_id: str,
                    now: dt.datetime | None = None) -> sqlite3.Row:
    """Read a thread, first retiring it if its idle deadline has passed.

    The deadline is enforced lazily on read because no daemon owns the mailbox:
    whichever session touches the thread next is the one that observes it went
    idle. Every read path goes through here so a stale thread can never be
    written to.
    """
    now = now or _now()
    row = conn.execute(
        "SELECT * FROM mailbox_threads WHERE id = ?", (thread_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"unknown mailbox thread: {thread_id}")
    if row["status"] == "open" and row["expires_at"] <= _iso(now):
        conn.execute(
            """UPDATE mailbox_threads
               SET status='paused', stop_reason='idle timeout', stopped_by='system',
                   updated_at=? WHERE id=?""",
            (_iso(now), thread_id),
        )
        row = conn.execute(
            "SELECT * FROM mailbox_threads WHERE id = ?", (thread_id,)
        ).fetchone()
    return row


def _agreed_roles(conn: sqlite3.Connection, thread_id: str) -> list[str]:
    return [r["role"] for r in conn.execute(
        "SELECT role FROM thread_agreements WHERE thread_id=? ORDER BY role",
        (thread_id,),
    )]


def _as_thread(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    out = dict(row)
    out["agreed_roles"] = _agreed_roles(conn, row["id"])
    return out


def open_thread(subject: str, *, kind: str = "live", idle_minutes: int = 45,
                max_messages: int = 12, max_discussion: int = 6,
                owner_role: str | None = None,
                db_path: Path | str | None = None) -> dict:
    """Open a conversation, or return the live one on the same subject.

    Idempotent by design: an agent woken twice about the same thing must not
    start two conversations about it. Any non-closed thread with this
    (kind, subject) is returned as-is — including a paused one, so the caller
    sees why it stopped instead of routing around it.

    ``owner_role`` optionally names the role that chairs a review: only the
    owner may file the final artifact or conclude a deadlocked discussion. Left
    unset, the engine has no opinion and any participant may do either.

    Sizing note for reviews: unanimity costs at least one discussion turn per
    participant, and a dissent costs the dissenter's re-agreement turn on top.
    Set ``max_discussion`` comfortably above the participant count or the
    budget will retire the discussion before consensus can form — which is
    safe (it advances to finalize) but wastes the round.
    """
    subject = " ".join(subject.split())
    if not subject:
        raise ValueError("thread subject is required")
    if kind not in THREAD_KINDS:
        raise ValueError(f"invalid thread kind: {kind}")
    if idle_minutes < 1 or max_messages < 2:
        raise ValueError("idle_minutes must be >=1 and max_messages >=2")
    if kind == "review" and max_discussion < 2:
        raise ValueError("review max_discussion must be >=2")
    now = _now()
    phase = "reports" if kind == "review" else "live"
    thread_id = _thread_id(kind, now)
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if owner_role is not None:
            owner_role = _role(conn, owner_role)
        same_subject = [r["id"] for r in conn.execute(
            """SELECT id FROM mailbox_threads
               WHERE kind=? AND subject=? AND status!='closed'
               ORDER BY updated_at DESC""",
            (kind, subject),
        )]
        for existing_id in same_subject:
            existing = _refresh_thread(conn, existing_id, now)
            return _as_thread(conn, existing)
        try:
            conn.execute(
                """INSERT INTO mailbox_threads
                   (id, kind, subject, status, phase, owner_role, created_at,
                    updated_at, expires_at, idle_minutes, max_messages, max_discussion)
                   VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (thread_id, kind, subject, phase, owner_role, _iso(now), _iso(now),
                 _deadline(now, idle_minutes), idle_minutes, max_messages,
                 max_discussion if kind == "review" else 0),
            )
        except sqlite3.IntegrityError:
            # Lost the race against a concurrent opener; adopt its thread.
            existing = conn.execute(
                """SELECT * FROM mailbox_threads
                   WHERE kind=? AND subject=? AND status='open'""",
                (kind, subject),
            ).fetchone()
            if existing:
                return _as_thread(conn, existing)
            raise
        return _as_thread(conn, _refresh_thread(conn, thread_id, now))


def get_thread(thread_id: str, *, db_path: Path | str | None = None) -> dict:
    with connect(db_path) as conn:
        return _as_thread(conn, _refresh_thread(conn, thread_id))


def list_threads(*, include_stopped: bool = False,
                 db_path: Path | str | None = None) -> list[dict]:
    with connect(db_path) as conn:
        now = _now()
        # Refresh first: listing is how a console discovers that threads went
        # idle, so deadlines must be applied before the filter runs.
        ids = [r["id"] for r in conn.execute("SELECT id FROM mailbox_threads")]
        for thread_id in ids:
            _refresh_thread(conn, thread_id, now)
        sql = "SELECT * FROM mailbox_threads"
        params: tuple = ()
        if not include_stopped:
            sql += " WHERE status = ?"
            params = ("open",)
        sql += " ORDER BY updated_at DESC"
        return [_as_thread(conn, r) for r in conn.execute(sql, params).fetchall()]


# --- messages ---------------------------------------------------------------

def _normalize_body(body: str) -> str:
    normalized = " ".join(body.split())
    if not normalized:
        raise ValueError("message body is required")
    return normalized


def _insert_message(conn: sqlite3.Connection, thread: sqlite3.Row, *, sender: str,
                    recipient: str, kind: str, body: str,
                    artifact_path: str | None = None,
                    requires_reply: bool = False, reply_to: int | None = None,
                    allow_authenticated_supervisor: bool = False,
                    allow_supervisor_recipient: bool = False,
                    allow_reference_reply: bool = False,
                    now: dt.datetime | None = None) -> int:
    """Append one message to an open thread, enforcing every budget.

    Internal: callers must already hold the write transaction and have refreshed
    ``thread``. The meetings module calls this directly to layer its own
    protocol on top of the same ledger.

    ``allow_authenticated_supervisor`` is the single door through which
    ``CONFIG.supervisor_role`` may author a message. Only the meetings module
    may open it, and only after verifying an Ed25519 assertion and burning its
    nonce — never in response to an agent's say-so.

    ``allow_supervisor_recipient`` is the mirror door on the *addressing* side:
    it lets an agent send a message TO the supervisor. The invariant these gates
    protect is sender authenticity — an agent must never *speak as* the
    supervisor — and naming the supervisor as a recipient forges nothing and
    confers no authority. Only the meetings module may open it, and only for a
    meeting the supervisor has actually checked into: a one-to-one meeting with
    the supervisor mandates an explicit reply, so without this door that reply
    is unsendable and the protocol deadlocks.
    """
    now = now or _now()
    if thread["status"] != "open":
        raise ValueError(
            f"thread is {thread['status']}: {thread['stop_reason'] or 'no reason recorded'}"
        )
    if sender == CONFIG.supervisor_role:
        if not allow_authenticated_supervisor:
            raise ValueError(f"{sender} is not an agent role")
    else:
        sender = _role(conn, sender)
    if recipient == CONFIG.supervisor_role and allow_supervisor_recipient:
        recipient = CONFIG.supervisor_role
    else:
        recipient = _role(conn, recipient, recipient=True)
    if sender == recipient:
        raise ValueError("sender and recipient must differ")
    if kind not in MESSAGE_KINDS:
        raise ValueError(f"invalid message kind: {kind}")
    body = _normalize_body(body)

    # Dedup on normalized body: a woken agent that cannot tell whether it
    # already spoke would otherwise repeat itself verbatim.
    digest = hashlib.sha256(body.casefold().encode()).hexdigest()
    duplicate = conn.execute(
        """SELECT id FROM mailbox_messages
           WHERE thread_id=? AND sender=? AND recipient=? AND body_hash=?
           ORDER BY id DESC LIMIT 1""",
        (thread["id"], sender, recipient, digest),
    ).fetchone()
    if duplicate:
        raise ValueError(f"duplicate message suppressed (matches #{duplicate['id']})")

    if thread["message_count"] >= thread["max_messages"]:
        conn.execute(
            """UPDATE mailbox_threads SET status='paused',
               stop_reason='message budget exhausted', stopped_by='system', updated_at=?
               WHERE id=?""",
            (_iso(now), thread["id"]),
        )
        raise ValueError("message budget exhausted; thread paused")

    if requires_reply:
        # Never ask a question the thread has no budget left to answer, and
        # never leave a recipient holding two open questions at once.
        if thread["message_count"] + 1 >= thread["max_messages"]:
            raise ValueError("no reply budget remains; send a decision or close the thread")
        pending = conn.execute(
            """SELECT id FROM mailbox_messages
               WHERE thread_id=? AND recipient=? AND requires_reply=1
                 AND resolved_at IS NULL ORDER BY id LIMIT 1""",
            (thread["id"], recipient),
        ).fetchone()
        if pending:
            raise ValueError(
                f"recipient already has unanswered request #{pending['id']}; await or close it"
            )

    original = None
    if reply_to is not None:
        original = conn.execute(
            "SELECT * FROM mailbox_messages WHERE id=? AND thread_id=?",
            (reply_to, thread["id"]),
        ).fetchone()
        if not original:
            raise ValueError(f"reply target #{reply_to} not found in thread")
        if original["recipient"] not in {sender, BROADCAST}:
            raise ValueError(f"message #{reply_to} was not addressed to {sender}")
        if not original["requires_reply"] and not allow_reference_reply:
            raise ValueError(f"message #{reply_to} does not request a reply; acknowledge it")
        if original["requires_reply"] and original["resolved_at"] is not None:
            raise ValueError(f"message #{reply_to} is already resolved")

    cursor = conn.execute(
        """INSERT INTO mailbox_messages
           (thread_id, sender, recipient, kind, body, artifact_path, body_hash,
            requires_reply, reply_to, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (thread["id"], sender, recipient, kind, body, artifact_path, digest,
         int(requires_reply), reply_to, _iso(now)),
    )
    if original is not None and original["requires_reply"]:
        conn.execute(
            "UPDATE mailbox_messages SET resolved_at=? WHERE id=?",
            (_iso(now), reply_to),
        )

    new_count = thread["message_count"] + 1
    status = "open"
    reason = None
    if new_count >= thread["max_messages"]:
        # A review that spends its budget mid-discussion is not a failure: it
        # has heard enough and moves to finalize instead of pausing.
        if thread["kind"] == "review" and thread["phase"] == "discussion":
            conn.execute(
                "UPDATE mailbox_threads SET phase='ready_to_finalize' WHERE id=?",
                (thread["id"],),
            )
        else:
            status = "paused"
            reason = "message budget exhausted"
    conn.execute(
        """UPDATE mailbox_threads
           SET message_count=?, status=?, stop_reason=?, stopped_by=?,
               updated_at=?, expires_at=? WHERE id=?""",
        (new_count, status, reason, "system" if reason else None, _iso(now),
         _deadline(now, thread["idle_minutes"]), thread["id"]),
    )
    return int(cursor.lastrowid)


def send_message(thread_id: str, *, sender: str, recipient: str, kind: str,
                 body: str, artifact_path: str | None = None,
                 requires_reply: bool = False, reply_to: int | None = None,
                 db_path: Path | str | None = None) -> dict:
    """Send one message. ``recipient`` may be a role or ``BROADCAST``."""
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        thread = _refresh_thread(conn, thread_id)
        message_id = _insert_message(
            conn, thread, sender=sender, recipient=recipient, kind=kind, body=body,
            artifact_path=artifact_path, requires_reply=requires_reply,
            reply_to=reply_to,
        )
        return dict(conn.execute(
            "SELECT * FROM mailbox_messages WHERE id=?", (message_id,)
        ).fetchone())


def inbox(role: str, *, thread_id: str | None = None, mark_read: bool = False,
          include_control: bool = True, db_path: Path | str | None = None) -> list[dict]:
    """Unread messages addressed to ``role`` (directly or by broadcast).

    ``mark_read`` is opt-in: a caller that merely inspects an inbox must not
    consume it on the real recipient's behalf.
    """
    with connect(db_path) as conn:
        role = _role(conn, role)
        if thread_id:
            _refresh_thread(conn, thread_id)
        filters = ["m.recipient IN (?, ?)", "r.message_id IS NULL"]
        params: list = [role, role, BROADCAST]
        if thread_id:
            filters.append("m.thread_id=?")
            params.append(thread_id)
        if not include_control:
            filters.append("m.kind != 'control'")
        rows = conn.execute(
            f"""SELECT m.* FROM mailbox_messages m
                LEFT JOIN mailbox_receipts r ON r.message_id=m.id AND r.role=?
                WHERE {' AND '.join(filters)} ORDER BY m.id""",
            params,
        ).fetchall()
        if mark_read and rows:
            now = _iso(_now())
            conn.executemany(
                "INSERT OR IGNORE INTO mailbox_receipts(message_id, role, read_at) VALUES (?, ?, ?)",
                [(r["id"], role, now) for r in rows],
            )
        return [dict(r) for r in rows]


def wait_for_inbox(role: str, *, thread_id: str | None = None,
                   wait_seconds: int = 0, mark_read: bool = False,
                   db_path: Path | str | None = None) -> list[dict]:
    """Poll the inbox for at most ``wait_seconds``, returning early on arrival.

    The cap is deliberately short. This is a courtesy for an agent that just
    asked a question and expects a fast answer — it is not a way to sit and
    wait for work. Agents end their turn; the orchestrator wakes them.
    """
    if not 0 <= wait_seconds <= 60:
        raise ValueError("wait_seconds must be between 0 and 60")
    deadline = time.monotonic() + wait_seconds
    while True:
        rows = inbox(role, thread_id=thread_id, mark_read=mark_read, db_path=db_path)
        if rows or time.monotonic() >= deadline:
            return rows
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def acknowledge(message_id: int, *, role: str,
                db_path: Path | str | None = None) -> None:
    """Record that ``role`` has read a message addressed to it."""
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        role = _role(conn, role)
        message = conn.execute(
            "SELECT * FROM mailbox_messages WHERE id=?", (message_id,)
        ).fetchone()
        if not message:
            raise ValueError(f"unknown message: {message_id}")
        if message["recipient"] not in {role, BROADCAST}:
            raise ValueError(f"message #{message_id} was not addressed to {role}")
        conn.execute(
            "INSERT OR IGNORE INTO mailbox_receipts(message_id, role, read_at) VALUES (?, ?, ?)",
            (message_id, role, _iso(_now())),
        )


def stop_thread(thread_id: str, *, action: str, actor: str, reason: str,
                db_path: Path | str | None = None) -> dict:
    """Pause, close or escalate a thread. Resume is intentionally absent.

    Stopping is always available to an agent; restarting never is. A thread that
    stopped itself on a budget must not be restarted by the agent that spent it,
    or the bound is decorative — resumption requires a signed supervisor
    assertion through the meetings module.
    """
    if action == "resume":
        raise ValueError(
            "raw mailbox resume is disabled; use a signed supervisor meeting assertion"
        )
    if action not in {"pause", "close", "escalate"}:
        raise ValueError(f"invalid thread action: {action}")
    reason = _normalize_body(reason)
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _role(conn, actor)
        row = _refresh_thread(conn, thread_id)
        now = _now()
        if row["status"] == "closed":
            raise ValueError("thread is already closed")
        status = {"pause": "paused", "close": "closed", "escalate": "escalated"}[action]
        conn.execute(
            """UPDATE mailbox_threads SET status=?, stop_reason=?, stopped_by=?,
               max_messages=?, updated_at=?, expires_at=? WHERE id=?""",
            (status, reason, actor, row["max_messages"], _iso(now),
             _deadline(now, row["idle_minutes"]), thread_id),
        )
        return _as_thread(conn, _refresh_thread(conn, thread_id, now))


# --- review workflow --------------------------------------------------------

def _artifact(path: str | Path) -> str:
    """Resolve an artifact path, refusing one that does not exist.

    A review cites work; a citation that does not resolve is not a review.
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"artifact does not exist: {resolved}")
    return str(resolved)


def _meeting_guard(conn: sqlite3.Connection, thread_id: str, role: str) -> bool:
    """Require attendance when a review thread is wrapped by a meeting.

    Returns True if a meeting owns this thread. The meetings module is optional
    — the table may not exist — so its absence simply means no guard applies.
    """
    if not _has_table(conn, "meetings"):
        return False
    meeting = conn.execute(
        "SELECT state FROM meetings WHERE thread_id=?", (thread_id,)
    ).fetchone()
    if not meeting:
        return False
    if meeting["state"] not in {"active", "consensus"}:
        raise ValueError(f"review meeting is {meeting['state']}; all attendees must check in")
    attendee = conn.execute(
        """SELECT checked_in_at FROM meeting_attendees
           WHERE thread_id=? AND role=?""",
        (thread_id, role),
    ).fetchone()
    if not attendee or not attendee["checked_in_at"]:
        raise ValueError(f"{role} has not checked in to the review meeting")
    return True


def _participants(conn: sqlite3.Connection, thread_id: str) -> tuple[str, ...]:
    """The roles a review waits for before advancing a phase.

    When a meeting wraps the thread its checked-in attendees are the
    participants — a five-role registry may hold a two-role review, and the
    review must not stall on roles that were never invited. A standalone review
    thread waits for every enabled role in the registry.
    """
    if _has_table(conn, "meeting_attendees"):
        rows = conn.execute(
            """SELECT role FROM meeting_attendees
               WHERE thread_id=? AND checked_in_at IS NOT NULL AND stopped_at IS NULL
               ORDER BY role""",
            (thread_id,),
        ).fetchall()
        if rows:
            return tuple(r["role"] for r in rows)
    return tuple(sorted(_known_roles(conn)))


def _clear_agreements(conn: sqlite3.Connection, thread_id: str,
                      role: str | None = None) -> None:
    """Drop recorded agreements — all of them (a phase transition resets
    consensus), or one role's (that role just voiced a NEW disagreement).
    A speaker's dissent withdraws only their own standing agreement: the
    counterpart's agreement is theirs to withdraw, and the alternation rule
    guarantees they speak — and can do so — after seeing the new dispute,
    so a stale agreement can never finalize a phase unseen."""
    if role is None:
        conn.execute("DELETE FROM thread_agreements WHERE thread_id=?",
                     (thread_id,))
    else:
        conn.execute("DELETE FROM thread_agreements WHERE thread_id=? AND role=?",
                     (thread_id, role))


def _submitted_roles(conn: sqlite3.Connection, thread_id: str, stage: str) -> set[str]:
    return {r["role"] for r in conn.execute(
        "SELECT role FROM review_artifacts WHERE thread_id=? AND stage=?",
        (thread_id, stage),
    )}


def _require_finalizer(conn: sqlite3.Connection, thread: sqlite3.Row,
                       role: str, what: str) -> None:
    """Only the thread's owner may finalize or conclude; if no owner was named,
    any participant may. The engine has no built-in chair."""
    owner = thread["owner_role"]
    if owner:
        if role != owner:
            raise ValueError(f"only {owner} (the thread owner) may {what}")
        return
    participants = _participants(conn, thread["id"])
    if participants and role not in participants:
        raise ValueError(f"{role} is not a participant of this review")


def submit_review_artifact(thread_id: str, *, role: str, stage: str,
                           path: str | Path,
                           db_path: Path | str | None = None) -> dict:
    """File a report, a cross-review, or the final artifact.

    The phase only advances once *every* participant has filed for the current
    stage, so nobody's review is skipped by a faster peer. Reaching a new stage
    clears any standing agreement: consent applies to what was on the table when
    it was given, not to whatever arrives next.
    """
    if stage not in REVIEW_STAGES:
        raise ValueError(f"invalid review stage: {stage}")
    artifact = _artifact(path)
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        role = _role(conn, role)
        thread = _refresh_thread(conn, thread_id)
        if thread["kind"] != "review":
            raise ValueError("artifacts may only be submitted to review threads")
        is_meeting = _meeting_guard(conn, thread_id, role)
        if stage == "final":
            _require_finalizer(conn, thread, role, "submit the final review")
        expected = _STAGE_PHASE[stage]
        if thread["phase"] != expected or thread["status"] != "open":
            raise ValueError(
                f"cannot submit {stage} while thread is {thread['status']}/{thread['phase']}"
            )
        try:
            conn.execute(
                """INSERT INTO review_artifacts(thread_id, role, stage, path, submitted_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (thread_id, role, stage, artifact, _iso(_now())),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"{role} already submitted a {stage}") from exc

        message_id = _insert_message(
            conn, thread, sender=role, recipient=BROADCAST,
            kind="decision" if stage == "final" else stage,
            body=f"{role} submitted {stage}: {artifact}", artifact_path=artifact,
        )

        if stage in _STAGE_NEXT_PHASE:
            participants = set(_participants(conn, thread_id))
            if participants and participants <= _submitted_roles(conn, thread_id, stage):
                conn.execute(
                    "UPDATE mailbox_threads SET phase=? WHERE id=?",
                    (_STAGE_NEXT_PHASE[stage], thread_id),
                )
                _clear_agreements(conn, thread_id)
        else:
            if is_meeting:
                # The meeting, not the mailbox, owns closure: every attendee
                # must agree to end it, so the thread waits.
                conn.execute(
                    """UPDATE mailbox_threads SET phase='finalized',
                       stop_reason='final review awaiting mutual meeting termination',
                       stopped_by=NULL WHERE id=?""",
                    (thread_id,),
                )
            else:
                conn.execute(
                    """UPDATE mailbox_threads SET phase='finalized', status='closed',
                       stop_reason='final review submitted', stopped_by=? WHERE id=?""",
                    (role, thread_id),
                )
        return {"message_id": message_id,
                "thread": _as_thread(conn, conn.execute(
                    "SELECT * FROM mailbox_threads WHERE id=?", (thread_id,)).fetchone())}


def review_discuss(thread_id: str, *, role: str, body: str, agree: bool = False,
                   db_path: Path | str | None = None) -> dict:
    """Speak once in the bounded discussion phase, optionally agreeing.

    Two rules keep this from becoming a loop. Speakers must alternate — nobody
    may talk twice in a row — and a dissent withdraws the dissenter's OWN
    standing agreement, so their consensus must be re-earned on the current
    state of the argument. The counterparts' agreements stand: alternation
    guarantees each of them speaks after seeing the new dispute, where they may
    re-agree or dissent in turn, so nothing finalizes on an unseen argument.
    The phase ends on unanimity or when the discussion budget runs out,
    whichever comes first.
    """
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        role = _role(conn, role)
        thread = _refresh_thread(conn, thread_id)
        _meeting_guard(conn, thread_id, role)
        if thread["kind"] != "review" or thread["phase"] != "discussion":
            raise ValueError(f"review is not in discussion phase: {thread['phase']}")
        if thread["status"] != "open":
            raise ValueError(f"review thread is {thread['status']}")
        last = conn.execute(
            """SELECT sender FROM mailbox_messages
               WHERE thread_id=? AND kind='discussion' ORDER BY id DESC LIMIT 1""",
            (thread_id,),
        ).fetchone()
        if last and last["sender"] == role:
            raise ValueError("discussion must alternate roles; await the other side")

        message_id = _insert_message(
            conn, thread, sender=role, recipient=BROADCAST, kind="discussion", body=body,
        )
        discussion_count = thread["discussion_count"] + 1
        if agree:
            conn.execute(
                """INSERT OR REPLACE INTO thread_agreements(thread_id, role, agreed_at)
                   VALUES (?, ?, ?)""",
                (thread_id, role, _iso(_now())),
            )
        else:
            # Only the dissenter's own agreement falls. Clearing the whole
            # table here forced BOTH sides back into discussion on every
            # dispute, doubling every round (parlay task #39, 2026-07-19).
            _clear_agreements(conn, thread_id, role)

        participants = set(_participants(conn, thread_id))
        agreed = set(_agreed_roles(conn, thread_id))
        phase = "discussion"
        reason = None
        if participants and participants <= agreed:
            phase, reason = "ready_to_finalize", "mutual agreement"
        elif discussion_count >= thread["max_discussion"]:
            phase, reason = "ready_to_finalize", "discussion budget exhausted"
        conn.execute(
            """UPDATE mailbox_threads SET discussion_count=?, phase=?,
               stop_reason=COALESCE(?, stop_reason) WHERE id=?""",
            (discussion_count, phase, reason, thread_id),
        )
        return {"message_id": message_id,
                "thread": _as_thread(conn, conn.execute(
                    "SELECT * FROM mailbox_threads WHERE id=?", (thread_id,)).fetchone())}


def conclude_review(thread_id: str, *, role: str, reason: str,
                    db_path: Path | str | None = None) -> dict:
    """End a deadlocked discussion without consensus and move to finalize.

    Disagreement is a legitimate outcome; a discussion that cannot converge must
    still terminate. The reason is recorded in the thread so the dissent stays
    visible in the record rather than being papered over.
    """
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        role = _role(conn, role)
        thread = _refresh_thread(conn, thread_id)
        _meeting_guard(conn, thread_id, role)
        if thread["kind"] != "review" or thread["phase"] != "discussion":
            raise ValueError(f"review cannot be concluded from phase {thread['phase']}")
        _require_finalizer(conn, thread, role, "conclude a disputed review")
        message_id = _insert_message(
            conn, thread, sender=role, recipient=BROADCAST, kind="decision",
            body=f"Discussion concluded by {role}: {_normalize_body(reason)}",
        )
        conn.execute(
            """UPDATE mailbox_threads SET phase='ready_to_finalize',
               stop_reason=? WHERE id=?""",
            (f"{role} concluded discussion", thread_id),
        )
        return {"message_id": message_id,
                "thread": _as_thread(conn, conn.execute(
                    "SELECT * FROM mailbox_threads WHERE id=?", (thread_id,)).fetchone())}


def review_artifacts(thread_id: str, *,
                     db_path: Path | str | None = None) -> list[dict]:
    """Every artifact filed against a review, in workflow order."""
    with connect(db_path) as conn:
        _refresh_thread(conn, thread_id)
        return [dict(r) for r in conn.execute(
            """SELECT * FROM review_artifacts WHERE thread_id=?
               ORDER BY CASE stage WHEN 'report' THEN 1 WHEN 'review' THEN 2 ELSE 3 END,
                        role""",
            (thread_id,),
        ).fetchall()]
