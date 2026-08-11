"""Testable HTTP + SSE client for the deskd control plane.

This module has no Textual dependency.  It also never opens deskd's SQLite
database: the remote control plane is the sole source of truth and the sole
authority boundary.  SSE events are invalidations, not state; the client
always refreshes durable projections after an event.
"""

from __future__ import annotations

import ipaddress
import json
import os
import ssl
import stat
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


class ClientError(RuntimeError):
    """A safe, user-facing control-plane client failure."""


class ProtocolError(ClientError):
    """The server replied, but not with the deskd protocol shape."""


class APIError(ClientError):
    """An HTTP error with a body that is safe to show to the operator."""

    def __init__(self, status: int, detail: str, *, retryable: bool = False):
        self.status = status
        self.detail = detail
        self.retryable = retryable
        super().__init__(f"control plane returned HTTP {status}: {detail}")


class CursorExpired(APIError):
    """The event log no longer retains the requested cursor."""

    def __init__(self, detail: str = "event cursor expired"):
        super().__init__(409, detail, retryable=True)


@dataclass(frozen=True)
class SSEEvent:
    """One decoded server-sent event.

    ``id`` remains an opaque string. Cursors such as
    ``evt_0123456789abcdef_000000000000000a`` contain a server epoch and a
    sequence, but clients must never parse either component or infer arithmetic.
    """

    event: str
    data: Any
    id: str | None = None
    retry_ms: int | None = None


@dataclass(frozen=True)
class ConnectionNotice:
    """Lifecycle information yielded alongside SSE events."""

    state: str
    message: str
    cursor: str | None = None
    attempt: int = 0
    retry_in: float | None = None


@dataclass(frozen=True)
class DeskSnapshot:
    """A complete terminal projection at one server revision when available."""

    cursor: str | None
    generated_at: str | None
    server_version: str | None
    board: dict[str, Any]
    tasks: dict[str, Any]
    inbox: list[dict[str, Any]]
    meetings: list[dict[str, Any]]
    hooks: list[dict[str, Any]]
    wake: dict[str, Any]
    runtime: dict[str, Any]
    meta: dict[str, Any]
    consistent: bool = True
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, consistent: bool) -> "DeskSnapshot":
        board = _mapping(payload.get("board"), "snapshot.board")
        tasks_raw = payload.get("tasks", {})
        tasks = (dict(tasks_raw) if isinstance(tasks_raw, Mapping)
                 else {"tasks": _list_of_dicts(tasks_raw, "snapshot.tasks"),
                       "stalled_ids": []})
        return cls(
            cursor=_optional_text(payload.get("cursor")),
            generated_at=_optional_text(payload.get("generated_at")
                                        or board.get("generated_at")),
            server_version=_optional_text(payload.get("server_version")
                                          or payload.get("version")),
            board=board,
            tasks=tasks,
            inbox=_list_of_dicts(payload.get("inbox", []), "snapshot.inbox"),
            meetings=_list_of_dicts(payload.get("meetings", []),
                                    "snapshot.meetings"),
            hooks=_list_of_dicts(payload.get("hooks", []), "snapshot.hooks"),
            wake=_mapping(payload.get("wake"), "snapshot.wake"),
            runtime=_mapping(payload.get("runtime"), "snapshot.runtime"),
            meta=_mapping(payload.get("meta"), "snapshot.meta"),
            consistent=consistent,
        )


class Response(Protocol):
    headers: Mapping[str, str]
    status: int

    def read(self, amt: int = -1) -> bytes: ...
    def __iter__(self) -> Iterator[bytes]: ...
    def close(self) -> None: ...
    def __enter__(self) -> "Response": ...
    def __exit__(self, *args: object) -> None: ...


class Transport(Protocol):
    def open(self, request: urllib.request.Request, *, timeout: float) -> Response: ...


