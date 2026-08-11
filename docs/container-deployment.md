# Container deployment

The repository Dockerfile builds the generic deskd control service. A host such
as parlay normally derives its own image from this one, installs its host module,
and sets `DESKD_CONFIG_MODULE` to the module that calls `deskd.configure(...)`.
The image deliberately has no package-manager step: it copies Git from a pinned
matching full Bookworm stage into the pinned slim runtime.

## Image provenance

The base image is pinned by Python patch version and multi-platform manifest
digest. Release builds should pass the source revision as an OCI image label and
publish one immutable digest. At deployment time set all of:

```text
DESKD_IMAGE_DIGEST=sha256:<deployed image digest>
DESKD_BUILD_REVISION=<source commit SHA>
DESKD_CONFIG_VERSION=<immutable host config revision>
DESKD_PROMPT_VERSION=<immutable prompt/playbook revision>
DESKD_AGENT_IMAGE=<human-readable image label>
```

Do not use `latest` or a mutable tag as a session assertion. The digest is the
identity; the tag is display text.

## Required mounts

The control container runs as uid/gid 65532 and needs:

- `/var/lib/deskd`: writable durable volume for SQLite, WAL, locks, and broker
  control data;
- one configured repository path: writable by the broker, never mounted into an
  agent container;
- one configured worktree root: writable by the broker; individual lease
  directories are mounted into their owner container only;
- `DESKD_REVIEW_ARTIFACT_ROOT`: writable owner-only durable directory;
- role token directory: read-only, each token regular and mode 0400/0600;
- service manifest and any referenced token files: read-only, regular and mode
  0400/0600;
- supervisor public key at `/etc/deskd/supervisor_ed25519.pub` only if the
  separately exposed supervisor adapter is enabled.

Many Docker secret implementations materialize files as 0444. That mode is
rejected. Use a secret projection with `defaultMode: 0400`, an owner-only bind
mount, or copy the secret in an init container into an owner-only tmpfs. Never
weaken deskd's file check to accommodate a permissive mount.

The SQLite volume and artifact root need coordinated backup. Quiesce writes or
use SQLite's online backup API/checkpoint procedure; copying only the `.db` file
while WAL writes continue is not a valid backup.

## Network policy

Expose the control socket only to agent, orchestrator, scheduler, operator CLI,
and trusted UI networks. The health endpoint may be probed without a token; all
other control endpoints require bearer auth.

The application enforces a hard mutation-body limit (1 MiB by default; tune
with `DESKD_CONTROL_MAX_REQUEST_BODY_BYTES`) for both declared and chunked
bodies. That protects parser memory but is not a substitute for an ingress
connection policy. Put a trusted proxy or service-mesh policy in front of the
generic image with per-source request rate limits, a small concurrent-connection
budget, header and request timeouts, and TLS. Bound the number of issued role and
service tokens; a compromised token must not be able to monopolize the sole
SQLite writer through unlimited parallel requests.

SQLite control events have an internal retention bound, but durable command
receipts, completed rollovers, task/mail/meeting history, and host-owned tables
need an explicit operational retention policy. Monitor database and WAL bytes,
checkpoint and back up before pruning, and delete only terminal rows older than
the host's audit/replay window. Retention maintenance must run as the trusted
control owner, in bounded batches, never from an agent container.

Agent containers should have:

- no Docker/container runtime socket;
- no control database or artifact volume;
- no seed repository or `.git` metadata;
- no service, peer-role, or supervisor credential;
- exactly one role token;
- a read/write source mount only for their current lease;
- outbound access limited to the control service and the chosen model/provider
  endpoints required by the host.

The worktree parent is a broker security boundary. Control and agent containers
must run as different UIDs; the parent must be owned by control and must not be
writable or mounted into an agent. Grant the agent write access only to the
contents of its one active lease mount. The broker additionally pins the lease
directory device/inode and passes an `O_NOFOLLOW` directory fd to Git, but that
does not make a shared same-UID writable parent an acceptable deployment.

The control/orchestrator container may create agent containers, but that
capability is infrastructure authority and must remain separate from role
tokens. If the host uses a dedicated launcher, give it the narrow service token
and container-runtime permissions; do not put those permissions in the deskd
HTTP process unless the deployment explicitly accepts that combined boundary.

