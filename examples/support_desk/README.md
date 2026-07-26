# The overnight helpdesk — a runnable deskd demo

![The board mid-escalation: the auditor went dark, the ladder climbed to the
human rung, and the console says so out loud](../../docs/images/board-escalation-dark.png)

Three scripted agents run a tiny customer-support desk for one night:
**Triage** (capability `triage`), **Writer** (`respond`), and **Auditor**
(`audit`). Tickets land, a bounded meeting settles the response plan — and
then the auditor goes dark with an urgent audit outstanding, so you get to
watch the wake ladder climb past every machine rung and page a person, on
camera, in real wall-clock time.

**No LLM, no API keys.** The engine never executes agent code; the "agents"
here are ~100 lines of scripted state machine (`agents.py`), and every move
they make goes through deskd's public API. That is the point of the demo:
if a plain Python loop can play every part, the orchestration you are
watching is all engine.

## Quickstart

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[web]"                 # console needs the web extra
python -m examples.support_desk.run_demo
```

Then open <http://127.0.0.1:8913/board> next to the terminal. ~2.5 minutes,
narrated. The demo wipes and recreates `./demo-desk.db` on every run
(`--keep-db` to opt out), so it is safe to re-run for screenshots.

## What to watch for

| Beat | On the console | The engine guarantee |
|---|---|---|
| 1 — the desk comes up | Three dots go green with live activity lines; watch the Auditor's heartbeat age start climbing once it goes quiet | Presence is **derived from heartbeat age** — agents never poll, liveness cannot go stale |
| 2 — tickets land | Red `[urgent]` item on Triage; a routed item on Writer; `unroutable demands: 1` in the health strip | One inbox for everything; **dedup** by key; **capability-addressed routing** — an unroutable demand is recorded red, never dropped |
| 3 — a bounded huddle | Transcript grows; the owed-reply SLA panel fills and clears; the meeting closes with a resolution | **Bounded meetings**: quorum, per-message read receipts, reply obligations with SLAs, message budget, unanimous termination handshake |
| 4 — the auditor went home | Auditor's dot decays online → suspect → presumed offline; "waking L2" then L3 chips; a boxed **PAGE** prints in the terminal | Demand → attempt → **closed-loop verification** → append-only escalation ladder → durable ledger row, whether or not any channel worked |
| 4b — the pager dies | `undelivered escalations` and `human rung unwired` light up red | The ledger is not the transport: the page is **parked durably** and retried the moment a channel returns |
| 5 — a human steps in | Supervisor message + force-close appear in the meeting; wake chips clear with latency numbers | Supervisor authority enters **only** through the authenticated web adapter; every wake resolution records its latency |

Also deliberate: the `legal-review` demand stays red to the end (no role
declares that capability), and the auditor's ordinary task queue never pages
anyone — `idle_task` wakes are fenced to machine rungs by construction; only
the *urgent* task was allowed to climb to a person.

## Watching from a second terminal

The demo module doubles as a `DESKD_CONFIG_MODULE` target, so the plain
`deskd` CLI can inspect the running desk (read-only commands; leave `wake
tick` to the demo, which is the driver):

```bash
PYTHONPATH=. DESKD_CONFIG_MODULE=examples.support_desk.desk \
    DESKD_DB=demo-desk.db deskd status show
```

## Playing the supervisor (Beat 5)

The demo pins the supervisor access code to `letmein-demo` for
reproducibility (export `DESKD_SUPERVISOR_ACCESS_CODE` to override). Agents
cannot mint this — a supervisor action only exists as an authenticated POST:

```bash
curl -s -X POST http://127.0.0.1:8913/api/meetings/supervisor-action \
  -H 'Content-Type: application/json' \
  -H 'X-Deskd-Supervisor-Code: letmein-demo' \
  -d '{"payload": {"action": "join", "meeting_id": "<id from the terminal>"}}'
```

then the same shape with `"action": "send"` (+ `"body"`) and
`"action": "force_close"` (+ `"reason"`). Run with `--auto-supervisor` to
have the script do this itself (screen recordings, CI).

## Flags

```
--port N            console port (default 8913)
--speed S           scale beats AND ladder; sets DESKD_DEMO_FAST (default 1)
--keep-db           keep the previous run's database
--pause-at NAME[:S] hold the story for screenshots (board, meeting,
                    escalation, outage)
--auto-supervisor   script Beat 5 instead of inviting you
--exit-when-done    quit after the last beat (default: keep serving)
```

Speed note: `DESKD_DEMO_FAST=1` is the on-camera profile (ladder rungs
5–10s, presence online < 8s); any float scales it (the smoke test runs at
`0.25`). Unset it and the same module configures production-speed defaults.
Engine floors are respected either way — the scenario never depends on
meeting wait-timeouts (min 30s) or recurring hooks (min 60s).

## Why a single process?

`CONFIG` and the channel registry are process-local. The console, the
scenario driver, and the pager channel share one interpreter so the board's
channel/health gauges tell the truth (a separate `deskd serve` process would
report the human rung unwired while this process pages happily). Hence
`uvicorn.Server` in a daemon thread — never `uvicorn.run()` off the main
thread, which would try to install signal handlers.

## Screenshots

`examples/support_desk/capture.py` re-runs the demo headlessly and captures
the console into `docs/images/` (board, meeting, escalation peak, agent
detail) using the `playwright` CLI. See the module docstring for setup.
