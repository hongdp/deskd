"""Response-obligation primitives: the reply-debt ledger shared by
check-in, the supervisor join, leave, and closing. Sits low so every
protocol module above may settle or waive debts without importing
sideways.
"""

from __future__ import annotations

import sqlite3
from typing import Sequence

from . import store


def _resolve_obligations(conn: sqlite3.Connection, thread_id: str, role: str, *,
                         resolution: str, reply_message_id: int | None = None) -> int:
    now = store._iso()
    cursor = conn.execute(
        """UPDATE meeting_response_obligations
           SET status='resolved',resolved_at=?,resolution=?,resolved_by_message_id=?
           WHERE thread_id=? AND owed_by=? AND status='pending'""",
        (now, resolution, reply_message_id, thread_id, role),
    )
    return int(cursor.rowcount)


def _discharge_obligations(conn: sqlite3.Connection, thread_id: str, role: str,
                           message_ids: Sequence[int], by_message_id: int,
                           now: str | None = None) -> list[int]:
    """Settle obligations owed BY `role`, citing `role`'s own covering message.

    Judgement decides what answers what; the transport cannot. One reply
    routinely settles several outstanding questions, and only its author knows
    that it did — so the engine refuses to guess. It checks only what is
    checkable, and each refusal below is a caller bug, not a protocol bound:

    * the obligation is this thread's, still pending, and owed by this role —
      discharging someone else's debt would let an agent answer for a
      counterpart it cannot speak for ("never create both sides");
    * the citing message came AFTER the question. A message cannot have
      answered one asked later, and allowing it would make the ledger's
      resolved_by_message_id lie about causality.

    Blanket auto-settling on any outgoing message was the tempting shortcut and
    is exactly wrong: an agent that changes the subject would silently mark the
    question answered, which is a dropped message wearing a clean ledger.
    """
    now = now or store._iso()
    discharged = []
    for message_id in message_ids:
        row = conn.execute(
            "SELECT * FROM meeting_response_obligations WHERE message_id=? AND thread_id=?",
            (message_id, thread_id),
        ).fetchone()
        if not row:
            raise ValueError(f"#{message_id} carries no response obligation in this meeting")
        if row["owed_by"] != role:
            raise ValueError(
                f"#{message_id} is owed by {row['owed_by']}, not {role}")
        if row["status"] != "pending":
            raise ValueError(f"#{message_id} is already {row['status']}")
        if by_message_id <= message_id:
            raise ValueError(
                f"#{by_message_id} cannot answer #{message_id}: it did not come after it")
        conn.execute(
            """UPDATE meeting_response_obligations
               SET status='resolved',resolved_at=?,resolution=?,resolved_by_message_id=?
               WHERE message_id=?""",
            (now, f"covered by #{by_message_id}", by_message_id, message_id),
        )
        discharged.append(message_id)
    return discharged


def _waive_pending_obligations(conn: sqlite3.Connection, thread_id: str,
                               reason: str) -> int:
    now = store._iso()
    cursor = conn.execute(
        """UPDATE meeting_response_obligations
           SET status='waived',resolved_at=?,resolution=?
           WHERE thread_id=? AND status='pending'""",
        (now, reason, thread_id),
    )
    return int(cursor.rowcount)
