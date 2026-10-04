# Gateway foundation

This page describes the storage and authorization primitives. The next
[local workflow](local-workflow.md) adds a runnable mock demonstration and
memo-only transport without changing their transaction contract.

This is an experimental library stage of deskd's overlay work. It adds no
installed service, network listener, model call, broker adapter, or trading
command. The existing 0.4.x CLI and coordination database keep their behavior.
No release version or dependency requirement changes in this stage.

## Identity and lifecycle

A seat has a stable principal; a root thread is a revocable binding to that
principal. Creating another thread or naming a helper after a different seat
does not create another principal. The registry checks a server-owned action
policy, a current binding, and a short-lived channel lease on every call.
Critical mutations require the registered root, not one of its descendants.

Only the harness-added top-level MCP metadata supplies session and thread
identifiers. Identity-like fields inside tool arguments have no authority.
Peer identity and channel-generation evidence must come from the trusted
transport, never from JSON supplied by a caller.

The administrative Python methods are trusted internal interfaces. Their
names do not authenticate a human or a service manager. A future controller
must have a separate, protected administration channel and verify effective
configuration before activation. The business interface must not expose
binding, activation, or lease creation. Restart and fencing invalidate old
channels; restoring an old database must not restore executable authority.

Initialize `Registry` on a new, explicit database path before opening
`GatewayEventStore` on the same path. `GatewayCommands` rejects a separate
producer database: authorization, domain changes and the outbox must share one
write transaction so a concurrent revocation has a defined order. Checking
identity first and then writing on another connection is insufficient. Existing
coordination databases must not be passed to the new registry initializer.

This stage does not implement grants, precise order review, policy checks,
order submission, reconciliation, or a live-trading switch.

## Durable events

Producer state changes, immutable events, and command receipts commit in one
SQLite transaction. The same principal and request ID with the same content
returns the original receipt. Reusing that ID for different content is an
error. The outbox is durable and is not the existing, pruned UI event stream.
Every replay rechecks current authority before returning the original receipt.
Root, binding and service provenance belongs to the original event, not its
semantic request fingerprint: changing roots does not make the same stable
principal's already applied command a new action.

A separate consumer database commits its projection and its
`(consumer_id, event_id)` receipt together. A caller may acknowledge delivery
only after that commit. Losing the acknowledgement can cause redelivery but
must not apply the projection again. Different consumers have independent
receipts. Reusing an event ID with different content is rejected.

These are two local transactions, not a transaction across both databases.
Projection failure cannot undo the producer's committed fact. Projection
callbacks are fixed, trusted SQL code using the supplied connection; they
must not perform external effects, open another write connection, or manage
their own transaction. They are not an agent extension interface. A callback
must never dispatch an order or treat a projected event as authorization.

## Acceptance boundary

Tests use fresh SQLite databases and synthetic principals, metadata, and
channel evidence. They check local authorization and persistence invariants.
Passing them does not establish that a real model cannot reach a private
socket or impersonate a trusted transport.

Before deployment, separately verify the fixed official harness, two service
users, immutable configuration and its parent directories, sandbox tool paths,
socket aliases, environment and process isolation, protected administrative
controller, and recovery with stale connections. A missing prerequisite must
leave mutation access fenced. Do not fall back to a less restrictive sandbox.
