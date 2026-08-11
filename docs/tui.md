# Realtime terminal interface

`deskd tui` is the remote companion to the supervisor Web console. It keeps the
credential's authorized desk projection visible in one terminal and turns an
instruction into a durable, urgent wake-producing control-plane command without
waiting for normal inbox batching.

It is a **client**, not another engine process. It never opens the desk's SQLite
file, mounts an agent's private state, starts a provider, or interprets a role
name as authority. Those boundaries stay in the control plane.

## Install and connect

```bash
pip install "deskd[tui]"

# Interactive agent tools: HTTPS plus one injected role bearer token.
DESKD_API_TOKEN="$(secret-tool lookup service deskd role engineer)" \
  deskd tui --url https://desk.example.net

# A file is also accepted if it is a regular owner-only file (chmod 600).
deskd tui --url https://desk.example.net --token-file /run/secrets/deskd-token
```

The token is held in memory and sent only in the `Authorization` header. There
is deliberately no `--token` flag (shell history and process listings are not
secret stores), no credential-in-URL support, and no on-disk token cache. A
bearer token over non-loopback plain HTTP is rejected unless the operator
explicitly chooses `--allow-insecure-http` for a trusted network.

`DESKD_API_TOKEN` is a control-plane API credential. It is **not** the simple
supervisor access code. The TUI never reads or sends that code and cannot use
the private supervisor endpoints. Supervisor mutations continue through the
authenticated Web/signed adapter; an API token receives only its server-issued
role and scopes.

One TUI process uses one bearer credential. deskd deliberately does not combine
agent and service authority:

| credential | visible state | composer authority |
|---|---|---|
| Agent role token | That role's private state plus redacted shared office state | Direct role mail, its inbox, meetings, hooks, tasks, wake and workspace tools permitted by host policy |
| `read` + `directive` service token | Whole-desk operator projection | Operator directives and service-authorized task operations; no meeting seat, inbox identity or role-owned hook |
| Service token with `operator` | Whole-desk operator projection | Reconcile indeterminate provider wake claims in quarantine; no agent identity |
| Scheduler/orchestrator service token | Service-specific projection | Background service verbs; not an interactive agent identity |

The server publishes the exact principal-specific list in
`snapshot.meta.allowed_verbs`. Help and the persistent composer hint are built
from that list, so a service TUI does not advertise role-only meeting, hook or
inbox commands. Older servers' global `meta.verbs` list is accepted only as a
compatibility fallback; the server still makes every authorization decision.

For an open or read-only development server:

```bash
deskd tui --url http://127.0.0.1:8000 --no-auth
```

An unauthenticated 401/403 from `/api/snapshot` falls back to the legacy public
projection endpoints only when the client has no token. This mode is visibly
labelled `POLLING` / `compat snapshot (non-atomic)`: it does not start an SSE
follower and offers no remote composer commands. If a token was supplied, the
same 401/403 is a hard authentication failure and never downgrades to public
reads.

Useful connection flags:

| flag | purpose |
|---|---|
| `--role ROLE` | Default *target* for unprefixed composer text. It never claims that identity. |
| `--ca-file PATH` | Add a private CA bundle while retaining TLS verification. |
| `--timeout SECONDS` | Bound projection and command requests. |
| `--stale-after SECONDS` | Turn the connection banner yellow when neither data nor heartbeat arrives. |
| `--refresh SECONDS` | Full-projection fallback interval; SSE remains the fast path. |

## What the screen shows

- **Overview** — every registered agent's liveness, session state, current
  activity, task/inbox/meeting load, wake level and heartbeat age. Selecting a
  role opens its live narration feed.
- **Tasks** — status, priority, owner, named dependency, overdue and stalled
  markers.
- **Inbox / mail** — per-recipient durable notifications, delivery state,
  source and age.
- **Meetings** — live and historical meeting state, quorum and agenda. Selecting
  a meeting loads the audit transcript and escalations.