The dedicated launcher uses the one-time `launcher.mount.claim` →
`launcher.mount.start` → `launcher.mount.land` protocol. Before `start`, fsync
the ticket id, lease/version, and exact labelled container launch id. It must
bind only the returned exact host lease path to the returned container target
and verify the returned device/inode immediately before the bind. Never
statically mount the worktree parent. Expired or version-mismatched tickets fail
closed.

On restart, call `launcher.mount.inspect {lease_id}` with a new command request
id (or repeat claim with a new request id and the same launcher subject) to
recover a consumed ticket without recovering its paths. Replaying the original
claim request id returns its immutable issued receipt and is not state
inspection. Stop and verify absence of the exact labelled orphan, record
`launcher.mount.land(... outcome=indeterminate ...)`, then call
`launcher.mount.reconcile(... resolution=orphan_stopped, note=...)` before a
new claim. Cancel an issued-but-unstarted ticket with
`resolution=cancelled_before_start`. `workspace.release` is launcher/operator
service-only and fails while any issued, started, or indeterminate mount ticket
is live.

The image runs `deskd` directly as PID 1 and handles `SIGTERM`; it does not ship
an init binary. Run it with Docker `--init`, Compose `init: true`, or the
equivalent orchestrator setting so an infrastructure init process reaps any
orphaned Git/provider children. Keep the documented stop signal as `SIGTERM`.
The Python 3.13 runtime also requires a current container runtime/seccomp
profile that permits its thread-creation syscall (`clone3`). Docker 20.10's old
default profile returns `EPERM` instead of a fallback-compatible `ENOSYS`;
upgrade Docker/containerd or deploy a reviewed profile that permits `clone3`.
Do not use `seccomp=unconfined` as the production workaround.

## Startup sequence

1. Create the SQLite and artifact directories as uid 65532, mode 0700.
2. Materialize owner-only role/service credentials.
3. Resolve the immutable image digest and inject all four build pins.
4. Import the host configuration module and validate roles, providers,
   repositories, quotas, and branch prefixes.
5. Start with `DESKD_CONTROL_API_ONLY=1`.
6. Wait for `/healthz`, then authenticate a service snapshot and one role
   `/api/self` request.
7. Run a workspace acquire/inspect/release smoke test in a disposable repository
   before enabling the scheduler.
8. Enable scheduler/orchestrator traffic, then agent traffic.

Startup is intentionally fail-closed when credentials are absent, secret modes
are unsafe, repository configuration is invalid, or the host config cannot be
imported.

## Rolling upgrade

Stop new wake claims, allow running role sessions to finish or explicitly mark
their launch outcome, and checkpoint/backup SQLite. Deploy the new immutable
image and config/prompt pins, then resume claims. Existing sessions retain their
recorded provenance; new starts must echo the new pins. A workspace leased under
old provenance cannot be attached to a new mismatched session: inspect and
release it, or complete the old build deliberately before the rollout.

Never run two control writers with different schema or build pins against the
same SQLite file. Horizontal API replicas require a different transactional
database/leader design; SQLite WAL plus process-local configuration is a
single-control-service deployment contract.

## Operational reconciliation

- A wake claim in `indeterminate` blocks that role until an operator proves
  `retry` or `landed` with a note.
- A command job in `indeterminate` represents a non-idempotent effect that deskd
  will not repeat. Reconcile the external system, then use a new explicit
  operator workflow; do not mutate the receipt.
- A recoverable workspace command remains reclaimable under the same request id.
  Retry with byte-identical params so the broker can prove the worktree, commit,
  or release intent.
- A stale workspace lease should be inspected before release. The broker refuses
  dirty or unrecorded HEAD state.
- Review artifact orphan cleanup must compare digest paths against committed DB
  references and honor a grace period longer than the maximum command retry
  window.

## Release workflow ownership

Use one release workflow as the sole writer of GHCR release tags. It configures
QEMU before a multi-architecture build, emits SBOM and provenance attestations,
and publishes only full version, release-tag and source-revision tags. Production
still resolves and records the resulting digest; a tag is never session identity.

The release workflow deliberately does not write `latest` or a floating
major/minor tag. If a project adds convenience tags, promote them only in a
separate post-verification operation with an explicit monotonic-version guard;
never let a prerelease or rerun of an older release move them backwards.
