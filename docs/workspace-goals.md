# Continuous work with independent roles

The installed workspace can run a finite, recurring research brief: gather
approved sources, prepare a memo, obtain an independent role's exact approval,
then publish it to the local shared ledger. The console exposes goals, source
configuration, shared notes, human questions, persistent notifications and
operational health. These are additive deskd services; native runtime goals and
automatic shared memory remain disabled.

This workflow does not trade, send arbitrary external actions, browse arbitrary
URLs or guarantee the quality of an independent model's judgment. Publication
means a retained local memo, not delivery to an outside service. Source and
memo content are untrusted data, never a policy or authorization update.

## Run a research brief

Start and pair the [installed console](workspace-console.md). In **信息源**, add
a short name and an exact public HTTPS URL containing no query, fragment or
embedded credentials. Use a small UTF-8 text, JSON, XML or HTML response. This
approves that one URL for retrieval; it does not fetch it immediately. At most
100 source names, including disabled configurations, are retained. Addresses
must be public and contain no secrets in their path; syntax checks cannot
determine whether a path component is sensitive.

In **持续目标**, describe what to monitor and what the brief must answer. Select
the source names, three distinct roles, a finite number of cycles and, when
needed, a repeat interval. The installed defaults are:

| Stage | Role | Required evidence |
| --- | --- | --- |
| Research | engineer | A captured, explicitly shared response from each configured source, plus an immutable memo proposal |
| Independent review | analyst | An active approval for the exact proposal digest and designated executor |
| Publication | trader | The retained local memo with the expected author, reviewer, executor and digest |

These role names describe the installation's capabilities, not a trading
instruction. The workflow is domain-neutral research and local memo publication.
Creation rejects missing sources, duplicate principals and participants without
the necessary gateway capabilities. A model cannot create or enlarge a goal's
schedule, followup allowance or cycle count.

The coordinator creates dependent tasks and wakes the correct existing role
root. The role reads its goal, requests named evidence through `source.request`,
checks `workspace.receipt` for the resulting job ID, then reads the completed
capture. The gateway's separate worker retrieves the URL; role sandboxes gain
no network permission. A role explicitly calls `source.publish` before the next
role can inspect its evidence.

The researcher uses `proposal.create` and reports the retained proposal through
`goal.report`. The reviewer verifies the evidence and proposal, issues the exact
approval, and reports it. The executor publishes with `action.execute` and
reports the retained memo. Each write is authenticated and idempotent. A queued
gateway receipt is distinct from an applied coordination receipt. Marking a
task done cannot substitute for the required artifacts.

Evidence freshness means retrieval completed during the current cycle. It does
not prove the publisher's underlying data is current, nor impose a maximum age
at eventual review. The reviewer must assess source dates and contradictions.
An unsuccessful review escalates through `goal.ask`; this version does not
automatically rewrite and reapprove a rejected brief.

## Questions, pauses and recovery

`goal.ask` records a question from the current stage's assigned role and moves
the goal to **等待决定**. The console shows the question and accepts a human
answer. That answer resumes the same stage; it is not an approval token.

Pause holds queued goal work and future followups. It cannot interrupt an
already-running turn or invalidate an existing approval. Cancel closes queued
goal work and installs a durable gateway barrier for the current retained
proposal before closing the coordination record. Existing active approvals are
revoked, and later approval or execution of that proposal is denied. An effect
committed before the barrier remains a historical fact. Arbitrary unrelated
proposals and an unreported proposal cannot be attributed to the cancelled goal.

Followups are bounded, coalesce with queued work, and stop when the participant
is paused, revoked or out of scheduled turns. Exhausted followups and failed
sources produce persistent attention items. The coordinator does not spend
model calls on heartbeat checks. Recurrence starts only after successful
publication, uses a finite cycle budget, and coalesces missed intervals instead
of replaying a backlog. Expired or revoked approvals block publication progress
and notify the human. Explicitly resuming that blocked goal returns its exact
proposal to the independent reviewer for a fresh approval; it does not reissue
authorization automatically.

After a gateway restart, durable outbox receipts prevent repeated writes. A
published memo whose final progress report was lost can complete its goal from
retained evidence. Repeating that report is accepted as the same fact. Runtime
turns with uncertain outcomes still require the existing
[reconciliation process](workspace.md); the goal engine does not invent a
successful model turn or replace a registered identity.

