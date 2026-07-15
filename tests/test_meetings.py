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


def test_meetings_track_stacked_questions_instead_of_refusing_them(desk):
    """This was test_transport_rejects_stacked_unresolved_questions, and it was
    green for the wrong reason. Meetings never passes requires_reply, so the
    transport's stacking bound is unreachable from this layer — it guards
    mailbox.send_message callers, and test_mailbox covers it there. What
    actually refused a second question here was the one_to_one turn-taking gate,
    which is gone: it silenced whoever was present on behalf of whoever was slow.

    Stacking is now allowed and *tracked*. The old docstring's worry was that
    "the SLA tracks a queue rather than a conversation" — that is now the design,
    not the failure. A queue whose every item has an owner and a due date is
    exactly what the wake ladder can act on, and one answer can settle several
    at once (see test_one_reply_can_settle_several_questions_at_once). Refusing
    the second question never made the first one get answered.
    """
    thread_id = _start("stacking is tracked", ["alpha", "beta"])
    first = meetings.send_update(thread_id, role="alpha", kind="question",
                                 body="first question")["message_id"]
    second = meetings.send_update(thread_id, role="alpha", kind="question",
                                  body="second question, entirely different")["message_id"]
    # And the party who owes the replies may change the subject — the debts stand.
    meetings.send_update(thread_id, role="beta", kind="evidence",
                         body="an unrelated new topic")

    owed = {o["message_id"]: o for o in
            meetings.meeting_status(thread_id)["response_obligations"]}
    assert (owed[first]["owed_by"], owed[first]["status"]) == ("beta", "pending")
    assert (owed[second]["owed_by"], owed[second]["status"]) == ("beta", "pending"), (
        "both questions must be tracked, each carrying its own SLA")


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


def test_an_unanswered_question_does_not_page_a_human_by_itself(desk):
    """The obligation is still what makes silence loud, and the asker still must
    not block waiting for it. What changed is who gets told.

    This used to queue an escalation straight from the sweep: one hop, in a
    human's direction, skipping every machine rung of the wake ladder — hook at
    60s, resume at 120s, spawn at 180s — that exists to fix exactly this without
    waking anybody. A slow agent is not an incident, and paging a person for one
    trains them to ignore the page that matters. The obligation rows stay
    exactly as they were; the orchestrator collects them as wake demand and
    reaches a human at the `human` rung, on the merits, once the machine has
    actually failed. That half is pinned in test_wake (this layer must never
    import orchestration, so it cannot assert it here).
    """
    thread_id = _start("sla", ["alpha", "beta"], wait_timeout_seconds=30)
    meetings.send_update(thread_id, role="alpha", kind="question",
                         body="a question that goes unanswered")
    with _db() as conn:
        conn.execute("UPDATE meeting_response_obligations SET due_at=? WHERE thread_id=?",
                     (_past(60), thread_id))

    meetings._sweep_timeouts()

    assert meetings.list_escalations(thread_id) == [], (
        "a slow reply must not page anyone from this layer")
    owed = meetings.meeting_status(thread_id)["response_obligations"]
    assert owed[0]["status"] == "pending", (
        "the debt itself must survive the sweep — it is the demand")


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


