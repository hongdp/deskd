# Glossary

Terms that mean something specific here, and the two that mean *two* specific
things.

This file exists because the codebase has genuine name collisions. `escalation`
names two unrelated ledgers plus two unrelated states; `store` names two modules
with the same seven helper names and two independent clocks. A reader who
guesses which one is meant reads the wrong subsystem's guarantee and believes it.
Renaming would break the public API (see [`roadmap.md`](roadmap.md), *Known
structural debt*), so naming the difference precisely is the answer for now.

Every entry below was read out of the source, not inferred from the name.

---

## The two collisions

### `escalation` — two ledgers, and two states that borrow the word

**meeting escalation** — a ledger row saying *a human should hear about this
meeting*, queued inside the meeting's own write transaction and mirrored out
through the channel layer only after that transaction commits (a slow channel
must never hold a write lock on a meeting).

- *Lives in:* `meetings/escalations.py`; table `meeting_escalations`
  (`thread_id`, `requested_by`, `reason`, `channel`, `status` =
  `queued|sent|failed`, `details`, `created_at`, `sent_at`).
- *Fires when* — all five call sites: an **urgent** meeting is called
  (`lifecycle._call_meeting`, riding on top of the machine wake, not replacing
  it); an **attendance timeout** elapses on a `waiting` meeting, once per
  meeting (`sweep._sweep_timeouts`, guarded by `waiting_escalated_at`); a
  meeting slides into **consensus with the supervisor absent**
  (`messaging._send_update`, which also stamps `auto_escalated_at`); a
  **termination proposal is rejected near the message budget** with no
  supervisor present (`termination._vote_end` — queued and deliberately *not*
  dispatched: a standing condition for the console, not a page); and an
  attendee **explicitly hands the meeting to a human**
  (`termination.escalate_meeting`).
- *Console:* the **Meeting escalations** card on `/escalations`
  (`GET /api/escalations` → `meetings.list_escalations()`), and per-meeting
  inside `meeting_transcript`.
- *Short name to hold:* **meeting escalation** — per-meeting, scoped to a
  `thread_id`, dispatched once, never retried.

**wake escalation** — the durable human-rung outbox of the wake ladder: one row
per arrival of a wake demand at a rung that declares `leaves_machine`, for
*every* reason kind. The row exists whether or not any channel is registered;
the channel layer only mirrors it out.

- *Lives in:* `orchestration/wake.py` (`_queue_wake_escalation`,
  `_dispatch_wake_escalation`, `_retry_wake_escalations`,
  `wake_escalations_recent`); table `wake_escalations` (`role`, `reason_kind`,
  `source_ref`, `level`, `channel`, `reason`, `status` = `queued|sent|failed`,
  `details`, `created_at`, `sent_at`).
- *Fires when:* `plan_wakes` puts a demand on a `leaves_machine` rung — either
  by climbing onto one, or by *starting* there if a host ladder is shaped that
  way. Written inside the planning transaction, dispatched after it commits,
  and then **retried every tick for 24h**: `failed` rows always, `queued` rows
  once a registered channel reports reachable again.
- *Console:* the **Wake escalations · human-rung ledger** card on
  `/escalations`, plus the `undelivered wake escalations` alarm — which counts
  `queued` **and** `failed`, because both mean *reached nobody*.
- *Short name to hold:* **wake escalation** — per-demand, the ladder's terminal
  sink, retried.

*The confusion this prevents:* a meeting escalation is not evidence the wake
ladder climbed, and a wake escalation is not something a meeting did. Different
tables, different owners, different lifecycles — only one of them retries, and
only one of them is the thing that makes "a wake never silently dies" true.
`/escalations` shows **both**, on purpose: that page is *every path by which
this desk pulls a human in*, so it lists both ledgers side by side (plus
unroutable demands and channel health).

Two more uses of the same stem, neither of them a ledger:

