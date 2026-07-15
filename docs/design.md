# Design

The decisions behind deskd, and why they're the way they are.

## The constraint everything follows from

A headless agent session **runs one turn and exits**. You cannot inject a prompt
into a turn that is already running. Everything below is a consequence.

- "Deliver to the agent" = two mechanisms: the in-session hook surfaces items
  into a *running* turn's context; the orchestrator **resumes the session id**
  when it's idle.
- "The agent's current session" = a **resumable session id**, not a live process.
  Between wakes there is no process. That is normal.
- Minimum latency to a busy agent = its current sub-task, because the hook can
  only surface; the agent acts at its next natural boundary. The alternative —
  interrupting mid-work — is worse.

## Storage: SQLite (WAL), and it is the only truth

No broker, no daemon holding state in memory. Every tick rebuilds all decisions
from the database, so a crashed orchestrator self-heals on the next tick.

SQLite cannot wake a dormant process. deskd doesn't pretend otherwise: instead of
faking push, it makes every wake attempt an **auditable row** with a closed loop
and an escalation path. Honest pull with verification beats fake push.

## Waking

**Four demand sources** feed one queue: meeting wakes, stuck deliveries, urgent
tasks, and inbox notifications (plus the hooks an agent registers itself).

**One rule with teeth:** a task's `due_at` is a *soft* deadline. It sorts to the
top and is surfaced everywhere, but it **never wakes anyone** — only
`priority=urgent` does. Deadlines shape attention; they don't manufacture
interrupts.

**The ladder** (in-session hook → resume → spawn → human → supervisor badge) is
append-only: escalating supersedes the old attempt and inserts a new one, so a
wake's full history is auditable. Each rung has an SLA; resolution closes the
attempt and records latency. The terminal rung never times out — it just stays red
on the console. Semantics are **at-least-once wake + idempotent ack**, not
exactly-once.

**Decision and execution are separate.** `plan_wakes()` is pure: it collects,
records, and returns a plan — it never spawns anything. The driver executes. This
is why the decision layer is testable and why a dry run can be genuinely
side-effect-free.

## Delivery

A ledger row per (message × recipient): `queued → notified → read`. Past SLA and
unread splits two ways:

- **`escalated`** — something is reacting. The system is working.
- **`overdue`** — nothing is reacting. **This is the guarantee breaking**, and it
  is surfaced red rather than hidden.

The ledger is a **projection** of durable messages, so it is self-healing: a
missing row is re-derived because the source message still exists. Rows are never
deleted. Time-dependent state is computed at read time, never stored, so it can't
go stale.

Two rules learned the hard way:

1. **Never mark delivered speculatively.** Delivery is proven by the in-session
   hook or an explicit ack — never by "we planned to send it". A plan whose
   execution is skipped (role lock held) or fails would otherwise lose the item
   *and* suppress the escalation that should have caught it.
2. **Scope "is this handled?" to (recipient, item)**, never to the container. A
   thread-level flag from one role once masked every other role's stuck message on
   that thread, permanently.

## Presence and one session per role

`agent_sessions` is keyed by **role** — reflecting the invariant that at most one
session per role exists, enforced by a role-scoped `flock` that every starter
takes. The kernel releases it on crash, so stale locks can't exist.

Liveness is derived from heartbeat age (`online`/`suspect`/`dead`/`offline`/
`never`). Heartbeats ride the in-session hook — no extra process. Registration is
explicit, so unrelated sessions never pollute the board.

The board shows "now executing" **only for a live session**. An ended session's
last activity is shown as history, never as current work.

## Sessions: intraday continuity, cross-day restart

Within a day, resume (context is preserved, cheap). Across days, restart: wind the
old session down with a **handoff note**, then open fresh.

Long-lived sessions rot: context grows monotonically, compaction drops detail
unpredictably, and instruction changes never land. A daily restart forces durable
state — anything worth keeping must be in a task, the journal, or the knowledge
base, not in a session's memory. Continuity is **structured state, not
conversation**.

## Hooks: agents schedule themselves

Agents must never poll. So they must be able to say "wake me later / when X":
`at`, `interval`, `cron` (calendar, DST-correct via `zoneinfo`, computed by a
minute-scan — trivial cost, no clever math to get wrong), and `probe` (a host
function; truthy return = notification = wake).

Probes are restricted to a configured allowlist, may only observe and notify, and
auto-disable after consecutive errors (with an inbox notice to the owner) so a
broken watcher can neither rot silently nor stall the tick.

**Anything that can block does not belong in a probe.** Detection that hits the
network runs in its own process and enqueues to the inbox; the orchestrator
delivers. Detection and delivery stay decoupled — otherwise one slow fetch stalls
all waking.

## Meetings

Bounded by construction: idle deadline, message budget, automatic consensus at a
threshold, one position each, and a mutual propose/confirm termination handshake.
The transport rejects duplicates, and rejects stacked unresolved questions for
callers that ask for a reply — meetings deliberately do not, tracking each as
its own obligation instead, so one answer may settle several. Turn-taking is
NOT a bound: refusing to let a present party speak because an absent one owes
a reply drops messages rather than preventing them, since a rejected insert is
the only way a message is lost once delivery and the wake ladder are carrying
it. An owed reply is nudged, never enforced at the door. Termination
votes don't consume the budget, so a meeting can always stop. The tally counts
only *active* attendees, so someone leaving can never deadlock closure.

The protocol's core integrity rule: **never create both sides**. An agent cannot
fabricate the counterpart's attendance, reports, or votes — if the other side
never arrives, the artifact stands alone and the meeting pauses or escalates.

## The supervisor is not an agent

A human oversight identity, outside the agent role space, reachable only through
an authenticated web adapter. Agent APIs reject it; a message claiming to be from
them carries no authority. See [security.md](security.md).

## What deskd deliberately does not do

- **Execute your domain.** It wakes agents and delivers notifications. It never
  acts *as* an agent, and has no path to your side-effecting systems.
- **Own your roles.** The registry is configuration. The engine has no idea what
  your agents are for.
- **Guarantee exactly-once anything.** At-least-once + idempotent acks, with every
  attempt auditable.
- **Pretend it can push.** See above.
