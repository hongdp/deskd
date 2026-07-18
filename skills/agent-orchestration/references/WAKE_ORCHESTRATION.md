# Wake orchestration

Why you never poll, how you get woken, and what to do when a wake doesn't land.

## The constraint everything follows from

A headless agent session runs **one turn and exits**. You cannot inject a prompt
into a turn that is already running. So "deliver to the agent" is two mechanisms:

- **Mid-turn (you're running)** — the `PostToolUse` hook surfaces new queue items
  into your context after a tool call. Passive; you act on it at your next
  natural boundary.
- **Between turns / dormant** — the orchestrator **resumes your session id** with
  the queued items as the prompt. This is the active path.

Therefore "your current session" means **a resumable session id**, not a live
process. Between wakes there is no process — that is normal, not a failure.

## Why you must never poll

Storage cannot wake a dormant process. An agent that "waits and checks again"
burns a turn to discover nothing, and still misses what mattered while it slept.
The orchestrator is the only thing that wakes agents, and it does so on evidence.

**Never** `sleep`, busy-wait, loop-until, or self-schedule. End your turn.

## What can wake you

| Demand | Source |
|---|---|
| `meeting_wake` | A meeting needs your check-in or reply |
| `stuck_delivery` | A message to you is past SLA and unread with nothing reacting |
| `urgent_task` | A task assigned to you with `priority=urgent`, still pending |
| `inbox` | Queued notifications (urgent → immediately; others batch) |
| your **hooks** | Timers/cron/probes **you** registered |

Note what is *absent*: a task's `due_at` **never** wakes anyone. Soft deadlines
sort to the top and are surfaced; only `priority=urgent` wakes.

See everything currently pointed at you, and how to change it:

```bash
deskd wake sources --role <you>
```

## The escalation ladder

A wake is not "fire and hope" — each attempt is a row, and the loop is verified.

| L | Channel | Meaning |
|---|---|---|
| 0 | `hook` | You're online — your in-session hook delivers. No spawn. |
| 1 | `resume` | Resume your existing session (keeps context). |
| 2 | `spawn` | No resumable session — start a fresh one (it rebuilds from the DB). |
| 3 | `human` | Discord/email — a person is now the wake path. |
| 4 | `supervisor_badge` | Terminal: a red badge on the console that never times out. |

Each rung has an SLA. If the demand isn't resolved in time, it climbs — the old
attempt is marked `superseded` and a new one is inserted (append-only, so the
full history of a wake is auditable). Resolution closes the attempt and records
the latency.

```bash
deskd wake list        # attempts: reason, channel/level, outcome, latency
```

## A wake that lands on a busy agent

If you're online but deep in work, the hook surfaces the new item and you handle
it **at your next natural boundary** — you do not abandon your current turn. So a
busy agent can take minutes to check into a new meeting. That is by design, not a
fault: the alternative is interrupting an agent mid-review.

The ladder still runs underneath; the per-role lock prevents anything from
double-driving your live session.

## Registering your own wakes

You own your hooks — add, extend, cancel on demand:

```bash
deskd hook add --for <you> --title "..." --at <ISO>              # one-shot
deskd hook add --for <you> --title "..." --every <secs>          # interval (>=60)
deskd hook add --for <you> --title "..." --cron "m h dom mon dow"  # calendar, your tz
deskd hook add --for <you> --title "..." --probe pkg.mod:fn --every <secs>
deskd hook cancel <id>
```

**Probe contract** — zero-arg function; `None`/falsy = stay quiet; a dict (or
list of dicts) fires a notification that wakes you:

```python
def my_watcher():
    if not condition():
        return None
    return {"title": "...", "priority": "urgent", "dedup_key": "..."}
```

Keep probes fast and read-only — they run inside the orchestrator tick, so a slow
probe delays every wake. Three consecutive errors auto-disable the hook and
notify you (a broken watcher must not rot silently). A probe may observe and
notify; it must never reach a side-effecting system.

Probes are importable only from the host's configured `probe_allowlist`.

## Anything that fires often and can block

Detection work that hits the network (fetching data to decide whether to alert)
does **not** belong in a probe — a slow fetch inside the tick stalls all waking.
Put it in its own scheduled process that enqueues to the inbox; the orchestrator
delivers. Detection and delivery stay decoupled.

## Diagnosing "why didn't X wake?"

1. `deskd wake sources --role X` — is there actually a demand?
2. `deskd wake list` — was an attempt made? what outcome/level?
3. `deskd delivery` — is a message `overdue` (past SLA, unread, nothing reacting)?
   That's the guarantee breaking, and it is deliberately visible.
4. Is a session already alive holding the role lock? Then the orchestrator chose
   L0 and is (correctly) letting the in-session hook deliver.
