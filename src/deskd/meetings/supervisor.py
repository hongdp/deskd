"""The supervisor boundary: the action-verb allowlist, the uninvited
join, and the adapter that dispatches a *verified* claim to the meeting
operation it names. Verification lives in `deskd.auth`; nothing here
verifies.

Top of the package layering — this module imports every protocol module
and nothing imports it. It is roadmap P4's supervisor-boundary module in
embryo: when a generic `deskd.supervisor` with verb registration is
extracted, this file's contents move rather than untangle.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .. import auth, mailbox
from ..config import CONFIG
from . import store
from .lifecycle import _call_meeting, _check_in, _leave
from .messaging import _meeting_updates, _send_update
from .obligations import _waive_pending_obligations
from .store import (DEFAULT_CONSENSUS_THRESHOLD, DEFAULT_IDLE_MINUTES,
                    DEFAULT_WAIT_TIMEOUT_SECONDS, _active_roles, _clean,
                    _event, _meeting, _mode, _supervisor_claim, connect)
from .termination import (_close_meeting, _propose_end, _vote_end,
                          _waiting_on_after_confirm)
from .views import meeting_status

#: Fields each supervisor action must carry INSIDE the signed payload, passed to
#: the auth verifier along with the allowlist below. `deskd.auth` knows no verbs
#: — this module owns them, so a new privileged verb cannot widen the trust
#: boundary without being declared here. Requiring the fields up front means a
#: handler can trust they exist rather than KeyError-ing mid-mutation; keep this
#: map in sync when adding an action. Every action except `call` (which creates
#: the meeting) targets an existing meeting.
REQUIRED_SIGNED_FIELDS: dict[str, tuple[str, ...]] = {
    "call": ("agenda", "attendees"),
    "join": ("meeting_id",),
    "leave": ("meeting_id", "reason"),
    "check_in": ("meeting_id",),
    "read": ("meeting_id",),
    "send": ("meeting_id", "body"),
    "position": ("meeting_id", "body"),
    "propose_end": ("meeting_id", "resolution"),
    "confirm_end": ("meeting_id",),
    "reject_end": ("meeting_id",),
    "resume": ("meeting_id",),
    "force_close": ("meeting_id", "reason"),
}

#: Meeting actions a verified supervisor assertion may carry. Derived from the
#: map above so the two can never drift apart.
SUPERVISOR_ACTIONS = frozenset(REQUIRED_SIGNED_FIELDS)


def _supervisor_join(conn: sqlite3.Connection, thread_id: str, auth_nonce: str) -> None:
    """The supervisor may drop into a meeting it was never invited to."""
    supervisor = CONFIG.supervisor_role
    meeting = _meeting(conn, thread_id)
    if meeting["state"] in {"closed", "paused"}:
        raise ValueError(f"cannot join while meeting is {meeting['state']}")
    _supervisor_claim(conn, auth_nonce, {"join"}, thread_id=thread_id)
    if meeting["state"] == "escalated":
        # An escalation is a page FOR the human; the human arriving IS the
        # resolution. Gating the join on a manual resume turned answering a
        # page into a two-step the console had to explain away (found
        # 2026-07-21: the supervisor's reply bounced with "cannot join").
        # Explicit `paused` stays a hard gate above: pausing is a supervisor
        # choice, and joining must not silently undo it.
        now_dt = store._now()
        next_state = ("consensus" if meeting["messages_remaining"] <=
                      meeting["consensus_threshold"] else "active")
        conn.execute(
            "UPDATE meetings SET state=?,updated_at=? WHERE thread_id=?",
            (next_state, store._iso(now_dt), thread_id),
        )
        thread = mailbox._refresh_thread(conn, thread_id)
        conn.execute(
            """UPDATE mailbox_threads SET status='open',stop_reason=NULL,
               stopped_by=NULL,updated_at=?,expires_at=? WHERE id=?""",
            (store._iso(now_dt), mailbox._deadline(now_dt, thread["idle_minutes"]),
             thread_id),
        )
        _event(conn, thread_id, "resumed", supervisor,
               "supervisor arrival resumed an escalated meeting", auth_nonce)
    previous_mode = _mode(conn, thread_id)
    now = store._iso()
    attendee = conn.execute(
        "SELECT * FROM meeting_attendees WHERE thread_id=? AND role=?",
        (thread_id, supervisor),
    ).fetchone()
    if attendee and attendee["checked_in_at"] and not attendee["stopped_at"]:
        return
    # required=0: a human is not a quorum condition. `required` has existed since
    # the first schema and every write hardcoded 1, so the knob was built and
    # never turned — which is why a watching supervisor silently became something
    # the agents had to wait for. Two places counted on it: `waiting` held the
    # meeting until the human checked in, and _finalize_if_unanimous demanded the
    # human's vote to close, so a supervisor who read the thread and walked away
    # left the agents unable either to start or to finish. Neither is a decision
    # anyone made; both fall out of the hardcoded 1.
    if attendee:
        conn.execute(
            """UPDATE meeting_attendees SET required=0,checked_in_at=?,
               checkin_auth_nonce=?,stopped_at=NULL WHERE thread_id=? AND role=?""",
            (now, auth_nonce, thread_id, supervisor),
        )
    else:
        conn.execute(
            """INSERT INTO meeting_attendees
               (thread_id,role,required,invited_at,checked_in_at,checkin_auth_nonce)
               VALUES (?,?,0,?,?,?)""",
            (thread_id, supervisor, now, now, auth_nonce),
        )
    _event(conn, thread_id, "join", supervisor, "supervisor joined meeting", auth_nonce)
    new_mode = _mode(conn, thread_id)
    if new_mode == "multi" and previous_mode != "multi":
        _waive_pending_obligations(
            conn, thread_id, "supervisor joined; multi-party replies are optional")
    if meeting["state"] == "waiting" and len(_active_roles(conn, thread_id)) >= 2:
        missing = conn.execute(
            """SELECT COUNT(*) AS n FROM meeting_attendees
               WHERE thread_id=? AND required=1 AND checked_in_at IS NULL
                 AND stopped_at IS NULL""",
            (thread_id,),
        ).fetchone()["n"]
        if not missing:
            conn.execute(
                "UPDATE meetings SET state='active',updated_at=? WHERE thread_id=?",
                (now, thread_id),
            )
    if new_mode != previous_mode:
        _event(conn, thread_id, "mode_changed", "system", new_mode)


# --- supervisor adapter -----------------------------------------------------
# Verification lives in deskd.auth; this is only the dispatch from a *verified*
# claim to the meeting operation it names.

def _apply_supervisor_payload(verified: auth.VerifiedAssertion, *,
                              db_path: Path | str | None = None) -> dict:
    supervisor = CONFIG.supervisor_role
    # Burn the nonce in its own committed transaction BEFORE running the action.
    # If the action then fails, the nonce stays spent: a rejected assertion must
    # never become replayable by failing on purpose.
    with connect(db_path, write=True) as conn:
        auth.consume_nonce(verified, conn=conn)
    payload = verified.payload
    action = verified.action
    nonce = verified.nonce
    if action == "call":
        if "attendees" not in payload:
            raise ValueError("supervisor call assertion must name its attendees")
        return _call_meeting(
            agenda=payload["agenda"], called_by=supervisor,
            attendees=list(payload["attendees"]),
            meeting_type=payload.get("meeting_type", "ad-hoc"),
            priority=payload.get("priority", "urgent"),
            idle_minutes=int(payload.get("idle_minutes", DEFAULT_IDLE_MINUTES)),
            max_messages=payload.get("max_messages"),
            consensus_threshold=int(
                payload.get("consensus_threshold", DEFAULT_CONSENSUS_THRESHOLD)),
            wait_timeout_seconds=int(
                payload.get("wait_timeout_seconds", DEFAULT_WAIT_TIMEOUT_SECONDS)),
            auth_nonce=nonce, db_path=db_path,
        )
    thread_id = payload["meeting_id"]
    if action == "read":
        return _meeting_updates(
            thread_id, role=supervisor, mark_read=bool(payload.get("mark_read", True)),
            auth_nonce=nonce, db_path=db_path,
        )
    result: dict        # per-action payload; the shape follows the action
    with connect(db_path, write=True) as conn:
        if action == "join":
            _supervisor_join(conn, thread_id, nonce)
            result = {"joined": True}
        elif action == "leave":
            _leave(conn, thread_id, supervisor, _clean(payload["reason"], "reason"), nonce)
            result = {"left": True}
        elif action == "check_in":
            _check_in(conn, thread_id, supervisor, nonce)
            result = {"checked_in": True}
        elif action in {"send", "position"}:
            kind = "position" if action == "position" else payload.get("kind", "decision")
            message_id, escalation_id = _send_update(
                conn, thread_id, supervisor, payload["body"], kind, nonce,
                reply_to=payload.get("reply_to"),
            )
            result = {"message_id": message_id, "escalation_id": escalation_id}
        elif action == "propose_end":
            result = {"proposal_id": _propose_end(
                conn, thread_id, supervisor, payload["resolution"], nonce,
            )}
        elif action in {"confirm_end", "reject_end"}:
            vote = "confirm" if action == "confirm_end" else "reject"
            closed = _vote_end(
                conn, thread_id, supervisor, vote, payload.get("reason"), nonce,
            )
            result = {"closed": closed}
            if vote == "confirm":
                result["waiting_on"] = _waiting_on_after_confirm(
                    conn, thread_id, closed)
        elif action == "resume":
            meeting = _meeting(conn, thread_id)
            now = store._iso()
            if meeting["state"] == "closed":
                # Reopening a CLOSED meeting, not just un-pausing a live one.
                # _close_meeting stops every attendee and spends the thread, so
                # the old resume left a meeting that was open and empty: nobody
                # active, `len(active_roles) < 2`, not a single message
                # accepted. It looked resumed and could not be spoken in.
                #
                # This is the counterweight to agents closing meetings without
                # the supervisor's vote (attendees.required=0). That is only
                # safe if a person can get back in — otherwise "the agents
                # agreed we were done" is final, and being outvoted about your
                # own reading is not a thing a human should have to argue with.
                conn.execute(
                    "UPDATE meeting_attendees SET stopped_at=NULL WHERE thread_id=?",
                    (thread_id,),
                )
                # Budget resets. It was spent reaching a conclusion that is now
                # being reopened, and inheriting the remainder would reopen
                # straight into `consensus` — or into "message budget
                # exhausted", which is reopening into the closed state again.
                conn.execute(
                    "UPDATE mailbox_threads SET message_count=0 WHERE id=?",
                    (thread_id,),
                )
                meeting = _meeting(conn, thread_id)
            next_state = ("consensus" if meeting["messages_remaining"] <=
                          meeting["consensus_threshold"] else "active")
            # closed_at is cleared: a live meeting has not ended, so it cannot
            # carry an end time. Leaving the old one is a row that contradicts
            # itself, and list_meetings ranks the live block by
            # COALESCE(closed_at, created_at) — a reopened meeting would sort by
            # when it used to finish rather than when it started.
            conn.execute(
                "UPDATE meetings SET state=?,updated_at=?,closed_at=NULL "
                "WHERE thread_id=?",
                (next_state, now, thread_id),
            )
            # Reopening must also grant a fresh idle window, as every mailbox
            # status write does. A thread paused ON the deadline still carries an
            # expires_at in the past, so leaving it would have the next read
            # retire the meeting again immediately — resumption would be a no-op
            # and the supervisor's override decorative.
            thread = mailbox._refresh_thread(conn, thread_id)
            conn.execute(
                """UPDATE mailbox_threads SET status='open',stop_reason=NULL,
                   stopped_by=NULL,updated_at=?,expires_at=? WHERE id=?""",
                (now, mailbox._deadline(store._now(), thread["idle_minutes"]), thread_id),
            )
            _event(conn, thread_id, "resumed", supervisor,
                   payload.get("reason", "supervisor resumed"), nonce)
            result = {"resumed": True}
        elif action == "force_close":
            _close_meeting(conn, thread_id, _clean(payload["reason"], "reason"),
                           supervisor, nonce)
            result = {"closed": True}
        else:
            raise ValueError(f"unimplemented supervisor action: {action}")
    result["meeting"] = meeting_status(thread_id, db_path=db_path)
    return result


def apply_supervisor_assertion_bytes(assertion: bytes, signature_input: bytes, *,
                                     db_path: Path | str | None = None) -> dict:
    """Verify a signed supervisor assertion and apply the meeting action it
    names. Strict (trusted-device) mode."""
    verified = auth.verify_bytes(
        assertion, signature_input, actions=SUPERVISOR_ACTIONS,
        required_fields=REQUIRED_SIGNED_FIELDS)
    return _apply_supervisor_payload(verified, db_path=db_path)


def apply_supervisor_assertion(assertion_path: str | Path, signature_path: str | Path,
                               *, db_path: Path | str | None = None) -> dict:
    return apply_supervisor_assertion_bytes(
        Path(assertion_path).read_bytes(), Path(signature_path).read_bytes(),
        db_path=db_path,
    )


def apply_simple_supervisor_action(action_payload: dict, *,
                                   db_path: Path | str | None = None) -> dict:
    """Apply a web-authenticated supervisor action in explicit simplified mode.

    The caller (the web adapter) has already gated on the access code; auth
    mints the short-lived, single-use claim so the ledger, the nonce burn, and
    every assertion binding check behave exactly as in strict mode.
    """
    verified = auth.mint_simple(
        action_payload, actions=SUPERVISOR_ACTIONS,
        required_fields=REQUIRED_SIGNED_FIELDS)
    return _apply_supervisor_payload(verified, db_path=db_path)
