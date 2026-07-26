"""The ledger/channel split and the wake ladder's human rung (roadmap P2).

Three claims under test:

1. **The ledger is not the transport.** `deskd.channels` owns pluggable
   egress only; the durable rows stay with their owners. Registration via
   `deskd.meetings` must keep working — hosts wired channels through it —
   but it is deprecated, and the warning has to actually fire or nobody
   migrates.
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

CHANNEL_SURFACE = ("OUTBOX_CHANNEL", "CallableChannel", "EscalationChannel",
                   "register_channel", "registered_channels",
                   "unregister_channel")


@pytest.mark.parametrize("name", CHANNEL_SURFACE)
def test_meetings_still_resolves_the_channel_surface(desk, name):
    """Hosts register channels via deskd.meetings today; the move to
    deskd.channels must not strand them. Same objects, not copies — two
    registries would mean a channel registered through one spelling is
    invisible to the other."""
    from deskd import meetings
    with pytest.warns(DeprecationWarning):
        assert getattr(meetings, name) is getattr(channels, name)


@pytest.mark.parametrize("name", CHANNEL_SURFACE)
def test_meetings_channel_surface_warns_and_names_its_replacement(desk, name):
    """A deprecation nobody can see is just a comment. The warning has to fire
    on the `from`-import spelling hosts actually wrote, and it has to say
    where the name went — an unactionable warning gets filtered, not fixed."""
    from deskd import meetings
    with pytest.warns(DeprecationWarning) as caught:
        getattr(meetings, name)
    message = str(caught[0].message)
    assert f"deskd.channels.{name}" in message
    assert "from deskd import channels" in message


def test_from_import_spelling_still_works_and_warns(desk):
    """The PEP 562 hook, exercised the way a host wrote it. `from X import Y`
    on a package goes through importlib's fromlist handling before the
    IMPORT_FROM opcode, and both read the attribute — so assert the warning
    fired, not how many times."""
    with pytest.warns(DeprecationWarning):
        from deskd.meetings import CallableChannel, register_channel
    assert CallableChannel is channels.CallableChannel
    assert register_channel is channels.register_channel


def test_the_new_spelling_is_silent(desk):
    """The migration target must be clean, or the warning is telling people to
    move from one noisy import to another."""
    import warnings

    from deskd import channels as fresh
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert fresh.register_channel is channels.register_channel
        assert fresh.CallableChannel is channels.CallableChannel
        assert fresh.OUTBOX_CHANNEL == channels.OUTBOX_CHANNEL


def test_deskd_exposes_channels_as_an_attribute(desk):
    """`deskd.channels.register_channel` is what the deprecation warning tells
    hosts to write, so plain `import deskd` has to make it reachable — the
    lazy submodule hook in deskd/__init__.py, not an accident of some other
    module having imported it first."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    import deskd

    # A fresh interpreter, because `deskd.channels` resolves for free once
    # anything in this process has imported the submodule — which the suite
    # has. Only a cold start proves the hook is doing the work.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(deskd.__file__).resolve().parents[1]),
         env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    probe = subprocess.run(
        [sys.executable, "-c",
         "import deskd; print(deskd.channels.registered_channels())"],
        capture_output=True, text=True, env=env)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "()"


def test_meetings_does_not_swallow_unknown_attributes(desk):
    """The hook must stay a narrow shim: anything else is still an
    AttributeError, so a typo does not silently resolve to nothing."""
    from deskd import meetings
    with pytest.raises(AttributeError):
        meetings.no_such_name


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


def test_the_deprecated_names_survive_star_import_and_dir():
    """PEP 562's `__getattr__` covers attribute access and `from X import Y`,
    but NOT star-import (which reads `__dict__`) and NOT `dir()`. Both gaps
    fail silently — a vanished star-import surfaces as a NameError somewhere
    else entirely, and a capability probe written as `"register_channel" in
    dir(meetings)` quietly takes the unsupported branch instead of getting the
    deprecation. `__all__` and `__dir__` close them; this pins both."""
    import warnings

    from deskd import meetings

    for name in meetings._DEPRECATED_CHANNEL_NAMES:
        assert name in dir(meetings), f"{name} vanished from dir()"

    namespace: dict = {}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        exec("from deskd.meetings import *", namespace)
    for name in meetings._DEPRECATED_CHANNEL_NAMES:
        assert name in namespace, f"{name} vanished from star-import"
    assert any("deprecated" in str(c.message) for c in caught), (
        "star-import resolved the names without warning — the shim must not "
        "become a silent permanent alias")
    # The real API must still come through the same door.
    assert "call_meeting" in namespace and "meeting_status" in namespace
