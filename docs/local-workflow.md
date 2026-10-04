# Local multi-principal workflow

deskd is building a persistent workspace in which separate principals can
propose work, authorize it independently, execute it with limited authority,
and inspect a shared record. This increment provides a runnable local loop
using a shared memo as the example action. It does not complete the persistent
multi-identity system or establish feature parity with another product.

The memo has one effect: an exact text body is published in a local SQLite
database. No model, credential, broker, external account, or network service is
needed for the offline demonstration.

## Run the offline demonstration

From an environment where this checkout is importable:

```sh
python -m deskd.gateway demo --output /absolute/path/to/a/fresh-directory
```

For an uninstalled source checkout, prefix the command with `PYTHONPATH=src`.
Choose a new output directory dedicated to this demonstration. Do not point it
at an existing desk, installation directory, or account database.

The demonstration provisions three synthetic seats:

| Seat | Responsibility |
| --- | --- |
| Operator | Propose the exact memo and execute an independently approved proposal. |
| Reviewer | Issue and revoke approvals. |
| Engineer | Observe results without approval or execution capability. |

The synthetic operator also receives approval capability for a negative test:
even that capability must not let it approve itself as the executor.

The path is **proposal → independent approval → designated execution → board**.
The output directory contains `board.html`, `report.json`, and the demonstration's
SQLite state. Open `board.html` to inspect the result, and use `report.json` for
the machine-readable acceptance outcome. These files are local artifacts; the
command does not publish a site.

An accepted run publishes the approved text once and records the three stable
identities involved. Rejections are part of acceptance: self-approval,
unauthorized or child-thread execution, changed content, and attempts to reuse
revoked authority must not create another published memo. Restart and replay
checks must preserve committed state while rejecting stale channels.

The demo uses synthetic transport evidence in one local development environment.
Its success proves the exercised application rules, not separation between OS
users, a model sandbox, official-runtime compatibility, or live trading safety.

## Identity and action contract

A principal is a stable `desk/seat` identity. A root thread is its replaceable,
revocable binding. Changing a thread ID or display label cannot manufacture an
independent approver. Capabilities come from the trusted registry configuration;
the example seat names do not confer authority.

| Action | Business arguments | Rule |
| --- | --- | --- |
| `proposal.create` | `executor_principal`, `body` | The designated executor must be a bound principal on the same desk. The exact UTF-8 body is limited to 65,536 bytes. |
| `approval.issue` | `proposal_id`, `body_sha256`, `ttl_seconds` | A capable root approves the stored digest. Its stable principal must differ from the executor. The executor must still be bound. |
| `action.execute` | `approval_id` | Only the designated capable root can consume a current, unrevoked approval and publish its original body. |
| `approval.revoke` | `approval_id` | The issuing principal's capable root can revoke an unconsumed approval. |

Unknown fields, including attempted author/issuer overrides and execution-time
body edits, are rejected. Execution takes no replacement body. A second approval
does not permit the same proposal to be published twice. A child may propose if
its principal has that capability; issuing, revoking and executing require the
registered root.

`approval.revoke_any` is a separately configured control capability. It is not
in the default MCP tool catalog and is never granted merely because a caller
uses an engineering or administrative-sounding label.

An approval remains attached to its stable executor across an authorized root
replacement. It has its own expiry and monotonic revocation state. Revoking an
issuer's current login does not retrospectively revoke approvals already issued;
use the explicit approval revocation operation for that transition. This
increment trusts the server UTC clock for expiry; clock and restore policy need
separate deployment validation.

## Persistence and retry behavior

Registry authorization, approval consumption, memo publication, the event
outbox, and the command receipt use one database write transaction. Concurrent
revocation and execution have a defined order: a revocation committed first
prevents execution, while execution committed first leaves a consumed approval.
An error before commit leaves no partial memo or consumed approval.

Each business call carries a stable `request_id`. The same principal, request
ID, and action arguments return the original committed receipt. Changed
arguments conflict. A different request ID cannot consume the same approval
again. Every retry must pass current authorization, including retries that would
only return a stored receipt. Original root and service provenance stays on the
original event when an authorized replacement root retries it.

If a connection fails after submission, the caller may not know whether the
transaction committed. The bridge stops instead of automatically resubmitting.
After restoring authorized access, use the original request ID and exact
arguments to resolve the outcome; a new ID is a different command.

Independent consumers can project durable events with their own receipts.
Projection and consumer receipt commit together; lost acknowledgements allow
redelivery without reapplying the projection. Rebuilding a view is not permission
to replay a business action. See [Gateway foundation](gateway-foundation.md) for
the producer/consumer transaction boundary.

## Developer and installation entry points

The domain module is `deskd.gateway.actions`. Initialize the trusted `Registry`
on a fresh explicit database first, then `GatewayEventStore` and `MemoWorkflow`
on that same database. Install `WORKFLOW_ACTIONS` and the fixed handlers from
`MemoWorkflow.handlers()` in `GatewayCommands`. These callbacks are trusted
server code, not agent-supplied SQL or plugins.

