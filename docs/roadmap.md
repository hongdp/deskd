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

## The 0.4 control-plane decision (2026-08-11)

**Status: release gate for 0.4.0.** Production isolation now has a concrete
deployment shape: one authoritative control process and one independently
contained runner per role. The embedded Python/SQLite mode remains supported;
the network control plane is an optional production boundary, not a rewrite of
the engine or a requirement for small local desks.

This supersedes one conclusion in the original P3 sketch below: a coordinator
*does* sit in the coordination write path. The old objection was correct about
an ordinary RPC — if the mutation commits and its reply is lost, a retry may
perform it twice. The missing piece was not "keep every role on the database";
it was a protocol that makes the reply recoverable:

1. every mutation carries a caller-scoped request id and a fingerprint of its
   verb and parameters;
2. the receipt, engine mutation and committed-change event are written in the
   same SQLite transaction;
3. a retry with the same caller/id/content returns the stored result, while the
   same id with different content fails;
4. operations outside that SQLite transaction are explicit durable jobs. Once
   a job may have started, an unknown outcome is `indeterminate` and is never
   blindly replayed.

That closes the lost-ack ambiguity without claiming exactly-once execution. It
also lets role containers receive no database mount at all: the bearer token is
the caller, rather than a role string supplied by code that can read every
other role's rows.

The resulting state boundary is deliberate:

- **shared authoritative state** — registry, presence projection, inbox and
  delivery receipts, tasks, meetings, wake ledger, command receipts, event
  cursor and workspace leases — lives in the control plane's one WAL database;
- **role-private state** — provider credentials, provider session files, local
  journals and caches — lives in a distinct volume for each role and is never
  mounted by another role or the terminal client;
- **provider session identity** is a shared reference to private provider state.
  A provider-minted id is bound with a compare-and-swap before the runner treats
  it as resumable;
- **source changes** live in a broker lease at an exact base SHA. The broker
  alone owns writable Git metadata and may acquire, inspect, renew, diff,
  commit or release through fixed operations; agents cannot push, merge, reset
  or choose an arbitrary repository path.

HTTP commands carry intent; SSE carries low-latency invalidation and heartbeat;
an atomic snapshot remains the truth. A client starts its event replay at the
cursor returned inside that snapshot transaction. Retention gaps and cursors
from a different future are explicit resynchronizations, never silent skips.

This is still not a distributed database or workflow cluster. There is one
coordination writer on one host, and role runners fail closed when it is
unavailable. The point of the network hop is identity and filesystem isolation,
not horizontal scale.

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

**Status: shipped in 0.1.3, except ingress adapters.** The terminal rung and
the ledger/channel split are done: `deskd.channels` owns pluggable egress
(`deskd.meetings` still resolves the six channel names for hosts that have not
migrated, but that spelling is deprecated and warns), arrival at any
`leaves_machine` rung writes a durable `wake_escalations` row for EVERY reason
kind and mirrors it out post-commit, and the board states which channels are
wired
(`health.channels`, `health.human_rung_unwired`,
`health.undelivered_escalations`). The supervisor-boundary extraction moved to
P4 (decision 2026-07-19, see there): what this section originally described
was partly overtaken — mode selection and the access code now live in `auth`
with the web app asking, not deciding — and the remaining piece, the action
verbs, lives inside meetings, so extracting it before meetings leave the core
would do the same surgery twice. Still open here: the non-Python ingress
adapters below.

### The terminal rung must not be defined as a UI

The ladder's last rung is "a red badge on the supervisor console". If the console
is swappable, the ladder's terminal rung — the thing that guarantees a wake never
silently dies — goes with it. Redefine it as an abstract durable human-visible
sink with an interface; the console becomes one implementation.

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

**Status: the isolation goal is the 0.4 control-plane release gate; the
per-desk-store proposal in this original sketch is superseded by the decision
above.** The useful boundary survived: an authenticated caller, private
role-local state, and one independently supervised runner per role. The storage
topology changed after the lost-ack problem acquired a transactional answer.

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

### The keystone: one question, four resolvers

Every agent-facing entry point takes `role: str` as a plain argument. The engine
has **no notion of a caller** — `check_in(role="beta")` *is* beta checking in.
Collapse all deployment modes into one seam:

```
resolve_caller() -> role
    local     : trust the argument            (a trusted single operator)
    unix      : ask the kernel (SO_PEERCRED)  (uid -> role via the registry)
    container : authenticate a bearer token   (token -> one role or service scope)
    tests     : a fake resolver               (arbitrary uid -> role)
```

