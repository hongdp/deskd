# Roadmap

What deskd is becoming, in the order the pieces depend on each other.

This is not a wish list. Each item says what it unlocks and what it must wait
for, because the ordering is the actual content: several of these are cheap only
if done before the thing above them, and permanent if done after publication.

## The thesis, sharpened

deskd's core is **reliable programmatic activation**: an agent ends its turn and
something outside it wakes it — on a timer, a calendar, a watcher, a message, or
an escalation ladder when a wake doesn't land. Everything else is an application
of that.

Two consequences run through this whole document:

1. **The core is smaller than the repo.** Presence, the inbox, the wake ladder,
   hooks and the delivery ledger are the framework. Bounded meetings are the
   first *application* of it — currently ~45% of the source, living in the core.
2. **The engine declares; something else enforces.** deskd has no path to a
   host's side-effecting systems and must not grow one. Where it seems to
   enforce something today, check whether it is actually just advising.

---

## Done in 0.1.0 — the defects that shaped these rules

These shipped fixed; they are kept here as the record of *why* the rules below
exist, because each was a real bug that a green suite hid.

- **The seam tests that didn't exist.** `agent_tasks.source_kind` validated
  against a module constant while `agent_inbox.source_kind` validated against
  config — same column, both CHECK-free, so a column diff showed nothing.
  `_demand_resolved`'s role predicate had the same shape: a mutation restoring
  the *original* bug failed a test, but dropping the role predicate from the
  *replacement* query passed. **Fixed:** `task_sources` is now a config field,
  every config seam has a host-extends-it test, and the standing rule is —
  **every invariant stated in a comment gets a test or stops being stated.**
- **Closed enum sets vs. the docs.** `mailbox_threads.kind` and
  `review_artifacts.stage` were CHECK-constrained with no host seam while the
  code claimed no CHECK froze a host's vocabulary. **Resolved:** reconciled so a
  reader cannot conclude the opposite of what the code does.
- **`BROADCAST`.** `"both"` was a two-role fossil, and `_role()` normalised the
  generic `"all"` *into* it. **Fixed:** `BROADCAST = "all"`, `"both"` kept as a
  read alias.

---

## P1 — Authority as a first-class dimension

**Status: shipped (unreleased).** Wake and rollover plans carry each role's
registry declaration, and the reference driver maps `authority["allowed_tools"]`
to the session's `--allowedTools` — the global default now covers only roles
that declare nothing. `inbox_route()` (`deskd inbox route`) is the
capability-addressed ingress; a demand no enabled role may take lands in
`unroutable_demands`, reads red on the board (`health.unroutable_demands`), and
is re-routed by the first planning tick in which a qualifying role exists.
Still true: a grant containing `Bash` is advisory (the reason this item is not
isolation — see P3), and routing enters at ingress rather than per-rung.

**Why this first:** heterogeneous authority is *why* multi-agent systems exist. If
every agent could do everything you would run one agent; different permissions are
what create the need for a provable handoff — which is deskd's core guarantee
applied to a new axis. This is the highest value-per-line item on the list.

### Connect the declaration to the enforcement point

`RoleSpec` already carries `capabilities` and an opaque `authority` dict. The
engine stores and surfaces them and never reads them for a decision. Meanwhile
the driver hands **every** role the same `--allowedTools`, including `Bash` and
`Edit`. So the declaration is decorative: a role declared with read-only
capabilities is woken with a shell.

The fix is a few lines in the driver, and it is the whole seam: **deskd declares,
the harness enforces.** Note that `--allowedTools` is only a boundary while `Bash`
is excluded — once a session has a shell, restricting `Edit` is theatre. That is
why this item alone is not isolation; see P3.

### Capability-aware routing, and unroutable-as-escalation

The ladder routes by presence. It should also route by capability, and — the part
that is genuinely deskd's job — treat "no role has the capability this demand
requires" as an **unroutable demand**, which is `overdue` on the authority axis.
Same guarantee, new dimension.

---

## P2 — Make the decoupling claims true

**Status: two of four shipped (unreleased).** The terminal rung and the
ledger/channel split are done: `deskd.channels` owns pluggable egress
(meetings re-exports for back-compat), arrival at any `leaves_machine` rung
writes a durable `wake_escalations` row for EVERY reason kind and mirrors it
out post-commit, and the board states which channels are wired
(`health.channels`, `health.human_rung_unwired`,
`health.undelivered_escalations`). Still open: the supervisor-boundary
extraction (partly overtaken — mode/code live in `auth` now; the action verbs
still live in meetings, which tangles with P4) and the non-Python ingress
adapters.

