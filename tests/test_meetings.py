"""Bounded meetings: every test pins one bound.

A meeting is the engine's only multi-agent conversation, and the whole claim
made for it is that it is *bounded by construction* — idle deadline, message
budget, automatic consensus, one position each, a mutual termination handshake.
"Bounded" is not a property you can observe by watching a meeting behave nicely;
it is a property of the paths that DON'T exist. So most of these tests assert a
rejection, and each one names the specific bound it guards.

Two things shape how they are written:

1. **Three roles, and no test may care which.** deskd was extracted from a host
   with two hardcoded roles; a third role's meeting call was silently denied —
   no attendee rows, no obligations, no wakes. That bug was invisible to a
   two-role suite. The caller-parameterized tests below exist solely to keep it
   dead, so they iterate `conftest.ROLES` rather than naming a role.

2. **`conn` is deliberately not used here.** That fixture holds `BEGIN
   IMMEDIATE` for the life of the test, and every meetings entry point opens its
   own writing connection — so a test that used both would block on the lock for
   `busy_timeout` and then fail with "database is locked". Tests that need to
   look at (or inject into) the ledger open a short-lived connection instead.

Time is injected by back-dating the durable row, never by sleeping: the engine
reads the clock through `_now()` at many layers, and a test that sleeps its way
to an SLA is a test that takes five minutes to run.
"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager

import pytest

from conftest import ROLES
from deskd import mailbox, meetings
from deskd.config import CONFIG

ROLE_NAMES = tuple(r.name for r in ROLES)


# --- helpers ----------------------------------------------------------------

def _past(seconds: float) -> str:
    """An ISO timestamp `seconds` in the past, in the engine's canonical form."""
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")


@contextmanager
def _db():
    """A short-lived write connection. See the module docstring for why the
    `conn` fixture cannot be used alongside the meetings API."""
    with meetings.connect(write=True) as conn:
        yield conn


def _start(agenda: str, attendees, called_by: str | None = None, **kwargs) -> str:
    """Call a meeting and check everyone in. Returns the thread id."""
    attendees = list(attendees)
    called_by = called_by or attendees[0]
    status = meetings.call_meeting(
        agenda=agenda, called_by=called_by, attendees=attendees, **kwargs)
    thread_id = status["meeting"]["thread_id"]
    for role in attendees:
        if role != called_by:
            meetings.check_in(thread_id, role=role)
    return thread_id


def _state(thread_id: str) -> str:
    return meetings.meeting_status(thread_id)["meeting"]["state"]


def _thread_row(thread_id: str) -> dict:
    """The raw thread row, read WITHOUT going through `mailbox._refresh_thread`.

    Deliberate: the mailbox refreshes (and retires) a thread on read, so calling
    a mailbox read path here would apply the very deadline a test is trying to
    observe the meetings layer applying for itself.
    """
    with _db() as conn:
        return dict(conn.execute(
            "SELECT * FROM mailbox_threads WHERE id=?", (thread_id,)).fetchone())


def _events(thread_id: str, event: str) -> list[dict]:
    return [e for e in meetings.meeting_transcript(thread_id)["events"]
            if e["event"] == event]


def _exhaust_budget(thread_id: str) -> None:
    """Spend every message of a 3-attendee / max_messages=6 / threshold=2
    meeting, leaving it in `consensus` with a paused thread."""
    for i, role in enumerate(["alpha", "beta", "gamma", "alpha"]):
        meetings.send_update(thread_id, role=role, kind="evidence",
                             body=f"distinct evidence number {i}")
    for role in ("beta", "gamma"):
        meetings.send_update(thread_id, role=role, kind="position",
                             body=f"{role} final position")


# --- 1. any role may call a meeting -----------------------------------------
# THE extraction test. The host deskd came from hardcoded two roles, so the
# third role's meeting call was accepted and then silently did nothing: no
# attendee rows, no projection to the others, no wakes. Every assertion below
# was true for the two blessed roles and false for the third.