- **Wake / hooks** — the wake-attempt ledger, prominently marked quarantined
  provider claims, and every self-registered timer, cron/probe or one-shot hook.
- **Runtime** — resolved provider/model/reasoning per role and the provider that
  owns each live session.
- **Activity** — accepted commands, SSE invalidations, reconnects and useful
  protocol errors.

The top banner is intentionally blunt:

- `LIVE` means a snapshot or SSE heartbeat arrived inside the stale window.
- `CONNECTING` / `RECONNECTING` means the client is retaining its cursor and
  retrying with bounded exponential backoff.
- `STALE` means the last known state is still rendered but must not be treated
  as current.
- `compat snapshot (non-atomic)` means an older server lacks `/api/snapshot`;
  the TUI used the legacy projection endpoints and will not pretend those
  separate reads were one revision.

## Fast composer

Press `Ctrl+L` from anywhere. A plain instruction immediately enters the urgent
wake path:

```text
@engineer investigate the failed deploy and report the exact blocker
```

If `--role engineer` was supplied, the `@engineer` prefix may be omitted. That
flag chooses a recipient only. The control plane still derives the caller from
the credential. With a role token this compiles to credential-derived
`message.send`; with a directive service token it compiles to `directive.send`.
Both use `priority=urgent`, so the next orchestrator tick creates the wake rather
than waiting for the normal inbox batch window. The TUI reports acceptance
immediately but never invents a `working` state before an authoritative snapshot
or event shows it.

The command grammar exposes the desk's coordination tools without copying their
rules into the UI:

```text
/mail @ROLE message
/task add @ROLE title
/task done ID [result note]
/task block ID named dependency
/task assign ID @ROLE
/task cancel ID [note]
/inbox ack ID[,ID...]
/wake reconcile CLAIM_ID retry|landed NOTE

/meeting call @A,@B agenda
/meeting checkin MEETING_ID
/meeting send MEETING_ID message
/meeting reply MEETING_ID MESSAGE_ID message
/meeting resolve MEETING_ID OWN_MESSAGE_ID MESSAGE_ID[,MESSAGE_ID...]
/meeting position MEETING_ID body
/meeting pause MEETING_ID reason
/meeting escalate MEETING_ID reason
/meeting end MEETING_ID resolution
/meeting confirm MEETING_ID

/hook at ISO_TIMESTAMP title
/hook every SECONDS title
/hook cron "EXPRESSION" title
/hook cancel ID

/refresh
/help
/quit
```

`/wake reconcile` appears only for a service credential whose
`allowed_verbs` contains `wake.reconcile`. It settles a provider launch whose
outcome is indeterminate: `retry` permits a fresh wake attempt; `landed` records
that external evidence proves the original launch succeeded. A non-empty audit
note is mandatory. Without operator scope the command is absent from help and is
rejected locally if typed.

Parsing is convenience, not authorization. The current credential's unavailable
verbs are rejected locally with a clear `/help` hint, and the server remains the
authority for the verb allowlist, valid roles, role-transfer policy, meeting
seat identity, inbox receipt rules, hook ownership and supervisor boundary. A
client request that crosses one of those boundaries is rejected even if the
composer can spell it.

## HTTP and SSE contract

The network client is a standalone stdlib module (`deskd.tui.client`) so hosts
can contract-test it without Textual or a terminal.

### Consistent bootstrap

```http
GET /api/snapshot
Accept: application/json
Authorization: Bearer <token>
```

The response contains the complete terminal projection and the highest event
cursor visible in that same consistent read:

```json
{
  "cursor": "evt_0123456789abcdef_000000000000002a",
  "generated_at": "2026-08-11T05:00:00Z",
  "server_version": "0.4.0",
  "board": {"agents": [], "health": {}},
  "tasks": {"tasks": [], "stalled_ids": []},
  "inbox": [],
  "meetings": [],
  "hooks": [],
  "wake": {"ladder": [], "attempts": []},
  "runtime": {"roles": [], "providers": {}},
  "meta": {
    "role": "engineer",
    "scopes": ["agent"],
    "allowed_verbs": ["message.send", "meeting.send", "hook.add"],
    "roles": []
  }
}
```

