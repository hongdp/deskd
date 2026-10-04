"""Bounded, single-owner client for an existing official Codex UDS daemon.

No process is spawned, credentials are read, or requests retried. Successful
start/resume verifies the *reported* root settings, not kernel sandbox enforcement
or every effective config entry (the protocol does not expose that attestation).
An ambiguous transport failure must be reconciled by the durable scheduler.
"""

from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import select
import socket
import struct
import time
from typing import Any

MAX_MESSAGE = 2 * 1024 * 1024
MAX_EVENTS = 256
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class RuntimeErrorBase(Exception):
    """Only a stable error code is exposed; daemon payloads may contain secrets."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class RuntimeUnavailable(RuntimeErrorBase):
    """Outcome may be unknown. Never automatically resubmit a mutating RPC."""


class RuntimePolicyError(RuntimeErrorBase):
    """The daemon did not report the trusted root or settings we required."""


class RuntimeRequestError(RuntimeErrorBase):
    """The daemon explicitly rejected a request."""


def _absolute(value: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("absolute_path_required")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError("canonical_absolute_path_required")
    return value


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise RuntimePolicyError("invalid_identifier")
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _depth(value: Any, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError("json_too_deep")
    if isinstance(value, dict):
        for item in value.values():
            _depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("nonfinite_json")


def _encode(value: Any) -> bytes:
    _depth(value)
    data = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()
    if len(data) > MAX_MESSAGE:
        raise ValueError("message_too_large")
    return data


@dataclass(frozen=True)
class RootConfig:
    """Trusted installation inputs. Role/model output must never create these."""

    cwd: str
    model: str
    model_provider: str
    expected_sandbox: dict
    sandbox: str | None = "read-only"
    permissions: str | None = None
    expected_instruction_sources: tuple[str, ...] | None = None
    expected_runtime_workspace_roots: tuple[str, ...] | None = None
    config: dict = field(default_factory=dict)
    developer_instructions: str | None = None

    def __post_init__(self) -> None:
        _absolute(self.cwd)
        _identifier(self.model)
        _identifier(self.model_provider)
        if (self.sandbox is None) == (self.permissions is None):
            raise ValueError("select_sandbox_or_permissions")
        if self.sandbox not in {None, "read-only", "workspace-write"}:
            raise ValueError("unrestricted_sandbox_rejected")
        if self.permissions is not None:
            _identifier(self.permissions)
        if not isinstance(self.expected_sandbox, dict):
            raise ValueError("expected_sandbox_required")
        if self.expected_sandbox.get("type") not in {"readOnly", "workspaceWrite"}:
            raise ValueError("restricted_sandbox_required")
        if self.expected_sandbox.get("networkAccess") is not False:
            raise ValueError("role_network_must_be_disabled")
        if not isinstance(self.config, dict):
            raise ValueError("config_mapping_required")
        _encode(self.config)
        _encode(self.expected_sandbox)

    def params(self) -> dict:
        result = {
            "cwd": self.cwd,
            "model": self.model,
            "modelProvider": self.model_provider,
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "config": self.config,
        }
        result["permissions" if self.permissions else "sandbox"] = (
            self.permissions or self.sandbox
        )
        if self.developer_instructions is not None:
            result["developerInstructions"] = self.developer_instructions
        if self.expected_runtime_workspace_roots is not None:
            result["runtimeWorkspaceRoots"] = list(
                self.expected_runtime_workspace_roots
            )
        return result


@dataclass(frozen=True)
class RootBinding:
    thread_id: str
    session_id: str
    cwd: str


class CodexRuntime:
    """Synchronous single-owner JSON-RPC client; callers serialize access.

    The expected uid and CODEX_HOME come from the protected installation, not
    from the socket response. This class never reconnects implicitly. Approvals,
    dynamic tools, and every other server request are rejected fail-closed.
    """

    def __init__(
        self,
        socket_path: str,
        expected_uid: int,
        expected_codex_home: str,
        *,
        timeout: float = 5,
    ):
        self.socket_path = _absolute(str(socket_path))
        self.expected_codex_home = _absolute(str(expected_codex_home))
        if type(expected_uid) is not int or expected_uid <= 0:
            raise ValueError("nonroot_daemon_required")
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("invalid_timeout")
        self.expected_uid = expected_uid
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._buffer = bytearray()
        self._events: deque[dict] = deque()
        self._roots: dict[str, RootConfig] = {}
        self._counter = 0
        self._ready = False
        self.peer_pid: int | None = None

    def __enter__(self) -> CodexRuntime:
        return self.connect()

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        sock, self._socket = self._socket, None
        self._ready = False
        self._roots.clear()
        self._buffer.clear()
        self._events.clear()
        if sock is not None:
            sock.close()

    def _fail(self, code: str) -> None:
        self.close()
        raise RuntimeUnavailable(code)

    def connect(self) -> CodexRuntime:
        if self._socket is not None:
            raise RuntimePolicyError("already_connected")
        if not hasattr(socket, "SO_PEERCRED"):
            raise RuntimePolicyError("linux_peer_credentials_required")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket = sock
        try:
            deadline = time.monotonic() + self.timeout
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
            pid, uid, _ = struct.unpack(
                "3i",
                sock.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
                ),
            )
            if uid != self.expected_uid or pid <= 0:
                raise RuntimePolicyError("daemon_peer_mismatch")
            self.peer_pid = pid
            key = base64.b64encode(os.urandom(16)).decode()
            request = f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            self._send_raw(request.encode("ascii"), deadline)
            while b"\r\n\r\n" not in self._buffer:
                if len(self._buffer) >= 16384:
                    self._fail("handshake_too_large")
                self._receive(deadline, min(4096, 16384 - len(self._buffer)))
            head, _, tail = self._buffer.partition(b"\r\n\r\n")
            self._buffer = bytearray(tail)
            lines = head.decode("ascii").split("\r\n")
            if lines[0].split(" ", 2)[:2] != ["HTTP/1.1", "101"]:
                self._fail("handshake_status")
            headers: dict[str, str] = {}
            for line in lines[1:]:
                name, separator, value = line.partition(":")
                name = name.lower()
                if not separator or name in headers or name.strip() != name:
                    self._fail("handshake_headers")
                headers[name] = value.strip()
            accept = base64.b64encode(
                hashlib.sha1((key + _GUID).encode()).digest()
            ).decode()
            if (
                headers.get("sec-websocket-accept") != accept
                or headers.get("upgrade", "").lower() != "websocket"
                or "upgrade"
                not in [
                    x.strip().lower() for x in headers.get("connection", "").split(",")
                ]
                or "sec-websocket-extensions" in headers
                or "sec-websocket-protocol" in headers
            ):
                self._fail("handshake_validation")
            result = self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "deskd_workspace",
                        "title": "deskd",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            if (
                result.get("codexHome") != self.expected_codex_home
                or result.get("platformOs") != "linux"
            ):
                raise RuntimePolicyError("daemon_installation_mismatch")
            self._send_json(
                {"method": "initialized", "params": {}}, time.monotonic() + self.timeout
            )
            self._ready = True
            return self
        except RuntimeErrorBase:
            self.close()
            raise
        except (OSError, UnicodeError, ValueError, OverflowError):
            self._fail("daemon_connection_failed")
        raise AssertionError("unreachable")

    def _send_raw(self, data: bytes, deadline: float) -> None:
        if self._socket is None:
            raise RuntimeUnavailable("daemon_disconnected")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._fail("daemon_timeout")
        self._socket.settimeout(remaining)
        self._socket.sendall(data)

    def _receive(self, deadline: float, size: int = 65536) -> None:
        if self._socket is None:
            raise RuntimeUnavailable("daemon_disconnected")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._fail("daemon_timeout")
        self._socket.settimeout(remaining)
        chunk = self._socket.recv(size)
        if not chunk:
            self._fail("daemon_disconnected")
        self._buffer.extend(chunk)

    def _read(self, count: int, deadline: float) -> bytes:
        while len(self._buffer) < count:
            self._receive(deadline, min(65536, count - len(self._buffer)))
        result = bytes(self._buffer[:count])
        del self._buffer[:count]
        return result

    def _send_frame(self, opcode: int, data: bytes, deadline: float) -> None:
        size = len(data)
        if size > MAX_MESSAGE:
            raise ValueError("message_too_large")
        prefix = bytes([0x80 | opcode])
        if size < 126:
            prefix += bytes([0x80 | size])
        elif size <= 65535:
            prefix += b"\xfe" + struct.pack("!H", size)
        else:
            prefix += b"\xff" + struct.pack("!Q", size)
        mask = os.urandom(4)
        self._send_raw(
            prefix + mask + bytes(x ^ mask[i % 4] for i, x in enumerate(data)), deadline
        )

    def _send_json(self, value: dict, deadline: float) -> None:
        self._send_frame(1, _encode(value), deadline)

    def _message(self, deadline: float) -> dict:
        content = bytearray()
        started = False
        while True:
            first, second = self._read(2, deadline)
            fin, opcode = bool(first & 0x80), first & 0x0F
            if first & 0x70 or second & 0x80:
                self._fail("invalid_websocket_flags")
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._read(2, deadline))[0]
                if size < 126:
                    self._fail("noncanonical_frame")
            elif size == 127:
                size = struct.unpack("!Q", self._read(8, deadline))[0]
                if size < 65536 or size & (1 << 63):
                    self._fail("noncanonical_frame")
            if size > MAX_MESSAGE or len(content) + size > MAX_MESSAGE:
                self._fail("message_too_large")
            if opcode >= 8 and (not fin or size > 125):
                self._fail("invalid_control_frame")
            data = self._read(size, deadline)
            if opcode == 8:
                self._fail("daemon_closed")
            if opcode == 9:
                self._send_frame(10, data, deadline)
                continue
            if opcode == 10:
                continue
            if opcode not in {0, 1} or (opcode == 0) != started:
                self._fail("invalid_message_frame")
            started = True
            content.extend(data)
            if fin:
                try:
                    value = json.loads(
                        content.decode("utf-8"), object_pairs_hook=_pairs
                    )
                    _depth(value)
                except (ValueError, UnicodeError, RecursionError):
                    self._fail("invalid_json")
                if not isinstance(value, dict):
                    self._fail("invalid_rpc_envelope")
                return value

    def _queue(self, event: dict) -> None:
        if len(self._events) >= MAX_EVENTS:
            self._fail("event_backlog_exceeded")
        if event.get("method") == "thread/settings/updated":
            params = event.get("params", {})
            thread_id = params.get("threadId")
            if not isinstance(thread_id, str):
                self._fail("invalid_settings_notification")
            config = self._roots.get(thread_id)
            if config is not None:
                settings = params.get("threadSettings")
                if not isinstance(settings, dict):
                    self.close()
                    raise RuntimePolicyError("root_settings_changed")
                self._validate_settings(settings, config, sandbox_key="sandboxPolicy")
        self._events.append(event)

    def _dispatch(self, value: dict, deadline: float) -> bool:
        if "method" not in value:
            return False
        if "result" in value or "error" in value:
            self._fail("invalid_rpc_envelope")
        if not isinstance(value["method"], str) or not isinstance(
            value.get("params", {}), dict
        ):
            self._fail("invalid_rpc_envelope")
        if "id" in value:
            if type(value["id"]) not in {str, int}:
                self._fail("invalid_rpc_id")
            self._send_json(
                {
                    "id": value["id"],
                    "error": {
                        "code": -32601,
                        "message": "Client-side requests are disabled",
                    },
                },
                deadline,
            )
            self._queue(
                {"method": "deskd/requestDenied", "params": {"method": value["method"]}}
            )
        else:
            self._queue(value)
        return True

    def _request(self, method: str, params: dict) -> dict:
        self._counter += 1
        request_id = f"deskd-{self._counter}"
        deadline = time.monotonic() + self.timeout
        try:
            self._send_json(
                {"id": request_id, "method": method, "params": params}, deadline
            )
            while True:
                value = self._message(deadline)
                if self._dispatch(value, deadline):
                    continue
                if value.get("id") != request_id or ("result" in value) == (
                    "error" in value
                ):
                    self._fail("unexpected_rpc_response")
                if "error" in value:
                    if (
                        not isinstance(value["error"], dict)
                        or type(value["error"].get("code")) is not int
                    ):
                        self._fail("invalid_rpc_error")
                    raise RuntimeRequestError(
                        f"daemon_rejected_{value['error']['code']}"
                    )
                if not isinstance(value["result"], dict):
                    self._fail("invalid_rpc_result")
                return value["result"]
        except (OSError, UnicodeError, ValueError, RecursionError, OverflowError):
            self._fail("daemon_transport_failed")

    def _require_root(self, thread_id: str) -> RootConfig:
        if not self._ready or thread_id not in self._roots:
            raise RuntimePolicyError("root_not_bound")
        return self._roots[thread_id]

    def _validate_settings(
        self, result: dict, config: RootConfig, *, sandbox_key: str = "sandbox"
    ) -> None:
        expected = {
            "cwd": config.cwd,
            "model": config.model,
            "modelProvider": config.model_provider,
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            sandbox_key: config.expected_sandbox,
        }
        if any(result.get(key) != value for key, value in expected.items()):
            self.close()
            raise RuntimePolicyError("root_settings_mismatch")
        if config.permissions is not None:
            profile = result.get("activePermissionProfile")
            if not isinstance(profile, dict) or profile.get("id") != config.permissions:
                self.close()
                raise RuntimePolicyError("root_permission_profile_mismatch")

    def _validate_thread(
        self, thread: Any, config: RootConfig, expected_id: str | None
    ) -> RootBinding:
        if not isinstance(thread, dict):
            self.close()
            raise RuntimePolicyError("missing_root")
        if (
            not {
                "id",
                "sessionId",
                "parentThreadId",
                "forkedFromId",
                "ephemeral",
                "cwd",
            }
            <= thread.keys()
        ):
            self.close()
            raise RuntimePolicyError("incomplete_root_identity")
        try:
            identifier = _identifier(thread.get("id"))
        except RuntimePolicyError:
            self.close()
            raise
        if (
            thread.get("sessionId") != identifier
            or (expected_id is not None and identifier != expected_id)
            or thread.get("parentThreadId") is not None
            or thread.get("forkedFromId") is not None
            or thread.get("ephemeral") is not False
            or thread.get("cwd") != config.cwd
        ):
            self.close()
            raise RuntimePolicyError("root_identity_mismatch")
        return RootBinding(identifier, thread["sessionId"], config.cwd)

    def _bind(
        self, result: dict, config: RootConfig, expected_id: str | None = None
    ) -> RootBinding:
        self._validate_settings(result, config)
        binding = self._validate_thread(result.get("thread"), config, expected_id)
        for key, expected in (
            ("instructionSources", config.expected_instruction_sources),
            ("runtimeWorkspaceRoots", config.expected_runtime_workspace_roots),
        ):
            if expected is not None and result.get(key) != list(expected):
                self.close()
                raise RuntimePolicyError("root_sources_mismatch")
        # Snapshot mutable nested mappings so a caller cannot change expectations
        # after a successful binding by mutating their installation input dict.
        copied = dict(config.__dict__)
        copied["config"] = json.loads(_encode(config.config))
        copied["expected_sandbox"] = json.loads(_encode(config.expected_sandbox))
        self._roots[binding.thread_id] = RootConfig(**copied)
        return binding

    def start_root(self, config: RootConfig) -> RootBinding:
        if not self._ready:
            raise RuntimePolicyError("daemon_not_initialized")
        return self._bind(
            self._request("thread/start", {**config.params(), "ephemeral": False}),
            config,
        )

    def resume_root(self, thread_id: str, config: RootConfig) -> RootBinding:
        if not self._ready:
            raise RuntimePolicyError("daemon_not_initialized")
        _identifier(thread_id)
        return self._bind(
            self._request("thread/resume", {**config.params(), "threadId": thread_id}),
            config,
            thread_id,
        )

    def read_root(self, thread_id: str, *, include_turns: bool = False) -> dict:
        config = self._require_root(thread_id)
        result = self._request(
            "thread/read", {"threadId": thread_id, "includeTurns": include_turns}
        )
        self._validate_thread(result.get("thread"), config, thread_id)
        return result["thread"]

    def start_turn(self, root_id: str, events: list[dict] | str) -> str:
        self._require_root(root_id)
        output = events if isinstance(events, str) else _encode(events).decode()
        result = self._request(
            "turn/start",
            {
                "threadId": root_id,
                "input": [],
                "toolOutput": {
                    "namespace": "deskd",
                    "name": "workspace_events",
                    "output": output,
                },
            },
        )
        turn = result.get("turn")
        if not isinstance(turn, dict):
            self._fail("missing_turn_result")
        try:
            return _identifier(turn.get("id"))
        except RuntimePolicyError:
            self._fail("invalid_turn_identity")
        raise AssertionError("unreachable")

    def read_turn(self, root_id: str, turn_id: str) -> str:
        """Read bounded turn metadata without hydrating a lifetime of messages.

        Up to 100 pages (10,000 turns) are inspected. Absence, an exhausted
        bound, or unsupported history never proves that dispatch did not occur.
        """
        _identifier(turn_id)
        self.read_root(root_id)
        cursor = None
        seen = set()
        for _ in range(100):
            params = {
                "threadId": root_id,
                "limit": 100,
                "itemsView": "notLoaded",
                "sortDirection": "desc",
            }
            if cursor is not None:
                params["cursor"] = cursor
            result = self._request("thread/turns/list", params)
            turns = result.get("data")
            if not isinstance(turns, list) or len(turns) > 100:
                self._fail("invalid_turn_page")
            for turn in turns:
                if not isinstance(turn, dict):
                    self._fail("invalid_turn_page")
                if turn.get("id") == turn_id:
                    status = turn.get("status")
                    if status not in {
                        "inProgress",
                        "completed",
                        "interrupted",
                        "failed",
                    }:
                        self._fail("invalid_turn_status")
                    return status
            if "nextCursor" not in result:
                self._fail("invalid_turn_cursor")
            cursor = result["nextCursor"]
            if cursor is None:
                return "unknown"
            if (
                not isinstance(cursor, str)
                or not cursor
                or len(cursor) > 4096
                or cursor in seen
            ):
                self._fail("invalid_turn_cursor")
            seen.add(cursor)
        return "unknown"

    def interrupt(self, root_id: str, turn_id: str) -> None:
        self._require_root(root_id)
        _identifier(turn_id)
        self._request("turn/interrupt", {"threadId": root_id, "turnId": turn_id})

    def poll_event(self, timeout: float = 0) -> dict | None:
        if not math.isfinite(timeout) or not 0 <= timeout <= 300:
            raise ValueError("invalid_poll_timeout")
        if self._events:
            return self._events.popleft()
        if self._socket is None:
            raise RuntimeUnavailable("daemon_disconnected")
        try:
            if (
                not self._buffer
                and not select.select([self._socket], [], [], timeout)[0]
            ):
                return None
            deadline = time.monotonic() + self.timeout
            value = self._message(deadline)
            if not self._dispatch(value, deadline):
                self._fail("unsolicited_rpc_response")
            return self._events.popleft() if self._events else None
        except (OSError, UnicodeError, ValueError, RecursionError, OverflowError):
            self._fail("daemon_transport_failed")
        raise AssertionError("unreachable")
