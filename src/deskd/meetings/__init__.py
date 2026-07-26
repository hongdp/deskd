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

# The public surface is unchanged by the package split: every name that lived
# on the old single-module `deskd.meetings` — including the underscored
# helpers the test suites pin — is re-exported here. Import from the facade;
# the submodule layout (store -> obligations/escalations -> sweep ->
# lifecycle/messaging/termination -> views -> supervisor) is an internal
# layering, not API. The one upward call — the protocol wrappers handing back
# views.meeting_status — is imported at call time so the module-level import
# graph stays layered. The meetings clock is `meetings.store._now`; submodules
# call it through the module attribute, so patch that single point (patching
# the facade binding does not reach internal callers).

from .. import auth, mailbox  # noqa: F401  (the old module exposed them)
from .. import channels as _channels  # noqa: F401
# Channel machinery lives in `deskd.channels` (engine infrastructure, not a
# meetings concern — the wake ladder's terminal rung dispatches through it
# too). Re-exported because hosts historically registered channels via this
# module; both spellings stay valid.
from ..channels import (  # noqa: F401  (re-exports)
    OUTBOX_CHANNEL, CallableChannel, EscalationChannel,
    register_channel, registered_channels, unregister_channel,
)
from ..config import CONFIG, PROJECT_NAME  # noqa: F401  (ditto)
from .store import (  # noqa: F401
    BROADCAST, DEFAULT_CONSENSUS_THRESHOLD, DEFAULT_IDLE_MINUTES,
    DEFAULT_MAX_MESSAGES, DEFAULT_REVIEW_MAX_MESSAGES,
    DEFAULT_WAIT_TIMEOUT_SECONDS, MAX_WAIT_SECONDS, MEETING_SCHEMA,
    MEETING_STATES, MEETING_TYPES, MIN_CONSENSUS_THRESHOLD,
    MIN_WAIT_TIMEOUT_SECONDS, UPDATE_KINDS, _active_roles, _agent_role,
    _attendee, _clean, _event, _has_supervisor, _in_clause, _iso,
    _known_roles, _meeting, _meeting_roles, _migrate, _mode, _now,
    _parse_time, _stamp_notifications, _supervisor_claim,
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
