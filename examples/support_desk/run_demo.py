"""The overnight helpdesk — single-entrypoint deskd demo.

Run it::

    python -m examples.support_desk.run_demo
    # or: python examples/support_desk/run_demo.py

then open http://127.0.0.1:8913/board next to the terminal. The terminal
narrates the story; the console shows the engine living it. ~2.5 minutes,
no LLM, no API keys — the "agents" are scripted state machines in
``agents.py`` and every move goes through deskd's public API.

Single process ON PURPOSE: ``CONFIG`` and the channel registry are
process-local, so the console, the scenario driver, and the pager channel
must share one interpreter or the board's channel/health gauges would lie
(a separate ``deskd serve`` would report the human rung unwired even while
this process pages happily). For the same reason the console runs via
``uvicorn.Server`` in a daemon thread — never ``uvicorn.run``, which would
try to install signal handlers off the main thread.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

if __package__ in (None, ""):  # `python examples/support_desk/run_demo.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.support_desk import desk  # noqa: E402  (sys.path shim above)
from examples.support_desk.agents import make_agents  # noqa: E402

from deskd import meetings, orchestration  # noqa: E402
from deskd.config import CONFIG, SUPERVISOR_CODE_HEADER  # noqa: E402

#: Where the scenario can hold for screenshots (--pause-at NAME[:SECONDS]).
PAUSE_POINTS = ("board", "meeting", "escalation", "outage")


def narrate(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _banner(msg: str) -> None:
    narrate("")
    narrate("=" * 66)
    narrate(msg)
    narrate("=" * 66)


# --- console ------------------------------------------------------------


def start_console(port: int):
    """Serve the deskd console from a daemon thread of THIS process."""
    import uvicorn
    from deskd.web.app import create_app

    server = uvicorn.Server(uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True,
                     name="deskd-console").start()
    for _ in range(100):  # wait until the socket answers
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return server
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"console did not come up on port {port}")


def supervisor_post(port: int, payload: dict) -> dict:
    """A supervisor action the only way one exists: through the console's
    authenticated adapter. Agents cannot mint this — the access code lives
    with the human."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/meetings/supervisor-action",
        data=json.dumps({"payload": payload}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 SUPERVISOR_CODE_HEADER: os.environ["DESKD_SUPERVISOR_ACCESS_CODE"]},
        method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --- the beat script ------------------------------------------------------

