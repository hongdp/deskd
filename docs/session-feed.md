# Session feed: the narration a headless turn produces while it works

**Status**: proposed + implemented on `engine/session-feed` (branch off
`board/live-tool-trace`), awaiting review.
**Motivation**: the supervisor asked to see an agent's working state and
intermediate reasoning as it happens, rather than only in the report at the end.

This is the second of two layers.

| layer | what it shows | shape | where |
|---|---|---|---|
| 1 — tool trace | the one thing the session is touching *now* | single row, UPDATE-only, hidden when stale | `agent_sessions.last_tool` |
| 2 — session feed (this) | the narration between tool calls, in order | append-only, capped ring per session | `session_feed` |

Layer 1 answers "is it alive and on what". Layer 2 answers "what did it say it
was doing, and why" — the sentences an agent writes between tool calls, which
in a headless run are otherwise written to a terminal nobody is attached to.

## What can actually be captured — measured, not assumed

The request that opened this task assumed the harness does not emit thinking at
all. That is nearly right, and the precise shape matters, so it was measured
against the real CLI (`claude -p … --output-format stream-json`, tools denied,
2026-08-09):

```
events    : rate_limit_event, system(init), assistant, result(success)
blocks    : {'thinking': 1, 'text': 1}
thinking  : text is ''            signature: 2880 chars
```

and again with `--include-partial-messages`:

```
stream_event: content_block_start:thinking
              content_block_delta:thinking_delta   x5   ← every payload ''
              content_block_delta:signature_delta  x1
              content_block_start:text
              content_block_delta:text_delta       x7   ← real narration
```

So the harness emits the **structure** of thinking — the block, five deltas, a
2880-character signature — and **redacts its content**. Both the completed
message and the partial deltas carry an empty string.

That is a sharper statement than "thinking is not available", and it changes
what the feature can honestly offer:

- **Not capturable**: what the agent thought. Nothing downstream can recover
  it; it is withheld at the source, not lost by us.
- **Capturable**: *that* it is thinking, *when* it started, and how long it ran
  — the deltas are real events with real timestamps even though their payloads
  are empty. A console can show `thinking…` truthfully and live.
- **Capturable in full**: the visible narration (`text` blocks), which is the
  part a human actually reads.

The feed therefore stores narration verbatim and thinking as a **marker with a
duration, never as content**. A row of kind `thinking` whose text is empty is
not a bug to be fixed later; it is the honest representation of a redacted
signal, and the schema comment says so, so that nobody "fixes" it by inventing
a summary.

## Schema

```sql
CREATE TABLE session_feed (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL,
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,   -- per session, monotonic, gap-free
    kind       TEXT NOT NULL,      -- 'narration' | 'thinking' | 'note'
    text       TEXT NOT NULL DEFAULT '',
    at         TEXT NOT NULL
);
```

`seq` is per-session rather than global so a reader can tell "I have everything
up to 41" without holding a cursor into a shared sequence, and so trimming old
rows never renumbers the ones that remain.

**Retention is a ring, per session.** `FEED_MAX_ROWS_PER_SESSION` (500) is
enforced on write: the oldest rows for that session are deleted once the count
exceeds it. A long turn keeps its most recent 500 entries. The bound is per
session and not global, so a chatty session cannot evict a quiet one's history.

## Why the writer is best-effort

`feed_append` follows layer 1's rule: it must never break the session it is
describing. A telemetry write that raises inside a driver loop would turn an
observability feature into an outage, so failures are swallowed. The cost is
that a feed can silently be incomplete — which is acceptable *only* because the
feed is never the system of record. Nothing decides anything from it; the
report, the journal and the task ledger remain authoritative.

## Where the stream is read

`run_agent` currently calls `subprocess.run(command)` and does not read the
child's stdout at all. Streaming changes that to `Popen(stdout=PIPE)` with a
line loop that:

1. **forwards every line to our own stdout unchanged**, so existing consumers
   (a human tailing the cron log, the wake orchestrator's log capture) see
   exactly what they saw before, and
2. parses each line as JSON, ignoring anything it does not recognise.

Point 1 is the compatibility contract: turning capture on must not change what
anybody already reads. A line that is not JSON, or is JSON of an unknown shape,
is forwarded and otherwise ignored — a new harness event type must never crash
a turn.

## Providers opt in

`Provider.streams` defaults to `False`. A provider that sets it True is
promising that its `command()` produces newline-delimited JSON on stdout in the
Claude Code stream shape. `CommandProvider` leaves it False — an arbitrary CLI
template has no such contract — so it degrades to exactly today's behaviour
rather than having its output parsed as something it is not.

`ClaudeCodeProvider` gains `stream: bool = False`, off by default: enabling it
changes the child's argv, and a provider that silently altered how the agent is
launched would be a surprising default. A host turns it on by registering
`ClaudeCodeProvider(stream=True)`.

## What this deliberately does not do

- **No summarisation.** The feed stores what was said. Nothing infers intent,
  and no thinking content is reconstructed, because there is none to
  reconstruct.
- **No new wake path.** The feed never wakes anyone; it is read by a human
  looking at a console.
- **Not a transcript.** The harness already writes its own; this is the
  narrow, queryable slice the desk surfaces.
