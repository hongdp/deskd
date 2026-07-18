"""deskd configuration — the single contract every engine module codes against.

deskd is a domain-agnostic orchestration engine for multi-agent desks: it owns
agent presence, a unified notification inbox, cross-session tasks, bounded
meetings, wake orchestration (timers/cron/probes + an escalation ladder), a
delivery ledger, and session lifecycle. It knows nothing about any domain — a
host application supplies the roles, the notification sources, and the prompt
that boots a woken session.

Nothing here is domain-specific. If you find yourself adding a domain concept
to this file, it belongs in the host application instead.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_NAME = "deskd"
ENV_PREFIX = "DESKD_"
__version__ = "0.1.0"


def env(name: str, default: str | None = None) -> str | None:
    """Read a deskd env var (DESKD_<NAME>)."""
    return os.environ.get(ENV_PREFIX + name, default)


# --- paths ------------------------------------------------------------------

BASE_DIR = Path(env("HOME") or Path.cwd())
DB_PATH = Path(env("DB") or (BASE_DIR / "data" / f"{PROJECT_NAME}.db"))

# The supervisor's Ed25519 public key path is INTENTIONALLY fixed and NOT
# environment-overridable: an agent must not be able to point verification at a
# key it generated in a writable workspace. Keep the private key off this host.
SUPERVISOR_PUBLIC_KEY_PATH = Path(f"/etc/{PROJECT_NAME}/supervisor_ed25519.pub")
SUPERVISOR_KEY_REQUIRE_ROOT = True
SUPERVISOR_ASSERTION_MAX_SECONDS = 600
SUPERVISOR_CODE_HEADER = f"X-{PROJECT_NAME.capitalize()}-Supervisor-Code"

# Role-scoped process lock: every path that can start a session for a role
# (scheduler, wake driver, host runner) MUST flock this same file, so at most
# one session per role ever runs.
def role_lock_path(role: str) -> Path:
    return Path(f"/tmp/{PROJECT_NAME}-role-{role}.lock")


def driver_lock_path() -> Path:
    return Path(f"/tmp/{PROJECT_NAME}-wake-driver.lock")


# --- role registry ----------------------------------------------------------

@dataclass(frozen=True)
class RoleSpec:
    """One agent role. `authority` is an opaque dict the engine stores and
    surfaces but never interprets — the host decides what it means."""
    name: str
    display_name: str = ""
    capabilities: tuple[str, ...] = ()
    authority: dict = field(default_factory=dict)


# --- wake ladder ------------------------------------------------------------

@dataclass(frozen=True)
class WakeRung:
    """One rung of the escalation ladder. `sla_seconds=None` = terminal.

    A rung DECLARES whether reaching it pulls a human in (`leaves_machine`)
    rather than the engine recognising it by name: the ladder is the host's to
    define, so channel names carry no meaning to the engine. Defaults to False,
    so an existing ladder keeps its previous (positional-fallback) behaviour.
    """
    channel: str
    sla_seconds: int | None
    leaves_machine: bool = False


#: L0 in-session hook → L1 resume → L2 spawn → L3 human → L4 supervisor badge.
DEFAULT_WAKE_LADDER: tuple[WakeRung, ...] = (
    WakeRung("hook", 60),            # agent online — its in-session hook delivers
    WakeRung("resume", 120),         # resume the role's existing session
    WakeRung("spawn", 180),          # spawn a fresh session for the role
    # From here up a person is being pulled in — the rungs that should make
    # someone look at the board.
    WakeRung("human", 300, leaves_machine=True),      # human channel (Discord/email)
    WakeRung("supervisor_badge", None, leaves_machine=True),  # terminal: red on the console
)


# --- prompts ----------------------------------------------------------------

class PromptBuilder:
    """How a woken session is booted. A cold-spawned session has NO context, so
    the host must tell it what it is and where its instructions live.

    Subclass and pass via EngineConfig.prompt_builder to inject your own
    bootstrap (e.g. "load the <x> skill, declare role=<role>, follow <playbook>").
    """

    def bootstrap(self, role: str) -> str:
        return (f"Headless orchestrator wake. You are role={role}. "
                f"Load your role's instructions before acting.")

    def wake(self, role: str, reasons: str, inbox_titles: list[str]) -> str:
        lines = "; ".join(inbox_titles[:5])
        more = f" (+{len(inbox_titles) - 5} more)" if len(inbox_titles) > 5 else ""
        notes = f" Notifications: {lines}{more}." if inbox_titles else ""
        return (f"{self.bootstrap(role)} Woken because: {reasons}.{notes} "
                f"First: `{PROJECT_NAME} wake sources --role {role}`, then handle "
                f"your inbox and ack it. End your turn when done — never sleep, "
                f"poll, or self-schedule; the orchestrator wakes you.")


# --- engine config ----------------------------------------------------------

@dataclass
class EngineConfig:
    """Everything domain-specific the engine needs, injected by the host."""

    #: Roles seeded into agent_registry. EMPTY by default — the host supplies
    #: them; the engine never assumes any particular role exists.
    roles: tuple[RoleSpec, ...] = ()

    #: The human/supervisor identity. NEVER a valid agent role: agent-facing
    #: APIs reject it, and supervisor actions only enter via the authenticated
    #: Web adapter.
    supervisor_role: str = "supervisor"

    #: Timezone for the session-rollover day boundary and cron hook defaults.
    timezone: str = env("TZ") or "UTC"

    #: Dotted-path prefixes a `probe` wake-hook may import, e.g.
    #: ("myapp.watchers",). EMPTY = deny all probes. The engine only ever runs
    #: code the host explicitly allows; a probe may observe and notify, nothing else.
    probe_allowlist: tuple[str, ...] = ()

    #: Allowed inbox source kinds. The host may extend with its own.
    inbox_sources: tuple[str, ...] = (
        "alert", "signal", "system", "meeting", "supervisor",
    )

    #: Allowed task provenance kinds. The host may extend with its own, exactly
    #: like inbox_sources — agent_tasks.source_kind carries no CHECK constraint
    #: precisely so the host owns this enumeration. `supervisor_role` is always
    #: accepted on top of these (it is configurable, so it cannot be a literal).
    task_sources: tuple[str, ...] = ("meeting", "self", "system")

    #: Escalation ladder.
    wake_ladder: tuple[WakeRung, ...] = DEFAULT_WAKE_LADDER

    #: Non-urgent inbox items coalesce for this long before they wake anyone.
    inbox_batch_seconds: int = 180

    #: How many `idle_task` wakes a task may sit through, without moving, before
    #: it is STALLED: it stops raising wakes and becomes a reported fact instead.
    #: This is what makes the queue-wake loop terminate structurally rather than
    #: by a cooldown. It is the host's number because it prices the host's wake:
    #: an attempt row is not proof a session ran (the driver may skip on a held
    #: role lock, or the launch may fail), so this must stay above 1 or one lost
    #: launch would retire a task nobody ever saw. Default 3 — the same shape of
    #: judgement, and the same number, as `max_error_streak`.
    idle_task_stall_wakes: int = 3

    #: Presence liveness thresholds (seconds since last heartbeat).
    online_max_seconds: int = 120
    suspect_max_seconds: int = 600

    #: Minimum interval for recurring hooks; probe default interval.
    min_hook_every: int = 60
    default_probe_every: int = 600
    #: Consecutive probe errors before the hook is auto-disabled + owner notified.
    max_error_streak: int = 3

    #: Session bootstrap / wake prompt construction.
    prompt_builder: PromptBuilder = field(default_factory=PromptBuilder)

    #: Coordination DB.
    db_path: Path = field(default_factory=lambda: DB_PATH)

    def role_names(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.roles)

    def tzinfo(self):
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(self.timezone)
        except Exception:
            return dt.timezone.utc


#: Process-wide default. A host may mutate this at startup, or pass an explicit
#: EngineConfig into the API.
CONFIG = EngineConfig()


def configure(**kwargs) -> EngineConfig:
    """Convenience: mutate the process-wide default config."""
    global CONFIG
    for k, v in kwargs.items():
        if not hasattr(CONFIG, k):
            raise ValueError(f"unknown config field: {k}")
        setattr(CONFIG, k, v)
    return CONFIG


def load_host_config() -> str | None:
    """Import the host's config module named by ``DESKD_CONFIG_MODULE``, if set.

    A deskd process — the CLI, ``deskd serve``, the cron driver — starts with an
    EMPTY config: no roles, no probe allowlist, deny-all. The host supplies those
    by calling :func:`configure`, but that call has to actually RUN inside every
    process that talks to the engine, and a separate ``deskd`` process never
    imports the host's application by itself. Without this hook the host's roles
    are registered nowhere the CLI can see, and every role-scoped command is
    rejected — which is exactly the gap the published Quickstart fell into.

    Set ``DESKD_CONFIG_MODULE=myapp.desk`` and this imports that module; importing
    it is expected to call ``configure()`` (at module top level, or via a
    ``configure_deskd()`` function this then calls if present). Idempotent and
    import-order-independent: engine modules read CONFIG at call time, so this
    only has to run before the first engine CALL, which every entry point below
    arranges by invoking it first thing.

    Returns the module name loaded, or None if the var is unset. Raises if the
    var names a module that cannot be imported — a misconfigured host should fail
    loudly at startup, not silently run with no roles.
    """
    import importlib

    name = env("CONFIG_MODULE")
    if not name:
        return None
    module = importlib.import_module(name)
    hook = getattr(module, "configure_deskd", None)
    if callable(hook):
        hook()
    return name
