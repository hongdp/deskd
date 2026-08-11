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

The optional isolated deployment strengthens that assumption: role runners are
separate containers with private provider homes and credentials, no coordination
database mount, no Docker socket, a read-only root filesystem and a narrowly
scoped source mount. The control process is still a high-value component and
the design is intentionally single-host; a compromised kernel, container
runtime or control process is outside this boundary.

## Role and service identity

The control API's bearer identity is separate from supervisor authentication.
A role token maps to one configured role and receives only that role's private
projection. A service token has explicit `read`, `directive`, `orchestrator` or
`scheduler` scopes but no role, so it cannot attend, speak, vote or own a
provider session. Neither token form inherits supervisor authority.

Raw tokens are loaded from regular non-symlink secret files with private modes,
hashed in memory and never stored in SQLite. Put operator/TUI tokens in a
mode-0600 file or an OS secret store; do not place them in shell history, URLs
or Compose environment metadata. Bind the full supervisor console to loopback
unless its read projections are protected by an external trusted boundary.

Every command is idempotent under `(principal, request_id, request body)`.
Changing the body under an existing id is rejected. In-database commands commit
their receipt, mutation and event together. External commands use a durable
execution lease; if the system cannot prove whether a non-recoverable operation
ran, it reports `indeterminate` instead of replaying it.

## Workspace broker

An isolated agent never receives the shared repository's writable `.git`
metadata. The broker allocates one allowlisted worktree/branch per role and
task, validates the exact base SHA and version provenance, bounds output and
tree size, rejects nested `.git` entries and special files, disables hooks,
filters, text conversion, signing, credentials and network protocols, and owns
commit identity. Its public vocabulary deliberately omits fetch, push, merge,
checkout and reset. Container mounts must keep the lease parent and broker lock
outside the role's writable namespace.

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
- **hybrid** — accepts either a simple code or a signed assertion, for migrating
  from one to the other.
- **open** — **no credential at all: every caller that can reach the socket is
  authorized as the supervisor.** The boundary is disabled. This exists for a
  single trusted operator on a private host who has decided the access code adds
  nothing (on a same-user host it does not stop the host's own agents, which can
  read `.env`) — it is a deliberate surrender, never a default. `auth_mode()`
  defaults to `simple`; `open` happens only when `DESKD_SUPERVISOR_AUTH_MODE=open`
  is set explicitly, and the server prints a banner and shows a red console
  warning every time it runs this way. Do not enable it on a shared network.

Keep the private key off the agent host. No signing utility or key generator
belongs in this repo.

### Access codes

Never hardcode the code into a client or static file — a pre-filled credential
in page source *is* the credential, served to anyone who can reach the server.
`deskd serve` binds loopback by default, but binding a network interface is one
flag away and the page must stay safe either way. Use `sessionStorage`: enter
once per browser session. If a code was ever committed or served, **rotate it**.

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
