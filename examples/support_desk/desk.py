"""Desk configuration for the overnight-helpdesk demo.

This module is a ``DESKD_CONFIG_MODULE`` target: importing it and calling
:func:`configure_deskd` is all a second process (the plain ``deskd`` CLI, or
``deskd serve``) needs to see the same roles, ladder, and database as the
running demo::

    PYTHONPATH=. DESKD_CONFIG_MODULE=examples.support_desk.desk \
        DESKD_DB=demo-desk.db deskd status show

Speed is controlled by ``DESKD_DEMO_FAST``:

* unset / ``0`` — the engine's production defaults (minutes-scale ladder).
* ``1``         — the demo ladder: hook 5s -> resume 6s -> spawn 8s ->
                  human 10s -> supervisor_badge (terminal), presence
                  online<8s / suspect<16s. Everything climbs on camera in
                  real wall-clock time, no time mocking.
* any float     — the demo ladder scaled by that factor (the smoke test runs
                  at ``0.25``).

Only the host-owned knobs change with speed; engine floors (meeting
``MIN_WAIT_TIMEOUT_SECONDS``, ``min_hook_every``) are left alone and the
scenario deliberately never depends on them.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the demo runnable from a source checkout without an install: if deskd
# is not importable, fall back to the sibling src/ tree.
try:  # pragma: no cover - trivial import shim
    import deskd  # noqa: F401
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from deskd import channels
from deskd.config import PromptBuilder, RoleSpec, WakeRung, configure, env

#: Three roles so capability routing has somewhere to land, somewhere to
#: overflow, and someone to go missing. The capability names are what
#: ``inbox_route`` targets — no code ever addresses a role by name for work.
ROLES = (
    RoleSpec("triage", "Triage", capabilities=("triage",)),
    RoleSpec("writer", "Writer", capabilities=("respond",)),
    RoleSpec("auditor", "Auditor", capabilities=("audit",)),
)

#: Pinned so a viewer can replay Beat 5 (the supervisor acting through the
#: authenticated web adapter) with a known code. Set your own in production.
DEMO_ACCESS_CODE = "letmein-demo"

#: The console port the README, the capture helper, and the narration agree on.
DEFAULT_PORT = 8913


class DemoPrompts(PromptBuilder):
    """Deterministic prompts: nothing in this demo interprets them (the fake
    agents are scripted), but the console and the plan_wakes output show them,
    so they should read like what a real host would send."""

    def bootstrap(self, role: str) -> str:
        return (f"[support-desk demo] You are the {role} agent on the "
                f"overnight helpdesk.")

    def wake(self, role: str, reasons: str, inbox_titles: list[str]) -> str:
        notes = ("; ".join(inbox_titles[:3])) if inbox_titles else ""
        return (f"{self.bootstrap(role)} Woken because: {reasons}."
                + (f" Inbox: {notes}." if notes else ""))


def demo_scale() -> float | None:
    """The ``DESKD_DEMO_FAST`` scale, or None for production speeds."""
    raw = (os.environ.get("DESKD_DEMO_FAST") or "").strip()
    if not raw or raw == "0":
        return None
    try:
        scale = float(raw)
    except ValueError:
        return 1.0
    return scale if scale > 0 else None


def demo_ladder(scale: float) -> tuple[WakeRung, ...]:
    """The on-camera ladder. Same shape as production — three machine rungs,
    then a human rung, then a terminal badge — just seconds instead of
    minutes, so the climb happens while you watch."""
    def s(base: float, floor: int = 1) -> int:
        return max(floor, round(base * scale))

    return (
        WakeRung("hook", s(5)),
        WakeRung("resume", s(6)),
        WakeRung("spawn", s(8, 2)),
        WakeRung("human", s(10, 2), leaves_machine=True),
        WakeRung("supervisor_badge", None, leaves_machine=True),
    )


def default_db_path() -> Path:
    """``DESKD_DB`` if set, else ./demo-desk.db in the current directory."""
    return Path(env("DB") or "demo-desk.db")


def configure_deskd(db_path: Path | str | None = None):
    """Install the demo config into the process-wide CONFIG.

    Called by ``run_demo`` at boot and by ``deskd.config.load_host_config``
    when this module is named as ``DESKD_CONFIG_MODULE``.
    """
    kwargs: dict = dict(
        roles=ROLES,
        db_path=Path(db_path) if db_path else default_db_path(),
        prompt_builder=DemoPrompts(),
        # The scenario enqueues plain support-desk events; "ticket" is the
        # host-owned source kind for them.
        inbox_sources=("alert", "signal", "system", "meeting", "supervisor",
                       "ticket"),
    )
    scale = demo_scale()
    if scale:
        kwargs.update(
            wake_ladder=demo_ladder(scale),
            online_max_seconds=max(2, round(8 * scale)),
            suspect_max_seconds=max(4, round(16 * scale)),
            # Short enough that a routed, non-urgent ticket wakes its owner
            # while the camera is still on the board.
            inbox_batch_seconds=max(2, round(6 * scale)),
            # Long enough that the terminal badge stays a badge for the whole
            # demo instead of recycling mid-story.
            terminal_retry_seconds=max(30, round(300 * scale)),
        )
    return configure(**kwargs)


class DemoPager:
    """The human rung, made visible: a CallableChannel that prints a boxed
    page to the terminal and appends it to ``pager.log``.

    ``available`` is a plain attribute so the scenario can take the pager
    offline mid-demo and show what the engine does when the human rung is
    unwired: the escalation ledger row goes ``queued`` (outbox only), the
    board lights ``undelivered_escalations`` / ``human_rung_unwired``, and
    the next tick after the pager returns re-delivers it.
    """

    CHANNEL = "pager"

    def __init__(self, log_path: Path | str | None = None, echo=print) -> None:
        self.available = True
        self.pages: list[tuple[str, str]] = []
        self.log_path = Path(log_path) if log_path else None
        self._echo = echo

    def send(self, subject: str, text: str) -> None:
        self.pages.append((subject, text))
        lines = [subject, *text.splitlines()]
        width = max(len(x) for x in lines) + 2
        box = "\n".join([
            "    +" + "=" * width + "+",
            "    |" + f" PAGE -> on-call human ".ljust(width) + "|",
            "    +" + "-" * width + "+",
            *("    | " + x.ljust(width - 1) + "|" for x in lines),
            "    +" + "=" * width + "+",
        ])
        self._echo(box)
        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(f"--- page #{len(self.pages)} ---\n{subject}\n{text}\n")

    def register(self) -> "DemoPager":
        channels.register_channel(channels.CallableChannel(
            self.CHANNEL, send=self.send,
            available=lambda: self.available,
        ))
        return self

    def unregister(self) -> None:
        channels.unregister_channel(self.CHANNEL)
