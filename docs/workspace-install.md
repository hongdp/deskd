# Installing an isolated workspace

`python -m deskd.workspace install` installs a fresh, protected workspace with
three seats: analyst, trader and engineer. `up` starts and supervises its gateway,
official Codex daemon and controller. It uses native Linux processes, two service
UIDs and per-role filesystem/network permissions; no container runtime is needed.

The implemented external effect is a durable mock memo. The workflow supports a
trader proposal, independent authorization by another principal, and idempotent
execution. It does not implement a live broker or submit an order.

## Prerequisites and artifact pins

Use a dedicated Linux x64 host with working unprivileged user namespaces and
Python 3.11 or newer. An administrator must provision two distinct, dedicated
non-root service UIDs and a business GID. Reserve these identities for this
workspace. The installer never creates accounts, adopts another daemon, imports
a personal Codex home or changes an existing service.

The Python interpreter, its environment and deskd package must be administrator
controlled, with no group/world-writable ancestors. The fresh installation prefix
must also have protected ancestors; installation refuses an existing prefix. The
installer copies the workspace/gateway code and fixed bridge/auth wrappers into
an inventory checked by the controller. It pins the selected Python executable.
The administrator remains responsible for the interpreter's standard library and
dependencies as part of the trusted installation.

Obtain official Codex **0.160.0 Linux x64** and its bundled bubblewrap helper from
the same official npm archive. Place the helper beside the source executable at
`codex-resources/bwrap`; copying only the executable is insufficient. The installer
checks both hashes before copying either into the protected prefix:

| Artifact | SHA-256 |
| --- | --- |
| `codex` | `12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad` |
| `codex-resources/bwrap` | `01fb705f067bd5365b63d8ad2323a61c8d007733ca5e649437e086f3fb9935d8` |

The isolation workflow also verifies the archive's fixed SHA-512 before extracting
only those two regular files. Runtime upgrades require new pins and a new actual
sandbox acceptance run; there is no unrestricted fallback.

## Install and start

The following paths and identity numbers are examples for a separately prepared
host. Replace them with the administrator-provisioned interpreter, artifacts and
unused dedicated service identities:

```sh
sudo /opt/deskd-python/bin/python -m deskd.workspace install \
  --prefix /opt/deskd \
  --python /opt/deskd-python/bin/python \
  --binary /opt/deskd-artifacts/codex \
  --harness-uid 26002 --gateway-uid 26001 --business-gid 26003 \
  --model YOUR_ENABLED_MODEL
```

This writes `/opt/deskd/policy/deployment.json`, protected per-role policies and
role data directories. API mode uses the fixed OpenAI API endpoint and the
protected model-auth helper. Installation neither reads nor imports a key. Using
your existing secure provisioning procedure, place the API key only in
`/opt/deskd/gateway/model.key`, owned by the selected gateway UID with mode `0600`
and one hard link. Its gateway directory must retain mode `0700`. Never put the
key in a command argument, role configuration, role environment or inbox. The
helper returns it privately to the trusted harness after gateway authorization;
model tools are not given the auth operation. Real-key/API operation is outside
the credential-free acceptance described below.

For a completely synthetic rehearsal, use `install-mock` with the same path/UID
arguments, `--mock-port PORT` and optional `--model`; it requires a separately
started local Responses mock and does not read a key. The repository's privileged
acceptance fixture supplies that mock. `demo --output PATH` is a smaller offline
coordination rehearsal and does not establish official-runtime isolation.

Start the installed workspace in a dedicated administrator terminal:

```sh
sudo /opt/deskd-python/bin/python -m deskd.workspace up \
  --deployment /opt/deskd/policy/deployment.json
```

The manager takes an exclusive installation lock, starts the gateway fenced,
starts the non-root daemon, registers one root per role on first startup, then
attests configuration and enables the controller. All coordination database writes
run as the gateway UID. Model work wakes from durable messages, tasks and timers;
there is no model heartbeat. Ctrl-C closes only this manager's own children.
A service manager may supervise this foreground command, but the installer does
not modify system services.

A child failure fences authority before bounded recovery. Recovered roles keep
their registered roots, pause/revocation state and queue. Missing evidence leaves
an uncertain dispatch as `unknown`; recovery does not silently rerun it. A new
manager refuses unknown pre-existing sockets, locks and daemon rendezvous files.
After an unclean manager/host termination, an administrator must establish the
state of those resources before recovery; the program does not delete or kill an
unidentified predecessor.

## Attach, observe and administer

From a second administrator terminal, attach to a registered seat:

```sh
sudo /opt/deskd-python/bin/python -m deskd.workspace attach \
  --deployment /opt/deskd/policy/deployment.json --seat analyst
```

This checks the installation, active authority and registered root, then drops to
the harness UID and invokes the official native terminal with an explicit Unix
socket endpoint. It must not fall back to an embedded runtime. This is a trusted
human entry into the harness, not an untrusted role sandbox. The automated
acceptance exercises the public daemon protocol; interactive terminal keystrokes
remain a separate manual acceptance item.

Run the read-only board in another administrator terminal:

```sh
sudo /opt/deskd-python/bin/python -m deskd.workspace board \
  --deployment /opt/deskd/policy/deployment.json --port 8765
```

