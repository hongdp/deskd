# Container deployment

The repository Dockerfile builds the generic deskd control service. A host such
as parlay normally derives its own image from this one, installs its host module,
and sets `DESKD_CONFIG_MODULE` to the module that calls `deskd.configure(...)`.

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

Use one release workflow as the sole writer of GHCR version tags and `latest`.
That workflow should configure QEMU before a multi-architecture build, emit SBOM
and provenance attestations, push immutable revision/version tags, and only then
advance a mutable convenience tag. A second branch-push workflow must not race
the release workflow for the same tags.