### The terminal rung must not be defined as a UI

The ladder's last rung is "a red badge on the supervisor console". If the console
is swappable, the ladder's terminal rung — the thing that guarantees a wake never
silently dies — goes with it. Redefine it as an abstract durable human-visible
sink with an interface; the console becomes one implementation.

### Extract the supervisor boundary out of the web adapter

`auth.py` owns verification and the nonce ledger, but mode selection, the action
allowlist and the nonce recording call site live in `web/app.py`. If the UI is
swappable, every new UI re-implements the boundary — and gets it wrong. This is
not hypothetical: a host's UI once held a live credential in its page source, and
another re-implemented the mode gate with its own fallback code minting, so the
code the server printed was not the code the verifier checked.

Extract a `supervisor` module that owns the boundary; the web app becomes a thin
HTTP shell over it.

### The ledger is not the transport

The headline guarantee — `queued → notified → read`, and `overdue` when nothing
is reacting — is manufactured by owning durable rows and per-recipient receipts.
A third-party chat service cannot give you per-message read proof for a bot, so
"replace the mailbox with Slack" would silently delete the product.

Split instead:

- **`ledger`** — durable rows, receipts, projection. Never pluggable.
- **`channel`** — pluggable egress/ingress: in-DB, chat, email, webhook.

A message is always recorded in the ledger; a channel mirrors it out and ingests
replies. Read proof comes from the ack path, never from a channel's own
semantics. This is already the shape — the ladder's human rung treats external
channels as a *rung*, not a transport replacement. The work is generalising that
into a plugin interface.

**Note the live gap this exposes:** deskd ships zero channel implementations by
design, so a host that registers none has an `auto` escalation that resolves to
the durable outbox only. The ladder's human rung then writes a row nobody reads —
"pull a human in" pulls in nobody. Hosts must be told this loudly, and the console
should show which rungs are actually wired.

### Ingress that doesn't require writing Python

`inbox_enqueue()` is already THE universal ingress and that design is right. What
is missing for the positioning to be credible is adapters that don't need a Python
host: an HTTP webhook, a file/directory watch, a queue consumer. Detection stays
decoupled from delivery — anything that can block does not belong in a probe.

---

## P3 — The desk model

**The reframing:** stop thinking of one global orchestrator. Think of a desk per
agent: a **phone** others can ring, an **alarm clock** it sets itself, a **watcher**
that shouts when the world outside changes, a **notepad**, and a **lock** so only
one of you sits there.

The insight that makes it work: **an alarm clock is not an agent.** A desk already
contains furniture that runs while its owner is away — so "must be outside the
agent" and "must be global" are different requirements, and deskd conflated them.
Most wake sources are already desk-local: hooks, tasks and inbox obviously; even
"nobody answered" localises, because *your own desk* knows your phone rang and
that you haven't been in since morning.

