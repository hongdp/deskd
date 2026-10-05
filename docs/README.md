# deskd docs

Deliberately a handful of Markdown files, not a docs site: at this size an
index you can read in one screen beats navigation you have to maintain.

- [`design.md`](design.md) — architecture and the decisions behind it: the
  headless-turn constraint, SQLite as the only truth, the wake ladder, the
  delivery ledger, bounded meetings, and what deskd deliberately does not do.
- [`security.md`](security.md) — threat model, the supervisor boundary
  (`simple` / `signed` / `hybrid` / the `open`-mode surrender), probes, and
  one-session-per-role, plus role/service tokens and container isolation.
- [`control-plane.md`](control-plane.md) — the optional isolated deployment:
  authenticated principals, atomic command receipts, snapshot/SSE recovery,
  shared/private state, workspace leases and operational boundaries.
- [`gateway-foundation.md`](gateway-foundation.md) — experimental overlay
  primitives: root identity bindings, lifecycle fencing, durable outbox and
  consumer receipts.
- [`workspace.md`](workspace.md) — persistent seats, durable collaboration, shared status, independent management and recovery.
- [`workspace-console.md`](workspace-console.md) — paired human workspace for
  task assignment, two-way messages, independent review requests and results.
- [`local-workflow.md`](local-workflow.md) — runnable credential-free memo
  workflow, independent approval, offline responsibility board, fixed MCP bridge
  and memo-only Unix service; actual harness isolation remains unverified.
- [`local-workflow-install.md`](local-workflow-install.md) — two-service-user
  host preparation, read-only metadata preflight and outstanding release gates.
- [`container-deployment.md`](container-deployment.md) — the production runbook:
  immutable image provenance, UID/mount/network boundaries, secret projection,
  startup, backup, reconciliation, rolling upgrade and rollback gates.
- [`glossary.md`](glossary.md) — the vocabulary, and the two words that name two
  different things: `escalation` (a per-meeting queue **and** the wake ladder's
  human-rung outbox) and `store` (one module per subpackage, with two
  independent clocks). Read it before you conclude a guarantee applies.
- [`roadmap.md`](roadmap.md) — where this is going, **in dependency order**;
  each item says what it unlocks, what it must wait for, and which claims are
  still untested. Ends with the known structural debt and why each piece is
  not being fixed yet.
- [`tui.md`](tui.md) — the remote realtime terminal interface, its fast
  multi-agent command composer, HTTP/SSE contract, reconnect semantics and
  credential boundaries.
- [`images/`](images/) — console screenshots (light/dark pairs), captured from
  the seeded demo desk in
  [`examples/support_desk/`](../examples/support_desk/README.md) — never from
  a production desk.

One split worth naming: `docs/` is written for humans;
[`skills/agent-orchestration/`](../skills/agent-orchestration/) is the same
system documented for **agents** — a skill an agent loads to operate and
evolve a desk. When behavior changes, both must move.
