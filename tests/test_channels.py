"""The ledger/channel split and the wake ladder's human rung (roadmap P2).

Three claims under test:

1. **The ledger is not the transport.** `deskd.channels` owns pluggable
   egress only; the durable rows stay with their owners. Registration via
   `deskd.meetings` must keep working — hosts wired channels through it.
2. **The terminal rung is not a UI.** Arrival at a `leaves_machine` rung
   writes a durable `wake_escalations` row for EVERY reason kind and mirrors
   it out through the registered channels — previously only meeting wakes
   escalated (via a driver-side branch) and every other demand reaching the
   human rung pulled in nobody.
3. **Hosts can SEE which rungs are wired.** An outbox-only escalation path is
   a fact the board must state (`health.human_rung_unwired`,
   `health.undelivered_escalations`), not something to discover at 3am.
"""

from __future__ import annotations

import pytest

from conftest import iso, scalar
from deskd import channels
from deskd import orchestration as o


@pytest.fixture
def recording_channel():
    """A registered, always-available channel that records what it sent."""
    sent = []
    channels.register_channel(channels.CallableChannel(
        "rec", send=lambda subject, text: sent.append((subject, text))))
    yield sent
    channels.unregister_channel("rec")


def _age_attempt(role: str, reason: str, ref: str, level: int) -> None:
    """Plant a pending attempt old enough that the next tick must escalate."""
    with o.connect(write=True) as conn:
        conn.execute(
            """INSERT INTO wake_attempts
                   (role, reason_kind, source_ref, channel, level,
                    attempted_at, outcome)
               VALUES (?,?,?,?,?,?, 'pending')""",
            (role, reason, ref, o._ladder()[level].channel, level, iso(-3600)))


# --- the module split, and back-compat ---------------------------------------

def test_meetings_reexports_the_channel_surface(desk):
    """Hosts register channels via deskd.meetings today; the move to
    deskd.channels must not strand them. Same objects, not copies — two
    registries would mean a channel registered through one spelling is
    invisible to the other."""
    from deskd import meetings
    assert meetings.CallableChannel is channels.CallableChannel
    assert meetings.register_channel is channels.register_channel
    assert meetings.OUTBOX_CHANNEL == channels.OUTBOX_CHANNEL


def test_channel_status_reports_outbox_and_availability(desk):
    down = channels.CallableChannel("down", send=lambda s, t: None,
                                    available=lambda: False)
    channels.register_channel(down)
    try:
        rows = {r["name"]: r for r in channels.channel_status()}
        assert rows["outbox"]["outbox"] is True
        assert rows["outbox"]["available"] is True
        assert rows["down"]["available"] is False
        assert not channels.human_reachable(), \
            "an unavailable channel must not count as reaching a person"
    finally:
        channels.unregister_channel("down")


# --- the human rung fires for every reason kind ------------------------------

def test_human_rung_arrival_dispatches_for_a_non_meeting_reason(desk,
                                                                recording_channel):
    """The exact live gap: an inbox demand climbing past the machine used to
    reach nobody. Now arrival writes the durable row and the channel mirrors
    it out, in the same tick."""
    o.inbox_enqueue("alpha", "alert", "act now", priority="urgent")
    _age_attempt("alpha", "inbox", "inbox:alpha", 2)

    plan = o.plan_wakes(record=True)

    esc = [e for e in plan["escalations"] if e["role"] == "alpha"]
    assert esc and esc[0]["status"] == "sent"
    assert esc[0]["reason_kind"] == "inbox"
    assert len(recording_channel) == 1
    subject, text = recording_channel[0]
    assert "alpha" in subject and "inbox" in text
    with o.connect() as conn:
        assert scalar(conn, "SELECT status FROM wake_escalations") == "sent"


def test_outbox_only_is_durable_and_counted_red(desk):
    """No channel registered: the row still exists (queued — the ledger IS the
    delivery of last resort) and the board says so out loud."""
    o.inbox_enqueue("beta", "alert", "act now", priority="urgent")
    _age_attempt("beta", "inbox", "inbox:beta", 2)

    plan = o.plan_wakes(record=True)
    esc = [e for e in plan["escalations"] if e["role"] == "beta"]
    assert esc and esc[0]["status"] == "queued"

    health = o.board()["health"]
    assert health["undelivered_escalations"] == 1
    assert health["human_rung_unwired"] is True
    names = [r["name"] for r in health["channels"]]
    assert names == ["outbox"]


def test_wired_rung_reads_wired(desk, recording_channel):
    assert o.board()["health"]["human_rung_unwired"] is False


def test_terminal_rung_arrival_is_its_own_escalation(desk, recording_channel):
    """L3 -> L4 is a second arrival: the demand outlived the human channel
    ping, and the terminal badge state deserves its own durable row."""
    o.inbox_enqueue("gamma", "alert", "act now", priority="urgent")
    _age_attempt("gamma", "inbox", "inbox:gamma", 2)
    o.plan_wakes(record=True)                       # -> L3, first escalation
    with o.connect(write=True) as conn:
        conn.execute(
            "UPDATE wake_attempts SET attempted_at=? WHERE outcome='pending'",
            (iso(-3600),))
    plan = o.plan_wakes(record=True)                # -> L4, second escalation
    esc = [e for e in plan["escalations"] if e["role"] == "gamma"]
    assert len(esc) == 1
    with o.connect() as conn:
        assert scalar(conn,
                      "SELECT COUNT(*) FROM wake_escalations WHERE role='gamma'"
                      ) == 2
    assert len(recording_channel) == 2


def test_dry_run_neither_records_nor_sends(desk, recording_channel):
    """record=False must stay inert on this axis too: no durable rows, and —
    the part a rollback cannot undo — no network."""
    o.inbox_enqueue("alpha", "alert", "act now", priority="urgent")
    _age_attempt("alpha", "inbox", "inbox:alpha", 2)

    plan = o.plan_wakes(record=False)
    assert plan["escalations"] == []
    assert recording_channel == []
    with o.connect() as conn:
        assert scalar(conn, "SELECT COUNT(*) FROM wake_escalations") == 0
