# Human workspace console

The console brings tasks, operator correspondence, independent review requests,
published memos, continuous goals, approved sources, shared notes, notifications
and role controls into one local browser interface. The UI is in
Chinese, supports narrow screens and light/dark themes, and requires no web
framework, external font or CDN. It is separate from the public, read-only board.

## Try it without credentials

From a checkout, use a **new** directory beneath your scratchpad:

```sh
mkdir -p scratchpad
PYTHONPATH=src python -m deskd.workspace console --demo ./scratchpad/console-demo
```

Open the printed loopback URL and select **请求连接**. Compare the public request
identifier in the browser with the one in the launching terminal. Type the
terminal's `pair IDENTIFIER` instruction only for the browser you opened. The
page opens automatically after confirmation. The identifier is not a password
and cannot authenticate another browser.

This demo creates fresh SQLite databases and synthetic initial tasks, replies,
memos, a human question, approved source metadata and private/shared notes. New submissions really enter that local ledger, but **no model or
background worker runs**. They remain queued until processed by a separately
configured runtime. Demo replies are labeled prewritten examples. Nothing reads
a provider credential, connects to a broker or sends an external notification.
Ctrl-C closes this console; it does not stop any other process.

## Connect an installed workspace

Run the installed entry point as the independent administrator, using an
installation containing the new console assets:

```sh
sudo /opt/deskd-python/bin/python -m deskd.workspace console \
  --deployment /opt/deskd/policy/deployment.json
```

Use the protected Python environment prepared for your installation.
Pair the browser in that same terminal. The console verifies the protected
installation and authenticates the gateway's kernel UID. Its HTML, CSS and
JavaScript come from the installation's hash-pinned asset inventory, including
when the command is invoked from a separate checkout. Older installations can
continue using their existing commands but need the new package/assets to serve
the console; copying loose web files into an old manifest is not an upgrade.

## Daily use

- **总览:** choose a recipient and submit a task or message as yourself. Selecting
  a recipient does not assume that role's identity. Role cards pause or resume
  future scheduled work. Pausing does not interrupt a turn already running.
- **任务:** inspect requirements, assignee, status and dependencies. Human-created
  unfinished tasks can be cancelled with a version check. Cancellation updates
  the task ledger; it does not cancel an in-flight model turn or undo an effect.
- **消息:** read your correspondence with each role and mark replies read. A role
  sends a result, question or progress update using the existing `mail.send`
  tool with `recipient: "@supervisor"`. This is a reserved mailbox, not a model
  seat or an authorization principal. Role-to-role private messages are omitted.
- **待复核:** inspect the exact proposal and select an eligible independent role
  to review it. This sends a human request identifying the proposal and content
  digest. The gateway loads the immutable original itself, checks the selected
  reviewer, and atomically queues review metadata plus a separate complete body.
  Even a maximum-size memo is preserved. Retries reuse the original request.
  This neither issues approval nor executes the action. The gateway still
  enforces the existing independent approver and designated executor rules.
- **成果:** read published shared memos and tasks explicitly recorded as done.
  A completed task is the assignee's recorded status, not independent proof that
  its requested real-world outcome was achieved.
- **持续目标:** start a finite research–review–publication workflow, pause or
  cancel it, and answer questions from its assigned roles.
- **信息来源:** approve exact public HTTPS URLs or disable configured sources.
- **共享知识:** search notes their owners explicitly shared; private role memory
  is omitted.
- **提醒:** inspect persistent decisions, completions, errors and exhausted
  budgets, then explicitly mark items read.
- **运行维护:** inspect metadata health and the offline backup/recovery boundary.
  See [continuous work](workspace-goals.md) for setup and operational commands.
- **动态:** inspect recent coordination events. No model transcript, private role
  file, arbitrary download path or credentials are exposed.

The console preserves delivery distinctions: queued, delivered and handled are
different states. A successful send receipt does not mean a role has answered.
Role status describes scheduled dispatches, not administrator terminal turns.
An installed snapshot is a point-in-time observation, not continuous health
attestation. A lost connection makes the previous display stale and blocks new
UI mutations until refreshed. A lost mutation acknowledgment is shown as an
unknown outcome; the server never retries it automatically. Task and message
submissions use stable request IDs, and state changes retain version checks.

Snapshots are bounded to at most 100 records per section and a total byte budget.
Whole records are retained rather than cutting off prose. The interface indicates
partial lists. Unread operator replies take priority, oldest first; marking the
visible batch read exposes the next batch. The ledger retains omitted history.
This version does not provide full historical search or arbitrary pagination.

## Browser access boundary

The console listens on `127.0.0.1` only. A browser creates a random proof scoped
to its origin and tab, keeps it in session storage, and sends it in a dedicated
header. The server stores only its hash. There are **no authentication cookies,
bearer URLs, printed session secrets or provider login imports**. This avoids
sharing a host-scoped cookie with unrelated services on other loopback ports.

The launching terminal must independently approve the proof's public request ID.
Pending requests expire after two minutes; approved access expires after eight
hours and is forgotten on logout or console restart. Multiple users, remote
hosting, TLS termination and reverse proxies are not supported by this mode.

Private reads require pairing. Mutations additionally require the exact local
Origin, an explicit request header and JSON content. Duplicate JSON fields,
oversized bodies and unknown commands are rejected. Responses are uncached,
framing is denied, model text is rendered as text, and neither a generic gateway
proxy nor administrative bind, lease, identity, credential or execution APIs are
available to the browser. The console process and its protected scripts belong
to the administrator's trust domain; compromise of that administrator is outside
the role isolation boundary.

The additive workspace schema migration adds an operator reply table. It does
not create a supervisor role, change role inbox ownership or alter the separate
gateway authorization database. Existing role-only read APIs and the public
board retain their original privacy scopes.
