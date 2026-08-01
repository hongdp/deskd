"""Bounded multi-agent meetings layered on the durable mailbox.

A meeting is a *bounded* conversation: it has an agenda, an invited attendee
list, a message budget, an idle deadline, and a state machine that always
terminates. The point of the bounds is that autonomous agents cannot turn a
conversation into an unproductive loop — every path leads to `closed`, either
through the termination handshake, an escalation, or the mailbox's own budget.

State machine
-------------
    waiting              invited, quorum not yet met
    active               quorum met, normal discussion
    consensus            near the message budget; only positions/decisions
    termination_pending  someone proposed an end; awaiting confirmations
    paused / escalated   parked for a human
    closed               terminal

Roles are NOT hardcoded. The `agent_registry` table (owned by the
orchestration layer) is the source of truth for which agent roles exist; this
package reads it via `_known_roles()` and binds every role literal in SQL as a
placeholder. There is deliberately no agent-facing path to act as the
supervisor (`CONFIG.supervisor_role`): supervisor actions enter only through
the authenticated web adapter in `deskd.auth`, either as a short-lived Ed25519
assertion (trusted-device mode) or a simple access-code gate (trusted local
mode). Every supervisor mutation carries a one-shot nonce that is burned before
the action runs, and a supervisor *message* is only visible once it has a
matching auth row — an unauthenticated row written straight into the mailbox
can never speak as the supervisor.

Layering: mailbox -> meetings -> orchestration. Never import orchestration
anywhere in this package; the orchestrator PULLS meeting demand (see
sweep.py). Submodule imports stay relative to the package so a violation is
greppable.
"""

# The public surface is unchanged by the package split: every meetings name
# that lived on the old single-module `deskd.meetings` — including the
# underscored helpers the test suites pin — is re-exported here. Import from
# the facade; the submodule layout (store -> obligations/escalations -> sweep
# -> lifecycle/messaging/termination -> views -> supervisor) is an internal
# layering, not API. The one upward call — the protocol wrappers handing back
# views.meeting_status — is imported at call time so the module-level import
# graph stays layered. The meetings clock is `meetings.store._now`; submodules
# call it through the module attribute, so patch that single point (patching
# the facade binding does not reach internal callers).
#
# The one shrinking part of that surface is the channel registry: see the
# PEP 562 `__getattr__` at the bottom of this module.

import warnings

from .. import auth, mailbox  # noqa: F401  (the old module exposed them)
from ..config import CONFIG, PROJECT_NAME  # noqa: F401  (ditto)
from .store import (  # noqa: F401
    BROADCAST, DEFAULT_CONSENSUS_THRESHOLD, DEFAULT_IDLE_MINUTES,
    DEFAULT_MAX_MESSAGES, DEFAULT_REVIEW_MAX_MESSAGES,
    DEFAULT_WAIT_TIMEOUT_SECONDS, MAX_WAIT_SECONDS, MEETING_SCHEMA,
    MEETING_STATES, MEETING_TYPES, MIN_CONSENSUS_THRESHOLD,
    MIN_WAIT_TIMEOUT_SECONDS, UPDATE_KINDS, _active_roles, _agent_role,
    _attendee, _clean, _event, _has_supervisor, _in_clause, _iso,
    _known_roles, _meeting, _meeting_projection, _meeting_roles, _migrate,
    _mode, _now, _parse_time, _rearm_agent_wakes, _stamp_notifications,
    _supervisor_claim,
    _thread_last_activity, _visible_message_sql, connect,
)
from .obligations import (  # noqa: F401
    _discharge_obligations, _resolve_obligations, _waive_pending_obligations,
)
from .escalations import (  # noqa: F401
    _queue_escalation, dispatch_escalation, list_escalations,
)
from .sweep import (  # noqa: F401
    _sweep_timeouts, acknowledge_wake, sweep_timeouts, wake_requests,
)
from .lifecycle import (  # noqa: F401
    _call_meeting, _check_in, _leave, call_meeting, check_in, discover,
    leave_meeting,
)
from .messaging import (  # noqa: F401
    _meeting_updates, _revive_idle_thread, _send_update, meeting_updates,
    resolve_obligations, send_update, submit_position, wait_for_updates,
)
from .termination import (  # noqa: F401
    _close_meeting, _finalize_if_unanimous, _is_supervisor_one_to_one,
    _missing_confirmations, _pending_termination, _propose_end, _vote_end,
    _waiting_on_after_confirm, confirm_end, escalate_meeting, pause_meeting,
    propose_end, reject_end,
)
from .views import (  # noqa: F401
    _local_day_bounds, _valid_day, list_meetings, meeting_days,
    meeting_status, meeting_transcript,
)
from .supervisor import (  # noqa: F401
    REQUIRED_SIGNED_FIELDS, SUPERVISOR_ACTIONS, _apply_supervisor_payload,
    _supervisor_join, apply_simple_supervisor_action,
    apply_supervisor_assertion, apply_supervisor_assertion_bytes,
)

# --- deprecated: the channel registry was never a meetings concern -----------

# Channel machinery lives in `deskd.channels` — engine infrastructure, not
# part of the meeting protocol: the wake ladder's terminal rung dispatches
# through the same registry without a meeting anywhere in sight. These six
# names sat here only because hosts historically registered their pager or
# chat webhook through this module, and an import that says `meetings` teaches
# the wrong mental model for a process-wide egress registry. They still
# resolve — same objects, one registry, so a channel registered through either
# spelling is visible to both — but the old spelling now warns.
_DEPRECATED_CHANNEL_NAMES = frozenset({
    "OUTBOX_CHANNEL", "CallableChannel", "EscalationChannel",
    "register_channel", "registered_channels", "unregister_channel",
})

#: Star-import reads `__dict__` directly and never consults `__getattr__`, so
#: without an explicit `__all__` the six names would silently disappear from
#: `from deskd.meetings import *` — a NameError at the call site much later,
#: which is the failure mode this shim exists to prevent. Naming them here
#: routes star-import through `__getattr__`, so it still warns.
#: Removal horizon: the shim goes away when meetings leaves the core (P4);
#: until then every release keeps it.
__all__ = sorted(set(globals()) | _DEPRECATED_CHANNEL_NAMES)


def __getattr__(name: str):
    """Resolve the moved channel names from `deskd.channels`, deprecated.

    PEP 562: a module-level `__getattr__` is what a plain attribute access
    AND a `from deskd.meetings import CallableChannel` both fall through to
    once the static re-export is gone, so removing the re-export does not
    break a host that has not migrated yet. (Do not read the warning count as
    a call count: importing a *name* from a *package* probes the attribute
    once through `hasattr` — importlib deciding whether the name is a
    submodule — before the IMPORT_FROM opcode reads it again, so one
    `from`-import raises this warning twice.)
    """
    if name in _DEPRECATED_CHANNEL_NAMES:
        warnings.warn(
            f"deskd.meetings.{name} is deprecated: escalation channels are "
            f"engine infrastructure, not a meetings concern. Use "
            f"`from deskd import channels` and `deskd.channels.{name}`.",
            DeprecationWarning, stacklevel=2,
        )
        from .. import channels
        return getattr(channels, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """PEP 562's other half. `__getattr__` alone leaves the deprecated names
    out of `dir()`, which silently breaks REPL completion and any capability
    probe written as `"register_channel" in dir(meetings)` — that probe would
    take the not-supported branch instead of getting the deprecation."""
    return sorted(set(globals()) | _DEPRECATED_CHANNEL_NAMES)