`MemoWorkflow.summary()` is a read-only local operator view. Its derived states
include pending/approved/executed proposals and active/expired/revoked/consumed
or superseded approvals. `summary_for(PrincipalId)` filters draft and approval
visibility to participants and shares published memos within their desk. This
filter does not authenticate callers: an exposed reader must obtain the
principal from its authorized server identity.

The command-line entry points also include:

```sh
python -m deskd.gateway preflight /absolute/path/to/non-secret-manifest.json
python -m deskd.gateway bridge --socket /absolute/path/to/business.sock --gateway-uid 20002
```

The numeric UID is illustrative. The bridge is a fixed stdio MCP adapter for an
already prepared Unix socket; it does not start or configure a gateway. It must
run in the trusted harness domain with the correct distinct gateway UID. It
adds `request_id` to the static tool schemas and preserves trusted top-level
session/thread metadata. Neither metadata alone nor being able to list tools
grants business authority.

The bridge's protocol target is MCP `2025-06-18`: initialization and the ready
notification follow the official [Lifecycle specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle),
and its static `tools/list` and `tools/call` surface uses the official
[Tools specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools).
This bounded adapter does not claim support for every optional MCP capability.

For an administrator-prepared installation, the memo-only service and its
separate control client have these exact entry points:

```sh
python -m deskd.gateway serve-memo --manifest /etc/deskd-local/manifest.json
python -m deskd.gateway control --socket /run/deskd-local/admin/admin.sock \
  --gateway-uid 20002 status --params '{}'
```

These example paths and UID must match the reviewed installation. The service
must run as the declared gateway UID. It checks the protected manifest and
metadata before opening `memo.db` in the gateway state directory, starts fenced,
and uses distinct business and administration sockets. It does not install
accounts, service units, an official runtime, or a controller. The control
client is for the separately authorized administration identity; knowing a
socket path does not grant control access.

`control` accepts one method and a non-secret JSON object in `--params`:

| Method | Required parameter fields |
| --- | --- |
| `status`, `connections`, `activate`, `fence` | Empty object. |
| `bind` | `desk_id`, `seat_id`, `root_session_id`, `manifest_hash`, `capabilities` (string array), `expected_binding_generation`. |
| `revoke` | `desk_id`, `seat_id`, `expected_binding_generation`. |
| `lease` | `desk_id`, `seat_id`, `connection_id`, `root_session_id`, `binding_generation`, `manifest_hash`, `ttl_seconds`. |

There is no automatic retry of an administrative mutation. Bindings are
explicit trusted administrative decisions; this increment does not discover or
authenticate a root through the official runtime on the administrator's behalf.
After reviewing bindings and activating the memo-only service, a connected
trusted bridge receives a channel ID visible through `connections`. A privileged
controller must authorize that live connection's exact binding through `lease`.
The bridge cannot issue its own lease. Restart invalidates old channels; the
demo's new root is an explicit trusted fixture rebind, not automatic runtime
recovery.

The `serve-memo` activation check repeats the manifest metadata checks. **Passing
this gate only permits the memo-only application path; it does not verify the
runtime sandbox or authorize credentials or external effects.** The service
continues to report `ready_for_credentials: false`. Follow
[Installation prerequisites](local-workflow-install.md)
for the manifest, protected paths, ownership requirements and release evidence.
Preflight checks declarations and filesystem metadata; it performs no repair
and its report does not authorize credentials or an external-effect runtime. The example
manifest deliberately contains placeholders that fail validation.

## Acceptance and remaining work

The focused automated tests use temporary databases and synthetic identities:

```sh
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -o addopts='' \
  tests/test_gateway_actions.py tests/test_gateway_commands.py \
  tests/test_gateway_events.py tests/test_gateway_demo.py
```

Action tests cover exact content, stable-principal independence, root and
capability restrictions, authorized retries, expiry, revocation, concurrent
execution, and rollback after a receipt-write failure. Keep the test databases
separate from any installed service state.

CLI tests run fresh interpreters with a synthetic host-configuration tripwire
and socket-creation denial. They verify the demo artifacts, rejection of existing
output directories, HTML escaping, and failure of a negative installation
manifest before a service database is created. They do not start an installed
gateway or claim to validate its two-UID boundary.

| Area | Available in this increment | Remaining acceptance |
| --- | --- | --- |
| Multi-principal action loop | Bound principals, independent approval, one-shot SQLite memo effect, durable receipts and local board. | Complete integration into a persistent user workspace. |
| Official runtime roots | Explicit binding and lease interfaces; fixed bridge. | Start and retain official-runtime roots per seat, verify effective configuration, and recover those roots safely. |
| Event-driven activity | Durable producer/consumer event primitives. | Persistent delivery, targeted wakeups, scheduling and recovery through the official runtime. |
| Native interaction | Offline HTML board and machine-readable report. | Role attach/switch, native TUI workflow and ongoing shared-board interaction. |
| Service isolation | Peer/channel checks and an installation preflight contract. | Install and test two real service UIDs, protected administration, sandbox denial, networking and process boundaries. |
| Paper trading | No order or brokerage action. | Separate order rules, paper ledger/adapter, review and reconciliation acceptance before enabling trading. |

An offline pass is useful end-to-end evidence for this local memo workflow.
Each remaining integration and isolation claim needs its own executable evidence
before the system is described as an installed, continuously running desk.