class UrllibTransport:
    """Default stdlib transport with normal system TLS verification."""

    def __init__(self, *, ca_file: str | Path | None = None):
        context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context))

    def open(self, request: urllib.request.Request, *, timeout: float) -> Response:
        return self._opener.open(request, timeout=timeout)  # type: ignore[return-value]


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_detail(value: object, limit: int = 1000) -> str:
    text = "".join(char for char in str(value)
                   if unicodedata.category(char) not in {"Cc", "Cf"})
    return text.strip()[:limit]


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _valid_token(value: str | None) -> str | None:
    if value is None:
        return None
    if any(char.isspace() or unicodedata.category(char) in {"Cc", "Cf"}
           for char in value):
        raise ClientError("API token must not contain whitespace or control characters")
    return value


def _mapping(value: object, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{name} must be a JSON object")
    return dict(value)


def _list_of_dicts(value: object, name: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, Mapping) for v in value):
        raise ProtocolError(f"{name} must be a JSON array of objects")
    return [dict(v) for v in value]


def load_api_token(path: str | Path | None, *, environ: Mapping[str, str] | None = None,
                   no_auth: bool = False) -> str | None:
    """Load a bearer token without ever accepting it on the command line.

    Explicit token files must be regular, owner-only files.  Environment
    injection is useful for containers and avoids credentials in process
    listings.  The value is held in memory and never persisted by deskd.
    """

    if no_auth:
        return None
    env = os.environ if environ is None else environ
    if path is None:
        return _valid_token(_optional_text(env.get("DESKD_API_TOKEN")))
    token_path = Path(path).expanduser()
    info = token_path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ClientError("API token path must be a regular, non-symlink file")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise ClientError("API token file must be owned by the current user")
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ClientError("API token file must not be accessible by group or others (chmod 600)")
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise ClientError("API token file is empty")
    return _valid_token(token)


def parse_sse(lines: Iterable[bytes | str]) -> Iterator[SSEEvent]:
    """Parse an SSE byte/line stream, including multiline data and heartbeats."""

    event_type = "message"
    event_id: str | None = None
    retry_ms: int | None = None
    data_lines: list[str] = []
    first = True

    def dispatch() -> SSEEvent | None:
        nonlocal event_type, event_id, retry_ms, data_lines
        if not data_lines:
            event_type, retry_ms = "message", None
            return None
        raw = "\n".join(data_lines)
        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError:
            data = {"raw": raw, "protocol_error": "event data was not JSON"}
        out = SSEEvent(event_type or "message", data, event_id, retry_ms)
        event_type, retry_ms, data_lines = "message", None, []
        return out

    for incoming in lines:
        line = (incoming.decode("utf-8", errors="replace")
                if isinstance(incoming, bytes) else incoming)
        line = line.rstrip("\r\n")
        if first:
            line = line.removeprefix("\ufeff")
            first = False
        if line == "":
            item = dispatch()
            if item is not None:
                yield item
            continue
        if line.startswith(":"):
            # SSE comments are the control plane's heartbeat.  Surfacing one
            # lets the TUI distinguish a quiet desk from a dead connection.
            yield SSEEvent("heartbeat", {"comment": line[1:].lstrip()}, event_id)
            continue
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_type = value
        elif field == "data":
            data_lines.append(value)
        elif field == "id" and "\x00" not in value:
            event_id = value
        elif field == "retry":
            try:
                parsed = int(value)
            except ValueError:
                continue
            if parsed >= 0:
                retry_ms = parsed
    item = dispatch()
    if item is not None:
        yield item