@pytest.mark.parametrize("caller", ROLE_NAMES)
def test_any_role_can_call_a_meeting_and_be_seen_by_the_others(desk, caller):
    """A meeting called by ANY role must reach the board and project its
    messages — the third role must not be a second-class caller."""
    others = [r for r in ROLE_NAMES if r != caller]
    status = meetings.call_meeting(agenda=f"{caller} calls this one",
                                   called_by=caller)
    thread_id = status["meeting"]["thread_id"]

    assert status["meeting"]["called_by"] == caller
    # Attendee rows for everyone, and the caller checked in by calling.
    invited = {a["role"]: a for a in status["attendees"]}
    assert set(invited) == set(ROLE_NAMES)
    assert invited[caller]["checked_in_at"] is not None
    assert all(invited[r]["checked_in_at"] is None for r in others)

    # The board: every invited role discovers it, attributed to the caller.
    for role in ROLE_NAMES:
        mine = [m for m in meetings.discover(role) if m["thread_id"] == thread_id]
        assert len(mine) == 1, f"{role} cannot see the meeting {caller} called"
        assert mine[0]["called_by"] == caller

    for role in others:
        meetings.check_in(thread_id, role=role)
    assert _state(thread_id) == "active"

    # Projection: what the caller says must reach both other attendees.
    meetings.send_update(thread_id, role=caller, kind="evidence",
                         body=f"evidence from {caller} for the others")
    for role in others:
        seen = meetings.meeting_updates(thread_id, role=role)["messages"]
        assert [m["sender"] for m in seen] == [caller], (
            f"{caller}'s message did not project to {role}")


@pytest.mark.parametrize("caller", ROLE_NAMES)
def test_urgent_call_by_any_role_wakes_every_other_attendee(desk, caller):
    """"No wakes" was the third role's other silent symptom: the meeting existed
    but nobody was ever told, so it sat in `waiting` until it timed out."""
    others = [r for r in ROLE_NAMES if r != caller]
    status = meetings.call_meeting(agenda=f"{caller} needs everyone now",
                                   called_by=caller, priority="urgent")
    thread_id = status["meeting"]["thread_id"]

    for role in others:
        woken = [w["thread_id"] for w in meetings.wake_requests(role)]
        assert thread_id in woken, f"{caller}'s urgent call did not wake {role}"
    # The caller is present already; waking it would be noise.
    assert thread_id not in [w["thread_id"] for w in meetings.wake_requests(caller)]


# --- 2. check-in / quorum ---------------------------------------------------

def test_quorum_needs_every_required_attendee_not_merely_enough_of_them(desk):
    """Quorum is "all required attendees", not a headcount. With two of three
    present a headcount rule would open discussion and cut the third out."""
    status = meetings.call_meeting(agenda="quorum shape", called_by="alpha")
    thread_id = status["meeting"]["thread_id"]
    assert _state(thread_id) == "waiting"

    meetings.check_in(thread_id, role="beta")
    assert _state(thread_id) == "waiting", (
        "two of three present is below quorum; gamma is still required")

    meetings.check_in(thread_id, role="gamma")
    assert _state(thread_id) == "active"
    assert meetings.meeting_status(thread_id)["mode"] == "multi"
    assert len(_events(thread_id, "quorum")) == 1


def test_a_required_attendee_that_never_arrives_keeps_it_waiting_and_escalates(desk):
    """The bound on `waiting`: a meeting whose counterpart never shows must not
    sit there forever, and must not quietly proceed without them either. It
    stays `waiting` and becomes a human's problem."""
    status = meetings.call_meeting(agenda="beta never arrives", called_by="alpha",
                                   attendees=["alpha", "beta"],
                                   wait_timeout_seconds=30)
    thread_id = status["meeting"]["thread_id"]
    with _db() as conn:
        conn.execute("UPDATE meetings SET created_at=? WHERE thread_id=?",
                     (_past(600), thread_id))

    meetings._sweep_timeouts()

    assert _state(thread_id) == "waiting", "a no-show must never yield quorum"
    reasons = [e["reason"] for e in meetings.list_escalations(thread_id)]
    assert any("attendance timeout" in r and "beta" in r for r in reasons), reasons
    # The escalation is paired with a wake, so the absence is also actionable.
    assert thread_id in [w["thread_id"] for w in meetings.wake_requests("beta")]


