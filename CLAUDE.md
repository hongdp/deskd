# deskd — orchestration engine for multi-agent desks

Domain-agnostic engine owning presence, a unified inbox, bounded meetings, and
a wake ladder that proves delivery. Plain Python over SQLite; published on PyPI
as `deskd`. See README.md and docs/design.md for the architecture.

## Hard constraints

- **Core dependencies are stdlib + `cryptography` only.** This is deliberate:
  the engine holds the supervisor trust boundary (Ed25519 signature
  verification) and must install anywhere without a web stack. FastAPI/uvicorn
  live behind the `web` extra; pytest behind `dev`. Do not add core deps.
- Signature verification is the trust boundary — never make it optional or
  silently degradable.
- **Commit messages and PR titles/bodies are written in English** (supervisor
  ruling 2026-07-26). This is a public project: the git history and PR record
  are part of the product. Chinese messages before this date predate the rule.

## Layout

- `src/deskd/orchestration/` — the engine core as a layered subpackage:
  `store.py` (schema/connect/registry/events — the only sibling that talks to
  the layers below) → `presence.py` / `tasks.py` / `delivery.py` / `inbox.py`
  (+ capability routing) / `hooks.py` → `wake.py` (demand collection, the
  ladder, `plan_wakes`) → `board.py` (console aggregates). **Import from the
  facade (`deskd.orchestration`)** — `__init__.py` re-exports every name, and
  the submodule layout is internal layering, not API. The engine clock is
  `orchestration.store._now`; submodules call it through the module attribute
  so tests can patch that single point.
- `src/deskd/meetings/` — bounded meetings as a layered subpackage:
  `store.py` (schema/clock/role gates/row helpers) → `obligations.py` /
  `escalations.py` → `sweep.py` (SLA clocks + the wake-request ledger) →
  `lifecycle.py` / `messaging.py` / `termination.py` → `views.py` →
  `supervisor.py` (verb allowlist + adapter — the P4 supervisor boundary in
  embryo). Same facade rule: **import from `deskd.meetings`** — the layout is
  internal layering, not API. The meetings clock is `meetings.store._now`,
  called through the module attribute; patch that single point. The split is
  the file boundary only — P4's *extraction* (bounds/integrity primitives,
  verb registration, meetings leaving the core) remains open. One exception to
  the facade rule, and it runs the other way: the channel registry
  (`register_channel` & co.) still resolves through `deskd.meetings` via a
  PEP 562 hook, but it is **deprecated and warns** — channels are engine
  infrastructure, so register through `deskd.channels`.
- `src/deskd/` — `mailbox.py` (threads + receipts), `channels.py`
  (pluggable human-facing egress; ledger rows never move here; a host
  registers its pager here, not through meetings), `auth.py`,
  `cli.py` (`deskd` entry point), `config.py` (holds `__version__`), `web/`
  (console; its `static/*.html` ships in the wheel — keep it under the package)
- `tests/` — pytest (configured via pyproject); `docs/` — design/security/roadmap
- `scripts/session_hook.py`, `skills/agent-orchestration` — the Claude Code
  integration surface (PostToolUse hook + skill)

## Dev environment

Primary consumer is the sibling checkout `~/Workspace/parlay`, whose venv
installs this repo editable (`pip install -r requirements-dev.txt` there).
This repo's own `.venv` exists to verify deskd in isolation — that it doesn't
accidentally lean on parlay's dependencies.

Cross-repo changes: change deskd first and run `pytest` here, then update
parlay and run both suites from parlay's venv; commit deskd first, then parlay.

## Releasing

1. Bump `__version__` in `src/deskd/config.py` (hatchling reads it from there).
2. Publish a GitHub release — `.github/workflows/release.yml` tests on 3.11/3.13
   and uploads to PyPI via trusted publishing.
3. Raise the `deskd>=` floor in parlay's requirements.txt if parlay needs the
   new version, and re-run `pip install -e ~/Workspace/deskd` in parlay's venv
   to refresh editable metadata (source changes apply instantly; metadata and
   new dependencies do not).
