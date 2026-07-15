"""deskd web console — read-only board/agent views + the supervisor adapter.

WHY this module exists, and why it is thin:

  * The engine's agent-facing APIs are deliberately unauthenticated *within the
    host* (an agent is its own session, identified by its role). The supervisor
    is different: it is a human whose instructions carry authority over agents,
    so its actions may only enter through an authenticated adapter. That adapter
    is here, and nowhere else.
  * Everything else this server does is projection: it renders aggregates the
    engine already computes (`board()`, `agent_detail()`, `delivery_ledger()`,
    `meeting_transcript()`). No orchestration logic lives in the web layer — if
    a page needs a new number, it is computed in the engine and surfaced here.

`create_app()` is a factory, not a module-level app: a host may run several
engines (different DBs/configs) in one process, and tests need a fresh app per
config. Uvicorn users: `uvicorn --factory deskd.web.app:create_app` (which is
exactly what `deskd serve` runs).

Two supervisor auth modes, selected by DESKD_SUPERVISOR_AUTH_MODE:

  signed  — the supervisor signs a JSON assertion with an Ed25519 key that lives
            OFF this host; the engine verifies it against a root-owned public key
            at a fixed path and burns the nonce. Strongest: nothing on this box
            can mint a supervisor action.
  simple  — a shared access code in a header, compared with hmac.compare_digest.
            Convenient; only as strong as the code. The code is NEVER embedded in
            any page we serve (see web/static/meetings.html).
  hybrid  — both accepted.

The mode and the access code are NOT read here: `deskd.auth` owns them, and this
module asks it. Two independent readers of the same credential is how a console
ends up cheerfully printing a code that the verifier never accepts. What this
module *does* own is the HTTP shape of the answer — a disabled mode is a 403 and
a wrong code is a 401, distinctions the engine layer has no business making.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import auth
from .. import config as config_mod
from .. import meetings, orchestration
from ..config import EngineConfig

STATIC = Path(__file__).parent / "static"


class SupervisorAssertionRequest(BaseModel):
    """A detached Ed25519 signature over `assertion` (the raw JSON bytes)."""

    assertion: str
    signature: str


class SupervisorActionRequest(BaseModel):
    """A simple-mode action; authenticated by the access-code header only."""

    payload: dict


def _install_config(config: EngineConfig | None) -> EngineConfig:
    """Adopt `config` as the process-wide engine config.

    Engine modules bind `from .config import CONFIG` at import time, so we must
    mutate that *same object* in place — rebinding `config_mod.CONFIG` would
    leave already-imported modules pointing at the old instance.
    """
    live = config_mod.CONFIG
    if config is None or config is live:
        return live
    for f in dataclasses.fields(EngineConfig):
        setattr(live, f.name, getattr(config, f.name))
    return live


def create_app(config: EngineConfig | None = None) -> FastAPI:
    """Build the console app. `config` defaults to the process-wide CONFIG."""
    cfg = _install_config(config)

    # Resolve the mode once, at construction: an invalid DESKD_SUPERVISOR_AUTH_MODE
    # must be a loud startup failure, never a surprise 500 mid-meeting.
    auth_mode = auth.auth_mode()
    if auth.access_code_is_ephemeral():
        # auth generates rather than defaulting: a checked-in default code is a
        # published credential. Surfaced once, to this server's terminal only —
        # auth itself never logs it.
        print(f"[{config_mod.PROJECT_NAME}] generated supervisor access code "
              f"(simple auth): {auth.simple_access_code()}")
    if auth_mode == "open":
        # Unmissable, on every boot: `open` means the socket is the only
        # boundary left, and whoever runs this should hear it from the server
        # rather than rediscover it in their own .env months later.
        print(f"[{config_mod.PROJECT_NAME}] *** supervisor authentication is OFF "
              "(DESKD_SUPERVISOR_AUTH_MODE=open) *** anyone who can reach this "
              "port acts as supervisor. Bind to a host you trust.")

    app = FastAPI(title=f"{config_mod.PROJECT_NAME} console")

    # --- pages --------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(STATIC / "board.html")

    @app.get("/board", include_in_schema=False)
    def board_page():
        return FileResponse(STATIC / "board.html")

    @app.get("/agent/{role}", include_in_schema=False)
    def agent_page(role: str):
        # The page reads the role off its own URL and calls /api/agent/{role};
        # an unknown role surfaces as that call's 404, not a missing page.
        return FileResponse(STATIC / "agent.html")

    @app.get("/meetings", include_in_schema=False)
    def meetings_page():
        return FileResponse(STATIC / "meetings.html")

    # --- read-only projections ----------------------------------------------

    @app.get("/api/board")
    def api_board():
        return orchestration.board()

    @app.get("/api/agent/{role}")
    def api_agent(role: str):
        try:
            return orchestration.agent_detail(role)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/delivery")
    def api_delivery(meeting: str | None = None):
        return orchestration.delivery_ledger(meeting)

    @app.get("/api/meetings")
    def api_meetings(include_closed: bool = False):
        return meetings.list_meetings(include_closed=include_closed)

    @app.get("/api/meeting-meta")
    def api_meeting_meta():
        """Everything the console must not hardcode: which roles exist, what the
        supervisor identity is called, which auth modes are live, and the name of
        the access-code header (it is derived from the project name)."""
        return {
            "project": config_mod.PROJECT_NAME,
            "supervisor_role": cfg.supervisor_role,
            # Registry is the source of truth for roles — never a literal list.
            "roles": [
                {"role": p["role"], "display_name": p.get("display_name") or p["role"]}
                for p in orchestration.presence()
            ],
            "supervisor_auth_mode": auth_mode,
            "simple_auth_enabled": auth.simple_auth_enabled(),
            # Whether the console should ask for a code at all. Read from auth,
            # like the rest of this boundary — a console deciding for itself is
            # the second reader that makes the two disagree.
            "access_code_required": auth.access_code_required(),
            # Usable, not merely enabled: a signed mode whose key is missing or
            # agent-writable is not a working mode, and the console must not
            # advertise it as one.
            "signed_auth_enabled": (auth.signed_auth_enabled()
                                    and auth.key_status()["usable"]),
            "supervisor_public_key_path": str(config_mod.SUPERVISOR_PUBLIC_KEY_PATH),
            "code_header": config_mod.SUPERVISOR_CODE_HEADER,
            "wait_timeout_seconds": meetings.DEFAULT_WAIT_TIMEOUT_SECONDS,
            # Invariant worth stating to the operator: signing happens off-host.
            "private_key_on_server": False,
        }

    # Declared before /api/meetings/{meeting_id} for readability; Starlette
    # method-matches anyway, so the GET wildcard never shadows these POSTs.
    @app.post("/api/meetings/supervisor-apply")
    def api_supervisor_apply(req: SupervisorAssertionRequest):
        """Signed mode: verify Ed25519 assertion + burn nonce, then apply."""
        if not auth.signed_auth_enabled():
            raise HTTPException(403, "signed supervisor authentication is disabled")
        try:
            return meetings.apply_supervisor_assertion_bytes(
                req.assertion.encode("utf-8"), req.signature.encode("ascii"),
            )
        except (KeyError, UnicodeEncodeError, ValueError) as exc:
            # Verification/replay failures are client errors; the engine's
            # message says which. Never leak more than it chose to say.
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/meetings/supervisor-action")
    def api_supervisor_action(
        req: SupervisorActionRequest,
        code: str = Header(default="", alias=config_mod.SUPERVISOR_CODE_HEADER),
    ):
        """Simple mode: shared access code in a header."""
        if not auth.simple_auth_enabled():
            raise HTTPException(403, "simplified supervisor authentication is disabled")
        # Constant-time compare, inside auth: never `==` here, and never a
        # second copy of the code in this module.
        if not auth.verify_access_code(code):
            raise HTTPException(401, "invalid supervisor access code")
        try:
            return meetings.apply_simple_supervisor_action(req.payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/meetings/{meeting_id}")
    def api_meeting(meeting_id: str):
        try:
            return meetings.meeting_transcript(meeting_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    return app