# --- 3. mandatory one-to-one replies, and their SLA -------------------------

def test_a_one_to_one_message_creates_a_tracked_response_obligation(desk):
    """With exactly two actives every message owes a reply, and the obligation
    is durable — the SLA has to survive the sender's session ending."""
    thread_id = _start("one to one", ["alpha", "beta"])
    assert meetings.meeting_status(thread_id)["mode"] == "one_to_one"

    sent = meetings.send_update(thread_id, role="alpha", kind="question",
                                body="does the position still hold")
    owed = meetings.meeting_status(thread_id)["response_obligations"]
    assert [(o["owed_by"], o["status"], o["message_id"]) for o in owed] == [
        ("beta", "pending", sent["message_id"])]

    replied = meetings.send_update(thread_id, role="beta", kind="answer",
                                   body="it holds, with one caveat",
                                   reply_to=sent["message_id"])
    owed = meetings.meeting_status(thread_id)["response_obligations"]
    assert [(o["owed_by"], o["status"]) for o in owed] == [("beta", "resolved")]
    assert owed[0]["resolved_by_message_id"] == replied["message_id"]
    # A reply discharges an obligation; it must not mint a fresh one, or two
    # agents could never stop taking turns.
    assert not [o for o in owed if o["status"] == "pending"]


def test_transport_rejects_stacked_unresolved_questions(desk):
    """You cannot ask a second question before the first is answered. Without
    this, one agent talks past the other and the SLA tracks a queue rather than
    a conversation."""
    thread_id = _start("no stacking", ["alpha", "beta"])
    first = meetings.send_update(thread_id, role="alpha", kind="question",
                                 body="first question")

    with pytest.raises(ValueError, match=f"#{first['message_id']}"):
        meetings.send_update(thread_id, role="alpha", kind="question",
                             body="second question, entirely different")

    # And the party who OWES the reply cannot change the subject either.
    with pytest.raises(ValueError, match="requires a reply"):
        meetings.send_update(thread_id, role="beta", kind="evidence",
                             body="an unrelated new topic")


def test_transport_rejects_duplicate_messages(desk):
    """A woken agent that cannot tell whether it already spoke will speak again.
    Dedup is what keeps a re-wake from spending the budget on an echo."""
    thread_id = _start("no echoes", ["alpha", "beta"])
    first = meetings.send_update(thread_id, role="alpha", kind="question",
                                 body="the very same words")
    meetings.send_update(thread_id, role="beta", kind="answer",
                         body="an answer", reply_to=first["message_id"])

    with pytest.raises(ValueError, match=f"duplicate.*#{first['message_id']}"):
        meetings.send_update(thread_id, role="alpha", kind="question",
                             body="the very same words")


def test_an_unanswered_question_escalates_once_past_its_sla(desk):
    """The obligation is what makes silence loud. It must escalate on its own —
    the asker is not allowed to block waiting for it."""
    thread_id = _start("sla", ["alpha", "beta"], wait_timeout_seconds=30)
    sent = meetings.send_update(thread_id, role="alpha", kind="question",
                                body="a question that goes unanswered")
    with _db() as conn:
        conn.execute("UPDATE meeting_response_obligations SET due_at=? WHERE thread_id=?",
                     (_past(60), thread_id))

    meetings._sweep_timeouts()

    reasons = [e["reason"] for e in meetings.list_escalations(thread_id)]
    assert any(f"#{sent['message_id']}" in r and "beta" in r for r in reasons), reasons
    owed = meetings.meeting_status(thread_id)["response_obligations"]
    assert owed[0]["escalated_at"] is not None

    # Escalating twice for one silence would page a human on every read.
    meetings._sweep_timeouts()
    assert len(meetings.list_escalations(thread_id)) == len(reasons)


# --- 4. the message budget --------------------------------------------------