- **`escalated` (meeting state / thread status)** — the meeting is parked for a
  human. `escalate_meeting(pause=True)` writes `meetings.state='escalated'` and
  `mailbox_threads.status='escalated'` in the same transaction that queues the
  meeting-escalation row. A *state*, not a row.
- **`escalated` (delivery state)** — past SLA, unread, **and something is
  actively re-driving it** to that exact recipient. It means the machine is on
  it; it does not mean a person was pulled in. See *delivery states* below.

### `store` — two modules, two schemas, two clocks

**`meetings/store.py`** — the bottom of the meetings package: `MEETING_SCHEMA`,
its own `_migrate`, `connect()` (which wraps `mailbox.connect()` and layers
`auth.SCHEMA` + the meeting tables), the meetings role gates (`_agent_role`,
`_known_roles`, `_meeting_roles`), the visibility predicate
(`_visible_message_sql`), the attendance primitives, and the row helpers.

**`orchestration/store.py`** — the bottom of the orchestration package:
`ORCH_SCHEMA`, its own `_migrate`, `_seed_registry`, `connect()` (which wraps
`meetings.connect()` and layers the orchestration tables), the role registry
reads, and the orchestration event log.

Both define `_now`, `_iso`, `_clean`, `_agent_role`, `_known_roles`, `connect`
and `_migrate`. Same names, different modules, no relationship between them.

*Why two, and why that is deliberate:* each subpackage is layered
`store → … → facade` (see both `__init__.py` docstrings and `CLAUDE.md`), and
the store is the layer nothing above it may bypass. Two consequences follow, and
both are contracts with tests behind them:

1. **Two clock patch points.** Submodules read the clock through the module
   attribute — `store._now()` — never a bound import, so patching that single
   attribute steers every SLA in that subsystem and *only* that subsystem.
   `tests/test_presence.py`'s clock patches `orchestration.store._now`;
   `tests/test_meetings.py::test_the_meetings_clock_has_one_patch_point` patches
   `meetings.store._now`. Patch the wrong one and the time-dependent test is
   silently testing nothing.
2. **Each layer migrates its own tables.** `meetings.closed_at` was briefly
   added by *orchestration's* `_migrate` — which a meetings-only host never
   runs — so `list_meetings` raised `no such column: closed_at` against every
   pre-existing DB, and only the deployment shape nobody exercises broke.

*The confusion this prevents:* "patch the clock" and "add the migration" are
both questions with two answers here. Merging the two stores is discussed in
[`roadmap.md`](roadmap.md) (*Known structural debt*) and is blocked on P4.

---

## Waking

**demand** — a reason some role needs waking *right now*, recomputed from the
database every tick and never stored: `{role, reason_kind, source_ref, label,
since_at}`. Six kinds: `meeting_wake`, `stuck_delivery`, `urgent_task`,
`owed_reply`, `inbox`, `idle_task`.
*Lives in:* `orchestration/wake.py::collect_wake_demand`.
*Prevents:* looking for a demand table. There isn't one — demand is derived,
which is why a crashed orchestrator self-heals on the next tick, and why
generation (`collect_wake_demand`) and resolution (`_demand_resolved`) must
mirror each other clause for clause.

**wake attempt** — the durable, append-only record of *trying* to satisfy one
demand at one rung. Escalating supersedes the old row and inserts a new one, so
a demand's full wake history is auditable.
*Lives in:* table `wake_attempts` (`role`, `reason_kind`, `source_ref`,
`channel`, `level`, `outcome` = `pending|acked|read|timeout|superseded|failed`,
`latency_seconds`).
*Prevents:* reading an attempt as proof a session ran. It is not — the driver
skips when the per-role lock is held, and launches fail.

**wake request** — a *meetings*-owned row asking that one role be woken for one
meeting. Written when a meeting is called (every agent invitee except the
caller — the invitation is itself a wake demand, whatever the priority), on an
attendance timeout, when a checked-in attendee sits on unread messages past the
SLA, and when a termination vote parks.
*Lives in:* table `meeting_wake_requests` (`thread_id`, `role`, `status` =
`pending|acknowledged`); `meetings/lifecycle.py` and `meetings/sweep.py`.
*Prevents:* confusing it with a wake attempt. A request is meetings *asking*; an
attempt is orchestration *trying*. meetings must never import orchestration, so
orchestration **pulls** these rows as `meeting_wake` demand.

