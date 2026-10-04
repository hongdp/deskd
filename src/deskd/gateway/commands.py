"""In-process command dispatch with authorization in the committing transaction.

This is an internal composition seam, not an MCP server. A protected transport
must supply authentic metadata and TransportEvidence before using it. Handlers
are fixed, trusted SQL implementations, never callbacks chosen by model input.
No handlers, network service, or external effects are installed by this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Any, Callable, Mapping

from .events import GatewayEventStore, PublishReceipt, canonical_json
from .identity import CallIdentity, IdentityError, TransportEvidence, identifier
from .registry import Registry


@dataclass(frozen=True)
class CommandHandler:
    event_type: str
    apply: Callable[[sqlite3.Connection, dict[str, Any], CallIdentity], Any]

    def __post_init__(self) -> None:
        identifier(self.event_type, "event_type")
        if not callable(self.apply):
            raise TypeError("command handler must be callable")


class GatewayCommands:
    """Bind local SQL commands to one explicit registry/outbox database.

    Request fingerprints bind semantic arguments. Event provenance records the
    authority that first applied the command. A later authorized retry returns
    that original receipt, even after the same principal changes its root.
    Every retry rechecks present authority before returning private receipts.
    """

    def __init__(self, registry: Registry, events: GatewayEventStore, *,
                 handlers: Mapping[str, CommandHandler]):
        if registry.db_path != events.db_path:
            raise ValueError("registry and producer outbox must share one database")
        self._registry = registry
        self._events = events
        self._handlers = dict(handlers)
        for name, handler in self._handlers.items():
            identifier(name, "action")
            if not isinstance(handler, CommandHandler):
                raise TypeError("invalid command handler")

    def execute(self, request_id: str, params: dict[str, Any],
                transport: TransportEvidence) -> PublishReceipt:
        """Apply one authenticated MCP-shaped request using a fixed handler.

        ``request_id`` is scoped to the authenticated stable principal. It is
        not an authorization secret. There is no raw SQL or arbitrary upstream
        tool forwarding surface. Domain handlers must validate their own schema.
        """
        snapshot = json.loads(canonical_json(params))
        if (type(snapshot) is not dict or set(snapshot) != {"name", "arguments", "_meta"}
                or type(snapshot["name"]) is not str
                or type(snapshot["arguments"]) is not dict):
            raise IdentityError("invalid_command_params")
        action = snapshot["name"]
        handler = self._handlers.get(action)
        if handler is None:
            raise IdentityError("unknown_command")

        # This first check supplies provenance only. It cannot authorize a
        # mutation; the second check below must succeed under the write lock.
        identity = self._registry.authorize(action, snapshot, transport)

        def validate(conn: sqlite3.Connection) -> None:
            current = self._registry.authorize(
                action, snapshot, transport, connection=conn)
            if current != identity:
                raise IdentityError("authority_changed")

        def effect(conn: sqlite3.Connection) -> Any:
            return handler.apply(conn, snapshot["arguments"], identity)

        return self._events.publish(
            identity.principal.value, request_id, handler.event_type,
            {"action": action, "arguments": snapshot["arguments"]},
            validate=validate, effect=effect,
            provenance={
                "root_session_id": identity.root_session_id,
                "thread_id": identity.thread_id,
                "binding_generation": identity.binding_generation,
                "service_generation": identity.service_generation,
                "manifest_hash": identity.manifest_hash,
            },
        )