def test_the_message_budget_stops_the_meeting(desk):
    """Progress must come from the budget being spent, never from an agent
    choosing to stop."""
    thread_id = _start("budget", ["alpha", "beta", "gamma"],
                       max_messages=6, consensus_threshold=2)
    _exhaust_budget(thread_id)

    thread = _thread_row(thread_id)
    assert (thread["message_count"], thread["status"]) == (6, "paused")
    assert thread["stop_reason"] == "message budget exhausted"
    with pytest.raises(ValueError, match="budget exhausted"):
        meetings.send_update(thread_id, role="alpha", kind="position",
                             body="one more thing")


def test_termination_votes_do_not_consume_the_budget(desk):
    """The bound that makes every other bound safe: a meeting must ALWAYS be
    able to stop. If votes were messages, exhausting the budget would strand a
    meeting open forever with no legal move left — the budget would have created
    the runaway it exists to prevent."""
    thread_id = _start("stoppable at zero", ["alpha", "beta", "gamma"],
                       max_messages=6, consensus_threshold=2)
    _exhaust_budget(thread_id)
    spent = _thread_row(thread_id)["message_count"]
    assert spent == _thread_row(thread_id)["max_messages"], "budget must be gone"

    # With zero budget left, the handshake still has to run end to end.
    meetings.propose_end(thread_id, role="alpha", resolution="ship the decision")
    meetings.confirm_end(thread_id, role="beta")
    assert meetings.confirm_end(thread_id, role="gamma")["closed"] is True

    assert _state(thread_id) == "closed"
    assert _thread_row(thread_id)["message_count"] == spent, (
        "a termination vote must not spend a message")


# --- 5. the termination handshake -------------------------------------------

def test_mutual_propose_and_confirm_closes_the_meeting(desk):
    thread_id = _start("handshake", ["alpha", "beta"])
    meetings.propose_end(thread_id, role="alpha", resolution="agreed outcome")
    assert _state(thread_id) == "termination_pending"

    assert meetings.confirm_end(thread_id, role="beta")["closed"] is True
    assert _state(thread_id) == "closed"
    assert _thread_row(thread_id)["status"] == "closed"
    assert _thread_row(thread_id)["stop_reason"] == "agreed outcome"


def test_a_rejected_proposal_keeps_the_meeting_open(desk):
    """Rejection returns the meeting to discussion. An end nobody agreed to is
    not an end."""
    thread_id = _start("rejection", ["alpha", "beta"])
    meetings.propose_end(thread_id, role="alpha", resolution="end it here")

    meetings.reject_end(thread_id, role="beta", reason="the risk is unresolved")

    assert _state(thread_id) == "active"
    assert _thread_row(thread_id)["status"] == "open"
    assert meetings.meeting_status(thread_id)["termination"] is None
    # Still a working meeting afterwards, not a wedged one.
    meetings.send_update(thread_id, role="beta", kind="evidence",
                         body="here is the unresolved risk")


def test_a_vote_binds_only_to_the_pending_proposal(desk):
    """Confirming a proposal OTHER than the pending one must not close the
    meeting: consent is to a specific resolution, not to ending in general.

    NOTE: the agent-facing `confirm_end` takes no proposal id — the pending
    proposal is looked up, and a partial unique index permits only one at a
    time. So "a different id" is reachable for an agent only as a *resolved*
    proposal, which is what this pins. (The literal id-mismatch check is on the
    supervisor assertion path — `meetings.py:1476` — which is the web adapter's
    to test, not this module's.)
    """
    thread_id = _start("vote binding", ["alpha", "beta"])
    first = meetings.propose_end(thread_id, role="alpha",
                                 resolution="first resolution")["proposal_id"]
    meetings.reject_end(thread_id, role="beta", reason="not that one")

    # The rejected proposal is not a live target for a vote any more.
    with pytest.raises(ValueError, match="no pending termination proposal"):
        meetings.confirm_end(thread_id, role="beta")

    second = meetings.propose_end(thread_id, role="alpha",
                                  resolution="second resolution")["proposal_id"]
    assert second != first
    # The fresh proposal gets a fresh tally: alpha's confirm on `first` must not
    # carry over, and beta's reject on `first` must not veto `second`.
    votes = meetings.meeting_status(thread_id)["votes"]
    assert [(v["role"], v["vote"]) for v in votes] == [("alpha", "confirm")]
    assert meetings.confirm_end(thread_id, role="beta")["closed"] is True
    assert _thread_row(thread_id)["stop_reason"] == "second resolution"


