"""Smoke: the shipped example must actually do what its README narrates.

Runs the full overnight-helpdesk scenario (examples/support_desk) against a
tmp database at DESKD_DEMO_FAST=0.25 — quarter-speed ladder, ~30s of real
wall clock, no console, no browser — then asserts the story's beats out of
the ENGINE, not out of the narration: the demo printing a lie would pass any
assertion made on its own output.

This is deliberately one long test, not four: the beats are one causal chain
(roles -> meeting -> silence -> ladder -> pager), and re-running the 30s
scenario per assertion would buy isolation between facts that cannot occur
independently anyway.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskd import channels, meetings, orchestration  # noqa: E402
from deskd.config import CONFIG  # noqa: E402


def test_support_desk_scenario_beats(desk, tmp_path, monkeypatch):
    # `desk` is reused purely as the CONFIG snapshot/restore guard —
    # configure_deskd below overwrites its alpha/beta/gamma setup, and the
    # fixture's finally puts every field back for the rest of the suite.
    monkeypatch.setenv("DESKD_DEMO_FAST", "0.25")

    from examples.support_desk import desk as demo_desk
    from examples.support_desk.run_demo import run_scenario

    demo_desk.configure_deskd(db_path=tmp_path / "demo.db")
    quiet = lambda *_a, **_k: None  # noqa: E731
    pager = demo_desk.DemoPager(echo=quiet).register()
    try:
        summary = run_scenario(pager=pager, port=None,
                               auto_supervisor=False, say=quiet)

        # Beat 1 — every role the desk module declares is registered and has
        # presence (a session row: even the auditor heartbeated once).
        pres = {p["role"]: p for p in orchestration.presence()}
        assert set(pres) == {"triage", "writer", "auditor"}
        assert all(p["last_heartbeat_at"] for p in pres.values())

        # Beat 3 — the outage meeting closed THROUGH the termination
        # handshake: a proposal and the counterpart's confirm vote are in the
        # event log, not just a closed state (force-close would fake that).
        tid = summary["outage_meeting"]
        status = meetings.meeting_status(tid)
        assert status["meeting"]["state"] == "closed"
        events = meetings.meeting_transcript(tid)["events"]
        votes = {(e["event"], e["actor"]) for e in events
                 if e["event"] in ("termination_proposed",
                                   "termination_confirm")}
        assert ("termination_proposed", "triage") in votes
        assert ("termination_confirm", "writer") in votes

        # Beat 4 — the silent auditor ignored machine wakes and the ladder
        # climbed to a rung that leaves the machine.
        assert summary["ignored_wakes"] >= 1
        human = next(i for i, r in enumerate(CONFIG.wake_ladder)
                     if r.leaves_machine)
        attempts = orchestration.wake_attempts_recent(100)
        assert any(a["role"] == "auditor" and a["level"] >= human
                   for a in attempts)

        # ...and the pager channel actually fired, with a matching SENT row
        # in the durable escalation ledger (the row is the guarantee; the
        # channel only mirrors it).
        assert pager.pages, "no page reached the human channel"
        assert any("auditor" in subject for subject, _ in pager.pages)
        with orchestration.connect() as conn:
            sent = conn.execute(
                "SELECT COUNT(*) FROM wake_escalations "
                "WHERE role='auditor' AND status='sent'").fetchone()[0]
        assert sent >= 1

        # Beat 5 — recovery resolved every wake: nothing is still pending
        # and nothing is still parked outbox-only.
        health = orchestration.board()["health"]
        assert health["pending_wakes"] == 0
        assert health["wakes_at_human_level"] == 0
        assert health["undelivered_escalations"] == 0
        # The unroutable legal-review demand is the one red left on purpose.
        assert health["unroutable_demands"] == 1
    finally:
        pager.unregister()  # the channel registry is process state