class Scenario:
    """The story as (gap_seconds, step) pairs; the runner plays them over the
    ticking wake driver. Gaps are story-time and get multiplied by the
    DESKD_DEMO_FAST scale, so one script serves the live demo, the capture
    run, and the smoke test."""

    def __init__(self, agents, pager, *, port=None, auto_supervisor=False,
                 pause_at=None, say=narrate):
        self.agents = agents
        self.pager = pager
        self.port = port
        self.auto_supervisor = auto_supervisor
        self.pause_at = dict(pause_at or {})
        self.say = say
        self.summary: dict = {"pager": pager}

    def steps(self):
        return [
            (0, self.beat1_desk_up),
            (6, self.beat1_presence),
            (8, self.beat2_tickets),          # t=14
            (6, self.beat2_board),            # t=20
            (0, self.hold("board")),
            (10, self.beat3_call),            # t=30
            (4, self.beat3_checkin),          # t=34
            (2, self.beat3_question),         # t=36
            (4, self.beat3_read),             # t=40
            (2, self.beat3_answer),           # t=42
            (2, self.beat3_position_triage),  # t=44
            (2, self.beat3_position_writer),  # t=46
            (0, self.hold("meeting")),
            (2, self.beat3_propose),          # t=48
            (2, self.beat3_confirm),          # t=50
            (6, self.beat4_urgent_task),      # t=56
            (12, self.beat4_watch),           # t=68
            (0, self.hold("escalation")),
            (6, self.beat4_outage),           # t=74
            (12, self.beat4_outage_board),    # t=86
            (0, self.hold("outage")),
            (6, self.beat5_pager_back),       # t=92
            (4, self.beat5_postmortem),       # t=96
            (2, self.beat5_postmortem_checkin),  # t=98
            (2, self.beat5_supervisor),       # t=100
            (8, self.beat5_recovery),         # t=108
            (4, self.beat5_task_done),        # t=112
            (4, self.finale),                 # t=116
        ]

    def hold(self, name):
        def _hold():
            extra = self.pause_at.get(name, 0)
            if extra:
                self.say(f"    (holding at pause point {name!r} for "
                         f"{extra}s — screenshots)")
            return extra
        _hold.__name__ = f"hold_{name}"
        return _hold

    # -- Beat 1 -------------------------------------------------------------

    def beat1_desk_up(self):
        _banner("BEAT 1 — the desk comes up")
        for agent in self.agents.values():
            agent.start()
        orchestration.record_todos("triage", [
            {"content": "work the overnight ticket queue", "status": "in_progress"},
            {"content": "hand off open incidents at 9am", "status": "pending"},
        ])
        orchestration.record_todos("writer", [
            {"content": "clear the reply backlog", "status": "in_progress"},
        ])
        self.say("three roles registered; triage and writer are heartbeating,"
                 " auditor heartbeated once and went home")
        if self.port:
            self.say(f"console: http://127.0.0.1:{self.port}/board "
                     f"(auto-refreshes every 5s)")

    def beat1_presence(self):
        for p in orchestration.presence():
            age = p["heartbeat_age_seconds"]
            self.say(f"    presence: {p['role']:<7} {p['liveness']:<8}"
                     f" (heartbeat {age if age is not None else '—'}s ago)")

    # -- Beat 2 -------------------------------------------------------------

    def beat2_tickets(self):
        _banner("BEAT 2 — tickets land")
        first = orchestration.inbox_enqueue(
            "triage", "ticket", "Ticket #4812: customers cannot log in",
            body="Spike of login failures since 02:10; status page silent.",
            ref="ticket-4812", priority="urgent", dedup_key="ticket-4812")
        dup = orchestration.inbox_enqueue(
            "triage", "ticket", "Ticket #4812: customers cannot log in",
            ref="ticket-4812", priority="urgent", dedup_key="ticket-4812")
        self.say(f"urgent ticket -> triage inbox (id {first}); the duplicate "
                 f"enqueue returned {dup!r} — deduped, landed once")
        routed = orchestration.inbox_route(
            "respond", "ticket", "Draft a status-page update for the login outage",
            body="Customers are asking; keep it hedged until we have an ETA.",
            ref="ticket-4812")
        self.say(f"'respond' demand routed by capability -> "
                 f"{routed['routed_to']!r} (nobody named the writer)")
        legal = orchestration.inbox_route(
            "legal-review", "ticket",
            "Customer threatens a GDPR complaint — needs legal review",
            ref="ticket-4813")
        self.say(f"'legal-review' demand: unroutable={legal.get('unroutable')} "
                 f"— no enabled role declares it; recorded, shown red, "
                 f"never dropped")
        self.summary["unroutable_id"] = legal.get("id")

    def beat2_board(self):
        health = orchestration.board()["health"]
        self.say(f"board health: unroutable_demands="
                 f"{health['unroutable_demands']}, inbox_total="
                 f"{health['inbox_total']}")

    # -- Beat 3 -------------------------------------------------------------

    def beat3_call(self):
        _banner("BEAT 3 — a bounded huddle")
        res = meetings.call_meeting(
            agenda="Ticket #4812: login outage — response plan",
            called_by="triage", attendees=["triage", "writer"],
            max_messages=12, consensus_threshold=2, wait_timeout_seconds=30)
        tid = res["meeting"]["thread_id"]
        self.summary["outage_meeting"] = tid
        self.say(f"triage called a meeting ({tid}): max 12 messages, "
                 f"reply SLA 30s — bounded by construction")
        self.say("the invitation itself is wake demand for the writer "
                 "(machine rungs only)")

    def beat3_checkin(self):
        tid = self.summary["outage_meeting"]
        res = meetings.check_in(tid, role="writer")
        self.say(f"writer checked in -> quorum, meeting is "
                 f"{res['meeting']['state']!r}")

    def beat3_question(self):
        tid = self.summary["outage_meeting"]
        res = meetings.send_update(
            tid, role="triage", kind="question",
            body="Can we commit to a 9am fix ETA in the status update, "
                 "or do we hedge?")
        self.summary["question_id"] = res["message_id"]
        self.say("triage asked a QUESTION — the engine now holds the writer "
                 "to a reply (response obligation with an SLA)")

    def beat3_read(self):
        tid = self.summary["outage_meeting"]
        out = meetings.wait_for_updates(tid, role="writer", mark_read=True)
        self.say(f"writer read {len(out['messages'])} message(s) with "
                 f"mark_read=True — read receipts are in the delivery ledger")

    def beat3_answer(self):
        tid = self.summary["outage_meeting"]
        meetings.send_update(
            tid, role="writer", kind="answer",
            reply_to=self.summary["question_id"],
            resolves=[self.summary["question_id"]],
            body="Hedge. I'll say 'engineers engaged, next update 07:00' — "
                 "no ETA until the auditor signs off.")
        self.say("writer answered with resolves=[...] — the reply obligation "
                 "is settled on the ledger, not by vibes")

    def beat3_position_triage(self):
        meetings.submit_position(
            self.summary["outage_meeting"], role="triage",
            body="Ship the hedged status update now; revisit ETA at 07:00.")
        self.say("triage submitted its position")

    def beat3_position_writer(self):
        meetings.submit_position(
            self.summary["outage_meeting"], role="writer",
            body="Agreed — hedged copy goes out immediately.")
        self.say("writer submitted its position")

    def beat3_propose(self):
        tid = self.summary["outage_meeting"]
        meetings.propose_end(
            tid, role="triage",
            resolution="Hedged status update ships now; ETA decision at "
                       "07:00 pending audit.")
        self.say("triage proposed termination (its own confirm vote is "
                 "implicit) — ending needs unanimity, not silence")

    def beat3_confirm(self):
        tid = self.summary["outage_meeting"]
        res = meetings.confirm_end(tid, role="writer")
        state = res["meeting"]["meeting"]["state"]
        self.say(f"writer confirmed -> closed={res['closed']}, state={state!r}"
                 f" — two votes on record, meeting over")

    # -- Beat 4 -------------------------------------------------------------

    def beat4_urgent_task(self):
        _banner("BEAT 4 — the auditor went home")
        task_id = orchestration.task_add(
            "Audit today's outbound replies before 9am",
            assignee_role="auditor", priority="urgent", source_kind="system",
            detail="Ticket #4812 replies must be audited before the morning "
                   "handoff.")
        self.summary["audit_task"] = task_id
        self.say(f"URGENT task #{task_id} dropped on the auditor — who has "
                 f"not heartbeated since boot")
        self.say("watch the ladder: spawn is attempted first; only a demand "
                 "that keeps not landing climbs toward a person")

    def beat4_watch(self):
        health = orchestration.board()["health"]
        self.say(f"board health: pending_wakes={health['pending_wakes']}, "
                 f"wakes_at_human_level={health['wakes_at_human_level']}")
        self.say("note: the auditor's own task queue (idle_task) is fenced to "
                 "machine rungs — only the URGENT task was allowed to page")

    def beat4_outage(self):
        self.say("")
        self.say("-- now the pager goes down --")
        self.pager.available = False
        orchestration.inbox_enqueue(
            "auditor", "ticket",
            "Second wave: password-reset emails bouncing — audit sign-off needed",
            ref="ticket-4820", priority="urgent", dedup_key="ticket-4820")
        self.say("pager offline + a second urgent demand on the silent "
                 "auditor: the ladder climbs again, but no channel can take "
                 "the page")

    def beat4_outage_board(self):
        health = orchestration.board()["health"]
        self.say(f"board health: undelivered_escalations="
                 f"{health['undelivered_escalations']}, human_rung_unwired="
                 f"{health['human_rung_unwired']}")
        self.say("the page is PARKED as a durable outbox row — delivery "
                 "'queued', never dropped; the board says out loud that the "
                 "human rung is unwired")

    # -- Beat 5 -------------------------------------------------------------

    def beat5_pager_back(self):
        _banner("BEAT 5 — a human steps in, the loop closes")
        self.pager.available = True
        self.say("pager back online — the next tick retries every parked "
                 "escalation")

    def beat5_postmortem(self):
        res = meetings.call_meeting(
            agenda="Postmortem: overnight login outage",
            called_by="triage", attendees=["triage", "writer"],
            max_messages=8, consensus_threshold=2, wait_timeout_seconds=30)
        self.summary["postmortem_meeting"] = res["meeting"]["thread_id"]
        self.say("triage opened a postmortem huddle")

    def beat5_postmortem_checkin(self):
        meetings.check_in(self.summary["postmortem_meeting"], role="writer")
        self.say("writer checked in — postmortem is live")

    def beat5_supervisor(self):
        tid = self.summary["postmortem_meeting"]
        if self.auto_supervisor and self.port:
            try:
                supervisor_post(self.port, {"action": "join", "meeting_id": tid})
                supervisor_post(self.port, {
                    "action": "send", "meeting_id": tid, "kind": "decision",
                    "body": "Nice save overnight. The auditor was paged exactly "
                            "once and the ledger is clean — closing this out."})
                supervisor_post(self.port, {
                    "action": "force_close", "meeting_id": tid,
                    "reason": "supervisor review complete"})
                self.say("the SUPERVISOR joined, spoke, and force-closed the "
                         "postmortem — via the authenticated web adapter, the "
                         "only door a human authority has (agents cannot fake "
                         "this)")
                self.summary["supervisor_acted"] = True
                return
            except Exception as exc:  # keep the story moving on camera
                self.say(f"supervisor call failed ({exc}); agents close the "
                         f"huddle themselves")
            meetings.propose_end(tid, role="triage",
                                 resolution="Postmortem recorded; actions filed.")
            meetings.confirm_end(tid, role="writer")
            self.say("postmortem closed by the agents' own termination "
                     "handshake")
        else:
            code = os.environ.get("DESKD_SUPERVISOR_ACCESS_CODE", "<code>")
            self.say("your turn, human: the huddle stays open — act as "
                     "supervisor from another terminal:")
            self.say(f'''    curl -s -X POST http://127.0.0.1:{self.port or desk.DEFAULT_PORT}/api/meetings/supervisor-action \\
      -H 'Content-Type: application/json' -H '{SUPERVISOR_CODE_HEADER}: {code}' \\
      -d '{{"payload": {{"action": "join", "meeting_id": "{tid}"}}}}' ''')
            self.say("    (then the same with action 'send' + a 'body', and "
                     "'force_close' + a 'reason'; if you pass, the idle "
                     "deadline retires the thread on its own)")

    def beat5_recovery(self):
        self.say("")
        self.say("-- the auditor gets to a laptop --")
        self.agents["auditor"].recover(
            activity="back online — auditing outbound replies")
        orchestration.task_update(
            self.summary["audit_task"], status="in_progress", actor="auditor")
        self.say(f"auditor is heartbeating again and moved task "
                 f"#{self.summary['audit_task']} to in_progress; the next "
                 f"tick resolves every pending wake attempt WITH its latency")

    def beat5_task_done(self):
        orchestration.task_close(
            self.summary["audit_task"], status="done",
            note="Outbound replies audited; two flagged for tone.")
        orchestration.set_status("auditor", state="idle_standby",
                                 activity="audit complete")
        self.say(f"auditor closed task #{self.summary['audit_task']} and "
                 f"parked (idle_standby — resting, not dead)")

    def finale(self):
        health = orchestration.board()["health"]
        pager = self.summary["pager"]
        _banner("EPILOGUE — what just happened")
        self.say(f"pages that reached a human: {len(pager.pages)} "
                 f"(pager.log has the transcript)")
        self.say(f"board health now: pending_wakes={health['pending_wakes']}, "
                 f"wakes_at_human_level={health['wakes_at_human_level']}, "
                 f"undelivered_escalations={health['undelivered_escalations']}")
        self.say(f"still red on purpose: unroutable_demands="
                 f"{health['unroutable_demands']} — the legal-review demand "
                 f"waits until a role declares that capability")
        self.say("no agent ever slept, polled, or self-scheduled: the engine "
                 "woke the right one, proved delivery, and paged a person "
                 "exactly as many times as it had to")