# --- 6. the tally counts only ACTIVE attendees ------------------------------

def test_a_departed_attendee_cannot_deadlock_closure(desk):
    """Three attendees, one leaves, the remaining two agree — it must close.
    If the tally counted invited-but-departed roles, the leaver's missing vote
    would hold the meeting open forever and nobody left could end it."""
    thread_id = _start("leaver", ["alpha", "beta", "gamma"])
    meetings.send_update(thread_id, role="alpha", kind="evidence",
                         body="something worth discussing")

    # Leaving is a last resort: refused while the thread is still live.
    with pytest.raises(ValueError, match="still active"):
        meetings.leave_meeting(thread_id, role="gamma", reason="reassigned")
    with _db() as conn:
        conn.execute("UPDATE mailbox_messages SET created_at=? WHERE thread_id=?",
                     (_past(3600), thread_id))

    meetings.leave_meeting(thread_id, role="gamma", reason="reassigned")
    status = meetings.meeting_status(thread_id)
    assert status["mode"] == "one_to_one", "three actives minus one is a pair"
    assert [a["role"] for a in status["attendees"] if a["stopped_at"] is None] == [
        "alpha", "beta"]

    meetings.propose_end(thread_id, role="alpha", resolution="closed without gamma")
    assert meetings.confirm_end(thread_id, role="beta")["closed"] is True, (
        "gamma left; its missing vote must not block the two who remain")
    assert _state(thread_id) == "closed"


def test_leaving_while_a_proposal_is_open_retallies_over_whoever_remains(desk):
    """The nastier ordering: the proposal is already open and waiting on gamma
    when gamma leaves. The tally must re-run on the leave — otherwise the
    meeting is unanimous among everyone present and still refuses to close,
    with no event left to trigger a recount."""
    thread_id = _start("retally", ["alpha", "beta", "gamma"])
    meetings.send_update(thread_id, role="alpha", kind="evidence",
                         body="discussion before the proposal")
    meetings.propose_end(thread_id, role="alpha", resolution="wrap up")
    meetings.confirm_end(thread_id, role="beta")
    assert _state(thread_id) == "termination_pending", "gamma has not voted"

    with _db() as conn:
        conn.execute("UPDATE mailbox_messages SET created_at=? WHERE thread_id=?",
                     (_past(3600), thread_id))
    meetings.leave_meeting(thread_id, role="gamma", reason="out of scope for me")

    assert _state(thread_id) == "closed", (
        "alpha and beta already unanimously confirmed; gamma's departure must "
        "resolve the proposal, not strand it")


# --- 7. automatic consensus, one position each ------------------------------

def test_a_meeting_enters_consensus_automatically_at_the_threshold(desk):
    """Nobody decides to wrap up; the remaining budget does. Once in consensus
    only positions and decisions are accepted, so the last messages cannot be
    spent debating wording."""
    thread_id = _start("consensus", ["alpha", "beta", "gamma"],
                       max_messages=6, consensus_threshold=2)
    for i, role in enumerate(["alpha", "beta", "gamma"]):
        meetings.send_update(thread_id, role=role, kind="evidence",
                             body=f"evidence {i}")
        assert _state(thread_id) == "active"

    meetings.send_update(thread_id, role="alpha", kind="evidence",
                         body="the evidence that crosses the threshold")

    status = meetings.meeting_status(thread_id)
    assert status["meeting"]["state"] == "consensus"
    assert status["meeting"]["messages_remaining"] == 2
    assert len(_events(thread_id, "consensus_mode")) == 1
    with pytest.raises(ValueError, match="only one position per attendee"):
        meetings.send_update(thread_id, role="beta", kind="evidence",
                             body="but let me argue some more")


