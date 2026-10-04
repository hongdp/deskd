# Local workflow installation prerequisites

This document is a reviewable host-preparation plan, **not a working service
installer or an isolation certification**. The preflight library checks declared
paths and filesystem metadata. It neither starts an official harness nor proves
that its tools enforce the proposed policy. Do not supply real credentials to
this stage. A successful mock workflow is not the M1 installation acceptance.

The reference deployment requires Linux, two distinct non-root service UIDs,
and a root-owned installation/control domain. The current development task does
not create accounts, change AppArmor, modify an existing runtime, or install a
service. An unavailable administrative operation leaves installation incomplete;
using one UID for both services or weakening the sandbox is not a fallback.

## Manifest and read-only API

[local-install-manifest.json](fixtures/local-install-manifest.json) is a clean,
non-secret schema example. Its numeric IDs are illustrative, and its version and
SHA-256 are deliberate placeholders that the validator rejects. Replace them
only after an administrator selects and verifies the official artifact and
allocates dedicated accounts. Do not copy an existing home or credential store.

The schema is strict: unknown fields, missing fields, relative/non-canonical
paths, root or duplicate service UIDs, duplicate seats/paths, overlapping role
boundaries and omitted deny paths are faults. There is no `unverified` override.

```python
from deskd.gateway.preflight import preflight

# manifest is an already-loaded, administrator-supplied non-secret dictionary.
report = preflight(manifest)
result = report.as_dict()
```

`result` contains `metadata_ok`, `ready_for_credentials`, structured
`faults` (`code`, `field`), and `release_gates`. Faults do not echo file contents,
paths, exception text or credentials. `ready_for_credentials` is always false
in this implementation, including when metadata passes. The caller must not
interpret `metadata_ok` as permission to admit secrets or activate capabilities
with external effects. The experimental `serve-memo` command permits a trusted
administrator to activate only its local memo actions after this check; it does
not attest role sandbox isolation or a complete runtime lifecycle.
The library opens no files: it only uses `lstat` on explicit manifest paths and
their ancestors. It does not enumerate homes, directories, accounts, processes,
environment variables, credentials or socket contents.

The required top-level fields are `schema_version` (1), `harness_uid`,
`gateway_uid`, `business_gid`, `paths`, `roles`, and `runtime`. A runtime pin is
`{kind: "official-codex", version, sha256}`; syntax validation does **not** prove
that the installed binary matches that declaration. The inspector deliberately
does not hash file contents. A trusted administrator must establish provenance
and effective configuration separately.

Each role declares `seat`, `root`, `config`, `data`, `network: "off"`,
`sandbox: "local-restricted"`, and `deny_read`. These policy names describe the
installation contract; they are not a generated or tested Codex configuration.
The deny list must include daemon, business, administration, harness state,
gateway state and secret directories, plus every other role root. The actual
runtime must also restrict all credential/authentication paths and every other
TCB entry point. Adding a string to this manifest does not enforce a deny rule.

## Fixed-prefix blueprint

The example uses a fresh `deskd-local` prefix. Every ancestor, including role
roots and `.codex`, must be root-owned and not group/world writable. Protecting
only a configuration file permits replacement through a writable ancestor.
The checker rejects symlink components, multiply linked regular files, unsafe
owners/modes, and inaccessible service ancestors. It rejects shared temporary
directories even if their sticky bit would prevent some replacements.

| Manifest path | Example | Owner / group | Exact mode |
| --- | --- | --- | --- |
| installation | `/opt/deskd-local` | root | 0755 |
| policy | `/etc/deskd-local` | root | 0755 |
| runtime | `/run/deskd-local` | root | 0755 |
| daemon | `/run/deskd-local/daemon` | harness | 0700 |
| gateway | `/run/deskd-local/business` | gateway / business group | 0750 |
| admin | `/run/deskd-local/admin` | gateway | 0700 |
| harness_state | `/var/lib/deskd-local/harness` | harness | 0700 |
| gateway_state | `/var/lib/deskd-local/gateway` | gateway | 0700 |
| secrets | `/var/lib/deskd-local/secrets` | gateway | 0700 |
| controller, harness_binary, bridge | three separate files below installation | root | 0755 |
| runtime_config | a non-secret file below policy | root | 0644 |
| each role root | `/srv/deskd-local/roles/<seat>` | root | 0755 |
| each role config | `<root>/.codex/config.toml` | root | 0644 |
| each role data | `<root>/data` | harness | 0700 |