# --- the driver loop --------------------------------------------------------


def run_scenario(*, pager, port=None, auto_supervisor=False, pause_at=None,
                 scale=None, say=narrate) -> dict:
    """Play the beats over a ticking wake driver. Returns the scenario
    summary (meeting ids, task id, pager). Assumes ``desk.configure_deskd``
    already ran in this process and ``pager`` is registered."""
    scale = scale or desk.demo_scale() or 1.0
    tick = max(0.4, 2.0 * scale)
    agents = make_agents(narrate=say)
    scenario = Scenario(agents, pager, port=port,
                        auto_supervisor=auto_supervisor, pause_at=pause_at,
                        say=say)
    queue = list(scenario.steps())
    ladder = CONFIG.wake_ladder
    seen_rungs: set[tuple] = set()
    start = time.monotonic()
    next_at = start

    while queue:
        now = time.monotonic()
        while queue and now >= next_at + queue[0][0] * scale:
            gap, step = queue.pop(0)
            next_at += gap * scale
            extra = step()
            if extra:  # a screenshot hold shifts everything after it
                next_at += extra
        for agent in agents.values():
            agent.pump()
        plan = orchestration.plan_wakes()
        for c in plan["changed"]:
            key = (c["role"], c["reason_kind"], c["source_ref"], c["level"])
            if c.get("escalated") or key not in seen_rungs:
                seen_rungs.add(key)
                rung = ladder[c["level"]]
                say(f"  ladder: {c['role']}/{c['reason_kind']} -> "
                    f"L{c['level']} ({rung.channel})"
                    + ("  ** leaves the machine **" if rung.leaves_machine
                       else ""))
        for action in plan["actions"]:
            if action["channel"] in {"resume", "spawn"}:
                agents[action["role"]].on_wake(action)
        for esc in plan["escalations"]:
            say(f"  escalation #{esc['id']} for {esc['role']} "
                f"({esc['reason_kind']}): delivery status={esc['status']!r}")
        for esc in plan["escalations_retried"]:
            say(f"  escalation #{esc['id']} RETRIED -> "
                f"status={esc['status']!r} (the outage row was never lost)")
        for res in plan["resolved"]:
            say(f"  wake resolved: {res['role']}/{res['reason_kind']} "
                f"outcome={res['outcome']} latency={res['latency_seconds']}s")
        time.sleep(max(0.05, tick - (time.monotonic() - now)))

    scenario.summary["ignored_wakes"] = agents["auditor"].ignored_wakes
    scenario.summary["agents"] = agents
    return scenario.summary