def test_an_agent_can_answer_the_supervisor_alone_in_a_one_to_one_meeting(desk):
    """The invariant is authorship, not addressing — and conflating the two
    deadlocks the meeting. Naming the supervisor as a recipient forges nothing,
    so that door stays open while test_agent_meeting_api_rejects_the_supervisor_role
    keeps authorship shut.

    This test used to assert a second door as well: strict turn-taking refusing
    any new topic until the outstanding reply was made. That door is gone. It
    never protected authorship — it was a pull-era stand-in for delivery
    receipts — and it silenced whoever was present on behalf of whoever was
    slow, which deadlocked the console outright the moment the debt was the
    supervisor's. Changing the subject is now allowed; the debt survives it, and
    is nudged rather than enforced.
    """
    called = meetings.apply_simple_supervisor_action(
        {"action": "call", "agenda": "one on one", "attendees": ["alpha"]})
    thread_id = called["meeting"]["thread_id"]
    meetings.check_in(thread_id, role="alpha")
    ask = meetings.apply_simple_supervisor_action(
        {"action": "send", "meeting_id": thread_id, "body": "analyze this"},
    )["message_id"]

    # Speaking out of turn is not refused any more...
    meetings.send_update(thread_id, role="alpha", kind="evidence",
                         body="changing the subject instead")
    # ...and doing so does NOT quietly mark the question answered. A ledger that
    # settles itself on any outgoing message is a dropped message with clean books.
    owed = meetings.meeting_status(thread_id)["response_obligations"]
    assert [o["status"] for o in owed if o["message_id"] == ask] == ["pending"], (
        "an unrelated message must leave the supervisor's question outstanding")

    meetings.send_update(thread_id, role="alpha", kind="answer", reply_to=ask,
                         body="here is the analysis you asked for")

    obligations = meetings.meeting_status(thread_id)["response_obligations"]
    assert [o["status"] for o in obligations if o["message_id"] == ask] == ["resolved"], (
        "an explicit reply must discharge the supervisor's obligation")
    with _db() as conn:
        recipient = conn.execute(
            "SELECT recipient FROM mailbox_messages WHERE reply_to=?", (ask,),
        ).fetchone()["recipient"]
    assert recipient == CONFIG.supervisor_role, (
        "the reply must be addressed to the supervisor, not broadcast — an "
        "obligation owed by a broadcast is owed by nobody")


def test_one_reply_can_settle_several_questions_at_once(desk):
    """reply_to threads at exactly one message, so when it was also the only way
    to discharge a debt, two questions cost two replies — the ping-pong the
    turn-taking gate then enforced. `resolves` is the separate act: what this
    message settles, decided by the only party that knows, the sender."""
    thread_id = _start("two questions", ["alpha", "beta"])
    q1 = meetings.send_update(thread_id, role="alpha", kind="question",
                              body="does the thesis still hold")["message_id"]
    # alpha may ask again without waiting — no gate, and the debts stack.
    q2 = meetings.send_update(thread_id, role="alpha", kind="question",
                              body="and what is the downside case")["message_id"]

    answer = meetings.send_update(
        thread_id, role="beta", kind="answer", reply_to=q1,
        resolves=[q2], body="yes, and the downside is a 12% drawdown")["message_id"]

    owed = {o["message_id"]: o for o in
            meetings.meeting_status(thread_id)["response_obligations"]}
    assert owed[q1]["status"] == "resolved" and owed[q1]["resolution"] == "explicit reply"
    assert owed[q2]["status"] == "resolved", "one answer settled both questions"
    assert owed[q2]["resolved_by_message_id"] == answer
    assert owed[q2]["resolution"] == f"covered by #{answer}"


def test_an_agent_can_settle_a_debt_its_earlier_message_already_answered(desk):
    """Noticing after the fact must not cost a redundant "as I said above"
    message: that trains noise, and a debt left pending because replying felt
    silly is a reply the counterpart never gets."""
    thread_id = _start("already covered", ["alpha", "beta"])
    broad = meetings.send_update(thread_id, role="beta", kind="evidence",
                                 body="the downside case is a 12% drawdown")["message_id"]
    ask = meetings.send_update(thread_id, role="alpha", kind="question",
                               body="what is the downside case")["message_id"]
    covering = meetings.send_update(thread_id, role="beta", kind="evidence",
                                    body="restating: 12% drawdown, as measured")["message_id"]

    out = meetings.resolve_obligations(thread_id, role="beta",
                                       message_ids=[ask], covered_by=covering)
    assert out["discharged"] == [ask]
    owed = {o["message_id"]: o for o in
            meetings.meeting_status(thread_id)["response_obligations"]}
    assert owed[ask]["status"] == "resolved"
    assert owed[ask]["resolved_by_message_id"] == covering

    # A message that predates the question cannot have answered it — allowing it
    # would let resolved_by_message_id lie about causality.
    ask2 = meetings.send_update(thread_id, role="alpha", kind="question",
                                body="and the upside")["message_id"]
    with pytest.raises(ValueError, match="did not come after"):
        meetings.resolve_obligations(thread_id, role="beta",
                                     message_ids=[ask2], covered_by=broad)


