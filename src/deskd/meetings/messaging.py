"""Reading and speaking: updates, the bounded poll, and the core
protocol function `_send_update` (turn-taking, obligations, the
budget/consensus flip, supervisor visibility).
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Sequence

from .. import mailbox
from ..config import CONFIG, PROJECT_NAME
from . import store
from .escalations import _queue_escalation, dispatch_escalation
from .obligations import _discharge_obligations
from .store import (BROADCAST, MAX_WAIT_SECONDS, UPDATE_KINDS, _active_roles,
                    _agent_role, _attendee, _clean, _event, _has_supervisor,
                    _meeting, _meeting_roles, _mode, _supervisor_claim,
                    _visible_message_sql, connect)
from .sweep import _sweep_timeouts

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
        # allow_stopped: reading is side-effect-free, and the protocol REQUIRES
        # a final `updates --mark-read` after a meeting closes — which stops
        # every attendee, so the mandatory last read used to be the one call
        # guaranteed to raise (parlay task #46, 2026-07-20). A stopped attendee
        # may always read what it was part of; only speaking needs a live seat.
        attendee = _attendee(conn, thread_id, role, checked_in=True,
                             allow_stopped=True)
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
            now = store._iso()
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

def _revive_idle_thread(conn: sqlite3.Connection, thread_id: str) -> bool:
    """A supervisor message IS the supervisor resuming. Idle-paused only.

    The idle deadline retires a thread lazily, on read, and touches only
    `mailbox_threads` — `meetings.state` stays `active`. The console reads state,
    so it renders a live composer and no resume button, then the send fails with
    "thread is paused: idle timeout" and offers no way out. That is the same
    dead end the turn-taking gate produced, reached from a different direction:
    the one surface a human has, refusing the human.

    Resuming stays a human act (agents open a new meeting instead) — this does
    not widen that, it just stops making the human say it twice. Writing to the
    thread IS the intent; a button that only ever gets pressed immediately before
    the message is ceremony.

    Idle ONLY. A budget-exhausted or explicitly paused thread stays paused: those
    are decisions, not lapsed attention, and a message must not quietly overturn
    them. Refreshing `expires_at` is mandatory, not tidiness — a thread paused ON
    its deadline still carries a past one, so the very next read would retire it
    again and this would be a no-op (the same trap `resume` documents).
    """
    thread = conn.execute(
        "SELECT * FROM mailbox_threads WHERE id=?", (thread_id,)).fetchone()
    # Direct read, deliberately: _meeting() already refreshed this thread, so the
    # status below is current — and the retiring helper cannot report WHY it
    # paused, which is the one thing this needs to know.
    if not thread or thread["status"] != "paused":
        return False
    if thread["stop_reason"] != "idle timeout":
        return False
    now = store._now()
    conn.execute(
        """UPDATE mailbox_threads SET status='open',stop_reason=NULL,stopped_by=NULL,
           updated_at=?,expires_at=? WHERE id=?""",
        (store._iso(now), mailbox._deadline(now, thread["idle_minutes"]), thread_id),
    )
    _event(conn, thread_id, "resumed", CONFIG.supervisor_role,
           "supervisor wrote to an idle-paused meeting")
    return True


def _send_update(conn: sqlite3.Connection, thread_id: str, role: str, body: str,
                 kind: str,
                 auth_nonce: str | None = None,
                 reply_to: int | None = None,
                 resolves: Sequence[int] | None = None) -> tuple[int, int | None]:
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
        # termination_pending accepts REPLIES: the final response to an existing
        # message is the substance of the two-sided termination handshake, and
        # refusing it broke the evidence chain at its last link (a trader's
        # acceptance of a correction could only live in its private journal —
        # parlay task #46, 2026-07-20). New topics stay refused: reopening
        # discussion is what reject_end is for.
        terminal_reply = (meeting["state"] == "termination_pending"
                          and reply_to is not None)
        if meeting["state"] not in {"active", "consensus"} and not terminal_reply:
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
        # No turn-taking gate here, deliberately. Strict alternation used to be
        # enforced at this point: an outstanding obligation refused EVERY send,
        # so whoever was still present got silenced on behalf of whoever had
        # gone quiet — and when the debt was the supervisor's, the console could
        # not speak at all. That gate was a pull-era proxy for "you may not have
        # seen their message yet"; delivery receipts answer that question
        # precisely now, and the orchestrator pushes.
        #
        # Liveness was never this gate's job: an obligation carries an SLA that
        # becomes an urgent task and climbs the wake ladder. What the gate did
        # produce was dropped messages, because a rejected insert is the ONE way
        # a message is lost here — once the row exists, delivery + wake land it
        # however late the sender was. So the debt is now tracked and nudged,
        # never enforced at the door. See _discharge_obligations: settling it is
        # the sender's judgement, which is the only place that knowledge lives.
        recipient = next(r for r in active_roles if r != role)
    else:
        recipient = BROADCAST
    if role == supervisor:
        _revive_idle_thread(conn, thread_id)
    # _insert_message documents that callers "must ... have refreshed `thread`";
    # handing it a raw row is what let a meeting write past its idle deadline.
    thread = mailbox._refresh_thread(conn, thread_id)
    message_id = mailbox._insert_message(
        conn, thread, sender=role, recipient=recipient, kind=kind,
        body=_clean(body, "message"), reply_to=reply_to,
        allow_authenticated_supervisor=(role == supervisor),
        # An agent replying to the supervisor addresses a human who is sitting
        # in this meeting; it never speaks as one. Gated on actual attendance so
        # the supervisor cannot be addressed in a meeting it is not in.
        allow_supervisor_recipient=(recipient == supervisor
                                    and _has_supervisor(conn, thread_id)),
        allow_reference_reply=(reply_to is not None),
    )
    now = store._iso()
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
    if resolves:
        # Settled by the sender's own judgement, alongside (not instead of) the
        # reply_to link below: one answer routinely closes several outstanding
        # questions, and reply_to points at exactly one. Conflating "what am I
        # replying to" with "what did I just settle" is what forced the ping-pong.
        _discharge_obligations(conn, thread_id, role, resolves, message_id, now)
    if reply_to is not None:
        conn.execute(
            """UPDATE meeting_response_obligations
               SET status='resolved',resolved_at=?,resolution='explicit reply',
                   resolved_by_message_id=?
               WHERE message_id=? AND owed_by=? AND status='pending'""",
            (now, message_id, reply_to, role),
        )
    # INDEPENDENT of the settle above, not the other arm of it. These two were
    # `if`/`elif`, and that quietly ended turn-taking in the commonest case a
    # conversation has: answering a question with a question. Observed live on
    # 2026-07-26 — the console sets reply_to on a supervisor message whenever the
    # supervisor owes a reply, so a supervisor asking an analyst "can we buy
    # Apple?" while discharging the previous turn settled its own debt and
    # created none. No obligation row, therefore no SLA, no overdue sweep, no
    # owed_reply wake demand: the analyst read it, never answered in thread, and
    # nothing in the engine noticed. Agent-to-agent pairs had the identical hole.
    #
    # What a message ANSWERS and what it ASKS are two different facts about it,
    # and a protocol that stores them in one slot can only ever record one. That
    # is the same conflation `resolves` was split out of reply_to to undo (see
    # above), one layer down: reply_to settles, mode creates, neither speaks for
    # the other. `_mode` has documented the contract all along — "one_to_one
    # imposes strict turn-taking (every message owes a reply)" — this is the
    # first code that implements it.
    #
    # `recipient in active_roles` is the whole guard, and it is what stops this
    # minting debt nobody can pay. BROADCAST is owed by nobody, and a reply to a
    # message from an attendee who has since LEFT must not obligate them: the
    # leave path just waived their debts, and re-arming one behind them would
    # wake a role that has no seat. The supervisor is obligated like any other
    # party (this already happened for non-reply messages) — a question an agent
    # asks a human must not evaporate either — and orchestration floors a
    # supervisor-owed wake at the first rung that leaves the machine rather than
    # spawning a session for a role no session may claim (wake.py::_role_floor).
    if mode == "one_to_one" and not preamble and recipient in active_roles:
        due_at = store._iso(store._now() + dt.timedelta(seconds=meeting["wait_timeout_seconds"]))
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
                resolves: Sequence[int] | None = None,
                db_path: Path | str | None = None) -> dict:
    """Say something. `reply_to` threads it; `resolves` settles the debts this
    message discharges — several at once, and independently of what it replies
    to. Nothing here is refused for speaking out of turn.
    """
    from . import views
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        message_id, escalation_id = _send_update(
            conn, thread_id, role, body, kind, reply_to=reply_to,
            resolves=resolves,
        )
    if escalation_id:
        dispatch_escalation(escalation_id, db_path=db_path)
    status = views.meeting_status(thread_id, db_path=db_path)
    out = {"message_id": message_id, "meeting": status}
    if status["meeting"]["state"] in {"active", "consensus"}:
        out["next"] = (
            f"meeting still open: run `{PROJECT_NAME} meeting updates ... "
            f"--mark-read --wait-seconds {MAX_WAIT_SECONDS}` once more before "
            f"ending this session; unread messages past the SLA trigger a wake "
            f"request + escalation"
        )
    return out


def resolve_obligations(thread_id: str, *, role: str, message_ids: Sequence[int],
                        covered_by: int,
                        db_path: Path | str | None = None) -> dict:
    """Settle debts an already-sent message of yours turned out to answer.

    The retroactive half of `send_update(resolves=...)`, for when you notice
    after the fact — a question you had already covered, or two questions your
    one answer addressed. Saying nothing new is the point: forcing a fresh
    message just to clear the ledger trains agents to emit "as I said above"
    noise, and an obligation left pending because replying felt redundant is a
    reply the counterpart never gets.

    The debt must be yours and the citing message must post-date it; see
    _discharge_obligations.
    """
    from . import views
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        _attendee(conn, thread_id, role, checked_in=True)
        covering = conn.execute(
            "SELECT * FROM mailbox_messages WHERE id=? AND thread_id=?",
            (covered_by, thread_id),
        ).fetchone()
        if not covering:
            raise ValueError(f"#{covered_by} is not a message in this meeting")
        if covering["sender"] != role:
            raise ValueError(
                f"#{covered_by} is {covering['sender']}'s message; cite your own")
        discharged = _discharge_obligations(
            conn, thread_id, role, message_ids, covered_by)
        _event(conn, thread_id, "obligations_discharged", role,
               f"#{covered_by} covers " + ", ".join(f"#{m}" for m in discharged))
    return {"discharged": discharged, "covered_by": covered_by,
            "meeting": views.meeting_status(thread_id, db_path=db_path)}


def submit_position(thread_id: str, *, role: str, body: str,
                    reply_to: int | None = None,
                    db_path: Path | str | None = None) -> dict:
    return send_update(
        thread_id, role=role, body=body, kind="position",
        reply_to=reply_to, db_path=db_path,
    )