**wake (the verb)** — booting or resuming a role's session so it has a turn in
which to act. The engine only ever wakes agents; it never acts as one.

**rung** / **level** — one step of `CONFIG.wake_ladder`; `level` is that rung's
**index**. The default ladder is `hook` (60s) → `resume` (120s) → `spawn`
(180s) → `human` (300s) → `supervisor_badge` (terminal).
*Lives in:* `config.py::WakeRung`, `DEFAULT_WAKE_LADDER`.
*Prevents:* hardcoding a rung count or assuming "L3 means a person". Hosts
define their own ladder — look rungs up by name (`_channel_level`) or by the
flag below, never by position.

**`leaves_machine`** — a rung's own declaration that reaching it pulls a person
in. `_human_level`, `_reason_ceiling` and `wake_ladder_view().wired` all read
the flag rather than matching channel names, so a host that renames or reorders
its rungs is still fenced correctly.
*Prevents:* a task wake reaching a phone. `MACHINE_ONLY_REASONS` (`idle_task`)
is ceilinged below the first `leaves_machine` rung by construction.

**terminal rung** — a rung with `sla_seconds=None`. It never times out; it just
stays red. It *is* recycled after `CONFIG.terminal_retry_seconds` (default 1800)
so a transient outage cannot park a demand there forever — the badge stays red,
the machine keeps trying.

**hook** — three different things, all called "hook":

- a **wake hook** is the self-service API by which an agent asks to be woken
  later: `at`, `interval`, `cron`, or `probe`. Firing enqueues an *inbox item*,
  which then rides the normal delivery/wake path. Table `wake_hooks`,
  `orchestration/hooks.py`.
- the **`hook` rung** (L0) means "the role is online, so its in-session hook
  will deliver this" — the one rung the driver takes no action on.
- **`scripts/session_hook.py`** is the Claude Code `PostToolUse` hook: it
  surfaces the queue into a running turn and heartbeats presence. It is host
  integration, not engine state.

**probe** — a host-supplied watcher function behind a wake hook, restricted to
`CONFIG.probe_allowlist` (empty = deny all). It may observe and notify, nothing
else; anything that can block does not belong in one.

**`plan_wakes` vs the driver** — `plan_wakes` collects, records and returns a
plan; it never spawns or resumes anything. The driver holds the per-role lock
and executes. `record=False` is a genuinely side-effect-free preview: it makes
the same decisions and rolls every write back.

---

## Delivery

**delivery ledger** — one row per (message × recipient), a pure *projection* of
the durable mailbox tables, re-derived idempotently by `sync_delivery`. Rows are
never deleted, and the time-dependent state is computed at read time so it
cannot go stale.
*Lives in:* table `message_delivery`; `orchestration/delivery.py`.

The five states (`DELIVERY_STATES`), all computed by `_delivery_state`:

- **`queued`** — projected, within SLA, nothing stamped yet.
- **`notified`** — a `mailbox_notifications` row exists: the message was put in
  front of the role. Stamped by `meetings.discover` (`_stamp_notifications`).
  *Not* read, and purely additive — it can never suppress an unread count.
- **`read`** — a `mailbox_receipts` row exists: an **explicit** ack, written
  only by `meeting_updates(..., mark_read=True)`, `mailbox.inbox(mark_read=
  True)`, or `mailbox.acknowledge()`. Nothing stamps it on the agent's behalf.
- **`escalated`** — past `sla_due_at`, unread, **and** a *pending*
  `meeting_wake_requests` row exists for that exact `(thread, role)` pair:
  something is re-driving delivery right now. Scope is `(recipient, item)`,
  never the containing thread, and the test is present-tense.