def test_an_agent_cannot_settle_a_debt_it_does_not_owe(desk):
    """"Never create both sides": discharging a counterpart's obligation would
    let an agent answer on behalf of someone it cannot speak for, and the ledger
    would record a reply that never happened."""
    thread_id = _start("not yours to settle", ["alpha", "beta"])
    ask = meetings.send_update(thread_id, role="alpha", kind="question",
                               body="does the thesis still hold")["message_id"]
    mine = meetings.send_update(thread_id, role="alpha", kind="evidence",
                                body="adding context to my own question")["message_id"]
    # The debt on `ask` is beta's; alpha must not clear it.
    with pytest.raises(ValueError, match="owed by beta"):
        meetings.resolve_obligations(thread_id, role="alpha",
                                     message_ids=[ask], covered_by=mine)
    owed = {o["message_id"]: o for o in
            meetings.meeting_status(thread_id)["response_obligations"]}
    assert owed[ask]["status"] == "pending"


def test_addressing_the_supervisor_stays_shut_outside_a_meeting_it_sits_in(desk):
    """The addressing door is opened by the meetings module for an attending
    supervisor only. Left open by default it would let any mailbox caller
    conjure supervisor-addressed traffic the console would render as a real
    exchange, so the raw ledger must still refuse."""
    thread_id = _start("agents only", ["alpha", "beta"])
    with _db() as conn:
        thread = mailbox._refresh_thread(conn, thread_id)
        with pytest.raises(ValueError, match="not an agent role"):
            mailbox._insert_message(conn, thread, sender="alpha",
                                    recipient=CONFIG.supervisor_role,
                                    kind="evidence", body="psst")


def test_a_supervisor_message_revives_an_idle_paused_meeting(desk):
    """The idle deadline touches mailbox_threads only — meetings.state stays
    `active`. The console reads state, so it draws a live composer and no resume
    button, then the send dies on "thread is paused: idle timeout" with no way
    out. Same dead end the turn-taking gate produced, reached from the other
    side: the one surface a human has, refusing the human.

    Resuming stays a human act. Writing IS that act; a button pressed only ever
    immediately before the message is ceremony. Agents get no such revival —
    they open a new meeting (see the companion test below).
    """
    called = meetings.apply_simple_supervisor_action(
        {"action": "call", "agenda": "gone quiet", "attendees": ["alpha"]})
    thread_id = called["meeting"]["thread_id"]
    meetings.check_in(thread_id, role="alpha")
    _expire(thread_id)
    assert meetings.meeting_status(thread_id)["meeting"]["state"] == "active", (
        "the trap's premise: state still reads active while the thread is retired")

    sent = meetings.apply_simple_supervisor_action(
        {"action": "send", "meeting_id": thread_id, "body": "still here?"})

    assert sent["message_id"]
    assert _thread_row(thread_id)["status"] == "open"
    assert _thread_row(thread_id)["expires_at"] > dt.datetime.now(
        dt.timezone.utc).isoformat(timespec="seconds"), (
        "a revived thread needs a fresh deadline, or the next read retires it "
        "again and the revival is decorative")


def test_reviving_an_idle_meeting_is_the_supervisors_alone(desk):
    """An agent that wandered back must not reopen what went quiet: it starts a
    new meeting instead. Nor may a message overturn a pause that was a decision
    rather than lapsed attention — only 'idle timeout' revives."""
    thread_id = _start("agents only", ["alpha", "beta"])
    _expire(thread_id)
    with pytest.raises(ValueError, match="idle|expired|paused"):
        meetings.send_update(thread_id, role="alpha", kind="evidence",
                             body="wandering back in")
    # Asserted on the deadline, not on `status`: the refusal rolls its own
    # transaction back, taking the 'paused' write with it, so the row reads
    # `open` with a deadline still in the past — retired again on the next read.
    # What matters is that the agent got no fresh window.
    assert _thread_row(thread_id)["expires_at"] < dt.datetime.now(
        dt.timezone.utc).isoformat(timespec="seconds"), (
        "an agent must not get the fresh deadline a revival grants")

    # A supervisor message revives idle, but must not revive a deliberate pause.
    called = meetings.apply_simple_supervisor_action(
        {"action": "call", "agenda": "deliberately parked", "attendees": ["alpha"]})
    parked = called["meeting"]["thread_id"]
    meetings.check_in(parked, role="alpha")
    with _db() as conn:
        conn.execute(
            """UPDATE mailbox_threads SET status='paused',
               stop_reason='message budget exhausted',stopped_by='system' WHERE id=?""",
            (parked,))
    with pytest.raises(ValueError, match="paused|budget"):
        meetings.apply_simple_supervisor_action(
            {"action": "send", "meeting_id": parked, "body": "talk anyway"})
    assert _thread_row(parked)["status"] == "paused", (
        "a budget pause is a decision; a message must not quietly overturn it")


