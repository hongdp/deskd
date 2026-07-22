"""The delivery ledger: a pure projection of the durable mailbox
tables into per-recipient receipts (queued -> notified -> read, and
overdue when nothing reacts).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from ..config import CONFIG
from .store import (_RECIPIENT_ALL, _iso, _role_params, connect)

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
    """(thread, role) pairs with a PENDING wake request — the per-role signal that
    the system is actively re-driving delivery to that specific role RIGHT NOW.

    `status='pending'` is the whole point and was once missing. Without it the set
    answers "has this role ever been woken on this thread?" instead of "is
    something reacting?" — so the first wake permanently pins every later unread
    message on that thread to `escalated`, it can never become `overdue`, and no
    demand is ever raised again. The delivery guarantee dies for that (thread,
    role) pair after its first success, silently, forever.

    Observed live, not in review: nine messages past SLA on a real desk — one of
    them nine hours old — with every wake request in the table `acknowledged` and
    plan_wakes returning zero actions.
    """
    return {(r["thread_id"], r["role"]) for r in conn.execute(
        "SELECT thread_id, role FROM meeting_wake_requests WHERE status='pending'")}


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
