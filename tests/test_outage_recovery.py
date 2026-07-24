"""The 2026-07-23 outage, pinned so it can never repeat.

A DNS outage killed every spawn (the agent process died at the API) AND the
host's Discord channel, in the same window. Two things then went wrong that
were the ENGINE's fault, not the network's:

1. The demands climbed to the terminal rung and PARKED. sla=None meant no
   retry, ever — and the inbox demand key aggregates per role, so every
   notification that arrived after the outage rode the parked attempt into
   permanent silence. Network recovery healed nothing.
2. The human-rung escalations were dispatched once, failed (channel down),
   and `failed` was a terminal state — the page explaining why nothing else
   works was dropped exactly when the transport was down.

The fixes under test: a terminal attempt recycles back to the machine rungs
after CONFIG.terminal_retry_seconds, and undelivered escalations re-mirror
each tick while fresh.
"""

from __future__ import annotations

import pytest

from conftest import iso, scalar
from deskd import channels
from deskd import config as cfg_mod
from deskd import orchestration as o


@pytest.fixture
def recording_channel():
    sent = []
    channels.register_channel(channels.CallableChannel(
        "rec", send=lambda subject, text: sent.append((subject, text))))
    yield sent
    channels.unregister_channel("rec")


def _park_at_terminal(role: str, age_seconds: float) -> None:
    """An urgent inbox demand whose attempt sits at the terminal rung, aged."""
    o.inbox_enqueue(role, "alert", "act now", priority="urgent")
    terminal = len(o._ladder()) - 1
    with o.connect(write=True) as conn:
        conn.execute(
            """INSERT INTO wake_attempts
                   (role, reason_kind, source_ref, channel, level,
                    attempted_at, outcome)
               VALUES (?,'inbox',?,?,?,?, 'pending')""",
            (role, f"inbox:{role}", o._ladder()[terminal].channel, terminal,
             iso(-age_seconds)))


def test_terminal_attempt_recycles_to_the_machine_rungs(desk):
    """The parking-brake bug. Past terminal_retry_seconds the ladder must
    supersede the parked attempt and climb again from the machine rungs —
    the recovered network gets its chance without a human doing surgery."""
    _park_at_terminal("alpha", cfg_mod.CONFIG.terminal_retry_seconds + 60)

    plan = o.plan_wakes(record=True)

    acts = [a for a in plan["actions"] if a["role"] == "alpha"]
    assert acts and acts[0]["channel"] in ("resume", "spawn"), \
        "the recycled demand must produce a machine-rung action"
    with o.connect() as conn:
        assert scalar(conn,
                      "SELECT COUNT(*) FROM wake_attempts WHERE outcome='pending' "
                      "AND level=?", (len(o._ladder()) - 1,)) == 0, \
            "the parked terminal attempt must be superseded"


def test_terminal_attempt_waits_out_the_configured_delay(desk):
    """Before the delay elapses the badge stands: no churn, no re-climb."""
    _park_at_terminal("beta", 60)
    plan = o.plan_wakes(record=True)
    assert [a for a in plan["actions"] if a["role"] == "beta"] == []
    with o.connect() as conn:
        assert scalar(conn,
                      "SELECT COUNT(*) FROM wake_attempts WHERE outcome='pending' "
                      "AND role='beta' AND level=?", (len(o._ladder()) - 1,)) == 1


def test_recycle_does_not_re_page_by_itself(desk, recording_channel):
    """Recycling is a machine act: no new escalation row, no ping. A person
    is pulled in again only if the ladder genuinely climbs back up."""
    _park_at_terminal("gamma", cfg_mod.CONFIG.terminal_retry_seconds + 60)
    o.plan_wakes(record=True)
    with o.connect() as conn:
        assert scalar(conn, "SELECT COUNT(*) FROM wake_escalations") == 0
    assert recording_channel == []


def test_failed_escalation_retries_until_a_channel_takes_it(desk,
                                                            recording_channel):
    """`failed` must mean "not yet", not "never": the page dropped during the
    outage goes out on the first tick after the transport recovers."""
    with o.connect(write=True) as conn:
        conn.execute(
            """INSERT INTO wake_escalations
                   (role, reason_kind, source_ref, level, channel, reason,
                    status, created_at)
               VALUES ('alpha','inbox','inbox:alpha',3,'auto','1 notification',
                       'failed',?)""", (iso(-600),))
    plan = o.plan_wakes(record=True)
    assert [r["status"] for r in plan["escalations_retried"]] == ["sent"]
    assert len(recording_channel) == 1
    with o.connect() as conn:
        assert scalar(conn, "SELECT status FROM wake_escalations") == "sent"


def test_queued_escalation_upgrades_only_when_a_channel_appears(desk):
    """Outbox-only delivery is legitimate for a channel-less host — a queued
    row must NOT churn. Once a channel reports available, it upgrades."""
    with o.connect(write=True) as conn:
        conn.execute(
            """INSERT INTO wake_escalations
                   (role, reason_kind, source_ref, level, channel, reason,
                    status, created_at)
               VALUES ('beta','inbox','inbox:beta',4,'auto','badge',
                       'queued',?)""", (iso(-600),))
    o.plan_wakes(record=True)
    with o.connect() as conn:
        assert scalar(conn, "SELECT status FROM wake_escalations") == "queued"

    sent = []
    channels.register_channel(channels.CallableChannel(
        "late", send=lambda s, t: sent.append((s, t))))
    try:
        plan = o.plan_wakes(record=True)
        assert [r["status"] for r in plan["escalations_retried"]] == ["sent"]
        assert len(sent) == 1
    finally:
        channels.unregister_channel("late")


def test_stale_escalations_are_history_not_pages(desk, recording_channel):
    """Past 24h the gauge is the record; nobody gets paged about last week."""
    with o.connect(write=True) as conn:
        conn.execute(
            """INSERT INTO wake_escalations
                   (role, reason_kind, source_ref, level, channel, reason,
                    status, created_at)
               VALUES ('gamma','inbox','inbox:gamma',3,'auto','old',
                       'failed',?)""", (iso(-90000),))
    plan = o.plan_wakes(record=True)
    assert plan["escalations_retried"] == []
    assert recording_channel == []


def test_dry_tick_neither_recycles_nor_retries(desk, recording_channel):
    _park_at_terminal("alpha", cfg_mod.CONFIG.terminal_retry_seconds + 60)
    with o.connect(write=True) as conn:
        conn.execute(
            """INSERT INTO wake_escalations
                   (role, reason_kind, source_ref, level, channel, reason,
                    status, created_at)
               VALUES ('alpha','inbox','inbox:alpha',3,'auto','x',
                       'failed',?)""", (iso(-600),))
    plan = o.plan_wakes(record=False)
    assert plan["escalations_retried"] == []
    assert recording_channel == []
    with o.connect() as conn:
        assert scalar(conn,
                      "SELECT COUNT(*) FROM wake_attempts WHERE outcome='pending' "
                      "AND level=?", (len(o._ladder()) - 1,)) == 1