# --- 9. the supervisor is present, not required ------------------------------

def test_a_silent_supervisor_does_not_block_the_agents_from_closing(desk):
    """A human reading along is not a quorum condition. `required` shipped in the
    first schema and every write hardcoded 1, so the knob was built and never
    turned — and a supervisor who joined, read, and went quiet left
    _finalize_if_unanimous waiting on a vote that was never coming. The agents
    could not finish, and the only cure was the person returning. That is the
    dependency this desk exists to remove, and nobody ever decided to have it.
    """
    thread_id = _start("work meeting", ["alpha", "beta"])
    meetings.apply_simple_supervisor_action(
        {"action": "join", "meeting_id": thread_id})

    meetings.propose_end(thread_id, role="alpha", resolution="we are done here")
    assert meetings.confirm_end(thread_id, role="beta")["closed"] is True, (
        "the agents agreed; a watching human owes them no vote")


def test_an_agent_cannot_close_its_one_to_one_with_the_supervisor(desk):
    """The mirror of the test above, and the reason it is safe.

    required=0 means the human is not counted — so alone with them the single
    agent IS the whole quorum, and _propose_end auto-confirms its own proposer.
    Unguarded, one agent would close a person's thread mid-sentence, and closing
    takes the mailbox with it, so the reply would be unsendable. A meeting that
    is just you and a person is theirs to end.
    """
    called = meetings.apply_simple_supervisor_action(
        {"action": "call", "agenda": "one on one", "attendees": ["alpha"]})
    thread_id = called["meeting"]["thread_id"]
    meetings.check_in(thread_id, role="alpha")

    with pytest.raises(ValueError, match="theirs to end"):
        meetings.propose_end(thread_id, role="alpha", resolution="done talking")
    assert _state(thread_id) == "active", "the human's thread stays open"


def test_a_second_agent_makes_it_a_work_meeting_the_agents_may_close(desk):
    """The guard keys on shape, not on the supervisor merely being present: with
    a colleague in the room it is work with a defined end, and the agents own
    finishing it. Keying on "a supervisor is here" instead would hand every
    meeting a human sat in back to the human to close, which is the dependency
    required=0 just removed."""
    called = meetings.apply_simple_supervisor_action(
        {"action": "call", "agenda": "work, observed",
         "attendees": ["alpha", "beta"]})
    thread_id = called["meeting"]["thread_id"]
    for role in ("alpha", "beta"):
        meetings.check_in(thread_id, role=role)

    meetings.propose_end(thread_id, role="alpha", resolution="concluded")
    assert meetings.confirm_end(thread_id, role="beta")["closed"] is True


def test_the_supervisor_can_reopen_a_meeting_the_agents_closed(desk):
    """The counterweight to closing without the human's vote: they can get back
    in. Reopening is not un-pausing. _close_meeting stops every attendee and the
    old resume left them stopped, so the meeting came back open and empty —
    nobody active, "needs at least two active attendees", not one message
    accepted. It looked resumed and could not be spoken in.
    """
    thread_id = _start("closed by agents", ["alpha", "beta"])
    meetings.send_update(thread_id, role="alpha", kind="evidence",
                         body="a finding worth revisiting")
    meetings.propose_end(thread_id, role="alpha", resolution="done")
    assert meetings.confirm_end(thread_id, role="beta")["closed"] is True

    meetings.apply_simple_supervisor_action(
        {"action": "resume", "meeting_id": thread_id, "reason": "not done after all"})

    assert _state(thread_id) in ("active", "consensus")
    assert _thread_row(thread_id)["status"] == "open"
    assert _thread_row(thread_id)["message_count"] == 0, (
        "the budget was spent reaching a conclusion now being reopened; "
        "inheriting the remainder reopens straight into consensus, or into "
        "'budget exhausted', which is reopening into the closed state again")

    # The proof the old resume could not pass: it accepts a message.
    meetings.send_update(thread_id, role="beta", kind="evidence",
                         body="picking the thread back up")