Open the printed `http://127.0.0.1:8765` URL. This mode checks the protected
management connection on each poll and combines gateway fencing with recorded
seat and delivery state. Failed observations are shown as stale. The separate
`--state` mode is for inspecting a ledger and does not verify live service health.
Both modes omit message bodies and proposal contents and have no mutation
endpoint. Do not expose the board through a public proxy.

Use the independent management socket for changes. First fetch the current
versions and binding state:

```sh
sudo /opt/deskd-python/bin/python -m deskd.workspace control \
  --socket /opt/deskd/admin/s --gateway-uid 26001 workspace.status
```

For example, enqueue a task for the trader through its durable inbox:

```sh
sudo /opt/deskd-python/bin/python -m deskd.workspace control \
  --socket /opt/deskd/admin/s --gateway-uid 26001 workspace.enqueue \
  --params '{"recipient":"desk/trader","body":"Prepare a proposal for independent review.","request_id":"operator-task-001"}'
```

`workspace.pause` accepts `principal`, `paused` and `expected_version`;
`workspace.budget` accepts `principal`, `budget_turns` and `expected_version`.
Use the current version from `workspace.status`; stale versions fail. Pausing
prevents new scheduling, while revocation removes gateway authority. To revoke a
seat, `workspace.revoke` requires its `principal`, current
`expected_binding_generation` and `expected_version`. Revocation survives restart
and is not undone by root bootstrap.

Administrative timers use `workspace.store.schedule_timer` with `actor`, `due_at`
(Unix seconds), `body`, `request_id` and optional `interval_seconds`.
`workspace.store.cancel_timer` requires `actor` and `timer_id`. For an `unknown`
dispatch, inspect independent runtime evidence before invoking
`workspace.store.reconcile`; supply its dispatch/root/turn IDs, current service
`generation` and supported `outcome`. `human_confirmed` is an explicit assertion
of reviewed evidence, not an automatic retry switch. The full parameter contract
is in `workspace/service.py` and `workspace/store.py`.

## Trust boundary and acceptance

An administrator owns the prefix, executables, role roots and `.codex` policy
layers. Only each role's `data` child is writable. Roles share the harness UID;
the official project loader uses `.codex/config.toml` as its root marker, so a
thread whose working directory is `data` loads the protected parent policy. The
native sandbox prevents writing or replacing a shadow config in writable data;
the official runtime may create an empty `.codex` directory as a mount target.
Roles use separate named permissions with their own writable data, minimal system
reads, no shell network and explicit denial of peer roles, management, gateway,
harness state and shared temporary state. Shells inherit no parent environment
except an explicit `PATH`. Browser/computer integration, plugins, apps, model
discovery, nested agents, hooks, hosted image generation, automatic goals,
shared memory generation/import, shell snapshots,
JavaScript REPL and code mode are off.
Enabling another execution surface requires its own isolation acceptance.

The gateway runs under a distinct UID. Its approved MCP bridge operates outside
the role shell sandbox and is bound to an attested root by the controller; role
shells cannot directly open business/control sockets. The official daemon and
administrator controller remain trusted. In particular, the daemon's writable
`CODEX_HOME` means the trusted harness can replace its root-owned base config;
roles cannot access it. Artifact and active-root checks are repeated during
recovery before leases are granted. These checks are not a defense against a
compromised administrator or malicious replacement of the trusted runtime.

The protected MCP configuration enables only the fixed deskd tool catalogue.
Its eight write verbs have explicit harness approval settings because their
authorization is enforced by the gateway. Every call still requires an attested
role connection and capability; publishing a memo also requires another stable
principal's approval. Shell escalation remains disabled. Adding a new tool does
not automatically include it in this approval list.

The `Workspace official runtime isolation` workflow runs on a fresh GitHub-hosted
Ubuntu 22.04 VM. Root creates a private mount namespace whose `/tmp` is backed by
the job scratchpad. It changes no accounts, AppArmor policy, sysctl or existing
service. All homes, files, model replies and effects are synthetic. A normal
unprivileged developer run skips these opt-in tests and cannot establish kernel
isolation.

The actual-runtime fixture checks both roles before and after daemon restart:
own data is writable; peer reads/writes, gateway dummy secrets, policy replacement,
shadow config creation, ancestor rename, hardlinks, symlink escapes, shared temp,
harness private files, gateway/daemon sockets and loopback networking are denied.
It also checks the official `apply_patch` tool and access to only the fixture's
newly created daemon through `/proc` and a non-stopping ptrace request. It never
reads another process's environment or memory. A positive unsandboxed control
proves the peer file/business socket are otherwise accessible to the harness UID.

The full installed fixture uses the actual installer, two service UIDs, official
daemon, public runtime adapter, MCP bridge, gateway and controller. It verifies
message wakeups, independent approval, one memo despite repeated execution,
task completion, read-only board output, durable pause/queue recovery and
revocation across restart. Every expected test must execute: skipped checks,
missing tool receipts, failed turns and missing kernel support fail CI.

Consult the workflow result for the exact tested revision. Mock acceptance does
not test the real OpenAI service, any broker, arbitrary third-party MCP servers,
or interactive terminal behavior. Those are separate acceptance scopes.
