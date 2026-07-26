"""The termination handshake (propose / confirm / reject, the one
closer), plus pause and escalate — every path that parks or ends a
meeting.
"""

from __future__ import annotations

from pathlib import Path

from ..channels import OUTBOX_CHANNEL, registered_channels
from ..config import CONFIG
from . import store
from .escalations import _queue_escalation, dispatch_escalation
from .obligations import _resolve_obligations
from .store import (_active_roles, _agent_role, _attendee, _clean, _event,
                    _has_supervisor, _known_roles, _meeting, _supervisor_claim,
                    connect)

# --- termination handshake --------------------------------------------------

def _pending_termination(conn, thread_id: str):
    return conn.execute(
        """SELECT * FROM meeting_terminations
           WHERE thread_id=? AND status='pending' ORDER BY id DESC LIMIT 1""",
        (thread_id,),
    ).fetchone()


def _is_supervisor_one_to_one(conn, thread_id: str) -> bool:
    """Exactly one agent, alone with the supervisor: a DM, not a work meeting."""
    actives = _active_roles(conn, thread_id)
    return (CONFIG.supervisor_role in actives
            and len([r for r in actives if r != CONFIG.supervisor_role]) == 1)


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
    elif _is_supervisor_one_to_one(conn, thread_id):
        # A meeting that is just you and a person is that person's, and they end
        # it when they are done — the way nobody hangs up a DM on a schedule.
        # This is not symmetry with agent meetings, it is the absence of it: an
        # agent-to-agent meeting is work with a defined end, and closing it is a
        # duty (see the wake ladder's close nudge). This is a conversation.
        #
        # Without this the supervisor's required=0 turns straight into a bug —
        # you are the only *required* attendee, and _propose_end auto-confirms
        # its own proposer, so a single agent would silently close the human's
        # thread mid-sentence and take the mailbox down with it. Left open costs
        # nothing now: no obligation is owed by the human, and the idle deadline
        # retires the thread on its own if it truly goes quiet.
        raise ValueError(
            "a one-to-one with the supervisor is theirs to end; leave it open")
    _resolve_obligations(
        conn, thread_id, role, resolution="termination proposal answered pending update",
    )
    now = store._iso()
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
    # A pending proposal is work owed by every other required attendee: their
    # vote. Wake them for it the way an invitation wakes its invitees — an end
    # is usually proposed precisely BECAUSE the counterpart finished its turn
    # and went idle, so without a wake the missing vote never arrives and the
    # meeting parks in termination_pending until someone happens to look
    # (M-001 handoff meeting, 2026-07-25: counter-proposal at 21:06, the other
    # agent already parked, still open three hours later).
    agent_roles = _known_roles(conn)
    voters = [r["role"] for r in conn.execute(
        """SELECT role FROM meeting_attendees
           WHERE thread_id=? AND required=1 AND checked_in_at IS NOT NULL
             AND stopped_at IS NULL AND role!=?""",
        (thread_id, role),
    ) if r["role"] in agent_roles]
    for voter in voters:
        # Unconditional re-arm (unlike the stale sweep's guarded one): a fresh
        # proposal is unambiguous new work even for a recently-acked attendee.
        conn.execute(
            """INSERT INTO meeting_wake_requests(thread_id,role,status,created_at)
               VALUES (?,?,'pending',?)
               ON CONFLICT(thread_id,role) DO UPDATE
               SET status='pending',created_at=excluded.created_at,
                   acknowledged_at=NULL""",
            (thread_id, voter, now),
        )
    _event(conn, thread_id, "termination_proposed", role,
           f"proposal #{proposal_id}: {resolution}", auth_nonce)
    return proposal_id


def propose_end(thread_id: str, *, role: str, resolution: str,
                db_path: Path | str | None = None) -> dict:
    from . import views
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        proposal_id = _propose_end(conn, thread_id, role, resolution)
    return {"proposal_id": proposal_id,
            "meeting": views.meeting_status(thread_id, db_path=db_path)}