What does **not** localise is a **shared object**. Quorum ("three of us checked
in"), a mutual termination handshake, "never create both sides" — these are not
facts on anyone's desk. They need one place where the fact is decided.

That boundary lands in exactly the same place as P4, derived independently. Two
unrelated lines of reasoning finding the same seam is evidence the seam is real.

### The keystone: one question, three resolvers

Every agent-facing entry point takes `role: str` as a plain argument. The engine
has **no notion of a caller** — `check_in(role="beta")` *is* beta checking in.
Collapse all deployment modes into one seam:

```
resolve_caller() -> role
    local     : trust the argument            (a trusted single operator)
    production: ask the kernel (SO_PEERCRED)  (uid -> role via the registry)
    tests     : a fake resolver               (arbitrary uid -> role)
```

**One code path, three resolvers — never `if mode == ...` sprinkled through the
engine.** The engine always asks; the local resolver is the trivial one.

This is what turns `role` from a *claim* into an *authenticated identity*, and it
is the first time the "never create both sides" rule could be enforced rather than
advised.

### Run the production architecture locally

Same processes, same code, same paths — only `chmod` and uid differ. This kills
the failure mode that otherwise dooms the whole item: **the shape you never
exercise is the shape that breaks.** (The two-role hardcoding survived for exactly
this reason: there were only ever two roles.) Locally every peer is the same uid,
so the plumbing is exercised daily while the *discrimination* is not — which is
what the fake resolver in the test suite is for. Production isolation must be in
CI from day one or it is fiction.

### The coordinator reads; it does not gate

Do **not** put a coordinator daemon in the write path. Today `inbox_enqueue()`
either commits to SQLite or raises: "landed" is a local, synchronous, durable
fact. Behind an RPC, "landed" becomes "the coordinator acked" — and a lost ack
means the caller does not know. That is the exact question the product exists to
answer: you would create an at-least-once problem *inside* the at-least-once
solver.

Instead: each desk owns its own store and writes it directly (local, synchronous,
same durability as today). The coordinator **reads all desks** and derives the
shared view, then rings bells. It holds no authoritative state — which matches the
existing principle that the ledger is a projection and therefore self-healing.

Two things fall out for free:

- **"Never create both sides" becomes a filesystem fact.** Alpha can only write
  alpha's store; beta's attendance can only be written by beta.
- **Private state stops leaking.** Today `session_todos` and
  `agent_sessions.activity` sit in a store every role can read, with no reason.

And one component-level property worth the whole cost: **nothing in the system can
act as more than one agent.** Each desk spawns only its own agent, so no
privileged spawner exists to impersonate anyone.

### Cost, stated honestly

deskd today has **zero always-on processes** — a real design virtue it would give
up, N daemons for N desks plus supervision. Today's shared store is not required
by waking; it is **cheaper than N daemons**, with cron as a free shared alarm
clock. That is an operational choice, not an architectural necessity, and the
trade only pays once isolation has value.

### The mode must be observable

The console must state which resolver is in force. Reading a production board, you
must be able to tell whether the identities shown are *authenticated* or merely
*claimed*. A system that cannot say which guarantee is actually running is where
security theatre begins — the same lesson as an access code that silently fell
back to a random ephemeral value with nothing anywhere saying so.

---

## P4 — Meetings out of the core

Bounded meetings are the most valuable code here, and that is precisely why they
should not be *in* the core: fused to the transport, they are ~45% of the source
and their bounds rot.

Evidence, not theory: the idle deadline was **inert**. `_send_update` read the
thread table raw instead of going through the refresh path, so a stale thread
accepted writes and pushed its own deadline out — while the refresh function's
docstring promised "every read path goes through here so a stale thread can never
be written to". One of four advertised bounds did nothing, because the bound lived
*beside* the application instead of *beneath* it, and a raw path was lying around
to bypass.

So extract the two things underneath, as primitives with a single enforcement
point the application cannot route around:

- **`bounds`** — budget, deadline, termination handshake.
- **`integrity`** — an actor may only ever speak as itself.

`integrity` belongs next to `auth`: it is the same class of invariant as "the
supervisor is not an agent", not an application concern.

Meetings then remain as the reference application — still the most important one.
The difference is that the third collaboration shape inherits the guarantees
instead of copying the logic and missing a bound. That third shape is not
hypothetical: `mailbox_threads.kind` already discriminates two shapes today, and a
review workflow already grew alongside meetings.

---

## What this does not become

- **A distributed system.** Not now. The differentiator is reliable activation
  with delivery proof — unsolved and hard. Distribution is solved, and building it
  here means rebuilding a queue, a scheduler and a broker with the budget that
  should go to the hard part. Delivery proof also gets *harder* across a network,
  not easier. Isolation is the actual goal; per-account is the cheap way to get it
  and multi-host is the expensive one.
- **An enforcement point for host permissions.** deskd has no path to a host's
  side-effecting systems and must not grow one. It declares; the harness, the OS,
  the container enforce.
- **A framework that pretends it can push.** Honest pull with verification beats
  fake push.

## The standing risk

deskd has been validated by **one** application. That is not nothing — most of the
defects found so far were surfaced by *use*, not by review, and a framework built
without a demanding application is built on speculation. But one host validates
that the framework *works*, never that it is *general*: the hardcoded-role bug
survived precisely because there were only ever two roles.

The test suite's arbitrary role names and non-default timezone are a cheap
substitute. The real check is a **second, deliberately unlike host** — one with no
daily open/close rhythm and no two-party review. Until then, treat every "general"
claim in these docs as untested.