- **`overdue`** — past SLA, unread, nothing reacting. **This is the guarantee
  breaking**; it is surfaced red and it raises the `stuck_delivery` demand.

*Prevents:* two specific misreadings. `escalated` here does not mean a human was
involved — it means the machine is on it. And `notified` is not `read`: the
whole product is the distance between those two words.

---

## Presence and sessions

**session** — one live agent process for a role. `agent_sessions` is keyed by
**role**, not by session id, because at most one live session per role exists —
enforced by a role-scoped `flock` the kernel releases on crash.

**liveness** — derived from heartbeat age and declared state, never stored
(`orchestration/presence.py::_presence_row`). Six values:

| value | means |
|---|---|
| `online` | heartbeat younger than `online_max_seconds` (120) |
| `suspect` | younger than `suspect_max_seconds` (600) |
| `idle` | quiet, but the session declared a **resting** state (`idle_standby` / `stopping`) — doing exactly what it was told, and still **resumable** |
| `dead` | claimed to be working and then stopped proving it |
| `offline` | `ended_at` is set |
| `never` | no heartbeat has ever been recorded |

*Prevents:* reading `idle` as a mild `dead`. They are opposite claims — `idle`
is this engine's normal resting condition between wakes and keeps the session
id resumable, while `dead` should look alarming. (`design.md`'s liveness list
predates `idle`; the six above are what the code returns.)

**busy** — `_is_busy`: liveness in `LIVE_LIVENESS` (`online`, `suspect`), i.e. a
turn is running that a wake would *interrupt*. Deliberately the same predicate
the board uses to decide whether a session's work breakdown may be shown as
current.

**session day / rollover / draining** — `session_day` is the local-tz calendar
day a session belongs to. A session stamped with an earlier day is stale;
`rollover_plan` marks it `draining` and the driver resumes it with a wind-down
prompt, after which a fresh session opens for the new day.

---

## Work items

**task** — a cross-session work item assigned to a role (`agent_tasks`).
`due_at` is a **soft** deadline: it orders and flags overdue, and never wakes
anyone. Only `priority='urgent'` wakes regardless of state; an ordinary open
task wakes its assignee only while that assignee is idle.

**actionable / stalled** — a derived split, never stored. A task woken
`CONFIG.idle_task_stall_wakes` times (default 3) since it last moved is
*stalled*: it **leaves** the actionable set, stops causing wakes, and becomes a
reported fact for someone to decide about. Derived from the `wake_attempts`
ledger, so it cannot go stale.

**blocked / `blocked_on`** — `status='blocked'` is only legal when the row names
the dependency it waits on. Nothing else counts as blocked; `pending` forever is
not a resting state.

**inbox item** — an agent-directed notification in `agent_inbox`, and
`inbox_enqueue()` is **the** public ingress for hosts injecting their own domain
events. Lifecycle `queued → delivered → acked`.
*Prevents:* confusing it with a task. A task is work you own across sessions; an
inbox item is a notification you must *see*. A wake hook fires into the inbox —
it never creates a task.

**delivered vs acked (inbox)** — `delivered_at` means the wake actually put the
item in front of the agent, stamped by the in-session hook or the agent's own
ack, **never speculatively at plan time**. `acked_at` means the agent processed
it. A failed launch therefore leaves items undelivered, the demand alive, and
the ladder escalating.

**unroutable demand** — capability-addressed work no enabled role may take:
recorded durably in `unroutable_demands`, counted red on the board, and re-routed
by the first planning tick in which a qualifying role exists. The wake ladder's
guarantee applied to the authority axis.
*Prevents:* confusing it with a wake demand. This is the one "demand" in the
codebase that is a stored row.

---

## Conversations

**thread** — the mailbox's durable conversation container: `kind`
(`live`|`review`), `status` (`open`|`paused`|`closed`|`escalated`), an idle
deadline and a message budget. Table `mailbox_threads`, `mailbox.py`. A thread
exists perfectly well with no meeting on it.

