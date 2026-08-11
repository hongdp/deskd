# deskd docs

Deliberately a handful of Markdown files, not a docs site: at this size an
index you can read in one screen beats navigation you have to maintain.

- [`design.md`](design.md) — architecture and the decisions behind it: the
  headless-turn constraint, SQLite as the only truth, the wake ladder, the
  delivery ledger, bounded meetings, and what deskd deliberately does not do.
- [`security.md`](security.md) — threat model, the supervisor boundary
  (`simple` / `signed` / `hybrid` / the `open`-mode surrender), probes, and
  one-session-per-role.
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