def test_each_attendee_gets_exactly_one_position(desk):
    """One position each, and a second is REJECTED rather than replacing the
    first (meetings.py:1228) — a position is a position of record, so nobody
    gets to overwrite what they staked out after hearing the others."""
    thread_id = _start("positions", ["alpha", "beta", "gamma"],
                       max_messages=8, consensus_threshold=2)
    meetings.submit_position(thread_id, role="beta", body="beta's position")

    with pytest.raises(ValueError, match="already submitted its consensus position"):
        meetings.submit_position(thread_id, role="beta", body="beta reconsiders")

    # Rejected, not replaced: the original still stands as the only one.
    stated = [m["body"] for m in meetings.meeting_transcript(thread_id)["messages"]
              if m["kind"] == "position"]
    assert stated == ["beta's position"]
    # And one rejection must not poison anyone else's turn.
    meetings.submit_position(thread_id, role="gamma", body="gamma's position")


# --- 8. the idle deadline ---------------------------------------------------

def test_idle_deadline_bounds_a_meeting_nobody_is_talking_in(desk):
    """The idle deadline is one of the four bounds design.md §Meetings claims,
    and mailbox._refresh_thread promises "a stale thread can never be written
    to". A meeting that went quiet past its deadline must be stopped, not
    resumable by whoever wanders back.

    The deadline is real and the mailbox enforces it — `mailbox.get_thread()` on
    this same thread pauses it with 'idle timeout'. Nothing is asserted through
    a mailbox read path here precisely because doing so would apply the deadline
    on the meetings layer's behalf and hide the gap.
    """
    thread_id = _start("nobody is talking", ["alpha", "beta"], idle_minutes=1)
    with _db() as conn:
        conn.execute("UPDATE mailbox_threads SET expires_at=? WHERE id=?",
                     (_past(3600), thread_id))
    assert _thread_row(thread_id)["expires_at"] < dt.datetime.now(
        dt.timezone.utc).isoformat(timespec="seconds"), "deadline is in the past"

    with pytest.raises(ValueError, match="idle|expired|paused"):
        meetings.send_update(thread_id, role="alpha", kind="evidence",
                             body="talking long past the idle deadline")


def _expire(thread_id: str) -> None:
    """Back-date the thread's idle deadline. See the module docstring on why
    time is injected rather than slept through."""
    with _db() as conn:
        conn.execute("UPDATE mailbox_threads SET expires_at=? WHERE id=?",
                     (_past(3600), thread_id))


@pytest.mark.parametrize("read", [
    pytest.param(lambda tid: meetings.meeting_status(tid)["meeting"]["thread_status"],
                 id="meeting_status"),
    pytest.param(lambda tid: next(m["thread_status"] for m in meetings.discover("alpha")
                                  if m["thread_id"] == tid), id="discover"),
    pytest.param(lambda tid: next(s["meeting"]["thread_status"]
                                  for s in meetings.list_meetings()
                                  if s["meeting"]["thread_id"] == tid), id="list_meetings"),
])
def test_every_meetings_read_reports_a_lapsed_deadline_rather_than_open(desk, read):
    """The deadline is enforced lazily on read, so a read that reports `open` past
    it is not a cosmetic bug: it is the write path's own view. `_send_update` and
    these surfaces all learn the thread's status the same way, so a raw
    `SELECT ... FROM mailbox_threads` in any of them is the bound rotting again —
    which is exactly how it rotted the first time.
    """
    thread_id = _start("read paths", ["alpha", "beta"], idle_minutes=1)
    _expire(thread_id)

    assert read(thread_id) == "paused", "a lapsed meeting must never read as open"