# --- 10. history: when a meeting ended, and how the console reads it back ----
# `list_meetings` is the console's whole view of the desk, and every test here
# guards it telling the truth about time. The three invariants are that a
# meeting's END is recorded (not inferred from its last write), that narrowing
# the history never narrows the present, and that history reads in the order
# things finished.

#: A fixed instant that is 2026-07-15 in UTC and 2026-07-14 in the conftest's
#: America/New_York (23:00 EDT). Every way the day filter can be wrong lives in
#: this gap, so the history tests close on it rather than on "now".
_UTC_EVENING = dt.datetime(2026, 7, 15, 3, 0, tzinfo=dt.timezone.utc)
_LOCAL_DAY = "2026-07-14"
_UTC_DAY = "2026-07-15"


def _close(thread_id: str, resolution: str = "concluded", *,
           ended_at: str | None = None) -> None:
    """Close a meeting through the real handshake, then optionally back-date the
    end. Injected rather than slept for — see the module docstring."""
    meetings.propose_end(thread_id, role="alpha", resolution=resolution)
    meetings.confirm_end(thread_id, role="beta")
    if ended_at is not None:
        with _db() as conn:
            conn.execute("UPDATE meetings SET closed_at=? WHERE thread_id=?",
                         (ended_at, thread_id))


def _listed(**kwargs) -> list[str]:
    return [s["meeting"]["thread_id"] for s in meetings.list_meetings(**kwargs)]


def test_closed_at_records_when_a_meeting_ended_not_when_it_was_last_written(desk):
    """`updated_at` coincides with the close on every row that exists today, which
    is exactly why it looked like an end timestamp. It is not one: closing merely
    happens to be the last thing that touches most meetings, and that is a
    coincidence, not an invariant. Anything writing a closed meeting afterwards
    turns updated_at into a lie about when it ended — silently — and a history
    sorted by "end time" quietly reorders itself. So the end is recorded, once,
    by the only thing that can end a meeting.
    """
    thread_id = _start("ends at a knowable time", ["alpha", "beta"])
    assert meetings.meeting_status(thread_id)["meeting"]["closed_at"] is None, (
        "a live meeting has not ended; a closed_at would file it under a day")

    _close(thread_id, "we are done here")
    meeting = meetings.meeting_status(thread_id)["meeting"]
    assert meeting["state"] == "closed"
    assert meeting["closed_at"] is not None, "closing must record when it closed"

    # Back-date the close so a later write is distinguishable without sleeping.
    ended = _past(3600)
    with _db() as conn:
        conn.execute("UPDATE meetings SET closed_at=?,updated_at=? WHERE thread_id=?",
                     (ended, ended, thread_id))

    # A supervisor read of the transcript, an escalation sweep, any later touch:
    # updated_at moves, closed_at does not. Simulated with a direct write because
    # the only path that writes an already-closed meeting today is `resume`, and
    # resume is the one case where the end legitimately stops existing (below).
    with _db() as conn:
        conn.execute("UPDATE meetings SET updated_at=? WHERE thread_id=?",
                     (_past(0), thread_id))
    with _db() as conn:
        row = dict(conn.execute(
            "SELECT closed_at,updated_at FROM meetings WHERE thread_id=?",
            (thread_id,)).fetchone())
    assert row["updated_at"] > ended, "the later write must move updated_at"
    assert row["closed_at"] == ended, (
        "...and must leave closed_at alone: it says when the meeting ended, not "
        "when the row was last touched")


def test_reopening_a_meeting_clears_the_end_it_no_longer_has(desk):
    """`resume` is not "a later write" — it is an un-ending.

    Everywhere else, closed_at must survive a subsequent touch (above). Here it
    must not: the meeting is live again, so "when it ended" has stopped being a
    fact about it. A live row carrying an end time contradicts itself, and
    list_meetings ranks the live block by COALESCE(closed_at, created_at) — a
    reopened meeting would sort by when it used to finish instead of when it
    began, i.e. wherever its old ending happened to fall.

    Re-closing must then record the SECOND ending, not resurrect the first.
    """
    thread_id = _start("reopened after all", ["alpha", "beta"])
    _close(thread_id, "premature")
    first_end = _past(3600)
    with _db() as conn:
        conn.execute("UPDATE meetings SET closed_at=? WHERE thread_id=?",
                     (first_end, thread_id))

    meetings.apply_simple_supervisor_action(
        {"action": "resume", "meeting_id": thread_id, "reason": "not done after all"})
    live = meetings.meeting_status(thread_id)["meeting"]
    assert live["state"] != "closed"
    assert live["closed_at"] is None, (
        "a live meeting has no end; leaving the old one files it under the day it "
        "used to close on and ranks it there among meetings that are still open")

    _close(thread_id, "now we really are done")
    again = meetings.meeting_status(thread_id)["meeting"]
    assert again["closed_at"] is not None
    assert again["closed_at"] > first_end, (
        "the second ending is the one that happened; the first is over")


