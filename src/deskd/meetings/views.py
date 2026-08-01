"""Read-only views: status, listings, day filters, and the audit
transcript.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from ..config import CONFIG
from .store import (_in_clause, _known_roles, _meeting, _meeting_projection,
                    _mode, connect)
from .sweep import _sweep_timeouts
from .termination import _pending_termination


def meeting_status(thread_id: str, *, db_path: Path | str | None = None,
                   sweep: bool = True) -> dict:
    if sweep:
        _sweep_timeouts(db_path)
    with connect(db_path) as conn:
        meeting = _meeting_projection(_meeting(conn, thread_id))
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


def list_meetings(*, include_closed: bool = False, day: str | None = None,
                  db_path: Path | str | None = None) -> list[dict]:
    """Meetings, live ones first, then closed ones newest-ended first.

    `day` is a local calendar date (YYYY-MM-DD in CONFIG.timezone) and filters
    CLOSED meetings only — a live meeting is always listed, whatever day it was
    called on. A filter that could hide a meeting still waiting on someone would
    be a filter that hides the one thing this console exists to show; "narrow the
    history" must never quietly mean "narrow the present".

    Closed meetings sort by `closed_at`, not `created_at`: history reads in the
    order things finished, and a long meeting called on Monday and closed on
    Friday belongs at Friday. Ordering by created_at instead puts it in Monday's
    slot, where nobody looking for what happened on Friday will find it.
    """
    _sweep_timeouts(db_path)
    with connect(db_path) as conn:
        where, params = [], []
        if not include_closed:
            where.append("state!='closed'")
        elif day:
            _valid_day(day)
            # A closed meeting belongs to the local day it CLOSED on. closed_at
            # is UTC, so compare in CONFIG.timezone rather than by string prefix
            # — a prefix match silently answers a different question everywhere
            # the offset is not zero, which is everywhere this desk runs.
            lo, hi = _local_day_bounds(day)
            where.append("(state!='closed' OR (closed_at >= ? AND closed_at < ?))")
            params += [lo, hi]
        sql = "SELECT thread_id FROM meetings"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # Live first (urgent ahead of normal), then closed newest-ended first.
        #
        # Priority ranks the LIVE block only. It answers "what needs you now",
        # which a finished meeting cannot; applying it to history hoists every
        # urgent meeting into its own block and cuts the timeline in two, so
        # 07-15 lands above 07-14 and reading the day back is impossible.
        # COALESCE keeps a legacy closed row with no closed_at off the top.
        sql += (" ORDER BY (state='closed'),"
                " CASE WHEN state!='closed' AND priority='urgent' THEN 0 ELSE 1 END,"
                " COALESCE(closed_at, created_at) DESC")
        ids = [r["thread_id"] for r in conn.execute(sql, params).fetchall()]
    return [meeting_status(i, db_path=db_path, sweep=False) for i in ids]


def _valid_day(day: str) -> None:
    try:
        dt.date.fromisoformat(day)
    except (TypeError, ValueError):
        raise ValueError(f"day must be YYYY-MM-DD, got {day!r}") from None


def _local_day_bounds(day: str) -> tuple[str, str]:
    """[start, end) of a local calendar day, as the UTC ISO strings the DB holds."""
    tz = CONFIG.tzinfo()
    start = dt.datetime.combine(dt.date.fromisoformat(day), dt.time.min, tzinfo=tz)
    return (start.astimezone(dt.timezone.utc).isoformat(),
            (start + dt.timedelta(days=1)).astimezone(dt.timezone.utc).isoformat())


def meeting_days(db_path: Path | str | None = None) -> list[str]:
    """Local dates that have at least one closed meeting, newest first — so the
    console can offer real choices instead of a blank date box."""
    tz = CONFIG.tzinfo()
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT COALESCE(closed_at, updated_at) AS t FROM meetings "
            "WHERE state='closed' AND t IS NOT NULL").fetchall()
    days = {dt.datetime.fromisoformat(r["t"]).astimezone(tz).date().isoformat()
            for r in rows}
    return sorted(days, reverse=True)


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
