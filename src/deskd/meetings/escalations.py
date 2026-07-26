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
from .store import _event, connect


def _queue_escalation(conn: sqlite3.Connection, thread_id: str,
                      requested_by: str, reason: str,
                      channel: str = "auto") -> int:
    cursor = conn.execute(
        """INSERT INTO meeting_escalations
           (thread_id,requested_by,reason,channel,status,created_at)
           VALUES (?,?,?,?, 'queued', ?)""",
        (thread_id, requested_by, reason, channel, store._iso()),
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
                     db_path: Path | str | None = None) -> list[dict]:
    with connect(db_path) as conn:
        sql = "SELECT * FROM meeting_escalations"
        params: tuple = ()
        if thread_id:
            sql += " WHERE thread_id=?"
            params = (thread_id,)
        sql += " ORDER BY id DESC"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
