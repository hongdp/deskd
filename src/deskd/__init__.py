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
* `deskd.auth` — the supervisor trust boundary (Ed25519 verification, the nonce
  ledger). Read this one before changing anything security-relevant.
* `deskd.web` — the optional console (`pip install deskd[web]`).
"""

from __future__ import annotations

from .config import (
    CONFIG,
    DEFAULT_WAKE_LADDER,
    ENV_PREFIX,
    PROJECT_NAME,
    EngineConfig,
    PromptBuilder,
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
    "RoleSpec",
    "PromptBuilder",
    "WakeRung",
    "DEFAULT_WAKE_LADDER",
    "PROJECT_NAME",
    "ENV_PREFIX",
    "env",
    "__version__",
    # engine modules (imported lazily; see __getattr__)
    "auth",
    "mailbox",
    "meetings",
    "orchestration",
]


def __getattr__(name: str):
    """Expose the engine submodules as attributes, imported on first use.

    Lazy on purpose. Importing `deskd` must stay cheap and side-effect-free: the
    engine modules open no database at import time, but they do pull in
    `cryptography` and build their schema constants, and a host that only wants
    `configure()` and `RoleSpec` should not pay for that. It also keeps
    `import deskd` working in an environment where an optional dependency of a
    submodule is missing.
    """
    if name in ("auth", "mailbox", "meetings", "orchestration"):
        import importlib

        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