def test_a_supervisor_resume_gives_the_meeting_a_fresh_idle_window(desk):
    """Retiring a thread on read must not make the supervisor's override
    decorative: a thread paused ON its deadline still carries a lapsed
    expires_at, so a resume that only flips status would be undone by the very
    next read. Resuming is the one authority that can outrank a bound
    (mailbox.py refuses a raw resume precisely to route it through here), and it
    has to actually stick.
    """
    thread_id = _start("resume me", ["alpha", "beta"], idle_minutes=45)
    _expire(thread_id)
    with pytest.raises(ValueError, match="idle|expired|paused"):
        meetings.send_update(thread_id, role="alpha", kind="evidence",
                             body="talking past the idle deadline")

    meetings.apply_simple_supervisor_action(
        {"action": "resume", "meeting_id": thread_id, "reason": "supervisor resumed"})

    assert _thread_row(thread_id)["expires_at"] > dt.datetime.now(
        dt.timezone.utc).isoformat(timespec="seconds"), "resume must re-arm the deadline"
    meetings.send_update(thread_id, role="alpha", kind="evidence",
                         body="talking after a legitimate resume")
    assert meetings.meeting_status(thread_id)["meeting"]["thread_status"] == "open"


# --- 9. integrity: never create both sides ----------------------------------
# The protocol's core rule. Note what is NOT tested here: "alpha calls check_in
# as beta". Every agent entry point takes `role` as a plain argument and there is
# no caller identity anywhere in the engine, so `check_in(role="beta")` is beta
# checking in — there is no way to express "alpha did it" and therefore nothing
# to assert. That is by design, not an oversight: docs/security.md scopes the
# threat model to "an agent fabricating another agent's attendance, reports, or
# votes" while explicitly declining to defend against hostile code running as the
# same user. The engine's defence is structural, and structure is what these
# tests pin: no single role can produce a counterpart's half of any exchange.

def test_an_agent_cannot_manufacture_the_counterpart_s_half_of_an_exchange(desk):
    """Replying to your own message would let one agent stage a whole Q&A and
    resolve its own obligations — both sides of the conversation, one author."""
    thread_id = _start("both sides", ["alpha", "beta"])
    mine = meetings.send_update(thread_id, role="alpha", kind="question",
                                body="a question i will answer myself")

    with pytest.raises(ValueError, match="must be another attendee's"):
        meetings.send_update(thread_id, role="alpha", kind="answer",
                             body="and here is my own answer",
                             reply_to=mine["message_id"])

    owed = meetings.meeting_status(thread_id)["response_obligations"]
    assert [(o["owed_by"], o["status"]) for o in owed] == [("beta", "pending")], (
        "the obligation must still stand on beta")


def test_one_agent_alone_cannot_open_a_meeting_for_discussion(desk):
    """If the counterpart never arrives the meeting must pause/escalate — never
    proceed as if it were quorate."""
    status = meetings.call_meeting(agenda="alone", called_by="alpha",
                                   attendees=["alpha", "beta"],
                                   wait_timeout_seconds=30)
    thread_id = status["meeting"]["thread_id"]

    with pytest.raises(ValueError, match="at least two active attendees"):
        meetings.send_update(thread_id, role="alpha", kind="evidence",
                             body="proceeding without beta")
    with pytest.raises(ValueError, match="while meeting is waiting"):
        meetings.propose_end(thread_id, role="alpha", resolution="closing alone")

    with _db() as conn:
        conn.execute("UPDATE meetings SET created_at=? WHERE thread_id=?",
                     (_past(600), thread_id))
    meetings._sweep_timeouts()
    assert _state(thread_id) == "waiting", "it escalates; it never auto-completes"


def test_one_agent_alone_cannot_close_a_meeting(desk):
    """The proposer implicitly confirms its own proposal. Voting again must not
    add a second confirm — the vote table is keyed by role, so alpha can only
    ever be one confirm, and closure needs beta's."""
    thread_id = _start("solo close", ["alpha", "beta"])
    meetings.propose_end(thread_id, role="alpha", resolution="close it")

    assert meetings.confirm_end(thread_id, role="alpha")["closed"] is False
    assert meetings.confirm_end(thread_id, role="alpha")["closed"] is False

    status = meetings.meeting_status(thread_id)
    assert [(v["role"], v["vote"]) for v in status["votes"]] == [("alpha", "confirm")]
    assert status["meeting"]["state"] == "termination_pending"
    assert _thread_row(thread_id)["status"] == "open"