The gateway's business socket uses the declared business group and mode 0660;
its administration socket uses mode 0600 in the separate private directory.
The administration directory must use the gateway service's primary group as
well as its UID. Preflight checks its owner and 0700 mode; transport startup
additionally checks that this directory group matches the service's actual
primary group. Schema v1 does not declare or validate that group separately.
The transport owns their lifecycle and checks their metadata. Preflight does not
connect to, create or delete sockets. Root can reach the administration socket;
schema v1 does not configure non-root administrators. Service identities are
never accepted as administrative identities merely because they own a socket.

A dedicated business group allows the trusted harness bridge to reach the
business socket. Since role tools share the harness UID, group membership alone
cannot distinguish the bridge from role tools. Effective sandbox denial,
network-off behavior, protected bridge/dependencies, and peer/channel/root
checks must be tested together. The group is not an additional principal.

## Administrator preparation, before any service starts

1. Review a fresh prefix and dedicated account/group names; reject existing or
   conflicting resources rather than adopting them. Allocate two non-login
   system accounts (`deskd-harness`, `deskd-gateway`) and a dedicated
   `deskd-business` group. Record actual IDs in the manifest. Only the two
   service identities may belong to the business group; ordinary login users
   and role-controlled commands must have no service-management authority.
2. In a reviewed privileged session, create only the empty directory skeleton
   in the table. Create `/var/lib/deskd-local`, `/srv/deskd-local/roles`, each
   role root, and each `.codex` parent as root-owned 0755. Assign the service
   leaves individually. Never recursively chown the entire prefix to a service
   UID: that would make the protected installation and role ancestors mutable.
   Existing paths, links, ACLs, mounts and aliases require separate review.
3. Verify the selected official binary and complete trusted executable/dependency
   inventory, then install their immutable copies and non-secret configuration
   in the declared locations. `controller` and `bridge` in the example are
   required future installed artifacts, not instructions to create empty
   executables that trick preflight. Do not create credential files at this step.
4. Run the read-only preflight against the reviewed manifest. Resolve every fault
   through the administrator process, not through automatic chmod/chown or an
   exception flag. The snapshot can become stale; service activation must use a
   separately verified, protected manifest and revalidate the effective tuple.
5. Supply reviewed service-manager definitions only when the complete runtime is
   available. They must create fresh runtime socket directories with these
   owners/modes, keep administration separate, fence before recovery, and allow
   the fixed controller to renew only the existing root/manifest binding. This
   document deliberately provides no untested service unit or broad privilege
   grant as a substitute for that implementation.

No commands in these steps have been executed on the current host. A developer
account without administrative access can test the validator on synthetic
metadata and private temporary fixtures; it cannot certify root ownership or
two-service-UID isolation by relabeling those fixtures.

## Required release evidence

All of the following remain independent of `metadata_ok`:

- The fixed official runtime runs restricted tools on the supported Linux/kernel
  configuration; missing user-namespace or mandatory-access-control conditions
  fail closed. There is no automatic unrestricted fallback.
- The actual artifact matches the administrator's pin. Effective role cwd,
  profile, local environment, complete MCP list and root binding match the
  protected manifest after initial start and recovery.
- Real two-UID tests prove gateway secret, socket and process isolation;
  the business group has the intended members. Root-only administration and
  per-connection peer/channel checks reject the harness, role children and
  unregistered callers.
- Shell, edits, descendants, Code Mode and every exposed executable path cannot
  replace the configuration/ancestors, reach other seats, read credential/env
  or process material, access private control sockets, or bypass denial through
  pre-existing links or mount/socket aliases. The full same-UID trusted code
  inventory must be closed; a model-callable unsandboxed helper invalidates it.
- Shell networking is actually off, including loopback and private services.
  Trusted harness inference is a separate path. Offline fixtures remain the
  only reference research data until a narrow read adapter is independently
  validated; opening general shell networking is not a remedy.
- POSIX ACLs, mounts, unexpected aliases, socket freshness, and platform hardlink
  restrictions are reviewed. A directory's link count does not enumerate socket
  aliases; a file's single link and a one-time `lstat` do not solve replacement
  races. Passing this checker is not a filesystem capability or a lasting lease.
- Crash/restart tests prove fencing precedes auto-continue, old connections stay
  revoked, and the controller cannot create a new root or grant more authority.

The tests for this module use synthetic root ownership and temporary fake files.
They demonstrate validator behavior, not a successful administrator installation,
network boundary, official harness sandbox or M1 release gate.
