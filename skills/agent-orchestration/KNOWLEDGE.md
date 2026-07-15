# Orchestration knowledge

Generalizable method/process/risk lessons for running a deskd desk. **Never edit
this file by hand** — use `scripts/knowledge_txn.py` (see
`references/EVOLUTION_PROTOCOL.md`). Domain-specific facts do NOT belong here.

Keep under ~150 lines. Every entry: a date and an evidence line. An entry
contradicted by later evidence gets corrected or removed, not accumulated
alongside.

## Delivery & waking

- **2026-07-14** — Never mark a message "delivered" at planning time. Delivery is
  proven only by the in-session hook surfacing it or an explicit ack; a plan is
  speculative and its execution can be skipped or fail. _Evidence: marking at plan
  time silently lost items whenever the driver skipped a spawn (role lock held),
  and simultaneously suppressed the escalation that would have caught it._
- **2026-07-14** — Scope "is this being handled?" checks to the exact
  (recipient, item), never to the container. _Evidence: a thread-level escalation
  flag from one role masked every other role's stuck message on that thread
  forever, so a genuinely stuck message never produced a wake demand._
- **2026-07-14** — A blanket "ack everything" must only ack what was actually
  delivered; items that arrived during processing were never seen. _Evidence:
  one-shot notifications enqueued inside the processing window were silently
  dropped by an unqualified ack._

## Scheduling & isolation

- **2026-07-14** — Any work that can block (network fetch) must run in its own
  process, never inside the orchestrator tick. _Evidence: a slow fetch inside the
  tick stalls every wake behind the single-instance lock._
- **2026-07-14** — Key mutual-exclusion locks by the **role**, not by the job
  name. _Evidence: a weekly job running as the same role used a different lock
  name and could run concurrently with that role's other session._
- **2026-07-14** — A dry run must mutate nothing — no recorded attempts, no
  advanced timers. Otherwise "preview" silently changes the state it previews.

## Credentials

- **2026-07-14** — A pre-filled credential in a client/static file **is** the
  credential, and "it only matters if it equals the real one" is exactly the
  condition under which the convenience works. _Evidence: an audit found the
  hardcoded default equalled the live access code, i.e. the page served the real
  credential in its source to anyone who could reach the host._