def test_a_lone_report_stands_alone_and_the_review_does_not_auto_complete(desk):
    """"If your counterpart never arrives, leave your artifact and let the
    meeting pause or escalate — do not fabricate their half." So one report must
    not advance the phase, and one agent must not be able to walk the whole
    review to `final` by itself."""
    artifact = desk.db_path.parent / "alpha_report.md"
    artifact.write_text("alpha's independent findings")

    status = meetings.call_meeting(agenda="post mortem", called_by="alpha",
                                   attendees=["alpha", "beta"],
                                   meeting_type="review")
    thread_id = status["meeting"]["thread_id"]
    # Before beta checks in there is no review to file into at all.
    with pytest.raises(ValueError, match="all attendees must check in"):
        mailbox.submit_review_artifact(thread_id, role="alpha", stage="report",
                                       path=artifact)

    meetings.check_in(thread_id, role="beta")
    mailbox.submit_review_artifact(thread_id, role="alpha", stage="report",
                                   path=artifact)

    # beta checked in and then went silent. alpha's report stands alone.
    assert _thread_row(thread_id)["phase"] == "reports", (
        "the phase must wait for every participant's report")
    with pytest.raises(ValueError, match="already submitted a report"):
        mailbox.submit_review_artifact(thread_id, role="alpha", stage="report",
                                       path=artifact)
    for stage in ("review", "final"):
        with pytest.raises(ValueError, match=f"cannot submit {stage}"):
            mailbox.submit_review_artifact(thread_id, role="alpha", stage=stage,
                                           path=artifact)

    assert [(a["role"], a["stage"]) for a in mailbox.review_artifacts(thread_id)] == [
        ("alpha", "report")]
    assert _state(thread_id) != "closed", "a one-sided review must not complete"


# --- 10. the supervisor is not an agent role --------------------------------

@pytest.mark.parametrize("action", [
    pytest.param(lambda tid, sup: meetings.call_meeting(agenda="x", called_by=sup),
                 id="call"),
    pytest.param(lambda tid, sup: meetings.check_in(tid, role=sup), id="check_in"),
    pytest.param(lambda tid, sup: meetings.send_update(tid, role=sup, body="b",
                                                       kind="evidence"), id="send"),
    pytest.param(lambda tid, sup: meetings.submit_position(tid, role=sup, body="b"),
                 id="position"),
    pytest.param(lambda tid, sup: meetings.propose_end(tid, role=sup, resolution="r"),
                 id="propose_end"),
    pytest.param(lambda tid, sup: meetings.confirm_end(tid, role=sup), id="confirm_end"),
    pytest.param(lambda tid, sup: meetings.reject_end(tid, role=sup, reason="r"),
                 id="reject_end"),
    pytest.param(lambda tid, sup: meetings.leave_meeting(tid, role=sup, reason="r"),
                 id="leave"),
    pytest.param(lambda tid, sup: meetings.meeting_updates(tid, role=sup), id="read"),
    pytest.param(lambda tid, sup: meetings.discover(sup), id="discover"),
    pytest.param(lambda tid, sup: meetings.escalate_meeting(tid, role=sup, reason="r"),
                 id="escalate"),
    pytest.param(lambda tid, sup: meetings.pause_meeting(tid, role=sup, reason="r"),
                 id="pause"),
    pytest.param(lambda tid, sup: meetings.wake_requests(sup), id="wake_requests"),
])
def test_agent_meeting_api_rejects_the_supervisor_role(desk, action):
    """The supervisor is a human authority, not a role. An agent naming it must
    be refused at every agent-facing door — supervisor actions enter only
    through the authenticated web adapter. A single unguarded entry point is a
    complete bypass of the trust boundary, which is why this enumerates them."""
    thread_id = _start("supervisor rejection", ["alpha", "beta"])
    with pytest.raises(ValueError, match="not an agent role"):
        action(thread_id, CONFIG.supervisor_role)


def test_an_agent_cannot_invite_the_supervisor(desk):
    """Inviting is impersonation's back door: an attendee row for the supervisor
    that no assertion ever authorized would make the console show them present."""
    with pytest.raises(ValueError, match="cannot add"):
        meetings.call_meeting(agenda="come join us", called_by="alpha",
                              attendees=["alpha", CONFIG.supervisor_role])