def test_the_day_filter_narrows_history_and_never_hides_a_live_meeting(desk):
    """The load-bearing one. `day` narrows HISTORY only. A filter that could hide
    a meeting still waiting on someone would hide the exact thing the console
    exists to show, so "narrow the history" must never quietly mean "narrow the
    present" — a live meeting is listed whatever day it was called on.
    """
    waiting = meetings.call_meeting(
        agenda="still waiting on the others", called_by="alpha")["meeting"]["thread_id"]
    closed = _start("finished on its own day", ["alpha", "beta"])
    _close(closed, "wrapped up", ended_at=meetings._iso(_UTC_EVENING))

    listed = _listed(include_closed=True, day="2026-01-02")
    assert waiting in listed, (
        "a live meeting has no end date to be filtered by; it is always listed")
    assert closed not in listed, "the day filter must still narrow the history"

    listed = _listed(include_closed=True, day=_LOCAL_DAY)
    assert {waiting, closed} <= set(listed), "the closed one is back on its own day"


def test_history_reads_newest_ended_first_and_urgency_ranks_only_live_meetings(desk):
    """Priority answers "what needs you now", which a finished meeting cannot.
    Applied to history it hoists every urgent meeting into its own block and cuts
    the timeline in two — the first cut of this did exactly that, and reading the
    output back showed a 07-15 meeting sorted above a 07-14 one, which makes the
    day impossible to follow. Live first (urgent ahead of normal); then history,
    strictly newest-ended first whatever its priority was.
    """
    live_normal = _start("live, and merely normal", ["alpha", "beta"])
    live_urgent = _start("live, and urgent", ["alpha", "beta"], priority="urgent")
    # Priority INTERLEAVED against close order: a global urgent-first key
    # reshuffles these three, a live-block-only one leaves them in time order.
    newest = _start("closed most recently", ["alpha", "beta"], priority="urgent")
    middle = _start("closed in between", ["alpha", "beta"])
    oldest = _start("closed first of all", ["alpha", "beta"], priority="urgent")
    for thread_id, age in ((newest, 60), (middle, 3600), (oldest, 7200)):
        _close(thread_id, f"done {age}s ago", ended_at=_past(age))

    assert _listed(include_closed=True) == [
        live_urgent, live_normal, newest, middle, oldest]


def test_the_day_is_a_local_calendar_date_not_a_prefix_of_the_stored_utc_time(desk):
    """`day` is a LOCAL date resolved through CONFIG.tzinfo() — America/New_York
    here, deliberately not UTC. This meeting ended at 23:00 on the 14th for the
    person reading the console, and is stored as '2026-07-15T03:00:00+00:00'. A
    string prefix match answers a different question everywhere the offset is not
    zero, which is everywhere this desk runs: it would file the meeting under a
    day nobody worked and leave the day they did work looking empty.
    """
    thread_id = _start("closed late in the local evening", ["alpha", "beta"])
    _close(thread_id, "late night", ended_at=meetings._iso(_UTC_EVENING))

    assert thread_id in _listed(include_closed=True, day=_LOCAL_DAY), (
        "it ended on the local evening of the 14th and belongs to that day")
    assert thread_id not in _listed(include_closed=True, day=_UTC_DAY), (
        "the local 15th had not begun yet; only a prefix match files it there")
    assert meetings.meeting_days() == [_LOCAL_DAY]


@pytest.mark.parametrize("garbage", ["", "yesterday", "2026-13-01", "2026-02-30",
                                     "2026-07-15T00:00:00", "2026-7-5", None])
def test_a_day_that_is_not_a_calendar_date_is_refused(desk, garbage):
    """The day reaches `_local_day_bounds`, which builds SQL bounds from it.
    Anything not a real date must die at the door with a message that says what
    was wanted, rather than deeper down as a TypeError or an empty history that
    looks like a quiet day."""
    with pytest.raises(ValueError, match="day must be YYYY-MM-DD"):
        meetings._valid_day(garbage)