State is authoritative; an SSE event is only an invalidation/feed item. After
bootstrap the client begins replay at `cursor`, so no commit can fall into a
snapshot-to-stream gap. A pre-snapshot server is supported through the existing
`/api/board`, `/api/tasks`, `/api/meetings`, `/api/hooks`, `/api/wake`,
`/api/runtime`, and `/api/meeting-meta` projections, visibly marked compatibility
mode.

### Realtime replay

```http
GET /api/events?after=evt_0123456789abcdef_000000000000002a
Accept: text/event-stream
Last-Event-ID: evt_0123456789abcdef_000000000000002a
Authorization: Bearer <token>
```

- Event IDs are opaque monotonic cursors. Their current wire form contains a
  server epoch and a sequence (`evt_<serverid16>_<seq16>`), but clients must not
  parse, compare or increment either component. The epoch is what lets the
  server reject a cursor copied from another instance.
- Both `after` and `Last-Event-ID` carry the last fully observed ID; duplicate
  delivery is harmless.
- Events use JSON `data` and name the affected resource/role/record. The client
  coalesces bursts, then refreshes authoritative projections.
- The server sends an SSE comment heartbeat at least every 15 seconds. A quiet
  desk therefore stays `LIVE` without manufacturing state changes.
- `409` with a cursor-expired detail means replay retention no longer reaches
  the cursor. The client discards it, obtains a new full snapshot and resumes.
- A mid-stream `event: reset` with `{"resnapshot": true}` has the same meaning.
  The current follower stops immediately; it never reconnects once with the
  stale cursor before obtaining a new atomic snapshot boundary.
- A closed socket, timeout, 429 or 5xx causes bounded exponential reconnect from
  the last cursor. Errors are shown without printing authorization headers.

### Commands

```http
POST /api/commands
Content-Type: application/json
Authorization: Bearer <token>
Idempotency-Key: 8fa74cec-d344-4dcc-975d-84de959d6b73

{
  "request_id": "8fa74cec-d344-4dcc-975d-84de959d6b73",
  "verb": "directive.send",
  "params": {
    "target_role": "engineer",
    "body": "inspect the failed deploy",
    "priority": "urgent"
  }
}
```

The envelope carries no `actor` field. Identity and scopes come only from the
credential. `request_id` and `Idempotency-Key` are the same UUID so a transport
retry can never create two tasks, messages or meeting updates. A successful
accept returns `202` with the request ID and committed event cursor; the TUI
shows acceptance immediately and independently waits for SSE to prove the state
transition.

A role credential sends the same composer text through `message.send` instead:

```json
{
  "request_id": "8fa74cec-d344-4dcc-975d-84de959d6b73",
  "verb": "message.send",
  "params": {
    "target_role": "operator",
    "subject": "deskd TUI: inspect the failed deploy",
    "body": "inspect the failed deploy",
    "kind": "note",
    "priority": "urgent"
  }
}
```

The subject is a deterministic, bounded projection of the visible message body;
it is not an identity claim. The control plane derives the sender from the role
token and atomically creates the direct mailbox row plus its urgent unified-inbox
notification.

## Consistency and failure semantics

1. SQLite/control-plane state remains the source of truth. The TUI holds only a
   replaceable projection and an opaque replay cursor.
2. A consistent snapshot plus replay-from-cursor makes reconnect lossless within
   event retention. Cursor expiry is explicit resynchronization, never silent
   skipping.
3. Commands are at-least-once transport with an idempotency key. Server-side
   command execution must be transactional with its event row.
4. The UI never optimistically invents agent state. It reports command
   acceptance immediately, then renders `working`, inbox delivery, wake attempts
   or failure only after those appear in an authoritative projection.
5. A rendered stale state remains useful history, but the yellow `STALE` banner
   prevents it being mistaken for liveness.
