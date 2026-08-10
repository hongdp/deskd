"""A page that reached a person must be retractable.

The ladder is one-directional by construction: it exists to push "you owe this"
harder until someone reacts, and it had no way to say "that one is settled".
`wake_escalations.status` only ever recorded whether the SEND worked.

Measured on the live desk 2026-08-09: two Discord pages went out at 22:31 and
22:36 for an owed reply, the reply landed at 22:41 and the demand was acked at
22:42 — and both rows still read `sent`, with nothing anywhere saying otherwise.
The only retraction available to the person paged was to go and query the
database. That cost is not symmetric with a missed page: a standing false alarm
spends attention with no floor, and it is what makes the next real page
ignorable.
"""

from __future__ import annotations

from deskd import channels
from deskd import orchestration as orch
from deskd.orchestration import connect, wake
from deskd.orchestration.store import _iso


def _sent_escalation(conn, role="alpha", reason="owed_reply",
                     ref="thread-1:473", level=4, status="sent"):
    cur = conn.execute(
        """INSERT INTO wake_escalations
               (role, reason_kind, source_ref, level, channel, reason, status,
                created_at, sent_at)
           VALUES (?,?,?,?,'auto','they owe a reply',?,?,?)""",
        (role, reason, ref, level, status, _iso(), _iso()))
    return cur.lastrowid


def _row(conn, eid):
    return conn.execute("SELECT * FROM wake_escalations WHERE id=?",
                        (eid,)).fetchone()


class TestResolution:

    def test_settling_a_demand_marks_its_open_escalations(self, desk):
        with connect(write=True) as conn:
            eid = _sent_escalation(conn)
            retract = wake._resolve_wake_escalations(
                conn, "alpha", "owed_reply", "thread-1:473", "acked", _iso())
            row = _row(conn, eid)
        assert row["resolved_at"]
        assert row["resolved_reason"] == "acked"
        assert retract == [eid], "a page that reached someone owes a retraction"

    def test_the_send_state_is_not_overwritten(self, desk):
        """`status` answers "did the send work"; resolution answers "is it still
        true". Folding the second into the first would erase the evidence that a
        page was ever delivered."""
        with connect(write=True) as conn:
            eid = _sent_escalation(conn)
            wake._resolve_wake_escalations(conn, "alpha", "owed_reply",
                                           "thread-1:473", "acked", _iso())
            assert _row(conn, eid)["status"] == "sent"

    def test_a_queued_row_is_settled_but_owes_no_retraction(self, desk):
        """Nobody saw it, so correcting them would be the same disease."""
        with connect(write=True) as conn:
            eid = _sent_escalation(conn, status="queued")
            retract = wake._resolve_wake_escalations(
                conn, "alpha", "owed_reply", "thread-1:473", "acked", _iso())
            assert _row(conn, eid)["resolved_at"]
        assert retract == []

    def test_an_already_settled_row_is_not_retracted_twice(self, desk):
        """The tick runs every minute; a second page about the same settled
        thing is the alarm this exists to remove."""
        with connect(write=True) as conn:
            _sent_escalation(conn)
            first = wake._resolve_wake_escalations(
                conn, "alpha", "owed_reply", "thread-1:473", "acked", _iso())
            second = wake._resolve_wake_escalations(
                conn, "alpha", "owed_reply", "thread-1:473", "acked", _iso())
        assert first and second == []

    def test_only_the_matching_demand_is_settled(self, desk):
        with connect(write=True) as conn:
            other = _sent_escalation(conn, ref="thread-2:99")
            wake._resolve_wake_escalations(conn, "alpha", "owed_reply",
                                           "thread-1:473", "acked", _iso())
            assert _row(conn, other)["resolved_at"] is None


class TestRetractionMessage:

    def test_it_says_settled_and_names_no_action(self, desk, monkeypatch):
        """A retraction that reads like another alarm costs what it was sent to
        refund."""
        seen = {}

        def _capture(subject, text, channel):
            seen.update(subject=subject, text=text, channel=channel)
            return [{"channel": "discord", "status": "sent"}]

        monkeypatch.setattr(channels, "deliver", _capture)
        with connect(write=True) as conn:
            eid = _sent_escalation(conn)
            wake._resolve_wake_escalations(conn, "alpha", "owed_reply",
                                           "thread-1:473", "acked", _iso())
        out = wake._dispatch_escalation_retraction(eid, None)

        assert out["status"] == "sent"
        assert "SETTLED" in seen["subject"]
        assert "no action needed" in seen["text"]
        # the ref is the only durable pointer, same rule as the page itself
        assert "thread-1:473" in seen["text"]

    def test_it_goes_out_on_the_channel_that_paged(self, desk, monkeypatch):
        """Recording the correction where the person paged will never look is
        the same as not correcting it."""
        seen = {}

        def _capture(subject, text, channel):
            seen["channel"] = channel
            return [{"channel": "discord", "status": "sent"}]

        monkeypatch.setattr(channels, "deliver", _capture)
        with connect(write=True) as conn:
            conn.execute("UPDATE wake_escalations SET channel='auto'")
            eid = _sent_escalation(conn)
            wake._resolve_wake_escalations(conn, "alpha", "owed_reply",
                                           "thread-1:473", "acked", _iso())
        wake._dispatch_escalation_retraction(eid, None)
        assert seen["channel"] == "auto"

    def test_a_missing_row_does_not_raise(self, desk):
        assert wake._dispatch_escalation_retraction(999_999, None)["status"] \
            == "missing"
