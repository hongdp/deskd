# Security model

## Threat model

deskd assumes agents are **capable but fallible**, and that code running as the
same OS user is *not* isolated from the engine. It defends against:

- an agent accidentally or deliberately acting as the **supervisor**;
- an agent fabricating another agent's attendance, reports, or votes;
- a wake/notification being silently lost;
- two sessions of one role corrupting shared state;
- a custom watcher (probe) reaching beyond observe-and-notify.

It does **not** claim to defend a `simple`-mode desk against malicious code
running as the same user (see below).

## The supervisor boundary

The supervisor is a human, not a role. Agent-facing APIs reject it. Supervisor
actions enter only via the authenticated web adapter, and every accepted action
records its auth mode and a single-use nonce.

- **simple** — an access code compared with `hmac.compare_digest`. A convenience
  boundary for a trusted host. It prevents accidental impersonation through
  supported commands. It is **not** cryptographic isolation from hostile code on
  the same host.
- **signed** — short-lived (≤10 min) Ed25519 assertions from a trusted device.
  The public key path is fixed at `/etc/deskd/supervisor_ed25519.pub`, must be
  root-owned and not group/world-writable, and is **not** environment-overridable —
  otherwise an agent could point verification at a key it wrote. Nonces are
  single-use; expiry, tampering, wrong action/content, and replay all fail closed.

Keep the private key off the agent host. No signing utility or key generator
belongs in this repo.

### Access codes

Never hardcode the code into a client or static file — a pre-filled credential
in page source *is* the credential, served to anyone who can reach the server
(which binds all interfaces by default). Use `sessionStorage`: enter once per
browser session. If a code was ever committed or served, **rotate it**.

## Probes

A `probe` wake-hook imports a dotted path from the host's `probe_allowlist`
(empty = deny all) and is validated at registration. A probe may **observe and
notify**; it must never reach a side-effecting system. Three consecutive errors
auto-disable it and notify its owner, so a broken watcher can neither rot
silently nor stall the tick.

## One session per role

Every starter takes the same role-scoped `flock`. The kernel releases it on
crash, so there are no stale locks. The lock coordinates automated starters only —
a hand-launched session bypassing them holds nothing.

## Engine has no domain path

deskd wakes agents and delivers notifications. It never acts *as* an agent and
has no route to your side-effecting systems. Whatever authority your agents have
is enforced by your host application, not by the engine.

## Reporting

Please open a private security advisory rather than a public issue.
