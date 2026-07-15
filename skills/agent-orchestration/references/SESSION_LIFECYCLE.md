# Session lifecycle

## One session per role — always

Every path that can start a session for a role (a scheduler, the wake driver, a
host runner) takes the **same role-scoped `flock`** (`/tmp/deskd-role-<role>.lock`).
If it's held, the second starter skips. Two sessions of one role would conflict:
same meeting identity, same knowledge transactions, same journal.

The lock is kernel-held on an fd, so a crashed/killed/OOM'd session **releases it
automatically** — no stale lock ever blocks a future session.

Caveat: the lock only coordinates the automated starters. A manually launched
session that bypasses them holds nothing. Don't do that while automation is live.

## Presence

`agent_sessions` is keyed by **role** (one row), reflecting the one-session-per-role
invariant. Liveness is derived from the heartbeat age:

| liveness | meaning |
|---|---|
| `online` | heartbeat < online_max_seconds |
| `suspect` | heartbeat aging — may be mid-long-turn |
| `dead` | heartbeat stale past suspect_max_seconds, never ended |
| `offline` | cleanly ended (`ended_at` set) |
| `never` | no session has registered |

The in-session hook refreshes the heartbeat automatically (role from `DESKD_ROLE`).
Registration is explicit — `deskd status set` — so a non-desk session never
pollutes the board.

Between wakes the process is gone and the heartbeat stops. `suspect`/`dead` for an
idle-but-resumable role is expected, not an incident.

## What the board shows as "now executing"

Only for a **live** session: your in-progress TodoWrite items, else your reported
`activity`. An ended session shows "session ended" — never a dead session's stale
activity presented as current work. Its `session_todos` are cleared on end,
because a work breakdown belongs to the session that made it.

Keep a TodoWrite list. It is the truest signal of what you're doing.

## Cross-day rollover

Each session is stamped with the trading/working **day** it belongs to (in the
configured timezone) and a phase (`active` → `draining` → `closed`).

On the first tick of a new day, a session left over from a prior day is detected,
marked `draining`, and driven through a wind-down: finish, write a **handoff note**
(open items / first thing tomorrow) at the end of that day's journal, commit any
generalizable lesson, then end. The next day opens a **fresh** session that reads
the handoff.

Why fresh rather than one long-lived session:

1. Context grows monotonically; auto-compaction drops detail unpredictably and
   behaviour degrades.
2. It forces durable state — anything worth keeping must be in the DB, a task, the
   journal, or the knowledge base, not in a session's memory.
3. A fresh session picks up instruction changes; a long-lived one never does.
4. Fault isolation — a session that went sideways doesn't carry into tomorrow.

Continuity across the boundary = **tasks + journal + knowledge base + the handoff
note**, never conversational memory.

Sessions with no recorded day (pre-migration rows) are never rolled.
