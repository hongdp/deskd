# Container control plane

This document defines the authenticated boundary used when deskd agents run in
separate containers. It is a protocol contract, not an SDK convention: a client
must discover its authority from the server and must not infer extra verbs.

## Trust and state boundaries

The control service is the only process that owns the coordination database,
Git metadata, review artifact store, and role/service credentials. Agent
containers receive one role token and make HTTP requests; they do not mount the
database, the seed repository, another role's files, or any supervisor secret.

State is split deliberately:

| State | Owner | Visibility | Consistency rule |
| --- | --- | --- | --- |
| Registry, presence, tasks, inbox, mailbox, meetings, wake attempts | deskd SQLite | Role-scoped projections; service principals by scope | Native command receipt, domain mutation, and event commit in one `BEGIN IMMEDIATE` transaction |
| Command receipt/job | deskd SQLite | Requesting role, or scoped service | Unique `(principal_id, request_id)` plus a request fingerprint |
| Role session memory/provider conversation | The role's agent container/provider | That role only | A provider-minted session id is compare-and-swap bound to its provisional id |
| Source worktree | Workspace broker; mounted into one role container | Lease owner only | One live lease per `(repository, role)`; head and workspace-version CAS |
| Git common dir, refs, index, locks | Workspace broker only | Never mounted into an agent | Fixed argv, repository lock, no shell/push/merge/fetch/reset/checkout |
| Review artifact bytes | Host-private artifact root | Never exposed by filesystem path | Caller-verified SHA-256, immutable content-addressed 0600 file |
| Supervisor authority | External signed assertion adapter | Never inherited by role/service tokens | Role and service token loaders reject supervisor identity/scope |

Containers communicate through the control API only. There is no peer socket
and no shared writable directory between roles. Durable mailbox messages,
meeting rows, wake claims, and the event cursor are the communication channels.

## Authentication

Every endpoint except `GET /healthz` requires `Authorization: Bearer <token>`.
Role tokens are loaded from `DESKD_ROLE_TOKENS_DIR`; each filename is either the
role or `<role>.token`. Service principals are declared in the JSON manifest at
`DESKD_SERVICE_TOKENS_FILE` with version 1 and one or more of these scopes:

- `read`
- `directive`
- `orchestrator`
- `scheduler`
- `operator`

Raw tokens are hashed in memory and are never written to SQLite. Secret and
manifest files are opened with `O_NOFOLLOW`, validated with `fstat` as regular
owner-only files, read through that same descriptor, and then closed. Group- or
world-accessible files are rejected. A service token cannot carry a role or a
supervisor scope.

`DESKD_CONTROL_API_ONLY=1` is the production setting. It fails startup if no
role/service token is configured and returns 404 for legacy open projections
and supervisor mutation routes. The only compatibility reads left on that
socket are the authenticated current-role feed and attendee-scoped meeting
detail.

## HTTP and idempotency

The stable endpoints are:

- `GET /api/snapshot`
- `GET /api/self`
- `GET /api/inbox`
- `GET /api/jobs`
- `GET /api/jobs/{request_id}`
- `POST /api/commands`
- `GET /api/events`

A command body is:

```json
{
  "request_id": "stable-client-generated-id",
  "verb": "task.add",
  "params": {"title": "Inspect the failure"}
}
```

The `Idempotency-Key` header must exactly equal `request_id`. Reusing a request
id with different verb/params is a conflict. Native commands commit their
receipt, mutation, and event atomically. A live duplicate of an external job
returns its current job state and does not invoke it twice.

Clients must use `snapshot.meta.allowed_verbs` as the exact command vocabulary
for their principal. `snapshot.meta.role` is the role name (or null for a
service principal). The top-level snapshot shape is:

```text
cursor, generated_at, server_version, board, tasks, inbox, meetings,
hooks, wake, runtime, meta
```

`tasks` is `{tasks, stalled_ids}`, `wake` includes `attempts` and `quarantine`,
and `runtime` is `{roles: [...]}`.

## Event stream

`GET /api/events` is an SSE stream. Resume with either `after=<cursor>` or
`Last-Event-ID`; supplying both with different values is rejected. Cursors are
opaque and bound to one server epoch. A malformed, expired, future, or
wrong-server cursor requires a fresh snapshot.

Events fail closed by role: any event with a non-null `role` different from the
principal is replaced by a cursor-only redaction. Role-less meeting/thread
events are visible only after an explicit attendee/participant lookup. Hidden
rows still advance the global cursor, so a client does not replay-loop or infer
how many private changes another role made. The server sends a heartbeat every
10 seconds when idle; `once=true` provides a finite catch-up response for tests
and simple clients.

## Session and build provenance

`agent.session.start` requires:

```text
session_id, mode, provider, image_digest, build_revision,
config_version, prompt_version
```