**meeting** — the bounded-conversation *application* layered on exactly one
thread: agenda, invited attendees, quorum, consensus threshold, termination
handshake. Table `meetings`, primary key `thread_id`, FK to `mailbox_threads.id`.
*Prevents:* conflating two status columns. A meeting has a **`state`**
(`waiting`, `active`, `consensus`, `termination_pending`, `paused`, `escalated`,
`closed`); its thread has a **`status`** (`open`, `paused`, `closed`,
`escalated`). `_meeting()` returns both, the second as `thread_status`.

**mode** — the discussion shape, derived purely from who is currently checked in
and not stopped: `waiting` (<2), `one_to_one` (2), `multi` (3+). Only
`one_to_one` creates response obligations — a broadcast cannot sensibly obligate
everyone.

**response obligation** — a ledger row saying a named role owes a reply to a
specific message, with a `due_at`. Table `meeting_response_obligations`
(`status` = `pending|resolved|waived`), created by `messaging._send_update`.

**owed reply** — the wake `reason_kind` raised from an obligation still
`pending` past its `due_at` in a live meeting.
*Prevents:* thinking obligation and owed reply are the same thing. The
obligation is the debt; `owed_reply` is the wake demand the debt raises once
it is overdue. And meetings deliberately do **not** page anyone for a late
reply any more — that branch of the sweep was removed because a slow agent is
not an incident; the ladder climbs its machine rungs first and reaches a person
only on the merits.

**discharge / resolve / waive** — three ways an obligation ends.
`_discharge_obligations` settles debts the *author* cites (`resolves=[…]`),
because only the author knows which questions their reply covered;
`_resolve_obligations` settles by a direct reply; `_waive_pending_obligations`
clears the remainder when a meeting closes. The engine refuses to guess — blanket
auto-settling on any outgoing message is a dropped question wearing a clean
ledger.

**broadcast** — the recipient token `all` (`mailbox.BROADCAST`), addressing every
participant of a thread. `both` is a read-only legacy alias from when the engine
had exactly two roles; nothing writes it.

---

## Identity and authority

**role** — a host-defined agent identity string. `agent_registry` is the source
of truth for which roles exist; the engine hardcodes none, and every role literal
reaching SQL is a bound placeholder.

**supervisor** — the human oversight identity (`CONFIG.supervisor_role`), and
**not** an agent role. It has no session, no heartbeat and no inbox; both
stores' `_agent_role()` reject it outright; its actions enter only through the
authenticated web adapter, each carrying a one-shot nonce burned before the
action runs; and a supervisor *message* is invisible until it has a
`meeting_message_auth` row.
*Prevents:* treating the supervisor as "role number three". It sits in meetings
and votes, but nothing wakes it, and no agent path can speak as it.

**capability** — a string on a role's registry row declaring what that role may
do. It is the addressing axis for `inbox_route()` ("reach whoever may do this"),
and the engine does nothing with it beyond matching.

**authority** — an opaque dict on a role's registry row that the engine stores,
carries in wake and rollover plans, and **never reads for a decision**. The
driver maps `authority["allowed_tools"]` to the session's harness grant.
*Prevents:* believing deskd enforces anything here. deskd declares; the harness,
the OS and the container enforce — and a grant containing `Bash` makes every
other restriction in the list advisory.

**channel** — pluggable human-facing egress (`deskd.channels`). A channel
*mirrors* a ledger row out; it never replaces one, and read proof always comes
from the ack path rather than a channel's own semantics. The engine ships zero
channel implementations by design.

**`outbox`** — the reserved, always-available terminal channel whose "delivery"
is the durable ledger row itself, surfaced by the console. A host cannot register
it.
*Prevents:* reading `status='queued'` as "in flight". It means the row is the
only delivery so far — nobody was pulled in unless somebody reads that page.

**nonce** — a one-shot supervisor authorization, verified in `deskd.auth`,
recorded in `supervisor_nonces`, burned before the action it authorizes runs,
and referenced by the row it produced.
