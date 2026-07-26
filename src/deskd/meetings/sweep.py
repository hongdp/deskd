"""The SLA sweep and the wake-request ledger it arms. Depends only on
the store and the escalation queue; every module above may call it.
The orchestrator PULLS the demand this module records (this layer must
never import orchestration) — see collect_wake_demand in
orchestration/wake.py.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from . import store
from .escalations import _queue_escalation, dispatch_escalation
from .store import (BROADCAST, _agent_role, _in_clause, _known_roles,
                    _parse_time, _visible_message_sql, connect)

# --- SLA sweep --------------------------------------------------------------

def _sweep_timeouts(db_path: Path | str | None = None) -> list[int]:
    """Escalate overdue attendance/replies without blocking the calling agent.

    Every read path runs this, so an agent that merely looks at a meeting also
    advances the clock for everyone. Dispatch happens after the write
    transaction closes.
    """
    escalation_ids: list[int] = []
    now = store._now()
    now_iso = store._iso(now)
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
        # 2. Response obligations are NOT swept here any more. An overdue reply
        #    used to queue an escalation straight from this loop — one hop, in
        #    the human's direction, jumping every machine rung of the wake
        #    ladder (hook at 60s, resume at 120s, spawn at 180s) that exists to
        #    fix precisely this without waking anybody. A slow agent is not an
        #    incident, and paging a person for one trains them to ignore the
        #    page that matters. The orchestrator collects the same overdue rows
        #    as wake demand and climbs the ladder properly; it reaches a human at
        #    the `human` rung, on the merits, once the machine has actually
        #    failed. This layer cannot push that demand (it must never import
        #    orchestration), so orchestration pulls it — see
        #    collect_wake_demand's `owed_reply`, whose predicate mirrors this
        #    one, and _demand_resolved, which mirrors it back.
        # 3. Stale attendees: checked in, but sitting on unread messages past
        #    the SLA. Re-arm a wake request only when the previous ack predates
        #    the oldest unread message, so an ack cannot silence new traffic.
        #
        #    Re-arm ONLY — no escalation from here, for exactly the reason
        #    branch 2 stopped escalating: a page queued in the same breath as
        #    the machine remedy races it, and the machine usually wins. Measured
        #    on a live desk the day its escalations first reached a phone: page
        #    at 20:24:15, messages read at 20:24:32 — seventeen seconds — four
        #    pages in one ordinary working meeting, every one self-resolved.
        #    An attendee mid-turn is not an incident; it is how headless agents
        #    work between wakes. The re-armed wake request below IS the fix:
        #    the orchestrator collects it, climbs hook -> resume -> spawn, and
        #    reaches the human rung on the merits once the machine has actually
        #    failed to make the agent read.
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
            conn.execute(
                """INSERT INTO meeting_wake_requests(thread_id,role,status,created_at)
                   VALUES (?,?,'pending',?)
                   ON CONFLICT(thread_id,role) DO UPDATE
                   SET status='pending',created_at=excluded.created_at,
                       acknowledged_at=NULL
                   WHERE meeting_wake_requests.status='acknowledged'
                     AND meeting_wake_requests.acknowledged_at<?""",
                (row["thread_id"], row["role"], now_iso, row["oldest_unread"]),
            )
        # 4. Parked termination votes. Branch 3 deliberately skips
        #    `termination_pending`, and the propose-time wake fires exactly
        #    once — so a voter that acknowledged its wake (or was woken for
        #    something else entirely) and finished its turn without voting
        #    would park the meeting for good. Re-arm after the meeting's own
        #    wait timeout, with the same acknowledged-only guard as branch 3:
        #    a request still pending is already being climbed by the ladder.
        vote_in, vote_params = _in_clause("a.role", sorted(agent_roles))
        parked = conn.execute(
            f"""SELECT a.thread_id, a.role, t.created_at AS proposed_at,
                       m.wait_timeout_seconds
                FROM meetings m
                JOIN meeting_terminations t ON t.thread_id=m.thread_id
                     AND t.status='pending'
                JOIN meeting_attendees a ON a.thread_id=m.thread_id
                     AND a.required=1 AND a.checked_in_at IS NOT NULL
                     AND a.stopped_at IS NULL
                WHERE m.state='termination_pending'
                  AND {vote_in}
                  AND a.role NOT IN (SELECT role FROM meeting_termination_votes
                                     WHERE proposal_id=t.id)""",
            vote_params,
        ).fetchall()
        for row in parked:
            timeout = dt.timedelta(seconds=row["wait_timeout_seconds"])
            if _parse_time(row["proposed_at"]) + timeout > now:
                continue
            conn.execute(
                """INSERT INTO meeting_wake_requests(thread_id,role,status,created_at)
                   VALUES (?,?,'pending',?)
                   ON CONFLICT(thread_id,role) DO UPDATE
                   SET status='pending',created_at=excluded.created_at,
                       acknowledged_at=NULL
                   WHERE meeting_wake_requests.status='acknowledged'
                     AND meeting_wake_requests.acknowledged_at<?""",
                (row["thread_id"], row["role"], now_iso, store._iso(now - timeout)),
            )
    for escalation_id in escalation_ids:
        dispatch_escalation(escalation_id, db_path=db_path)
    return escalation_ids


def sweep_timeouts(db_path: Path | str | None = None) -> list[int]:
    """Advance the meeting SLA clocks (attendance timeout, stale-attendee
    re-arm) and dispatch what they queue.

    Public because the orchestrator's planning tick calls it: the sweep runs
    on every meetings READ path, which advances the clocks only while someone
    happens to be looking. On a quiet desk nobody looks — a 300s attendance
    timeout once fired after 13.5 minutes because the first read of the
    meeting WAS the timeout check. The tick makes the clocks tick."""
    return _sweep_timeouts(db_path)


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
            (store._iso(), thread_id, role),
        )
        if not cursor.rowcount:
            raise ValueError("no pending wake request for this role/meeting")
    return {"thread_id": thread_id, "role": role, "status": "acknowledged"}