**One code path, four resolvers — never `if mode == ...` sprinkled through the
engine.** The engine always asks; the local resolver is the trivial one.

This is what turns `role` from a *claim* into an *authenticated identity*, and it
is the first time the "never create both sides" rule could be enforced rather than
advised.

### Run the production architecture locally

Same images, API paths, token resolver, read-only root filesystems and private
volumes are exercised in local Compose before production. This kills the
failure mode that otherwise dooms the whole item: **the shape you never
exercise is the shape that breaks.** (The two-role hardcoding survived for
exactly this reason: there were only ever two roles.) Unit tests still use fake
principals, while integration tests prove that a real role token cannot read or
mutate another role's private projection. Production isolation must be in CI
from day one or it is fiction.

### The control plane gates coordination; receipts remove ambiguity

The earlier version of this section rejected an RPC writer because a lost ack
made "did it land?" unknowable. That diagnosis remains the acceptance test; the
0.4 command receipt protocol above is the answer. A role cannot open SQLite or
claim `role="beta"`; the server derives beta only from beta's token, and commits
the receipt with the mutation and event. Private provider state does not move
into this database merely because coordination does.

Two properties are now explicit instead of accidental:

- **"Never create both sides" is an authenticated-command fact.** Alpha's token
  cannot create beta's attendance, report or vote, even though the authoritative
  rows share a database.
- **Private state stops leaking.** Provider homes, credentials, transcripts not
  promoted to the shared feed, caches and journals are distinct role volumes.
  Shared presence and activity remain visible only through scoped projections.

There is no privileged *agent* spawner. Each role runner can launch only its own
provider seat; service principals can plan and schedule wakes but carry no role
and cannot speak in a meeting. The control process is privileged over
coordination and Git leases, which is why it receives neither provider
credentials nor a path to the host's side-effecting domain systems.

### Cost, stated honestly

The embedded mode still has **zero always-on processes**. The isolated mode
deliberately pays for one control process and N role runners plus supervision.
It also adds token rotation, backup, health checks, image provenance and a
network failure mode. That cost only pays when filesystem and identity isolation
have value; it is not hidden behind the simple local install.

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

### The supervisor boundary comes out with the application

(Moved here from P2, 2026-07-19.) `auth.py` owns verification, the nonce
ledger, and — since the console rework — mode selection and the access code,
with the web app asking rather than deciding. What still lives in the wrong
place is the **action-verb allowlist and its apply functions**: they sit
inside meetings, so today "what may a supervisor do" is answered by an
application. If the UI is swappable, every new UI re-implements whatever the
boundary doesn't own — and gets it wrong; this is not hypothetical: a host's
UI once held a live credential in its page source, and another re-implemented
the mode gate with its own fallback code minting, so the code the server
printed was not the code the verifier checked.

The reason this rides with P4 rather than standing alone: the verbs are
meeting verbs. Extract a `supervisor` module that owns the boundary — verbs,
claim-checking, nonce recording — as part of pulling meetings out of the
core, so applications *register* their supervisor verbs with the boundary
instead of embedding it. The web app stays a thin HTTP shell either way.

Meetings then remain as the reference application — still the most important one.
The difference is that the third collaboration shape inherits the guarantees
instead of copying the logic and missing a bound. That third shape is not
hypothetical: `mailbox_threads.kind` already discriminates two shapes today, and a
review workflow already grew alongside meetings.

### Status — the file boundary is drawn (2026-07-25)

`meetings.py` became the `meetings/` subpackage: a mechanical move mirroring
the orchestration split, not this extraction. Everything P4 owes is still
owed — the `bounds` and `integrity` primitives, the verb-registration API,
meetings actually leaving the core. What the split buys P4 is that the
supervisor boundary now has a file: `meetings/supervisor.py` holds exactly
the verb allowlist, the claim-checked adapter, and the uninvited join, and
nothing imports it — so extracting a generic `deskd.supervisor` becomes a
move rather than an untangling.

---

## Known structural debt

Found by a supervisor architecture review of `src/` (2026-07-26), recorded here
rather than fixed, because in each case the fix is either cheaper *after* P4 or
more expensive than the confusion it removes. An item earns its place on this
list by having a stated reason it is not being done now; when the reason expires,
it moves up into a numbered section or gets done.

### `mailbox.py` is now the largest single module (990 lines)

The meetings split broke a 2,248-line module into ten, and that promoted the
mailbox to the biggest file in the repo — the next candidate on size alone.