## Memory and evidence boundaries

Roles can remember, search, revise and forget their own versioned notes. A note
is private until its owner explicitly publishes that version within the same
desk. Revising a published note makes the new version private until republished.
The human console searches explicitly shared notes only; selecting a role in
the UI does not impersonate it or reveal its private memory.

Source references bind a captured response ID and content hash. Publishing a
note requires its referenced captures to be shared first. Forget removes stored
note revisions from the memory store, but cannot retract the authenticated
command journal, copies already delivered, separate evidence captures or
historical backups. It is not secure erasure. Notes have provenance and versions; they are
not executable instructions and do not grant capabilities.

Retrieval enforces a bounded response, total deadline, HTTPS certificate and
hostname validation, public-address checks and a pinned resolved address. It
rejects redirects, private or mixed public/private DNS answers, compression and
unsupported content. Resolver calls are bounded; stuck operating-system DNS
calls consume a fixed slot rather than creating unlimited threads. The explicit
loopback test seam is never enabled by the installed gateway.

## Notifications

Attention items survive restart and distinguish decisions, completions,
stalled work, errors and exhausted budgets. Their stable identities prevent
unchanged conditions from repeatedly appearing. Acknowledgment is explicit;
ordinary polling does not mark them read.

External delivery is off by default. An independent administrator can configure
one approved HTTPS webhook through the protected control socket:

```sh
python -m deskd.workspace control --socket /opt/deskd/admin/s \
  --gateway-uid 27001 workspace.notification.configure \
  --params '{"url":"https://notifications.example.org/deskd","enabled":true}'
```

Use the actual socket path and gateway UID from your installation. The endpoint
must meet the same URL restrictions as sources. Configuration persists; setting
`enabled` to false stops subsequent sends. A request already in flight may
finish. No destination is configured by the demo or by tests.

Webhook payloads contain a generic event kind and opaque stable notification
and delivery IDs. They omit message bodies, titles, role names, source URLs and
credentials. Deliveries use a bounded deadline and retry/backoff budget. The
receiver should deduplicate the `Idempotency-Key`: a lost acknowledgment can
cause a repeated POST, so this is not external exactly-once delivery.

## Operations

**运行状态** shows fencing, paused roles, unknown deliveries, scheduled turns,
goal/source counts and notification delivery states. Turn budgets are scheduler
turn limits, not token counts, monetary cost estimates or limits on independent
administrator terminal turns. The demo explicitly labels health as synthetic.

The existing manager restarts only its own children and checks artifacts and
root identities before resuming. It never adopts or kills an unknown service.
To review an optional service-manager template without installing it:

```sh
python -m deskd.workspace service-template \
  --python /opt/deskd-python/bin/python \
  --deployment /opt/deskd/policy/deployment.json
```

To export a stopped, fenced deployment, explicitly supply its two domain
databases and a fresh private destination:

```sh
python -m deskd.workspace backup \
  --workspace-db /path/to/stopped/workspace.sqlite \
  --gateway-db /path/to/stopped/gateway.sqlite \
  --output /private/backups/deskd-archive --offline-confirmed
python -m deskd.workspace verify-backup --archive /private/backups/deskd-archive
python -m deskd.workspace restore-archive \
  --archive /private/backups/deskd-archive --output /private/recovery/deskd-facts
```

Stop only the deployment you administer before confirming it is offline. The
exporter does not stop processes or discover configuration. It copies an
allowlist of domain facts into a private archive and verifies its integrity.
Provider credentials, runtime roots, bindings, leases and live approval
authority are excluded. The archive can contain private research and messages;
retain it as private data.

Restore creates **detached facts in a fresh directory**, not a runnable
deployment. Recovery into a new installation requires reviewed import and fresh
identity binding. Never resume pending approvals, uncertain turns or external
deliveries solely because a backup contains related text. Existing in-place
restart recovery remains the supported automatic continuation path.

## Validation scope

The integration suite uses real gateway authorization, coordination databases,
captured mock HTTP responses and separately bound scripted roles to exercise
research, review, publication, questions, cancellation, retry and recovery.
Source/network and browser tests use fresh scratchpad state only. The isolated
CI suite additionally runs the pinned official runtime and kernel sandbox
against a local synthetic model. Real model research quality, real external
source availability and delivery to an actual notification provider require
separate operator acceptance; passing mock tests does not establish them.