class ControlPlaneClient:
    """Remote deskd API client with an at-least-once reconnecting event tail."""

    def __init__(self, base_url: str, *, token: str | None = None,
                 timeout: float = 10.0, ca_file: str | Path | None = None,
                 allow_insecure_http: bool = False,
                 transport: Transport | None = None):
        parsed = urllib.parse.urlsplit(base_url)
        if any(unicodedata.category(char) in {"Cc", "Cf"} for char in base_url):
            raise ClientError("control-plane URL contains forbidden control characters")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ClientError("control-plane URL must be an absolute http(s) URL")
        if parsed.username or parsed.password:
            raise ClientError("credentials are forbidden in the control-plane URL")
        if parsed.query or parsed.fragment:
            raise ClientError("control-plane URL must not contain a query or fragment")
        host = (parsed.hostname or "").lower()
        loopback = _is_loopback(host)
        token = _valid_token(token)
        if token and parsed.scheme != "https" and not loopback and not allow_insecure_http:
            raise ClientError(
                "refusing to send an API token over remote HTTP; use HTTPS or "
                "explicitly pass --allow-insecure-http on a trusted network")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = max(float(timeout), 0.1)
        self._transport = transport or UrllibTransport(ca_file=ca_file)

    @property
    def authenticated(self) -> bool:
        return bool(self._token)

    def _url(self, path: str, params: Mapping[str, object] | None = None) -> str:
        url = self.base_url + "/" + path.lstrip("/")
        if params:
            query = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None})
            if query:
                url += "?" + query
        return url

    def _headers(self, *, accept: str = "application/json") -> dict[str, str]:
        headers = {"Accept": accept, "User-Agent": "deskd-tui"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _decode_error(self, exc: urllib.error.HTTPError) -> APIError:
        try:
            raw = exc.read(64_000).decode("utf-8", errors="replace")
            value = json.loads(raw)
            detail = (value.get("detail") if isinstance(value, Mapping) else None) or raw
        except Exception:
            detail = exc.reason or "request rejected"
        detail = _safe_detail(detail) or "request rejected"
        if exc.code == 409 and "cursor" in detail.lower():
            return CursorExpired(detail)
        return APIError(exc.code, detail, retryable=exc.code >= 500 or exc.code == 429)

    def request_json(self, method: str, path: str, *,
                     params: Mapping[str, object] | None = None,
                     payload: Mapping[str, Any] | None = None,
                     headers: Mapping[str, str] | None = None) -> Any:
        body = None
        merged = self._headers()
        if headers:
            merged.update(headers)
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            merged["Content-Type"] = "application/json"
        req = urllib.request.Request(self._url(path, params), data=body,
                                     headers=merged, method=method.upper())
        try:
            with self._transport.open(req, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise self._decode_error(exc) from None
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ClientError(f"cannot reach control plane at {self.base_url}: {reason}") from None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"{path} did not return valid JSON: {exc}") from None

    def fetch_snapshot(self) -> DeskSnapshot:
        """Fetch an atomic snapshot, falling back to pre-SSE console APIs.

        The compatibility fallback is intentionally marked inconsistent: its
        projections come from separate transactions.  The UI displays that
        fact instead of presenting a torn read as a coherent revision.
        """

        try:
            payload = self.request_json("GET", "/api/snapshot")
        except APIError as exc:
            legacy_without_auth = (not self.authenticated
                                   and exc.status in {401, 403})
            if exc.status != 404 and not legacy_without_auth:
                raise
        else:
            return DeskSnapshot.from_payload(
                _mapping(payload, "/api/snapshot"), consistent=True)

        paths = {
            "board": "/api/board",
            "tasks": "/api/tasks",
            "meetings": "/api/meetings",
            "hooks": "/api/hooks",
            "wake": "/api/wake",
            "runtime": "/api/runtime",
            "meta": "/api/meeting-meta",
        }
        results: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=len(paths),
                                thread_name_prefix="deskd-snapshot") as pool:
            pending = {pool.submit(self.request_json, "GET", path): name
                       for name, path in paths.items()}
            for future in as_completed(pending):
                results[pending[future]] = future.result()
        board = _mapping(results["board"], "/api/board")
        inbox: list[dict[str, Any]] = []
        for agent in _list_of_dicts(board.get("agents", []), "/api/board.agents"):
            grouped = agent.get("inbox") or {}
            if not isinstance(grouped, Mapping):
                continue
            for state in ("queued", "delivered"):
                for row in _list_of_dicts(grouped.get(state, []),
                                          f"agent.inbox.{state}"):
                    row.setdefault("target_role", agent.get("role"))
                    row.setdefault("delivery_state", state)
                    inbox.append(row)
        legacy = {
            **results,
            "board": board,
            "inbox": inbox,
            "generated_at": board.get("generated_at"),
            "server_version": (_mapping(results.get("runtime"), "/api/runtime")
                               .get("version")),
        }
        return DeskSnapshot.from_payload(legacy, consistent=False)

    def fetch_meeting(self, meeting_id: str) -> dict[str, Any]:
        safe = urllib.parse.quote(meeting_id, safe="")
        return _mapping(self.request_json("GET", f"/api/meetings/{safe}"),
                        "meeting transcript")

    def fetch_agent_feed(self, role: str, *, after_seq: int = 0,
                         limit: int = 100) -> dict[str, Any]:
        safe = urllib.parse.quote(role, safe="")
        return _mapping(self.request_json(
            "GET", f"/api/agent/{safe}/feed",
            params={"after_seq": after_seq, "limit": limit}), "agent feed")

    def submit_command(self, verb: str, params: Mapping[str, Any], *,
                       request_id: str | None = None) -> dict[str, Any]:
        """Submit one allowlisted command with an idempotency identity.

        The actor is deliberately absent.  The control plane derives it from
        the bearer token and enforces role/supervisor scopes server-side.
        """

        request_id = request_id or str(uuid.uuid4())
        payload = {"request_id": request_id, "verb": verb, "params": dict(params)}
        answer = self.request_json(
            "POST", "/api/commands", payload=payload,
            headers={"Idempotency-Key": request_id})
        return _mapping(answer, "/api/commands")

    def _open_stream(self, cursor: str | None) -> Response:
        headers = self._headers(accept="text/event-stream")
        if cursor:
            headers["Last-Event-ID"] = cursor
        req = urllib.request.Request(
            self._url("/api/events", {"after": cursor}), headers=headers, method="GET")
        try:
            response = self._transport.open(req, timeout=max(self.timeout, 30.0))
        except urllib.error.HTTPError as exc:
            raise self._decode_error(exc) from None
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ClientError(f"event stream unavailable at {self.base_url}: {reason}") from None
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type.lower():
            try:
                response.close()
            finally:
                raise ProtocolError(
                    f"/api/events returned {content_type or 'no content type'}, "
                    "expected text/event-stream")
        return response

    def follow_events(self, stop: threading.Event, *, cursor: str | None = None,
                      min_backoff: float = 0.5,
                      max_backoff: float = 15.0) -> Iterator[SSEEvent | ConnectionNotice]:
        """Tail events until ``stop``, reconnecting from the last seen cursor."""

        delay = max(min_backoff, 0.05)
        attempt = 0
        while not stop.is_set():
            attempt += 1
            yield ConnectionNotice("connecting", "connecting to event stream",
                                   cursor=cursor, attempt=attempt)
            server_retry: float | None = None
            try:
                with self._open_stream(cursor) as response:
                    yield ConnectionNotice("connected", "event stream connected",
                                           cursor=cursor, attempt=attempt)
                    delay = max(min_backoff, 0.05)
                    for event in parse_sse(response):
                        if stop.is_set():
                            return
                        if (event.event == "reset"
                                and isinstance(event.data, Mapping)
                                and event.data.get("resnapshot") is True):
                            detail = _safe_detail(
                                event.data.get("error") or "server requested a new snapshot")
                            yield ConnectionNotice(
                                "cursor_expired", detail, cursor=None,
                                attempt=attempt, retry_in=0.0)
                            # Do not reconnect this follower with the stale ID.
                            # The app must obtain a new atomic snapshot boundary
                            # and start a new follower from that returned cursor.
                            return
                        if event.id:
                            cursor = event.id
                        if event.retry_ms is not None:
                            server_retry = max(event.retry_ms / 1000.0, 0.05)
                        yield event
                if stop.is_set():
                    return
                error: ClientError = ClientError("event stream closed by server")
            except CursorExpired as exc:
                yield ConnectionNotice(
                    "cursor_expired", str(exc), cursor=None, attempt=attempt,
                    retry_in=0.0)
                # A retention gap cannot be repaired by reconnecting from an
                # empty cursor: that loses the snapshot boundary. The caller
                # must fetch an atomic snapshot and start a NEW follower from
                # the returned cursor.
                return
            except ClientError as exc:
                error = exc
            except Exception as exc:  # a malformed/aborted response is recoverable
                error = ClientError(f"event stream failed: {exc}")
            wait_for = min(server_retry or delay, max_backoff)
            yield ConnectionNotice("reconnecting", str(error), cursor=cursor,
                                   attempt=attempt, retry_in=wait_for)
            if stop.wait(wait_for):
                return
            delay = min(delay * 2, max_backoff)
