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
import re
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_NAME = "deskd"
ENV_PREFIX = "DESKD_"
__version__ = "0.4.1"


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
    surfaces but never interprets — the host decides what it means.

    Two declarations the engine SURFACES without ever acting on them itself:

    - `capabilities`: what the role may do. Capability-addressed ingress
      (`orchestration.inbox_route`) targets these, and "no enabled role
      declares it" is an unroutable demand — recorded, shown red on the board,
      re-routed automatically once a qualifying role exists.
    - `authority["allowed_tools"]` (list of harness tool names): the tool
      grant a session woken for this role should run under. Wake and rollover
      plans carry it; the reference driver maps it to `--allowedTools`, falling
      back to its global default when absent. The engine declares, the harness
      enforces — and note that a grant containing a shell (`Bash`) makes every
      other restriction in the list advisory.
    """
    name: str
    display_name: str = ""
    capabilities: tuple[str, ...] = ()
    authority: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ConsoleLink:
    """One host-owned link appended to the shared Web console navigation.

    ``page`` matches the identifier passed to ``Shell.init`` so a host page can
    highlight itself. Links are deliberately root-relative: the console may
    surface a host view, but host configuration must not become a script or
    off-site navigation injection point.
    """

    page: str
    href: str
    label: str

    def __post_init__(self) -> None:
        page = self.page.replace("-", "").replace("_", "")
        if not page or not page.isalnum():
            raise ValueError("console link page must be an alphanumeric slug")
        if (
            not self.href.startswith("/")
            or self.href.startswith("//")
            or "\\" in self.href
            or any(ord(char) < 32 or ord(char) == 127 for char in self.href)
        ):
            raise ValueError("console link href must be a root-relative path")
        if not self.label.strip():
            raise ValueError("console link label is required")


_REPOSITORY_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_BRANCH_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*/$")


@dataclass(frozen=True)
class RepositorySpec:
    """One repository the deterministic workspace broker may touch.

    Agent requests name ``name``; they never supply a filesystem path.  Bases
    are exact allowlisted ref names, and every created branch must start with
    ``branch_prefix``.  ``allowed_roles`` being empty means every registered
    agent role may lease this repository; production hosts should normally
    narrow it (for example to their engineer role).

    Both paths must be absolute.  The repository contains the broker-owned Git
    metadata; ``worktree_root`` contains lease directories that may be mounted
    into agent containers.  The agent container must not receive the repository
    path or any writable Git metadata mount.
    """

    name: str
    path: Path
    worktree_root: Path
    allowed_bases: tuple[str, ...] = ("origin/main",)
    branch_prefix: str = "codex/"
    allowed_roles: tuple[str, ...] = ()
    lease_seconds: int = 86_400
    # Broker-side defence in depth.  Production should also put a filesystem
    # quota on ``worktree_root``; these limits make a single lease fail closed
    # before Git is asked to enumerate or stage an unbounded tree.
    max_files: int = 20_000
    max_file_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    max_git_output_bytes: int = 2 * 1024 * 1024
    # Kept last so the original positional RepositorySpec contract remains
    # stable (the fourth positional argument is still ``allowed_bases``).
    container_worktree_root: Path = Path("/workspaces")

    def __post_init__(self) -> None:
        if not _REPOSITORY_NAME_RE.fullmatch(self.name):
            raise ValueError(
                "repository name must match [a-z][a-z0-9_-]{0,62}")
        if (not self.path.is_absolute() or not self.worktree_root.is_absolute()
                or not self.container_worktree_root.is_absolute()):
            raise ValueError(
                "repository, broker worktree and container worktree paths must be absolute")
        if not self.allowed_bases or any(
                not ref or ref.startswith("-") or any(c.isspace() for c in ref)
                for ref in self.allowed_bases):
            raise ValueError("repository allowed_bases must be non-empty safe refs")
        if (not _BRANCH_PREFIX_RE.fullmatch(self.branch_prefix)
                or ".." in self.branch_prefix or "//" in self.branch_prefix):
            raise ValueError("repository branch_prefix must be a safe ref prefix")
        if self.lease_seconds < 60:
            raise ValueError("repository lease_seconds must be at least 60")
        if (self.max_files < 1 or self.max_file_bytes < 1
                or self.max_total_bytes < self.max_file_bytes
                or self.max_git_output_bytes < 4096):
            raise ValueError("repository workspace/output quotas are invalid")


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

    #: How long a demand may sit on a TERMINAL rung (sla_seconds=None) before
    #: the ladder recycles it back to the machine rungs and climbs again.
    #: The terminal rung is a badge, not a parking brake: on 2026-07-23 a DNS
    #: outage killed every spawn AND the Discord channel, two demands
    #: terminal'd at supervisor_badge, and when the network returned nothing
    #: retried — the inbox demand key aggregates per role, so every later
    #: notification rode the parked attempt into permanent silence. The badge
    #: stays red in the escalation ledger; the MACHINE must keep trying.
    terminal_retry_seconds: int = 1800

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

    #: Host-owned pages appended to the shared console nav. Kept last so adding
    #: this optional seam does not shift any existing positional arguments.
    #: EMPTY keeps the standalone deskd console domain-neutral.
    console_links: tuple[ConsoleLink, ...] = ()

    #: Host-registered agent providers (see deskd.providers). Merged over the
    #: built-ins by NAME, host wins — replacing the default claude provider
    #: (different binary path, different thinking budgets) is a registration,
    #: not a fork. Appended after console_links for the same
    #: positional-stability reason as it.
    providers: tuple = ()
    #: Provider a role launches with when its registry row pins none.
    default_provider: str = "claude"

    #: Repositories exposed through the non-LLM workspace broker.  Empty is a
    #: fail-closed default: no agent can cause deskd to run Git anywhere.
    repositories: tuple[RepositorySpec, ...] = ()

    #: Host command adapters installed into the authenticated control API.
    #: Entries are ``deskd.control.HostCommand`` objects, kept opaque here to
    #: preserve the config -> control import direction.
    command_handlers: tuple = ()

    #: Explicit role-to-role task delegation edges.  Self-assignment is always
    #: allowed; cross-role assignment is denied unless ``(sender, target)`` is
    #: listed here.  The message transport has no equivalent policy because it
    #: carries no execution authority: every configured role may address every
    #: other configured role, with the sender always credential-derived.
    task_delegations: tuple[tuple[str, str], ...] = ()

    #: A duplicate external request only takes over a recoverable operation
    #: after this durable execution lease has gone stale.  Live duplicates
    #: return the existing running job and never invoke the callback twice.
    external_command_lease_seconds: int = 900

    #: Automatic drain attempts for one stale session before the rollover is
    #: held as a visible operator escalation.  A failed worker must not leave
    #: an invisible, permanently draining session or retry without a bound.
    rollover_max_attempts: int = 3

    #: Hard transport limit for JSON accepted by authenticated mutation
    #: endpoints.  The ASGI adapter enforces this before Pydantic parsing and
    #: also counts streamed/chunked bytes, so Content-Length is only an early
    #: rejection hint and never the security boundary.
    control_max_request_body_bytes: int = field(default_factory=lambda:
        int(env("CONTROL_MAX_REQUEST_BODY_BYTES") or 1024 * 1024))

    #: One-time launcher mount authorization.  A caller may request a shorter
    #: lifetime, never a longer one; tickets are consumed before a container is
    #: started and cannot be reused for another workspace version.
    launcher_ticket_ttl_seconds: int = 60
    launcher_ticket_max_seconds: int = 300

    #: Production control containers set this true.  Legacy HTML and open
    #: projection routes then return 404, leaving only bearer-authenticated
    #: control endpoints plus /healthz.  A separate trusted console process may
    #: expose the legacy UI; agent containers must never share that socket.
    control_api_only: bool = field(default_factory=lambda:
        str(env("CONTROL_API_ONLY") or "").lower() in {"1", "true", "yes", "on"})

    #: Version provenance copied into agent self-projections and workspace
    #: leases.  Hosts should pin immutable build identifiers in production.
    config_version: str = field(
        default_factory=lambda: env("CONFIG_VERSION") or "unversioned")
    prompt_version: str = field(
        default_factory=lambda: env("PROMPT_VERSION") or "unversioned")
    agent_image: str = field(
        default_factory=lambda: env("AGENT_IMAGE") or "unversioned")
    #: Immutable deployment provenance. ``agent_image`` remains a human label;
    #: these two are the content-addressed image/source pins workers must echo.
    image_digest: str = field(
        default_factory=lambda: env("IMAGE_DIGEST") or "unversioned")
    build_revision: str = field(
        default_factory=lambda: env("BUILD_REVISION") or "unversioned")

    #: Private host-only root for artifacts uploaded through the authenticated
    #: review control API.  ``None`` is deliberately fail-closed: a production
    #: host must choose and mount the path; agent containers never name it.
    review_artifact_root: Path | None = field(default_factory=lambda: (
        Path(value) if (value := env("REVIEW_ARTIFACT_ROOT")) else None))
    review_artifact_max_bytes: int = field(default_factory=lambda:
        int(env("REVIEW_ARTIFACT_MAX_BYTES") or 1024 * 1024))

    def role_names(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.roles)

    def tzinfo(self) -> dt.tzinfo:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(self.timezone)
        except Exception:
            return dt.timezone.utc


#: Process-wide default. A host may mutate this at startup, or pass an explicit
#: EngineConfig into the API.
CONFIG = EngineConfig()


def configure(**kwargs: object) -> EngineConfig:
    """Convenience: mutate the process-wide default config."""
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
