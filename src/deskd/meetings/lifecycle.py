"""Calling a meeting, discovering yours, and the check-in / leave
attendance protocol.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from .. import mailbox
from ..config import CONFIG
from . import store
from .escalations import _queue_escalation, dispatch_escalation
from .obligations import _waive_pending_obligations
from .store import (BROADCAST, DEFAULT_CONSENSUS_THRESHOLD,
                    DEFAULT_IDLE_MINUTES, DEFAULT_MAX_MESSAGES,
                    DEFAULT_REVIEW_MAX_MESSAGES, DEFAULT_WAIT_TIMEOUT_SECONDS,
                    MEETING_TYPES, MIN_CONSENSUS_THRESHOLD,
                    MIN_WAIT_TIMEOUT_SECONDS, _active_roles, _agent_role,
                    _attendee, _clean, _event, _has_supervisor, _known_roles,
                    _meeting, _meeting_projection, _meeting_roles, _mode,
                    _stamp_notifications, _supervisor_claim, _thread_last_activity,
                    _visible_message_sql, connect)
from .sweep import _sweep_timeouts
from .termination import _finalize_if_unanimous, _pending_termination

# --- calling a meeting ------------------------------------------------------

def _call_meeting(*, agenda: str, called_by: str, attendees: list[str],
                  meeting_type: str = "ad-hoc", priority: str = "normal",
                  idle_minutes: int = DEFAULT_IDLE_MINUTES,
                  max_messages: int | None = None,
                  consensus_threshold: int = DEFAULT_CONSENSUS_THRESHOLD,
                  wait_timeout_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS,
                  auth_nonce: str | None = None,
                  db_path: Path | str | None = None) -> dict:
    from . import views
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
            now = store._iso()
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
                       VALUES (?,?,?,?,?,?)""",
                    (thread["id"], role, int(role != supervisor), now,
                     now if role == called_by else None,
                     auth_nonce if role == supervisor and role == called_by else None),
                )
            _event(conn, thread["id"], "called", called_by, agenda, auth_nonce)
            # The INVITATION is itself a wake demand, whatever the priority:
            # machine rungs only (hook -> resume -> spawn), nobody paged. These
            # used to be urgent-only, which left a normal call with NO path to
            # its invitees except the attendance-timeout sweep — and that sweep
            # only ran when some session happened to read the meeting, so on a
            # quiet desk a routine weekly review took 17 minutes to convene
            # (2026-07-19: called 22:00:39, sweep finally ran 22:14:17, trader
            # checked in 22:17:33). The caller is present already; waking it
            # would be noise. The supervisor is a human and is never woken.
            for role in sorted((roles & agent_roles) - {called_by}):
                conn.execute(
                    """INSERT OR IGNORE INTO meeting_wake_requests
                       (thread_id,role,status,created_at) VALUES (?,?,'pending',?)""",
                    (thread["id"], role, now),
                )
            if priority == "urgent":
                # Urgency changes who ELSE hears about it, not whether the
                # machine tries: the human escalation rides on top of the wake.
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
    return views.meeting_status(thread["id"], db_path=db_path)


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
                       t.stop_reason AS thread_stop_reason,
                       t.stopped_by AS thread_stopped_by,
                       t.expires_at AS thread_expires_at,
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
        return [_meeting_projection(r) for r in rows]


# --- check-in / join / leave ------------------------------------------------

def _check_in(conn: sqlite3.Connection, thread_id: str, role: str,
              auth_nonce: str | None = None) -> None:
    supervisor = CONFIG.supervisor_role
    meeting = _meeting(conn, thread_id)
    if meeting["state"] in {"closed", "paused", "escalated"}:
        raise ValueError(f"cannot check in while meeting is {meeting['state']}")
    if meeting["thread_status"] != "open":
        raise ValueError(
            "cannot check in while message thread is "
            f"{meeting['thread_status']}: "
            f"{meeting['thread_stop_reason'] or 'no reason recorded'}")
    attendee = _attendee(conn, thread_id, role)
    if attendee["checked_in_at"] and not attendee["stopped_at"]:
        return
    if role == supervisor and not auth_nonce:
        raise ValueError("supervisor check-in lacks a verified assertion")
    if role == supervisor:
        _supervisor_claim(conn, auth_nonce, {"check_in"}, thread_id=thread_id)
    previous_mode = _mode(conn, thread_id)
    now = store._iso()
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
    if (meeting["state"] == "waiting" and not missing
            and len(_active_roles(conn, thread_id)) >= 2):
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
    from . import views
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        _check_in(conn, thread_id, role)
    return views.meeting_status(thread_id, db_path=db_path)


def _leave(conn: sqlite3.Connection, thread_id: str, role: str, reason: str,
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
        idle_cutoff = store._now() - dt.timedelta(seconds=meeting["wait_timeout_seconds"])
        if _thread_last_activity(conn, thread_id) > idle_cutoff:
            raise ValueError(
                "meeting thread is still active; leaving is only allowed once "
                "the whole thread is idle — propose end or escalate instead"
            )
    previous_mode = _mode(conn, thread_id)
    now = store._iso()
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
    from . import views
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        _leave(conn, thread_id, role, reason)
    return views.meeting_status(thread_id, db_path=db_path)