def test_no_day_means_no_filter(desk):
    """`day=None` is the console's default view: the whole history, not none of
    it."""
    live = _start("live", ["alpha", "beta"])
    closed = _start("closed", ["alpha", "beta"])
    _close(closed, "done", ended_at=meetings._iso(_UTC_EVENING))

    assert set(_listed(include_closed=True, day=None)) == {live, closed}


def test_the_day_filter_is_ignored_when_history_was_not_asked_for(desk):
    """With include_closed=False there are no closed meetings to narrow, so `day`
    has nothing left to say. It must not become a second, silent filter on the
    live board — the same failure as hiding a live meeting, reached by a
    different route."""
    live = _start("live", ["alpha", "beta"])
    closed = _start("closed", ["alpha", "beta"])
    _close(closed, "done", ended_at=meetings._iso(_UTC_EVENING))

    assert _listed(include_closed=False) == [live]
    assert _listed(include_closed=False, day=_LOCAL_DAY) == [live]
    assert _listed(include_closed=False, day="2026-01-02") == [live], (
        "a day with no closed meetings at all must not empty the live board")


def test_meeting_days_offers_only_days_that_have_a_closed_meeting(desk):
    """The console's date picker is populated from this, so every entry has to be
    a day that lists something: a day with nothing in it is a dead choice, and a
    live meeting has no end date to offer yet."""
    _start("never closed", ["alpha", "beta"])
    morning = _start("day one, local morning", ["alpha", "beta"])
    evening = _start("day one, local evening", ["alpha", "beta"])
    later = _start("day two", ["alpha", "beta"])
    _close(morning, "done", ended_at=meetings._iso(
        dt.datetime(2026, 7, 14, 14, 0, tzinfo=dt.timezone.utc)))
    _close(evening, "done", ended_at=meetings._iso(_UTC_EVENING))
    _close(later, "done", ended_at=meetings._iso(
        dt.datetime(2026, 7, 16, 12, 0, tzinfo=dt.timezone.utc)))

    assert meetings.meeting_days() == ["2026-07-16", _LOCAL_DAY], (
        "newest first; two meetings on one local day are one choice, not two; "
        "and the live meeting contributes no day at all")


def test_migrating_a_db_without_closed_at_adds_it_and_backfills_only_closed_rows(desk):
    """closed_at is new, so every DB that predates it holds closed meetings whose
    end survives only as updated_at. Backfilling from it is an APPROXIMATION and
    the only one available — the tightest true upper bound, and exact on the rows
    that exist, because closing was in fact their last write. A live row has no
    end to approximate and must stay NULL: inventing one files a meeting that is
    still running into the history.

    This reaches across a layer on purpose. The column is meetings', but
    orchestration owns _migrate — meetings must never import it (module
    docstring), so the migration cannot be exercised from that side.
    """
    from deskd import orchestration

    closed = _start("closed before the column existed", ["alpha", "beta"])
    _close(closed, "done long ago")
    live = _start("still going", ["alpha", "beta"])

    legacy_end = _past(7200)
    with _db() as conn:
        conn.execute("UPDATE meetings SET updated_at=? WHERE thread_id=?",
                     (legacy_end, closed))
        # The pre-migration shape, in raw SQL: _migrate keys off the column
        # actually being absent, so it has to actually be absent.
        conn.execute("ALTER TABLE meetings DROP COLUMN closed_at")
        assert "closed_at" not in {
            r["name"] for r in conn.execute("PRAGMA table_info(meetings)")}

    with orchestration.connect():
        pass   # opening the DB IS the migration

    def _row(thread_id: str) -> dict:
        with _db() as conn:
            return dict(conn.execute(
                "SELECT state,closed_at FROM meetings WHERE thread_id=?",
                (thread_id,)).fetchone())

    assert _row(closed) == {"state": "closed", "closed_at": legacy_end}
    assert _row(live)["closed_at"] is None

    # Idempotent: every connect() runs _migrate, so a second ALTER would raise
    # "duplicate column name" and a re-backfill would overwrite real end times
    # with whatever last touched the row.
    with orchestration.connect():
        pass
    assert _row(closed) == {"state": "closed", "closed_at": legacy_end}
    assert _row(live)["closed_at"] is None
