"""Container control-plane API primitives.

The optional FastAPI adapter lives in :mod:`deskd.web`; this package remains
stdlib-only and is usable by another HTTP shell without adding web dependencies.
"""

from .auth import ControlAuthError, Principal, TokenStore
from .commands import (
    CommandConflict, CommandContext, CommandError, HostCommand, execute, verbs,
)
from .store import (
    CursorExpired, command_jobs, current_cursor, cursor_for, cursor_id,
    ensure_schema, event_bounds, events_after,
)

__all__ = [
    "ControlAuthError", "Principal", "TokenStore", "CommandError",
    "CommandConflict", "CommandContext", "HostCommand", "execute", "verbs",
    "CursorExpired", "command_jobs", "current_cursor", "cursor_for",
    "cursor_id", "ensure_schema", "event_bounds", "events_after",
]
