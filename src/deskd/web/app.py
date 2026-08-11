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

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import auth, channels, mailbox
from .. import config as config_mod
from .. import meetings, orchestration
from .. import transaction
from ..config import EngineConfig, __version__
from ..control import commands as control_commands
from ..control import store as control_store
from ..control.auth import (ControlAuthError, Principal,
                            TokenStore as ControlTokenStore)

STATIC = Path(__file__).parent / "static"

_BODY_CONTROL_PATHS = frozenset({
    "/api/commands",
    "/api/runtime",
    "/api/meetings/supervisor-apply",
    "/api/meetings/supervisor-action",
})


class _BoundedControlBody:
    """Bound mutation bodies at the ASGI receive boundary.

    FastAPI resolves a body model before it invokes an endpoint, which means an
    endpoint-level check is too late: a chunked request can already have forced
    the sole control process to buffer arbitrary bytes.  Content-Length is used
    only for a cheap early rejection.  Every actual ``http.request`` chunk is
    counted and replayed to FastAPI only after the complete body is known to fit.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int,
                 paths: frozenset[str] = _BODY_CONTROL_PATHS) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.paths = paths

    async def _error(self, scope: Scope, receive: Receive, send: Send,
                     status: int, detail: str) -> None:
        await JSONResponse({"detail": detail}, status_code=status)(
            scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] != "http" or scope.get("path") not in self.paths
                or scope.get("method") not in {"POST", "PUT", "PATCH"}):
            await self.app(scope, receive, send)
            return

        lengths = [value for name, value in scope.get("headers", [])
                   if name.lower() == b"content-length"]
        if lengths:
            # Reject duplicate lengths even when equal.  Intermediaries do not
            # agree universally on how to collapse them, so accepting one here
            # would reopen request-smuggling ambiguity at the security boundary.
            try:
                if len(lengths) != 1:
                    raise ValueError
                raw_length = lengths[0].decode("ascii")
                if not raw_length.isdigit():
                    raise ValueError
                declared = int(raw_length)
            except (UnicodeDecodeError, ValueError):
                await self._error(
                    scope, receive, send, 400, "invalid Content-Length")
                return
            if declared > self.max_bytes:
                await self._error(
                    scope, receive, send, 413,
                    f"request body exceeds {self.max_bytes} bytes")
                return

        buffered: list[Message] = []
        received = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            received += len(message.get("body", b""))
            if received > self.max_bytes:
                await self._error(
                    scope, receive, send, 413,
                    f"request body exceeds {self.max_bytes} bytes")
                return
            if not message.get("more_body", False):
                break

        position = 0

        async def replay() -> Message:
            nonlocal position
            if position < len(buffered):
                message = buffered[position]
                position += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)


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


class ControlCommandRequest(BaseModel):
    request_id: str
    verb: str
    params: dict = Field(default_factory=dict)


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


def create_app(config: EngineConfig | None = None, *,
               token_store: ControlTokenStore | None = None) -> FastAPI:
    """Build the console app. `config` defaults to the process-wide CONFIG."""
    # `deskd serve` runs uvicorn with factory=True, so this factory is what a
    # reloaded WORKER process calls — a fresh interpreter where main()'s
    # load_host_config() never ran and CONFIG is empty. Load it here too (no-op
    # if DESKD_CONFIG_MODULE is unset, or if an explicit config is passed in).
    if config is None:
        config_mod.load_host_config()
    cfg = _install_config(config)
    control_store.ensure_schema()
    role_tokens = token_store or ControlTokenStore.from_environment()
    if cfg.control_api_only and not role_tokens.configured:
        raise RuntimeError(
            "DESKD_CONTROL_API_ONLY requires at least one role/service token")

    # Resolve the mode once, at construction: an invalid DESKD_SUPERVISOR_AUTH_MODE
    # must be a loud startup failure, never a surprise 500 mid-meeting.
    # The isolated control socket has no supervisor surface at all.  Do not
    # even initialize/generate a legacy supervisor credential in that process.
    auth_mode = "disabled" if cfg.control_api_only else auth.auth_mode()
    if not cfg.control_api_only and auth.access_code_is_ephemeral():
        # auth generates rather than defaulting: a checked-in default code is a
        # published credential. Surfaced once, to this server's terminal only —
        # auth itself never logs it.
        print(f"[{config_mod.PROJECT_NAME}] generated supervisor access code "
              f"(simple auth): {auth.simple_access_code()}")
    if not cfg.control_api_only and auth_mode == "open":
        # Unmissable, on every boot: `open` means the socket is the only
        # boundary left, and whoever runs this should hear it from the server
        # rather than rediscover it in their own .env months later.
        print(f"[{config_mod.PROJECT_NAME}] *** supervisor authentication is OFF "
              "(DESKD_SUPERVISOR_AUTH_MODE=open) *** anyone who can reach this "
              "port acts as supervisor. Bind to a host you trust.")

    app = FastAPI(title=f"{config_mod.PROJECT_NAME} console")
    max_body_bytes = int(cfg.control_max_request_body_bytes)
    if max_body_bytes <= 0:
        raise RuntimeError("control_max_request_body_bytes must be positive")
    app.add_middleware(_BoundedControlBody, max_bytes=max_body_bytes)

    if cfg.control_api_only:
        control_paths = {
            "/healthz", "/api/snapshot", "/api/self", "/api/inbox",
            "/api/commands", "/api/jobs", "/api/events",
        }

        @app.middleware("http")
        async def isolate_control_socket(request: Request, call_next):
            path = request.url.path
            detail_path = (
                request.method == "GET" and (
                    (path.startswith("/api/agent/") and path.endswith("/feed"))
                    or (path.startswith("/api/meetings/")
                        and path.count("/") == 3)
                )
            )
            if (path not in control_paths and not path.startswith("/api/jobs/")
                    and not detail_path):
                return JSONResponse({"detail": "not found"}, status_code=404)
            return await call_next(request)

    def control_principal(authorization: str | None) -> Principal:
        try:
            return role_tokens.authenticate(authorization)
        except ControlAuthError as exc:
            raise HTTPException(401, str(exc), headers={
                "WWW-Authenticate": "Bearer"}) from exc

    def require_read(principal: Principal) -> None:
        if principal.role is None:
            try:
                principal.require("read")
            except ControlAuthError as exc:
                raise HTTPException(403, str(exc)) from exc

    def role_or_service(principal: Principal, requested: str | None) -> str | None:
        require_read(principal)
        if principal.role is not None:
            if requested is not None and requested != principal.role:
                raise HTTPException(403, "role token cannot read another role")
            return principal.role
        return requested

    def command_error(exc: Exception) -> HTTPException:
        if isinstance(exc, ControlAuthError):
            return HTTPException(403, str(exc))
        if isinstance(exc, (control_commands.CommandConflict,
                            control_store.CursorExpired,
                            control_store.CursorAhead,
                            control_store.CursorWrongServer)):
            return HTTPException(409, str(exc))
        return HTTPException(400, str(exc))

    @app.get("/healthz")
    def healthz() -> dict:
        try:
            cursor = control_store.current_cursor()
        except Exception as exc:
            raise HTTPException(503, "control database unavailable") from exc
        return {"ok": True, "version": __version__, "cursor": cursor,
                "auth_configured": role_tokens.configured}

    def scoped_board(role: str | None) -> dict:
        board = orchestration.board()
        if role is None:
            return board
        # The shared office state is useful to every role, but another role's
        # task titles, inbox bodies, session id/tool trace and meeting agenda are
        # private execution state.  Keep only availability/counts for peers.
        agents = []
        for raw in board.get("agents", []):
            item = dict(raw)
            if item.get("role") != role:
                for key in ("session_id", "harness", "activity", "last_tool",
                            "last_tool_age_seconds", "session_todos", "tasks"):
                    item.pop(key, None)
                inbox = item.get("inbox") or {}
                item["inbox"] = {
                    "queued_count": inbox.get("queued_count", 0),
                    "delivered_count": inbox.get("delivered_count", 0),
                    "urgent_queued": inbox.get("urgent_queued", 0),
                }
                meeting = item.get("meeting") or {}
                item["meeting"] = {
                    "pending_wakes": meeting.get("pending_wakes", 0),
                    "unread_messages": meeting.get("unread_messages", 0),
                    "response_obligations": meeting.get(
                        "response_obligations", {}).get("pending", 0),
                    "active_meeting_count": len(meeting.get("active_meetings", [])),
                }
            agents.append(item)
        board["agents"] = agents
        # Global wake activity carries source refs/details.  Role clients get
        # their own wake projection separately in ``snapshot.wake``.
        board.pop("wake_activity", None)
        board.pop("recent_events", None)
        return board

    def scoped_meetings(conn, role: str | None) -> list[dict]:
        if role is None:
            rows = conn.execute(
                "SELECT * FROM meetings ORDER BY created_at DESC LIMIT 500").fetchall()
        else:
            rows = conn.execute(
                """SELECT m.* FROM meetings m
                   JOIN meeting_attendees a ON a.thread_id=m.thread_id
                   WHERE a.role=? ORDER BY m.created_at DESC LIMIT 500""",
                (role,)).fetchall()
        return [meetings.meeting_status(row["thread_id"], sweep=False)
                for row in rows]

    def control_snapshot(principal: Principal) -> dict:
        require_read(principal)
        role = principal.role
        # One SQLite snapshot supplies every DB-backed projection and its event
        # cursor. Nested engine calls reuse this connection through the ambient
        # seam, so cursor N means exactly the state shown alongside it.
        with orchestration.connect(write=True) as conn:
            with transaction.bind(conn, cfg.db_path):
                board = scoped_board(role)
                inbox = orchestration.inbox_pending(target_role=role)
                tasks = orchestration.tasks(assignee_role=role,
                                            include_closed=False)
                queue = orchestration.task_queue(role)
                hooks = orchestration.hooks(owner_role=role,
                                             include_closed=False)
                meeting_rows = scoped_meetings(conn, role)
                if role:
                    wake = orchestration.wake_sources(role)
                    wake["attempts"] = wake.pop("pending_wake_attempts", [])
                    one_runtime = orchestration.role_runtime(role)
                    runtime = {"roles": [{"role": role, **one_runtime}]}
                else:
                    wake = {
                        "attempts": orchestration.wake_attempts_recent(200),
                        "ladder": orchestration.wake_ladder_view(),
                    }
                    runtime = orchestration.runtime_overview()
                claim_where = "WHERE state='indeterminate'"
                claim_params: list[str] = []
                if role:
                    claim_where += " AND role=?"
                    claim_params.append(role)
                wake["quarantine"] = [dict(row) for row in conn.execute(
                    "SELECT claim_id,role,state,channel,mode,claimed_at,error "
                    f"FROM control_wake_claims {claim_where} ORDER BY claimed_at",
                    claim_params).fetchall()]
                rollover_where = ""
                rollover_params: list[str] = []
                if role:
                    rollover_where = "WHERE role=?"
                    rollover_params.append(role)
                wake["rollovers"] = [dict(row) for row in conn.execute(
                    """SELECT request_id,role,resume_session_id,from_day,to_day,
                              state,attempt_count,max_attempts,claim_id,last_error,
                              created_at,updated_at,completed_at
                       FROM control_rollover_requests """
                    f"{rollover_where} ORDER BY created_at DESC LIMIT 200",
                    rollover_params).fetchall()]
                # Some historical "read" projections materialize durable
                # delivery state.  Capture the cursor after those writes, in
                # this same transaction, so the returned state is exactly at N.
                cursor = control_store.current_cursor(conn)
                generated = control_store.now_iso()
        return {
            "cursor": cursor,
            "generated_at": generated,
            "server_version": __version__,
            "board": board,
            "tasks": {"tasks": tasks,
                      "stalled_ids": [int(t["id"]) for t in queue["stalled"]]},
            "inbox": inbox,
            "meetings": meeting_rows,
            "hooks": hooks,
            "wake": wake,
            "runtime": runtime,
            "meta": {
                "role": role,
                "subject": principal.subject,
                "verbs": control_commands.verbs(),
                "allowed_verbs": control_commands.allowed_verbs(principal),
                "scopes": sorted(principal.scopes),
                "auth": role_tokens.status(),
                "config_version": cfg.config_version,
                "prompt_version": cfg.prompt_version,
                "agent_image": cfg.agent_image,
                "image_digest": cfg.image_digest,
                "build_revision": cfg.build_revision,
                "build_pins": {
                    "image_digest": cfg.image_digest,
                    "build_revision": cfg.build_revision,
                    "config_version": cfg.config_version,
                    "prompt_version": cfg.prompt_version,
                },
            },
        }

    @app.get("/api/snapshot")
    def api_control_snapshot(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict:
        return control_snapshot(control_principal(authorization))

    @app.get("/api/self")
    def api_control_self(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict:
        principal = control_principal(authorization)
        if principal.role is None:
            raise HTTPException(403, "self requires a role token")
        try:
            return control_commands.self_projection(principal.role)
        except Exception as exc:
            raise command_error(exc) from exc

    @app.get("/api/inbox")
    def api_control_inbox(
        role: str | None = None,
        include_delivered: bool = True,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> list[dict]:
        principal = control_principal(authorization)
        target = role_or_service(principal, role)
        return orchestration.inbox_pending(
            target_role=target, include_delivered=include_delivered)

    @app.post("/api/commands", status_code=202)
    def api_control_command(
        req: ControlCommandRequest,
        authorization: str | None = Header(default=None, alias="Authorization"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        if idempotency_key != req.request_id:
            raise HTTPException(
                400, "Idempotency-Key must exactly equal body request_id")
        principal = control_principal(authorization)
        try:
            return control_commands.execute(
                principal, req.request_id, req.verb, req.params)
        except Exception as exc:
            raise command_error(exc) from exc

    @app.get("/api/jobs")
    def api_control_jobs(
        limit: int = 100,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> list[dict]:
        principal = control_principal(authorization)
        require_read(principal)
        return control_store.command_jobs(
            principal_id=(principal.subject if principal.role is not None else None),
            limit=limit)

    @app.get("/api/jobs/{request_id}")
    def api_control_job(
        request_id: str,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict:
        principal = control_principal(authorization)
        require_read(principal)
        jobs = control_store.command_jobs(
            principal_id=(principal.subject if principal.role is not None else None),
            limit=1000)
        match = [job for job in jobs if job["request_id"] == request_id]
        if not match:
            raise HTTPException(404, "job not found")
        return match[0]

    def event_visible(principal: Principal, event: dict) -> bool:
        if principal.role is None:
            return True
        event_role = event.get("role")
        # Fail closed by identity, independent of resource vocabulary.  Adding
        # a new role-bearing event table must not silently make it public.
        if event_role is not None:
            return event_role == principal.role
        resource = event.get("resource")
        resource_id = event.get("resource_id")
        if resource not in {"thread", "meeting_event"}:
            return True
        if not resource_id:
            return False
        # Thread-level rows have no single role column.  Their only role
        # visibility is an explicit attendee/participant relationship.
        with orchestration.connect() as conn:
            meeting = conn.execute(
                "SELECT 1 FROM meetings WHERE thread_id=?", (resource_id,)
            ).fetchone()
            if meeting:
                return conn.execute(
                    "SELECT 1 FROM meeting_attendees WHERE thread_id=? AND role=?",
                    (resource_id, principal.role)).fetchone() is not None
            if resource == "meeting_event":
                return False
            return conn.execute(
                """SELECT 1 FROM mailbox_threads t WHERE t.id=? AND
                   (t.owner_role=? OR EXISTS (
                     SELECT 1 FROM mailbox_messages mm WHERE mm.thread_id=t.id
                     AND (mm.sender=? OR mm.recipient IN (?,?))))""",
                (resource_id, principal.role, principal.role, principal.role,
                 mailbox.BROADCAST)).fetchone() is not None

    @app.get("/api/events")
    async def api_control_events(
        request: Request,
        after: str | None = None,
        once: bool = False,
        authorization: str | None = Header(default=None, alias="Authorization"),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        principal = control_principal(authorization)
        require_read(principal)
        if after is not None and last_event_id is not None and after != last_event_id:
            raise HTTPException(400, "after and Last-Event-ID disagree")
        cursor = after or last_event_id or control_store.current_cursor()
        try:
            # Validate before constructing StreamingResponse so wrong-server,
            # future and expired cursors are real HTTP 409 responses.
            control_store.events_after(cursor, limit=1)
        except Exception as exc:
            raise command_error(exc) from exc

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor
            while not await request.is_disconnected():
                try:
                    events = control_store.events_after(cursor, limit=500)
                except (control_store.CursorExpired,
                        control_store.CursorAhead,
                        control_store.CursorWrongServer) as exc:
                    data = json.dumps({"error": str(exc), "resnapshot": True})
                    yield f"event: reset\ndata: {data}\n\n"
                    return
                if events:
                    hidden_cursor: str | None = None
                    for event in events:
                        cursor = event["cursor"]
                        if event_visible(principal, event):
                            if hidden_cursor is not None:
                                hidden = json.dumps(
                                    {"cursor": hidden_cursor, "redacted": True},
                                    separators=(",", ":"))
                                yield (f"id: {hidden_cursor}\nevent: cursor\n"
                                       f"data: {hidden}\n\n")
                                hidden_cursor = None
                            payload = event
                            name = "change"
                        else:
                            # A global monotonic cursor must advance across rows
                            # hidden from this role or it replay-loops forever.
                            hidden_cursor = cursor
                            continue
                        data = json.dumps(payload, separators=(",", ":"),
                                          ensure_ascii=False)
                        yield (f"id: {cursor}\nevent: {name}\n"
                               f"data: {data}\n\n")
                    if hidden_cursor is not None:
                        hidden = json.dumps(
                            {"cursor": hidden_cursor, "redacted": True},
                            separators=(",", ":"))
                        yield (f"id: {hidden_cursor}\nevent: cursor\n"
                               f"data: {hidden}\n\n")
                    if once:
                        return
                    continue
                yield f": heartbeat cursor={cursor}\n\n"
                if once:
                    return
                await asyncio.sleep(10)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

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

    @app.get("/api/agent/{role}/feed")
    def api_agent_feed(
        role: str, after_seq: int = 0, limit: int = 100,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict:
        """What this agent's live session has been saying, oldest first.

        `after_seq` makes tailing cheap: the page sends the highest seq it
        holds and gets back only what is new. A role with no live session
        returns an empty feed rather than 404 — "nothing is running" is an
        answer, not an error, and a console that 404s on an idle agent teaches
        people to ignore its errors.
        """
        if cfg.control_api_only:
            principal = control_principal(authorization)
            role_or_service(principal, role)
        try:
            detail = orchestration.agent_detail(role)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        session_id = (detail.get("presence") or {}).get("session_id")
        if not session_id:
            return {"role": role, "session_id": None, "lines": []}
        return {"role": role, "session_id": session_id,
                "lines": orchestration.session_feed(
                    session_id, after_seq=after_seq, limit=limit)}

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
    def api_meeting(
        meeting_id: str,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict:
        if cfg.control_api_only:
            principal = control_principal(authorization)
            require_read(principal)
            if principal.role is not None:
                with orchestration.connect() as conn:
                    attendee = conn.execute(
                        "SELECT 1 FROM meeting_attendees WHERE thread_id=? AND role=?",
                        (meeting_id, principal.role)).fetchone()
                if attendee is None:
                    raise HTTPException(403, "role is not an attendee")
        try:
            return meetings.meeting_transcript(meeting_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    return app
