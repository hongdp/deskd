# deskd

**Wakes your agents. Proves the message landed.**

[![PyPI](https://img.shields.io/pypi/v/deskd)](https://pypi.org/project/deskd/)
[![Python](https://img.shields.io/pypi/pyversions/deskd)](https://pypi.org/project/deskd/)
[![tests](https://github.com/hongdp/deskd/actions/workflows/ci.yml/badge.svg)](https://github.com/hongdp/deskd/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

deskd never runs your agents. It orchestrates everything between their turns:
who's alive, what's queued, which message nobody read — and, the difficult bit,
**reliably waking the right agent at the right time and proving the wake landed.**

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/board-escalation-dark.png">
  <img alt="The deskd console board mid-escalation: three agent columns with live presence, queues, and a health strip; the unroutable-demands chip, the at-human-level tile, and the Auditor's waking-L3 chip are red" src="docs/images/board-escalation-light.png">
</picture>

*Red is the guarantee breaking, on purpose: a demand no role can take
(`unroutable demands: 1`), a person being pulled in (`at human level: 1`), and
the Auditor — presumed offline, its wake ladder at `waking L3`.*

## Why

Multi-agent systems usually rot in the same three places:

1. **Agents poll.** Every agent runs its own `sleep`/wake loop, burning tokens
   to discover there's nothing to do — and still missing the thing that mattered.
2. **Messages vanish.** "I sent it" ≠ "they read it." Nothing distinguishes
   *notified* from *read*, so a stuck message is invisible until someone notices
   hours later.
3. **Nobody knows what's running.** Two sessions of the same role stomp each
   other; a crashed agent looks identical to an idle one.

deskd's position: **agents must never manage their own waking.** They end their
turn and the orchestrator wakes them — on a timer, on a calendar, on a custom
watcher, on a message, or on an escalation ladder when a wake doesn't land.

And one boundary holds everywhere: **the supervisor is not an agent.** Human
authority enters only through an authenticated adapter — agents cannot claim
it, fake it, or vote with it.

## 60 seconds to a live board

```bash
pip install "deskd[web]"                     # the engine + the web console
git clone https://github.com/hongdp/deskd    # the demo ships in the repo, not the wheel
cd deskd
python -m examples.support_desk.run_demo
```

Open **<http://127.0.0.1:8913/board>** next to the terminal. Three scripted
agents run an overnight helpdesk for ~2.5 minutes — **no LLM, no API keys**:
tickets land, a bounded meeting settles the response plan, then the auditor
goes dark with an urgent audit outstanding, and you watch the wake ladder climb
past every machine rung and page a person. Every move goes through deskd's
public API; if a plain Python loop can play every part, the orchestration you
are watching is all engine. Beat-by-beat script:
[`examples/support_desk/README.md`](examples/support_desk/README.md).

### Wire your own desk

Describe your desk in a module that defines `configure_deskd()`:

```python
# myapp/desk.py
from deskd.config import RoleSpec, PromptBuilder, configure

class MyPrompts(PromptBuilder):
    def bootstrap(self, role: str) -> str:
        return f"Load the myapp skill, declare role={role}, follow its playbook."

def configure_deskd():                        # deskd calls this at startup
    configure(
        roles=(
            RoleSpec("researcher", "Researcher", ("research", "review")),
            RoleSpec("operator",   "Operator",   ("execution",), {"can_execute": True}),
        ),
        timezone="America/New_York",
        inbox_sources=("alert", "signal", "system", "meeting", "supervisor"),
        probe_allowlist=("myapp.watchers",),   # empty = no probes may run
        prompt_builder=MyPrompts(),
    )
```

Point deskd at it with **`DESKD_CONFIG_MODULE`**. Every deskd process — the CLI,
`deskd serve`, the cron driver — imports that module and calls `configure_deskd()`
before it touches the engine. Without it a deskd process starts empty (no roles)
and every role-scoped command is rejected.

```bash
export DESKD_CONFIG_MODULE=myapp.desk         # (myapp must be importable — on PYTHONPATH)

deskd serve                                   # supervisor console on 127.0.0.1:8000
deskd status set --role operator --activity "watching the queue"
deskd inbox enqueue --for operator --source alert --title "threshold crossed" --priority urgent
deskd wake sources --role operator            # what can wake me, and how to change it
```

Wake the desk from cron (the driver is the **only** thing that spawns sessions):

```cron
# cron has its own environment — set both vars on the line (or in the crontab header)
* * * * * DESKD_CONFIG_MODULE=myapp.desk DESKD_WAKE_EXECUTE=1 /path/to/deskd/scripts/cron/wake_orchestrator.sh
```

It is **dry-run by default** — schedule it, watch the log, then set
`DESKD_WAKE_EXECUTE=1` when the decisions look right.

## Architecture

One tick: collect demand, decide (pure), execute, verify the loop closed,
escalate when it didn't.

```mermaid
flowchart LR
    subgraph demand["Demand sources"]
        direction TB
        A["meeting wakes"]
        B["stuck deliveries"]
        C["urgent tasks"]
        D["owed replies"]
        E["inbox notifications"]
        F["idle agent's own queue"]
        G["self-registered hooks<br/>(at / every / cron / probe)"]
    end
    demand --> P["plan_wakes()<br/>pure: decides and records,<br/>never spawns anything"]
    P --> DRV["driver<br/>the only thing that<br/>spawns sessions"]
    DRV --> L["escalation ladder<br/>in-session hook → resume → spawn<br/>→ human channel → red badge<br/>(terminal rung never times out)"]
    DB[("SQLite, WAL<br/>the only source of truth")]
    P --- DB
    DRV --- DB
    L --- DB
```

Storage is SQLite (WAL) and it is the only source of truth — no broker, no
daemon holding state. Every tick rebuilds its decisions from the database, so a
crashed orchestrator self-heals on the next tick. SQLite can't wake a dormant
process, and the engine doesn't pretend otherwise: it makes every wake attempt
an auditable row with a closed loop and an escalation path.

Delivery is a ledger row per message × recipient:

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> notified: wake attempt lands
    notified --> read: in-session hook / explicit ack
    notified --> escalated: past SLA, the ladder is reacting
    notified --> overdue: past SLA, nothing is reacting — red
    escalated --> read
    overdue --> read
```

Past-SLA state is computed at read time, never stored, so it can't go stale —
and a ledger row is a projection of a durable message, so a delivery can't be
silently lost.

## What you get

| | |
|---|---|
| **Presence** | One live session per role, enforced by a role-scoped `flock`. Heartbeats ride the in-session hook; crash-safe (the kernel releases the lock). |
| **Unified inbox** | Every notification — alerts, signals, system events, meeting messages — lands in one queue per role, with per-key dedup so a re-firing alert never piles up. |
| **Capability routing** | Address work to a capability, not a name: `deskd inbox route` reaches any enabled role declaring it. A demand nobody can take lands in `unroutable demands` — red on the board, re-routed the moment a qualifying role exists. |
| **Wake orchestration** | Collect demand → route by presence → record the attempt → **verify the loop closed** → escalate. A wake that doesn't land climbs: in-session hook → resume → spawn → a human channel **you wire** (the engine ships none) → a red badge on the supervisor console that never times out. |
| **Self-service wake hooks** | An agent registers its own wakes: `--at` (one-shot), `--every` (interval), `--cron` (calendar, DST-correct), or `--probe` (**your own watcher function** — return a dict and it wakes you). Three consecutive probe errors auto-disable the hook and notify its owner. |
| **Delivery ledger** | Per message × recipient: `queued → notified → read`. Past SLA and unread with nobody reacting = **`overdue`** — surfaced red. |
| **Bounded meetings** | Check-in/quorum, mandatory 1:1 replies with an SLA, message budgets, and a mutual termination handshake. Bounded by construction — no infinite agent chatter — and no agent can fabricate the other side's attendance, reports, or votes. |
| **Cross-session tasks** | Work items that outlive a session. Soft deadlines (`due_at`) sort to the top but **never wake anyone**; only `priority=urgent` does — and a task wake can never reach a human rung. |
| **Session lifecycle** | Intraday continuity, cross-day rollover: wind the old session down with a handoff, start fresh the next day. |
| **Supervisor console** | Six views over one shell: the board (every number links to the view that explains it), per-agent detail (wake history, delivery ledger, full event log), meetings, the wake ladder, escalation ledgers, and tasks & hooks — behind an access-code or Ed25519 trusted-device gate. |

## Works with headless Claude Code

Built for [Claude Code](https://claude.com/claude-code) agents (`claude -p`),
tied to no runtime. A headless session runs one turn and exits — you cannot
inject a prompt into a turn that is already running — so "deliver to the agent"
is two mechanisms: while a turn is running, [`scripts/session_hook.py`](scripts/session_hook.py)
(a `PostToolUse` hook) surfaces the queue into context and heartbeats presence;
while it's idle, the driver resumes its session id with the queued items as the
prompt. [`skills/agent-orchestration/`](skills/agent-orchestration/) teaches an
agent to live on a desk: declare status, register hooks, meet, hand off. The
engine itself is a plain Python package over SQLite with no hard dependency on
any particular agent runtime.

## How it compares

LangGraph, CrewAI, and AG2 orchestrate what happens **inside** a running
process: the framework runs your agent code and manages its state. deskd never
runs your agents — it orchestrates **between** turns of processes that exit,
which is why it composes with those frameworks instead of replacing them (a
LangGraph app can be one deskd role). Temporal gives you durable workflow
**steps**, at the cost of a server and a worker fleet; deskd gives you durable
**activation** — a ladder that escalates until a wake provably lands, with
per-recipient delivery receipts — on one SQLite file and a cron tick. And what
deskd deliberately does not do: run agents, in-graph control flow, streaming,
memory, RAG, distribution. It guarantees at-least-once with idempotent acks,
not exactly-once, and it ships zero human-notification channels — the human
rung of the ladder is yours to wire. If you want durable workflow steps and can
run a cluster, use Temporal; if you want an in-process agent graph, use
LangGraph. deskd covers the part both leave open: the time when your agent
doesn't exist.

## The console

The meetings view, live: the budget and the reply SLA are visible state, and
the force-close button is a supervisor action — it exists only as an
authenticated request, which is why the demo's agents can't press it.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/meeting-live-dark.png">
  <img alt="The meetings console: an active one-to-one meeting with 11 messages left on the budget, a 30-second reply SLA counting down against the writer, the transcript, and a supervisor force-close control" src="docs/images/meeting-live-light.png">
</picture>

Per-agent detail: everything the engine knows about one role — what can wake
it, the ladder's history rung by rung with outcomes and latency, its delivery
ledger, and the raw orchestration event log.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/agent-auditor-dark.png">
  <img alt="The Auditor's agent-detail page after an outage: wake history showing spawn L2 and human-channel L3 superseded and supervisor badge L4 pending, the in-flight wake attempt chip, task history, and the orchestration event log" src="docs/images/agent-auditor-light.png">
</picture>

## Security

- The supervisor is **not** an agent role: agent APIs reject it, and supervisor
  actions enter only through the authenticated web adapter — `simple` (access
  code), `signed` (short-lived Ed25519 assertions from a trusted device), or
  `hybrid`.
- In `signed` mode the public key path is fixed at
  `/etc/deskd/supervisor_ed25519.pub`, must be root-owned, and is deliberately
  **not** environment-overridable — an agent must not be able to point
  verification at a key it wrote. Never hardcode an access code into a
  client/static file: a pre-filled credential in page source *is* the
  credential. (Ask us how we know.)
- Probes only import from your explicit `probe_allowlist`. Empty = deny all.

Threat model and the `open`-mode surrender: [`docs/security.md`](docs/security.md).

## Status

0.1.x, alpha. deskd runs one production desk daily — an automated trading desk
with a real brokerage account — and is validated by exactly one host, which
proves it works, not that it's general. The roadmap is written in dependency
order and says which claims are untested; if you're building a desk that is
deliberately unlike a trading desk, you are the second host we're looking for.

## Docs

- [`docs/design.md`](docs/design.md) — architecture and the decisions behind it
- [`docs/security.md`](docs/security.md) — threat model and the supervisor boundary
- [`docs/roadmap.md`](docs/roadmap.md) — where this is going, in dependency order
- [`examples/support_desk/`](examples/support_desk/README.md) — the runnable demo
- [`skills/agent-orchestration/`](skills/agent-orchestration/) — a skill teaching an agent to operate and evolve a deskd desk

## License

MIT
