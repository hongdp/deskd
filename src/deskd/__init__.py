"""deskd — a domain-agnostic orchestration engine for multi-agent desks.

deskd owns the part of a multi-agent system that is hard and has nothing to do
with your domain: which agents are alive, what is queued for them, and — the
difficult bit — reliably waking the right agent at the right time and proving
the message actually landed. Your agents do the domain work; deskd does the
coordination. It never acts *as* an agent.

Start here::

    from deskd import RoleSpec, configure

    configure(
        roles=(RoleSpec("researcher", "Researcher"),
               RoleSpec("operator", "Operator")),
        timezone="America/New_York",
        probe_allowlist=("myapp.watchers",),   # empty = no probes may run
    )

    from deskd import orchestration
    orchestration.inbox_enqueue("operator", "alert", "threshold crossed",
                                priority="urgent")

`configure()` mutates the process-wide `CONFIG` in place, and engine modules
read it at call time — so a host must configure before it calls the engine, and
importing a module does not freeze the configuration.

Layering, which imports must respect: config -> auth -> mailbox -> meetings ->
orchestration -> (cli, web). Nothing lower may import anything higher.

The submodules are the API surface; this package re-exports only the entry
points a host actually needs:

* `deskd.orchestration` — presence, tasks, the unified inbox, wake orchestration,
  wake hooks, the delivery ledger, session lifecycle, board/agent aggregates.
  `inbox_enqueue()` is THE public ingress: hosts inject their own domain events
  through it, and the engine never reaches into the host to collect them.
* `deskd.meetings` — bounded multi-agent meetings and the supervisor adapter.
* `deskd.mailbox` — the durable thread/message transport and review workflow.
* `deskd.channels` — the pluggable human-notification egress a host registers
  at startup (`channels.register_channel`). The engine ships none; without one
  the escalation path terminates in the durable outbox.
* `deskd.auth` — the supervisor trust boundary (Ed25519 verification, the nonce
  ledger). Read this one before changing anything security-relevant.
* `deskd.web` — the optional console (`pip install deskd[web]`).
"""

from __future__ import annotations

from types import ModuleType

from .config import (
    CONFIG,
    DEFAULT_WAKE_LADDER,
    ENV_PREFIX,
    PROJECT_NAME,
    ConsoleLink,
    EngineConfig,
    PromptBuilder,
    RepositorySpec,
    RoleSpec,
    WakeRung,
    __version__,
    configure,
    env,
)

__all__ = [
    # configuration — what a host touches first
    "CONFIG",
    "EngineConfig",
    "configure",
    "ConsoleLink",
    "RoleSpec",
    "RepositorySpec",
    "PromptBuilder",
    "WakeRung",
    "DEFAULT_WAKE_LADDER",
    "PROJECT_NAME",
    "ENV_PREFIX",
    "env",
    "__version__",
    # engine modules (imported lazily; see __getattr__)
    "auth",
    "channels",
    "mailbox",
    "meetings",
    "orchestration",
    "control",
    "workspaces",
]


def __getattr__(name: str) -> ModuleType:
    """Expose the engine submodules as attributes, imported on first use.

    Lazy on purpose. Importing `deskd` must stay cheap and side-effect-free: the
    engine modules open no database at import time, but they do pull in
    `cryptography` and build their schema constants, and a host that only wants
    `configure()` and `RoleSpec` should not pay for that. It also keeps
    `import deskd` working in an environment where an optional dependency of a
    submodule is missing.

    `channels` is listed for a different reason: it is where a host registers
    its pager at startup, and it must be reachable the same way as everything
    else a host touches. `from deskd import channels` already worked (the
    import system resolves a submodule by name), but `import deskd` followed
    by `deskd.channels.register_channel(...)` did not — which is precisely the
    spelling the meetings deprecation warning points people at.
    """
    if name in ("auth", "channels", "mailbox", "meetings", "orchestration",
                "control", "workspaces"):
        import importlib

        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
