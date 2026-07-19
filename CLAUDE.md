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

## Layout

- `src/deskd/` — `orchestration.py` (presence/wake ladder), `mailbox.py`
  (inbox + delivery ledger), `meetings.py`, `auth.py`, `cli.py` (`deskd`
  entry point), `config.py` (holds `__version__`), `web/` (console; its
  `static/*.html` ships in the wheel — keep it under the package)
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
