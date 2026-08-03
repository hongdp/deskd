"""The escalation queue: ledger rows for "a human should hear about
this", and their delivery through the pluggable channel layer.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .. import channels as _channels
from ..config import PROJECT_NAME
from . import store
from .store import _clean, _event, connect


def _queue_escalation(conn: sqlite3.Connection, thread_id: str,
                      requested_by: str, reason: str,
                      channel: str = "auto",
                      origin: str = "agent") -> int:
    """Queue a row for "a human should hear about this".

    `origin` separates the two things that shared this queue. An *agent* asked
    a question and is waiting on an answer; the *engine* noted that it had to
    do something unusual — wake someone off-hours, give up waiting for a
    check-in. Both belong in the ledger, only the first belongs in front of a
    person, and mixing them buried fourteen real questions under fifty-eight
    notes.

    The default is `agent` on purpose: a new call site that forgets to say
    which it is gets the loud answer. Being told about something that did not
    need you is a nuisance; not being told is the failure this parameter
    exists to prevent.
    """
    cursor = conn.execute(
        """INSERT INTO meeting_escalations
           (thread_id,requested_by,reason,channel,status,created_at,origin)
           VALUES (?,?,?,?, 'queued', ?,?)""",
        (thread_id, requested_by, reason, channel, store._iso(), origin),
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
    subject = f"{PROJECT_NAME} meeting: {row['agenda']}"
    text = (f"{PROJECT_NAME} meeting escalation [{row['thread_id']}]\n"
            f"Agenda: {row['agenda']}\nReason: {row['reason']}")
    # The ledger row (meeting_escalations) is ours and already written; the
    # channel layer only mirrors it out. An outbox result means the row IS the
    # delivery, surfaced by the console.
    results = _channels.deliver(subject, text, row["channel"])
    status = _channels.summarize(results)
    with connect(db_path, write=True) as conn:
        conn.execute(
            "UPDATE meeting_escalations SET status=?,details=?,sent_at=? WHERE id=?",
            (status, json.dumps(results, ensure_ascii=False),
             store._iso() if status == "sent" else None, escalation_id),
        )
    return {"id": escalation_id, "status": status, "results": results}


def list_escalations(thread_id: str | None = None, *,
                     origin: str | None = None,
                     unresolved_only: bool = False,
                     db_path: Path | str | None = None) -> list[dict]:
    """The ledger, filtered. Defaults return everything, as they always have —
    the callers that want "what is still on the supervisor's plate" ask for it
    (`origin="agent", unresolved_only=True`) rather than the history being
    silently rewritten under readers that wanted the history."""
    where, params = [], []
    if thread_id:
        where.append("thread_id=?")
        params.append(thread_id)
    if origin:
        where.append("origin=?")
        params.append(origin)
    if unresolved_only:
        where.append("resolved_at IS NULL")
    sql = "SELECT * FROM meeting_escalations"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def resolve_escalation(escalation_id: int, *, by: str, note: str,
                       db_path: Path | str | None = None) -> dict:
    """Close the loop a human just closed outside the system.

    The missing half of the lifecycle: rows were queued and delivered, and
    nothing could ever say "dealt with". A supervisor who acts on an
    escalation — completes the broker form, rules on the proposal — leaves the
    queue looking exactly as it did before they helped, so the next reader
    cannot tell the answered from the unanswered. Idempotent: re-resolving
    keeps the first disposition, because the first one is the true one.
    """
    with connect(db_path, write=True) as conn:
        return _resolve_escalation(conn, escalation_id, by=by, note=note)


def _resolve_escalation(conn: sqlite3.Connection, escalation_id: int, *,
                        by: str, note: str) -> dict:
    """The conn-taking half, so a caller already inside a write transaction
    does not open a second one — SQLite answers that with `database is
    locked`, and the supervisor verb runs inside exactly such a transaction."""
    note = _clean(note, "note")
    row = conn.execute(
        "SELECT thread_id, resolved_at FROM meeting_escalations WHERE id=?",
        (escalation_id,)).fetchone()
    if not row:
        raise ValueError(f"unknown escalation: {escalation_id}")
    if row["resolved_at"] is None:
        conn.execute(
            """UPDATE meeting_escalations
                  SET resolved_at=?, resolved_by=?, resolution=? WHERE id=?""",
            (store._iso(), by, note, escalation_id))
        _event(conn, row["thread_id"], "escalation_resolved", by,
               f"#{escalation_id}: {note}")
    return dict(conn.execute(
        "SELECT * FROM meeting_escalations WHERE id=?",
        (escalation_id,)).fetchone())


def _resolve_engine_escalations(conn: sqlite3.Connection, thread_id: str,
                                reason: str) -> None:
    """Retire the engine's own notes when their meeting ends.

    An "attendance timeout" or an off-hours wake note is about a conversation;
    once that conversation is over there is nothing left for anyone to do
    about it. Agent questions are deliberately left standing — one of them
    outlived its meeting by four days and was still the live question on the
    day it was finally answered.
    """
    conn.execute(
        """UPDATE meeting_escalations
              SET resolved_at=?, resolved_by='engine', resolution=?
            WHERE thread_id=? AND origin='engine' AND resolved_at IS NULL""",
        (store._iso(), reason, thread_id))
