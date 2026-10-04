# Installing an isolated workspace

The workspace installation planner produces an **inert plan** for the official
Codex 0.160.0 Linux x64 executable. It creates no users, copies no credentials,
changes no services and starts no daemon. It pins the executable SHA-256 to
`12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad`.
The npm archive integrity is independently fixed in the isolation workflow.

An administrator owns the installation prefix, executable, role roots and their
`.codex` policy directories. Only each role's `data` child is writable. The
shared official daemon runs under a non-root harness UID. The action gateway
uses a different non-root UID; management belongs to the separate administrator
trust domain. A shell belonging to a role must not be able to connect to either
the gateway business socket or the daemon control socket. The harness's approved
bridge process can access the business socket outside the role shell sandbox.

## Render and review

`deskd.workspace.installation.Installation` accepts a protected absolute prefix,
explicit service IDs and two to eight `RoleInstallation(seat, root, data)` values.
Its `plan()` result lists directories, owner IDs, exact modes, configuration
contents, executable digest and the daemon command/environment. `configuration()`
also renders a loopback-only provider when given `mock_port`; that provider is
only for acceptance and has no API key. Rendering never reads an existing
credential or user configuration.

`plan(with_gateway_bridge=True)` renders a complete fixed stdio MCP transport
for the protected `bin/deskd-bridge` wrapper: it is disabled in the base layer
and enabled and required in each role's immutable project layer. A missing
project layer therefore provides no bridge. The wrapper receives only the
installation's fixed business socket and gateway UID. The CLI bundle renderer
must supply this executable; the generic plan does not install it. Each emitted
configuration includes its own SHA-256 for the manager's artifact checks.

The generated profiles permit minimal system reads, their own role root and
writes only to their own data. Other roles, harness state, gateway state, shared
temporary state and daemon rendezvous paths are explicitly denied. Command
networking is disabled. Browser/computer integration, plugins, apps, model
discovery, nested agents, shell snapshots, JavaScript REPL and code mode are
turned off in the baseline. The `codex_tui` namespace is reserved with a disabled
stub. Enabling any of those execution surfaces needs its own isolation review.

The official daemon requires writable `CODEX_HOME` state. Its root-owned
`config.toml` is inside that harness-owned state directory, so the **trusted
harness can replace it**; a role cannot. Root-owned role policy and immutable
ancestors prevent a role from replacing its own project layer. Installation
metadata checks alone do not attest the daemon's effective configuration. A
manager must compare the protected plan, actual runtime digest, role cwd,
active named profile, root identity and allowed tools before granting gateway
leases, and repeat this after every restart or policy change.

## Executable acceptance

The `Workspace official runtime isolation` workflow uses a fresh GitHub-hosted
Ubuntu 22.04 VM. It runs the fixed official artifact as a non-root UID, with a
fresh home and Codex home, and a local Responses mock that emits deterministic
shell probes. A private mount namespace binds the job's scratchpad over `/tmp`
so stock daemon rendezvous files stay inside that disposable tree. No AppArmor
policy, sysctl, account or existing host service is changed.

The acceptance checks two roots under the same harness UID, before and after
restarting only its own daemon. Each role must write its own data while failing
to read or write the other role, read a synthetic gateway secret, replace policy, create a shadow project configuration
or replace a role ancestor, create a cross-role hardlink, connect to gateway/daemon Unix
sockets, or reach the loopback model server from its shell. There is no
unrestricted fallback. Missing kernel support, no actual shell invocation, a
missing receipt, a failed check or a skipped test fails the privileged job.

A normal developer test run skips this opt-in acceptance. A green unit test run
therefore does not establish kernel isolation. The separate gateway UID workflow
checks the real gateway/bridge peer boundary. Neither workflow proves complete
credential readiness: a deployment still needs executable/dependency inventory,
control-plane authorization, exact channel/root binding, recovery, and the
remaining configured tool surfaces reviewed together.

## Deployment procedure

Use a dedicated Linux machine with working unprivileged user namespaces. Review
the rendered plan and create its accounts/directories through normal local
administration. Verify the official executable against the pinned digest and
install the deskd package and its dependencies in an administrator-controlled
location. Provision synthetic data first and run acceptance. Never copy a
personal Codex home into a service home.

Keep the gateway fenced until the manager validates the running daemon and
binds each stable role to one root session. Stop granting leases before restart,
configuration replacement or recovery; a restarted daemon does not itself prove
that a persisted role still has the approved effective permissions. Do not
expose management endpoints to a role, and keep the shared board observational.
Model credentials and live broker adapters remain outside the mock acceptance
and require their separately reviewed deployment flow.
