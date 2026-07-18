# deskd

**An orchestration engine for multi-agent desks.** deskd owns the part that is
hard and domain-agnostic: knowing which agents are alive, what they're doing,
what's queued for them, and — the difficult bit — **reliably waking the right
agent at the right time and proving the message actually landed.**

Your agents do the domain work. deskd does everything else.

> Built for headless [Claude Code](https://claude.com/claude-code) agents
> (`claude -p` + a `PostToolUse` hook), but the engine is a plain Python package
> over SQLite with no hard dependency on any particular agent runtime.

---

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

## What you get

| | |
|---|---|
| **Presence** | One live session per role, enforced by a role-scoped `flock`. Heartbeats from the in-session hook; crash-safe (the kernel releases the lock). |
| **Unified inbox** | Every notification — alerts, signals, system events, meeting messages — lands in one queue per role, with per-key dedup so a re-firing alert never piles up. |
| **Wake orchestration** | Collect demand → route by presence → record the attempt → **verify the loop closed** → escalate. A wake that doesn't land climbs: in-session hook → resume → spawn → human (Discord/email) → a red badge on the supervisor console that never times out. |
| **Self-service wake hooks** | An agent registers its own wakes: `--at` (one-shot), `--every` (interval), `--cron` (calendar, DST-correct), or `--probe` (**your own watcher function** — return a dict and it wakes you). |
| **Delivery ledger** | Per message × recipient: `queued → notified → read`. Past SLA and unread with nobody reacting = **`overdue`** — surfaced red. Rows are a projection of durable messages, so a delivery can't be silently lost. |
| **Bounded meetings** | Multi-agent meetings with check-in/quorum, mandatory 1:1 replies with an SLA, message budgets, and a mutual termination handshake. Bounded by construction — no infinite agent chatter. |
| **Cross-session tasks** | Work items that outlive a session. Soft deadlines (`due_at`) sort to the top but **never wake anyone**; only `priority=urgent` does. |
| **Session lifecycle** | Intraday continuity, cross-day rollover: wind the old session down with a handoff, start fresh the next day. |
| **Supervisor console** | A web board (live status + queue + hooks + wake activity), a per-agent detail page with full execution history, and a meetings console — behind an access-code or Ed25519 trusted-device gate. |

## Install

```bash
pip install -e ".[web]"      # engine + web console
```

## Quickstart

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
before it touches the engine, so your roles are registered everywhere. Without it
a deskd process starts empty (no roles) and every role-scoped command is rejected.

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

### Agents schedule themselves — declaratively

```bash
# a calendar wake (weekday 06:15, in your configured tz)
deskd hook add --for operator --title "daily digest" --cron "15 6 * * *"

# your own watcher algorithm: return a dict -> it wakes you
deskd hook add --for operator --title "queue depth watch" \
  --probe myapp.watchers:queue_depth --every 600
```

```python
# myapp/watchers.py — a probe may observe and notify. Nothing else.
def queue_depth():
    n = measure()
    if n > 100:
        return {"title": f"queue at {n}", "priority": "urgent"}
    return None          # None = don't wake anyone
```

Three consecutive probe errors auto-disable the hook and notify its owner — a
broken watcher can't rot silently or stall the tick.

## Design notes

**Headless sessions can't be interrupted mid-turn.** So "deliver to the agent"
means two things: while it's running, its `PostToolUse` hook surfaces the queue
into context; while it's idle, the orchestrator resumes its session with the
queued items as the prompt. "Current session" = a *resumable session id*, not a
live process.

**Storage is SQLite (WAL) and it is the only source of truth.** No broker, no
daemon holding state. Every tick rebuilds its decisions from the DB, so a
crashed orchestrator self-heals on the next tick. SQLite can't wake a dormant
process — the engine doesn't pretend otherwise; it makes every wake attempt an
auditable row with a closed loop and an escalation path.

**Nothing here executes your domain.** The engine wakes agents and delivers
notifications. It never acts *as* an agent, and it has no path to your
side-effecting systems.

## Security

- The supervisor is **not** an agent role: agent APIs reject it, and supervisor
  actions only enter through the authenticated web adapter.
- `simple` mode = an access code (convenience, trusted host). `signed` mode =
  short-lived Ed25519 assertions from a trusted device; the public key path is
  fixed at `/etc/deskd/supervisor_ed25519.pub`, must be root-owned, and is
  deliberately **not** environment-overridable — an agent must not be able to
  point verification at a key it wrote. Keep the private key off the host.
- **Never hardcode the access code into a client/static file.** A pre-filled
  credential in page source *is* the credential. (Ask us how we know.)
- Probes only import from your explicit `probe_allowlist`. Empty = deny all.

See [`docs/security.md`](docs/security.md).

## Docs

- [`docs/design.md`](docs/design.md) — architecture and the decisions behind it
- [`docs/roadmap.md`](docs/roadmap.md) — where this is going, in dependency order
- [`skills/agent-orchestration/`](skills/agent-orchestration/) — a skill teaching an agent to operate and evolve a deskd desk

## License

MIT
