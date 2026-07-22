"""Agent orchestration: presence, tasks, a unified inbox, and wake orchestration.

Layered on the durable mailbox / meeting tables (same SQLite WAL file). This
module tracks *agent* state — who is online, what they are doing, and their
cross-session work items — as distinct from meeting attendance, which is
per-meeting. It is domain-agnostic: it knows nothing about what the agents
actually do. A host application supplies the roles (``CONFIG.roles``), the
notification sources (``CONFIG.inbox_sources``), the probe allowlist, and the
prompt that boots a woken session (``CONFIG.prompt_builder``).

Design invariants worth knowing before you change anything here:

- ``agent_sessions`` is keyed by ROLE, not session id. The system enforces at
  most one live session per role (``config.role_lock_path`` + flock), and the
  supervisor is never an agent role, so a role-keyed row is faithful and lets an
  in-session hook write a heartbeat without solving "which role am I?".
- Task ``priority`` (urgent/normal/low) is the only axis that drives waking.
  ``due_at`` is a *soft* deadline — pure visibility/ordering, never a wake
  trigger. Overdue open tasks sort to the top everywhere.
- The role registry (``agent_registry``) is the single source of truth for which
  roles exist. Nothing in this module hardcodes a role name; every role literal
  that reaches SQL is a bound placeholder built from the registry.
- This layer only ever WAKES agents. It never acts as one, and it never
  executes anything on their behalf. The only code it runs is a host-allowlisted
  probe, which may observe and notify — nothing else.
- ``plan_wakes`` decides, the driver executes. Nothing here spawns or resumes a
  session; the driver holds the per-role lock and does that.
"""

# The public surface is unchanged by the package split: every name that lived
# on the old single-module `deskd.orchestration` — including the underscored
# helpers the test suites pin — is re-exported here. Import from the facade;
# the submodule layout (store -> presence/tasks/delivery/inbox/hooks -> wake
# -> board) is an internal layering, not API.

from ..config import CONFIG  # noqa: F401  (the old module exposed it)
from .board import (  # noqa: F401
    _count_by_status, _delivery_health, _meeting_load, agent_detail, board,
)
from .delivery import (  # noqa: F401
    DELIVERY_STATES, _delivery_state, _wake_keys, delivery_ledger, sync_delivery,
)
from .hooks import (  # noqa: F401
    WAKE_HOOK_KINDS, _cron_field, _eval_wake_hooks, _next_cron_fire,
    _probe_path_ok, _resolve_probe, hook_add, hook_cancel, hooks,
)
from .inbox import (  # noqa: F401
    _INBOX_RANK, _inbox_insert, _inbox_sort_key, _inbox_view,
    _roles_with_capability, _route_role, _route_unroutable, inbox_ack,
    inbox_enqueue, inbox_mark_delivered, inbox_pending, inbox_route,
)
from .presence import (  # noqa: F401
    _is_busy, _presence_list, _presence_row, _role_presence, end_session,
    heartbeat, presence, record_todos, set_status,
)
from .store import (  # noqa: F401
    LIVE_LIVENESS, ORCH_SCHEMA, RESTING_STATES, SESSION_STATES,
    TASK_OPEN_STATUSES, TASK_PRIORITIES, TASK_STATUSES, _RECIPIENT_ALL,
    _agent_role, _clean, _has_enum_check, _iso, _known_roles, _load_json,
    _log_event, _migrate, _normalize_due, _now, _rebuild, _role_params,
    _seed_registry, _session_day, _task_sources, connect, recent_events,
)
from .tasks import (  # noqa: F401
    _PRIO_RANK, _URGENT_TASK_WHERE, _apply_blocked_on, _queued_tasks,
    _task_sort_key, _task_view, sync_meeting_close_tasks, task_add, task_close,
    task_update, tasks,
)
from .wake import (  # noqa: F401
    MACHINE_ONLY_REASONS, WAKE_REASONS, _channel_level, _demand_resolved,
    _dispatch_wake_escalation, _human_level, _idle_task_demand,
    _insert_attempt, _ladder, _planning_txn, _queue_wake_escalation,
    _reason_ceiling, _rollover_prompt, _start_level, _wake_prompt,
    _wake_reasons_text, collect_wake_demand, plan_wakes, rollover_plan,
    wake_attempts_recent, wake_sources,
)
