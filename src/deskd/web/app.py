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
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import auth, channels
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


class RuntimeTuningRequest(BaseModel):
    """Per-role runtime tuning; each field optional, 'default' clears it.

    Model and reasoning apply on the role's next turn; provider on its next
    new session. Supervisor writes: which engine does a role's thinking is
    an operator decision, gated like every other supervisor action."""

    role: str
    provider: str | None = None
    model: str | None = None
    reasoning: str | None = None


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


class _RevalidatingStatic(StaticFiles):
    """Static files that must be re-checked, not re-downloaded.

    The console's nav lives in shell.js, so a browser holding an old copy
    silently hides every view added since — the page is there, the link is
    not, and nothing looks broken. Starlette sends ETag and Last-Modified but
    no Cache-Control, which leaves browsers on heuristic freshness (a fraction
    of the file's age), so an asset that had been stable for days could be
    served from cache for hours after a release. Observed exactly that on a
    live desk the day the office view shipped.

    `no-cache` does not mean "do not store" — it means "revalidate before
    use", so the conditional request still answers 304 and costs one header
    round trip.
    """

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(config: EngineConfig | None = None) -> FastAPI:
    """Build the console app. `config` defaults to the process-wide CONFIG."""
    # `deskd serve` runs uvicorn with factory=True, so this factory is what a
    # reloaded WORKER process calls — a fresh interpreter where main()'s
    # load_host_config() never ran and CONFIG is empty. Load it here too (no-op
    # if DESKD_CONFIG_MODULE is unset, or if an explicit config is passed in).
    if config is None:
        config_mod.load_host_config()
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

    # Shared shell assets (deskd.css + shell.js): every page loads these two
    # files instead of pasting its own CSS/JS — the design system lives in one
    # place. Starlette's StaticFiles is read-only and part of FastAPI itself.
    app.mount("/static", _RevalidatingStatic(directory=STATIC), name="static")

    # --- pages --------------------------------------------------------------

    def page(name: str) -> FileResponse:
        """HTML shell must revalidate too, or it may never load new assets."""
        response = FileResponse(STATIC / name)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return page("board.html")

    @app.get("/board", include_in_schema=False)
    def board_page() -> FileResponse:
        return page("board.html")

    @app.get("/office", include_in_schema=False)
    def office_page() -> FileResponse:
        # The floor plan. Pure projection like every other page: it joins
        # /api/board with /api/meetings in the browser and adds no endpoint.
        return page("office.html")

    @app.get("/agent/{role}", include_in_schema=False)
    def agent_page(role: str) -> FileResponse:
        # The page reads the role off its own URL and calls /api/agent/{role};
        # an unknown role surfaces as that call's 404, not a missing page.
        return page("agent.html")

    @app.get("/meetings", include_in_schema=False)
    def meetings_page() -> FileResponse:
        return page("meetings.html")

    @app.get("/wake", include_in_schema=False)
    def wake_page() -> FileResponse:
        return page("wake.html")

    @app.get("/escalations", include_in_schema=False)
    def escalations_page() -> FileResponse:
        return page("escalations.html")

    @app.get("/tasks", include_in_schema=False)
    def tasks_page() -> FileResponse:
        return page("tasks.html")

    @app.get("/runtime", include_in_schema=False)
    def runtime_page() -> FileResponse:
        return page("runtime.html")

    # --- read-only projections ----------------------------------------------

    @app.get("/api/board")
    def api_board() -> dict:
        return orchestration.board()

    @app.get("/api/agent/{role}")
    def api_agent(role: str) -> dict:
        try:
            return orchestration.agent_detail(role)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/delivery")
    def api_delivery(meeting: str | None = None) -> dict:
        return orchestration.delivery_ledger(meeting)

    @app.get("/api/meetings")
    def api_meetings(include_closed: bool = False,
                     day: str | None = None) -> list[dict]:
        # `day` narrows CLOSED meetings only — the engine refuses to let a
        # history filter hide a live meeting (see list_meetings).
        try:
            return meetings.list_meetings(include_closed=include_closed, day=day)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/meeting-days")
    def api_meeting_days() -> list[str]:
        """Local dates with at least one closed meeting — real choices for the
        console's day picker instead of a blank date box."""
        return meetings.meeting_days()

    @app.get("/api/agent/{role}/wake-sources")
    def api_agent_wake_sources(role: str) -> dict:
        """'What can currently wake this agent' — the engine's own answer."""
        try:
            return orchestration.wake_sources(role)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/wake")
    def api_wake(limit: int = 200) -> dict:
        """The escalation ladder in force (with per-rung wiring status) and the
        attempt ledger. Pending demands are the attempts with outcome='pending';
        grouping them per demand is presentation, so it stays in the client."""
        return {
            "ladder": orchestration.wake_ladder_view(),
            "attempts": orchestration.wake_attempts_recent(min(limit, 1000)),
        }

    @app.get("/api/escalations")
    def api_escalations(limit: int = 200) -> dict:
        """Every path by which this desk pulls a human in, and whether each one
        currently works: the wake_escalations human-rung ledger, meeting
        escalations, demands no enabled role may take, and channel health."""
        capped = min(limit, 1000)
        return {
            "wake": orchestration.wake_escalations_recent(capped),
            # Split, because these were never one kind of thing: `needs_you`
            # is an agent waiting on an answer, `meetings` is the whole
            # ledger including the engine's own notes about odd hours and
            # missed check-ins. Fourteen of the former hid inside fifty-eight
            # of the latter until they were told apart.
            "needs_you": meetings.list_escalations(origin="agent",
                                                   unresolved_only=True)[:capped],
            "meetings": meetings.list_escalations()[:capped],
            "unroutable": orchestration.unroutable_list(include_routed=True,
                                                        limit=capped),
            "channels": channels.channel_status(),
            "human_reachable": channels.human_reachable(),
        }

    @app.get("/api/tasks")
    def api_tasks(role: str | None = None, status: str | None = None,
                  include_closed: bool = False) -> dict:
        """Cross-role task browser + the engine's actionable/stalled verdict.
        stalled_ids lets the client mark rows without re-deriving the split."""
        queue = orchestration.task_queue(role)
        return {
            "tasks": orchestration.tasks(assignee_role=role, status=status,
                                         include_closed=include_closed),
            "stalled_ids": [t["id"] for t in queue["stalled"]],
        }

    @app.get("/api/hooks")
    def api_hooks(role: str | None = None,
                  include_closed: bool = True) -> list[dict]:
        return orchestration.hooks(owner_role=role, include_closed=include_closed)

    @app.get("/api/channels")
    def api_channels() -> dict:
        return {"channels": channels.channel_status(),
                "human_reachable": channels.human_reachable()}

    @app.get("/api/meeting-meta")
    def api_meeting_meta() -> dict:
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
            "console_links": [
                {
                    "page": link.page,
                    "href": link.href,
                    "label": link.label,
                }
                for link in cfg.console_links
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
    @app.get("/api/runtime")
    def api_runtime() -> dict:
        """Per-role tuning + provider preflights + who owns each live session."""
        overview = orchestration.runtime_overview()
        sessions: dict = {}
        for row in orchestration.presence():
            if not row.get("session_id"):
                continue
            harness = row.get("harness") or ""
            provider = (harness.rsplit("#", 1)[1] if "#" in harness
                        else cfg_provider_default())
            sessions[row["role"]] = {"session_provider": provider,
                                     "state": row.get("state")}
        return {**overview, "sessions": sessions}

    def cfg_provider_default() -> str:
        from ..config import CONFIG
        return CONFIG.default_provider

    @app.post("/api/runtime")
    def api_set_runtime(
        req: RuntimeTuningRequest,
        code: str = Header(default="", alias=config_mod.SUPERVISOR_CODE_HEADER),
    ) -> dict:
        if not auth.simple_auth_enabled():
            raise HTTPException(
                403, "runtime tuning requires simple supervisor authentication")
        if not auth.verify_access_code(code):
            raise HTTPException(401, "invalid supervisor access code")
        fields = {k: getattr(req, k) for k in ("provider", "model", "reasoning")
                  if getattr(req, k) is not None}
        if not fields:
            raise HTTPException(422, "provide provider, model, and/or reasoning")
        changed = []
        try:
            for key, raw in fields.items():
                value = None if raw.strip().lower() == "default" else raw.strip()
                changed.append(orchestration.set_role_runtime(
                    req.role, key, value, actor="supervisor_web"))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"changed": changed, **api_runtime()}

    @app.post("/api/meetings/supervisor-apply")
    def api_supervisor_apply(req: SupervisorAssertionRequest) -> dict:
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
    ) -> dict:
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
    def api_meeting(meeting_id: str) -> dict:
        try:
            return meetings.meeting_transcript(meeting_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    return app