def fresh_db(db_path: Path, keep: bool) -> None:
    """A stale DB poisons the story (old attempts climb ladders at boot):
    default to a fresh file every run."""
    if keep:
        return
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()


def parse_pause(values) -> dict:
    out = {}
    for v in values or ():
        name, _, secs = v.partition(":")
        if name not in PAUSE_POINTS:
            raise SystemExit(f"--pause-at {name!r}: pick from "
                             f"{', '.join(PAUSE_POINTS)}")
        out[name] = float(secs) if secs else 30.0
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="deskd demo: the overnight helpdesk")
    ap.add_argument("--port", type=int, default=desk.DEFAULT_PORT,
                    help="console port (default %(default)s)")
    ap.add_argument("--db", default=None,
                    help="database file (default: DESKD_DB or ./demo-desk.db)")
    ap.add_argument("--keep-db", action="store_true",
                    help="do not wipe the demo DB at boot")
    ap.add_argument("--speed", type=float, default=None,
                    help="scale factor for beats AND ladder (sets "
                         "DESKD_DEMO_FAST; 1 = the 2.5-minute demo, "
                         "0.5 = twice as fast)")
    ap.add_argument("--pause-at", action="append", metavar="NAME[:SECONDS]",
                    help=f"hold the story for screenshots at one of: "
                         f"{', '.join(PAUSE_POINTS)} (default 30s)")
    ap.add_argument("--auto-supervisor", action="store_true",
                    help="script Beat 5's supervisor action instead of "
                         "inviting the viewer to do it")
    ap.add_argument("--exit-when-done", action="store_true",
                    help="exit after the last beat instead of keeping the "
                         "console up")
    args = ap.parse_args(argv)

    # Speed: an explicit --speed wins; otherwise honour DESKD_DEMO_FAST;
    # otherwise this is a demo, so default to demo speed.
    if args.speed is not None:
        os.environ["DESKD_DEMO_FAST"] = str(args.speed)
    else:
        os.environ.setdefault("DESKD_DEMO_FAST", "1")
    # Pinned so Beat 5 is reproducible; export your own to override.
    os.environ.setdefault("DESKD_SUPERVISOR_ACCESS_CODE", desk.DEMO_ACCESS_CODE)

    db_path = Path(args.db) if args.db else desk.default_db_path()
    fresh_db(db_path, args.keep_db)
    desk.configure_deskd(db_path=db_path)
    pager = desk.DemoPager(log_path=db_path.with_name("pager.log")).register()

    start_console(args.port)
    narrate(f"deskd console up: http://127.0.0.1:{args.port}/board  "
            f"(db: {db_path})")
    narrate(f"supervisor access code: "
            f"{os.environ['DESKD_SUPERVISOR_ACCESS_CODE']}")

    summary = run_scenario(pager=pager, port=args.port,
                           auto_supervisor=args.auto_supervisor,
                           pause_at=parse_pause(args.pause_at))

    if args.exit_when_done:
        return
    narrate("")
    narrate(f"demo complete — console still live at "
            f"http://127.0.0.1:{args.port}/board ; Ctrl+C to quit")
    try:
        while True:  # keep the desk breathing for browsing
            for agent in summary["agents"].values():
                agent.pump()
            orchestration.plan_wakes()
            time.sleep(2)
    except KeyboardInterrupt:
        narrate("bye")


if __name__ == "__main__":
    main()