`mode` is `spawn` or `resume`. Every immutable pin must equal the host's current
configuration. When `workspace_lease_id` is supplied, provider, model,
agent-version, and all four build pins must also equal the values captured on
that lease. Missing or mismatched values fail before presence is changed.

The provider's real conversation id is persisted with `agent.session.bind`
using `{provisional_session_id, actual_session_id}`. Heartbeat, feed, and stop
must present the current session id and cannot replace identity.

`GET /api/self` returns:

```text
role, runtime, presence, authority, capabilities, session_id,
session_provider, session_provenance, config_version, prompt_version,
agent_image, image_digest, build_revision, build_pins, workspace_leases,
wake_quarantine, wake_reconciliations
```

## Workspace broker

Repositories, bases, branch prefix, allowed roles, quotas, host path, and
container mount path are host configuration. An agent supplies only allowlisted
identifiers. The broker offers:

- `workspace.acquire`
- `workspace.inspect`
- `workspace.renew`
- `workspace.status`
- `workspace.diff`
- `workspace.commit`
- `workspace.release`

Commit requires the inspected head and workspace version. It rejects a
pre-staged index, special files, nested `.git`, quota overflow, unsafe Git
filters/textconv, hooks/signing, and a moved head. Release requires an exact
workspace version and a clean tree whose HEAD equals the broker ledger.

The broker never pushes or merges. A human or separately authorized release
service reviews and publishes the resulting commit.

External recovery is proof-based:

- allocation recovers only the exact registered worktree/branch;
- commit recovers only a direct child of the asserted head with the exact
  message and broker role email;
- release records a durable intent after proving the tree clean, then removes
  only the derived lease path and completes that intent on retry.

If one of those side effects may have happened before the command receipt was
written, the same fingerprint and request id remains reclaimable. Deterministic
validation failures are terminal. Non-recoverable external work with a lost
result is `indeterminate` and is never blindly retried.

## Mail, meetings, and reviews

The credential-derived mailbox verbs are:

```text
mailbox.open, mailbox.inbox, mailbox.send, mailbox.ack,
mailbox.status, mailbox.stop
```

`mailbox.inbox` reads actual mailbox messages and optionally marks receipts; it
is not the unified orchestration inbox. `mailbox.send` accepts a role or
`all|both`. Broadcast is stored using the engine broadcast token and the wake
notification is fanned out to every other enabled role individually. Replies
use `reply_to`, which resolves the original bounded reply obligation.

Meetings use the typed `meeting.*` verbs for attendance, updates, positions,
obligations, and mutual termination. Generic meeting narration does not replace
the formal review state machine.

Formal review verbs are:

```text
review.start, review.submit, review.report, review.review, review.finalize,
review.discuss, review.agree, review.conclude, review.status
```

`review.submit` takes `{thread_id, stage, name, content, sha256}`. Stage aliases
omit `stage`. A client path is never accepted. The engine preserves checked-in
participant gates, one submission per role/stage, report/cross-review phases,
bounded alternating discussion, caller-derived finalizer, and meeting-owned
closure.

Artifact writes are deterministic: validate bounded UTF-8/name/hash, write a
0600 temporary file, fsync it, atomically link it at `<root>/<sha-prefix>/<sha>`,
and verify an existing blob before reuse. A crash before the SQLite reference
commits can leave a harmless orphan. Garbage collection is intentionally an
operator boundary: retain every `stored_path` referenced by
`control_review_artifacts`, and delete only unreferenced digest files older than
a deployment-defined grace period. There is no automatic GC in the control
request path.

## Wake execution and quarantine

`agent.wake.claim` atomically claims planner attempts and returns one stable
claim with mode, prompt, reasons, attempt ids, and inbox ids. A duplicate claim
recovers the same row. `agent.wake.land` records `landed`, `failed`, or
`indeterminate`.

An indeterminate launch quarantines that role: no new claim is issued until an
operator calls:

```json
{
  "verb": "wake.reconcile",
  "params": {
    "claim_id": "...",
    "resolution": "retry",
    "note": "operator evidence"
  }
}
```

`retry` returns pending attempts to a failed/retryable outcome. `landed` marks
the attempts acknowledged and the claimed inbox items delivered. Reconciliation
is retained in `/api/self` for audit.

## Failure rules

| Failure | Required behavior |
| --- | --- |
| Duplicate native request | Replay the completed receipt; never mutate twice |
| Live duplicate external request | Return running job/lease; never invoke twice |
| Recoverable Git outcome unknown | Expire execution lease and let identical request prove/recover |
| Non-recoverable external outcome unknown | Terminal `indeterminate`; operator reconciliation |
| Wake process may have launched | Role quarantine; no blind second launch |
| Wrong/missing build pin | Reject before session presence changes |
| Wrong role or private event | 403 for reads/commands; cursor-only redaction for SSE |
| Stale/mismatched workspace CAS | Reject; require a new inspect/status |
| Artifact DB commit lost | Reuse verified content digest; orphan may be GC'd after grace |