def _close_meeting(conn, thread_id: str, resolution: str, actor: str,
                   auth_nonce: str | None = None) -> None:
    now = store._iso()
    # The only place a meeting closes, so the only place closed_at is written.
    conn.execute(
        "UPDATE meetings SET state='closed',updated_at=?,closed_at=? WHERE thread_id=?",
        (now, now, thread_id),
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
    # A closed meeting demands nothing. Settle every outstanding wake request,
    # or a force_close that races a pending invitation/vote wake leaves rows
    # the demand collector regenerates into wake attempts every tick, forever —
    # for a meeting no one can even check in to.
    conn.execute(
        """UPDATE meeting_wake_requests SET status='acknowledged',acknowledged_at=?
           WHERE thread_id=? AND status='pending'""",
        (now, thread_id),
    )
    _event(conn, thread_id, "closed", actor, resolution, auth_nonce)


def _missing_confirmations(conn, thread_id: str, proposal_id: int) -> list[str]:
    """Active required attendees whose confirm the pending proposal still
    lacks — the denominator _finalize_if_unanimous is waiting on, by name."""
    return [r["role"] for r in conn.execute(
        """SELECT role FROM meeting_attendees
           WHERE thread_id=? AND required=1 AND checked_in_at IS NOT NULL
             AND stopped_at IS NULL
             AND role NOT IN (SELECT role FROM meeting_termination_votes
                              WHERE proposal_id=? AND vote='confirm')
           ORDER BY role""",
        (thread_id, proposal_id),
    )]


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
            (store._iso(), proposal["id"]),
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
    now = store._iso()
    conn.execute(
        """INSERT OR REPLACE INTO meeting_termination_votes
           (proposal_id,role,vote,reason,auth_nonce,voted_at) VALUES (?,?,?,?,?,?)""",
        (proposal["id"], role, vote, reason,
         auth_nonce if role == supervisor else None, now),
    )
    # The vote is exactly what the proposal-time wake demanded; settle it, or
    # the wake ladder keeps climbing for an agent that already did the work
    # (check-in acks don't fire here — the voter was checked in all along).
    conn.execute(
        """UPDATE meeting_wake_requests SET status='acknowledged',acknowledged_at=?
           WHERE thread_id=? AND role=? AND status='pending'""",
        (now, thread_id, role),
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
    from . import views
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        closed = _vote_end(conn, thread_id, role, "confirm", None)
        waiting = _waiting_on_after_confirm(conn, thread_id, closed)
    return {"closed": closed, "waiting_on": waiting,
            "meeting": views.meeting_status(thread_id, db_path=db_path)}


def _waiting_on_after_confirm(conn, thread_id: str, closed: bool) -> list[str]:
    """Who a recorded-but-not-closing confirm is still waiting for. A confirm
    that returns closed=False with no explanation reads as a silent failure —
    the supervisor's console vote on the M-001 handoff meeting was accepted,
    changed nothing (their confirm is outside the required-attendee
    denominator), and showed nothing. Name the missing voters instead."""
    if closed:
        return []
    pending = _pending_termination(conn, thread_id)
    return _missing_confirmations(conn, thread_id, pending["id"]) if pending else []


def reject_end(thread_id: str, *, role: str, reason: str,
               db_path: Path | str | None = None) -> dict:
    from . import views
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        _vote_end(conn, thread_id, role, "reject", _clean(reason, "reason"))
    return {"closed": False,
            "meeting": views.meeting_status(thread_id, db_path=db_path)}


# --- pause / escalate -------------------------------------------------------

def pause_meeting(thread_id: str, *, role: str, reason: str,
                  db_path: Path | str | None = None) -> dict:
    from . import views
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        _attendee(conn, thread_id, role, checked_in=True)
        now = store._iso()
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
    return views.meeting_status(thread_id, db_path=db_path)


def escalate_meeting(thread_id: str, *, role: str, reason: str,
                     channel: str = "auto", pause: bool = True,
                     db_path: Path | str | None = None) -> dict:
    """Hand the meeting to a human. `channel` is `auto` (every available
    registered channel), `outbox`, or a channel the host registered."""
    from . import views
    valid = {"auto", OUTBOX_CHANNEL} | set(registered_channels())
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
            now = store._iso()
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
            "meeting": views.meeting_status(thread_id, db_path=db_path)}
