"""Scripted stand-ins for real agent sessions. No LLM anywhere.

The engine never executes agent code — it decides who should be woken and a
driver executes the plan. Here the demo IS the driver, and a ``FakeAgent`` is
the whole "session" it launches: a tiny state machine that heartbeats, stamps
its inbox delivered (the in-session hook's job), acks what it handled, and
answers wake actions. Every call goes through the public
``deskd.orchestration`` / ``deskd.meetings`` API, so this file doubles as a
reference for what a real harness-side driver would do.
"""

from __future__ import annotations

from deskd import meetings, orchestration


class FakeAgent:
    """One role's scripted presence.

    ``responsive=False`` is the demo's off-switch: the agent stops
    heartbeating, ignores every wake the planner hands the driver, and lets
    the ladder climb — which is the point of Beat 4. Flip it back and the
    next pump behaves like the woken session finally booting.
    """

    def __init__(self, role: str, *, activity: str = "", responsive: bool = True,
                 ack_after_pumps: int = 2, narrate=print) -> None:
        self.role = role
        self.activity = activity
        self.responsive = responsive
        self.ack_after_pumps = ack_after_pumps
        self.narrate = narrate
        self.ignored_wakes = 0
        self._to_ack: list[tuple[int, list[int]]] = []  # (due_pump, inbox ids)
        self._first_seen: dict[int, int] = {}           # inbox id -> pump seen
        self._pumps = 0

    # -- session lifecycle -----------------------------------------------

    def start(self, *, state: str = "working") -> None:
        """Boot: one heartbeat, whatever `responsive` says. An agent that will
        go dark still shows up once — that is what makes its decay visible."""
        orchestration.set_status(self.role, state=state, activity=self.activity)

    def set_activity(self, activity: str) -> None:
        self.activity = activity

    # -- the driver tick ---------------------------------------------------

    def pump(self) -> None:
        """One driver tick of in-session behaviour: heartbeat, then play the
        in-session hook (deliver queued inbox items now, ack them a couple of
        pumps later, so the board's queued -> delivered -> acked transitions
        are visible rather than instantaneous)."""
        if not self.responsive:
            return
        self._pumps += 1
        orchestration.heartbeat(self.role, state="working", activity=self.activity)

        # Deliver an item one pump after it appears, not instantly: the
        # board's queued -> delivered transition deserves a frame of its own.
        pending = orchestration.inbox_pending(self.role)
        queued = [i for i in pending if not i["delivered_at"]]
        ready = []
        for i in queued:
            seen = self._first_seen.setdefault(i["id"], self._pumps)
            if self._pumps > seen:
                ready.append(i)
        if ready:
            orchestration.inbox_mark_delivered([i["id"] for i in ready])
            for i in ready:
                self.narrate(f"{self.role}: picked up "
                             f"[{i['priority']}] {i['title']!r}")
            self._to_ack.append((self._pumps + self.ack_after_pumps,
                                 [i["id"] for i in ready]))
        while self._to_ack and self._to_ack[0][0] <= self._pumps:
            _, ids = self._to_ack.pop(0)
            orchestration.inbox_ack(self.role, ids=ids)
            self.narrate(f"{self.role}: handled and acked "
                         f"{len(ids)} inbox item(s)")

    # -- executing a wake plan ----------------------------------------------

    def on_wake(self, action: dict) -> bool:
        """Execute one ``plan_wakes`` action for this role. Returns whether
        the wake landed. A real driver would resume/spawn a session with
        ``action['prompt']`` under ``action['authority']``; here the fake
        session just does what a freshly-booted one would."""
        channel = action.get("channel")
        if not self.responsive:
            self.ignored_wakes += 1
            self.narrate(f"{self.role}: ...silence (ignored the {channel!r} "
                         f"wake, attempt #{self.ignored_wakes})")
            return False
        orchestration.heartbeat(self.role, state="working", activity=self.activity)
        # A booted session's first duties: see what woke it, take delivery.
        for reason in action.get("reasons", []):
            if reason.get("reason_kind") == "meeting_wake":
                try:
                    meetings.check_in(reason["source_ref"], role=self.role)
                    self.narrate(f"{self.role}: checked in to meeting "
                                 f"{reason['source_ref']}")
                except ValueError:
                    pass  # meeting already closed/paused; nothing owed
        self.pump()
        return True

    # -- recovery (Beat 5) ---------------------------------------------------

    def recover(self, *, activity: str) -> None:
        """The dark agent gets to a laptop: heartbeat, take delivery of
        everything, ack it. Task movement is the scenario's line to speak —
        it narrates WHICH task — so it stays out of here."""
        self.responsive = True
        self.activity = activity
        orchestration.set_status(self.role, state="working", activity=activity)
        pending = orchestration.inbox_pending(self.role)
        if pending:
            orchestration.inbox_mark_delivered([i["id"] for i in pending])
            orchestration.inbox_ack(self.role, ids=[i["id"] for i in pending])
            self.narrate(f"{self.role}: caught up on {len(pending)} "
                         f"inbox item(s)")


def make_agents(narrate=print) -> dict[str, FakeAgent]:
    """The demo cast. Auditor starts unresponsive-after-boot on purpose."""
    return {
        "triage": FakeAgent("triage", activity="triaging the overnight queue",
                            narrate=narrate),
        "writer": FakeAgent("writer", activity="drafting customer replies",
                            narrate=narrate),
        "auditor": FakeAgent("auditor", activity="wrapping up the evening audit",
                             responsive=False, narrate=narrate),
    }
