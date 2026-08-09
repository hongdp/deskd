"""The send response names the debts a message did not pay.

Measured live 2026-08-09 (weekly review): the analyst answered a question in
substance but with no reply_to, the obligation stayed pending, and the
ladder paged a human twice about an already-written answer. The ledger's
strictness is right; this mirror tells the SENDER at send time."""

from deskd import meetings


def _start(agenda, attendees):
    status = meetings.call_meeting(agenda=agenda, called_by=attendees[0],
                                   attendees=list(attendees))
    thread_id = status["meeting"]["thread_id"]
    for role in attendees[1:]:
        meetings.check_in(thread_id, role=role)
    return thread_id


def test_reply_without_reply_to_is_warned_and_named(desk):
    thread_id = _start("ledger mirror", ["alpha", "beta"])
    asked = meetings.send_update(thread_id, role="alpha", kind="question",
                                 body="what is the window?")
    qid = asked["message_id"]

    # Substantive answer, no reply_to: the live failure shape.
    out = meetings.send_update(thread_id, role="beta", kind="evidence",
                               body="six days, measured")
    assert f"#{qid}" in out["warning"]
    assert [o["message_id"] for o in out["unsettled_obligations"]] == [qid]
    assert "--reply-to" in out["warning"], "the warning carries the remedy"

    # Named answer settles it; the next send is warning-free.
    meetings.send_update(thread_id, role="beta", kind="answer",
                         body="as said: six days", reply_to=qid)
    clean = meetings.send_update(thread_id, role="beta", kind="evidence",
                                 body="one more note")
    assert "warning" not in clean
    assert "unsettled_obligations" not in clean


def test_a_debt_free_send_stays_quiet(desk):
    thread_id = _start("quiet", ["alpha", "beta"])
    out = meetings.send_update(thread_id, role="alpha", kind="evidence",
                               body="opening note")
    assert "warning" not in out