Size is the whole of the case, though, and the rest of it does not follow.
Cohesion here is genuinely good — threads, messages and receipts are one thing,
and the review workflow that grew alongside them reads the same rows — so a
split would buy an extra import graph and none of the clarity. What made the
meetings split worth doing was ten jobs tangled in one file. That is not this
file's shape.

More importantly, **the mailbox is the meetings substrate**. P4 pulls meetings
out of the core, and the seam it pulls along is the mailbox's — `bounds` and
`integrity` are extracted from exactly the transport that `mailbox.py` owns
today. Splitting the mailbox first would draw file boundaries against a shape
P4 is about to change, and then P4 would redraw them. So: not urgent, and
explicitly ordered after P4.

### Two clocks, two `store` modules

`orchestration/store.py` and `meetings/store.py` each own their subpackage's
schema, `connect()`, `_migrate()` and — the part that matters — their own
`_now`. Both also define `_iso`, `_clean`, `_agent_role` and `_known_roles`.
That is real duplication, and it reads like an obvious merge.

It is deliberate, and today it is load-bearing. The clock is the *single patch
point* for a subsystem's SLAs, and both are pinned by a test that says so
(`test_presence.py`'s clock, and
`test_meetings.py::test_the_meetings_clock_has_one_patch_point`). One merged
clock is one patch point that two subsystems fight over: a meetings test that
winds time forward 400s to fire an attendance timeout would also age every
heartbeat into `dead` and every delivery past its SLA, and the test would then
be asserting about a desk it did not mean to build. The migration split has
already drawn blood in the other direction — `meetings.closed_at` was briefly
migrated by orchestration's `_migrate`, which a meetings-only host never runs,
so `list_meetings` raised `no such column` on every pre-existing DB and only the
deployment shape nobody exercises broke.

A merge becomes *possible* when P4 moves meetings out of the core (they stop
being a peer layer and become a consumer with its own store, which is the same
split drawn at a package boundary instead of inside one) or when the `bounds`
and `integrity` primitives land and own the time-dependent invariants outright.
Until one of those, the duplication is documented — see
[`glossary.md`](glossary.md), *the two collisions* — and unifying it early would
buy tidiness with a shared mutable seam.

### Ten call-time `from . import views` upward imports

`meetings/lifecycle.py` (3), `meetings/messaging.py` (2) and
`meetings/termination.py` (5) each import `views` *inside* the function, because
`views` sits above them in the package layering and importing it at module level
would make the graph a cycle. The split's own docstring names this as the
compromise it made.

It is a compromise and not a lie — the module-level graph really is layered, and
the deferred import is greppable — but ten of them is a smell with a cause: every
protocol wrapper (`send_update`, `confirm_end`, `pause_meeting`, …) ends by
handing back `views.meeting_status(...)` for the caller's convenience, and that
one lightweight assembly is the only thing any of them wants from up there.

The fix is to sink `meeting_status` — or the small status assembly underneath it
— low enough that the wrappers stop calling up. That is not hard, but doing it as
its own change is churn across three files for no behavior, and it touches
precisely the functions P4's extraction rewrites when protocol and views land on
opposite sides of a package boundary. It rides with P4.

### The `escalation` name collision

`meeting_escalations` (a per-meeting queue: attendance timeouts, an urgent call,
consensus without the supervisor, a rejected end near the budget, an explicit
hand-off) and `wake_escalations` (the wake ladder's durable human-rung outbox,
retried for 24h) share a word and nothing else. `/escalations` shows both, which
is right — it is the "how does this desk reach a person" page — and is also
exactly where a reader first assumes they are the same ledger. The word is
further overloaded by the meeting/thread *state* `escalated` and the delivery
*state* `escalated`, which is not about humans at all.

Renaming is a public-API break: both `list_escalations()` and
`wake_escalations_recent()` are exported from their facades, the table names are
on disk in every host's database, and `/api/escalations` is consumed by the
console. That is a major-version change, and this collision is a documentation
problem long before it is a compatibility problem.

So the interim answer is [`glossary.md`](glossary.md), which pins each term with
its table, its trigger list, its retry behavior and its console view. If a rename
ever happens it rides a major version, with the glossary as the migration note.

---

## What this does not become

- **A distributed state system.** The optional control API crosses a container
  network, but one process still owns one SQLite WAL database. There is no
  leader election, replication, partition tolerance or multi-host execution
  claim. The differentiator is reliable activation with delivery proof;
  isolation is the goal, and a cluster would make that proof harder rather than
  stronger.
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
