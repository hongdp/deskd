"""Protocol tests use only a private local synthetic daemon; no model or auth."""

import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time

import pytest

from deskd.workspace.runtime import (
    CodexRuntime,
    MAX_MESSAGE,
    RootConfig,
    RuntimePolicyError,
    RuntimeRequestError,
    RuntimeUnavailable,
)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
UID = os.getuid()
pytestmark = pytest.mark.skipif(
    UID == 0 or not hasattr(socket, "SO_PEERCRED"),
    reason="mock daemon needs a nonroot Linux uid",
)


def frame(payload, opcode=1, fin=True):
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode()
    first = bytes([(0x80 if fin else 0) | opcode])
    if len(payload) < 126:
        return first + bytes([len(payload)]) + payload
    if len(payload) <= 65535:
        return first + b"\x7e" + struct.pack("!H", len(payload)) + payload
    return first + b"\x7f" + struct.pack("!Q", len(payload)) + payload


def exact(sock, count):
    result = b""
    while len(result) < count:
        chunk = sock.recv(count - len(result))
        if not chunk:
            raise EOFError
        result += chunk
    return result


def receive(sock):
    first, second = exact(sock, 2)
    assert second & 0x80, "client frames must be masked"
    size = second & 127
    if size == 126:
        size = struct.unpack("!H", exact(sock, 2))[0]
    elif size == 127:
        size = struct.unpack("!Q", exact(sock, 8))[0]
    mask = exact(sock, 4)
    payload = bytes(value ^ mask[i % 4] for i, value in enumerate(exact(sock, size)))
    return first & 15, json.loads(payload) if first & 15 == 1 else payload


def config(**kwargs):
    return RootConfig(
        cwd="/work/role",
        model="mock-model",
        model_provider="mock",
        expected_sandbox={"type": "readOnly", "networkAccess": False},
        **kwargs,
    )


def thread_info(**kwargs):
    return {
        "id": "root-1",
        "sessionId": "root-1",
        "parentThreadId": None,
        "forkedFromId": None,
        "cwd": "/work/role",
        "ephemeral": False,
        "turns": [{"id": "turn-1", "status": "completed"}],
        **kwargs,
    }


def settings(**kwargs):
    return {
        "thread": thread_info(),
        "cwd": "/work/role",
        "model": "mock-model",
        "modelProvider": "mock",
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "sandbox": {"type": "readOnly", "networkAccess": False},
        "instructionSources": [],
        "runtimeWorkspaceRoots": [],
        **kwargs,
    }


class MockDaemon:
    def __init__(self, path, handler=None, handshake=None):
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(1)
        self.listener.settimeout(3)
        self.handler = handler
        self.handshake = handshake
        self.messages = []
        self.failures = []
        self.connection = None
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def run(self):
        try:
            conn, _ = self.listener.accept()
            self.connection = conn
            conn.settimeout(3)
            with conn:
                head = b""
                while not head.endswith(b"\r\n\r\n"):
                    head += exact(conn, 1)
                headers = dict(
                    line.split(": ", 1)
                    for line in head.decode().split("\r\n")[1:]
                    if ": " in line
                )
                accept = base64.b64encode(
                    hashlib.sha1(
                        (headers["Sec-WebSocket-Key"] + GUID).encode()
                    ).digest()
                ).decode()
                response = f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: keep-alive, Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode()
                if self.handshake:
                    response = self.handshake(response)
                conn.sendall(response)
                while True:
                    opcode, message = receive(conn)
                    self.messages.append(message)
                    if opcode != 1 or "method" not in message:
                        continue
                    if "id" not in message:
                        continue
                    if self.handler and self.handler(conn, message):
                        continue
                    method = message["method"]
                    result = {
                        "initialize": {
                            "codexHome": "/private/fresh",
                            "platformOs": "linux",
                        },
                        "thread/start": settings(),
                        "thread/resume": settings(),
                        "thread/read": {"thread": thread_info()},
                        "turn/start": {
                            "turn": {"id": "turn-1", "status": "inProgress"}
                        },
                        "turn/interrupt": {},
                    }[method]
                    conn.sendall(frame({"id": message["id"], "result": result}))
        except (
            EOFError,
            BrokenPipeError,
            ConnectionResetError,
            socket.timeout,
            OSError,
        ):
            pass
        except BaseException as exc:
            self.failures.append(exc)

    def close(self):
        if self.connection is not None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.listener.close()
        self.worker.join(4)
        assert not self.worker.is_alive()
        assert not self.failures


@contextmanager
def daemon(handler=None, handshake=None, timeout=1):
    with tempfile.TemporaryDirectory(prefix="dr-") as directory:
        path = Path(directory) / "s"
        server = MockDaemon(path, handler, handshake)
        client = CodexRuntime(str(path), UID, "/private/fresh", timeout=timeout)
        try:
            yield client, server
        finally:
            client.close()
            server.close()


def test_start_resume_read_event_turn_and_interrupt():
    def handler(conn, message):
        if message["method"] == "turn/start":
            conn.sendall(
                frame(
                    {
                        "method": "thread/status/changed",
                        "params": {"threadId": "root-1", "status": {"type": "active"}},
                    }
                )
            )
        return False

    with daemon(handler) as (client, server):
        client.connect()
        binding = client.start_root(
            config(expected_instruction_sources=(), expected_runtime_workspace_roots=())
        )
        assert binding.session_id == binding.thread_id == "root-1"
        assert client.resume_root("root-1", config()) == binding
        assert client.read_root("root-1")["cwd"] == "/work/role"
        assert (
            client.start_turn(
                "root-1", [{"event_id": "event-1", "body": "untrusted instruction"}]
            )
            == "turn-1"
        )
        assert client.poll_event()["method"] == "thread/status/changed"
        assert client.read_turn("root-1", "turn-1") == "completed"
        assert client.read_turn("root-1", "missing") == "unknown"
        client.interrupt("root-1", "turn-1")
        assert client.poll_event() is None
        call = next(m for m in server.messages if m.get("method") == "turn/start")
        assert call["params"]["input"] == []
        assert call["params"]["toolOutput"]["name"] == "workspace_events"
        assert (
            json.loads(call["params"]["toolOutput"]["output"])[0]["event_id"]
            == "event-1"
        )
        assert (
            not {
                "baseInstructions",
                "developerInstructions",
                "approvalPolicy",
                "sandboxPolicy",
            }
            & call["params"].keys()
        )


def test_pending_server_request_is_rejected_while_rpc_continues():
    def handler(conn, message):
        if message["method"] == "thread/start":
            conn.sendall(
                frame(
                    {
                        "id": 123,
                        "method": "item/commandExecution/requestApproval",
                        "params": {"command": "forbidden"},
                    }
                )
            )
            _, rejection = receive(conn)
            assert rejection["id"] == 123
            assert rejection["error"]["code"] == -32601
        return False

    with daemon(handler) as (client, _):
        client.connect()
        client.start_root(config())
        assert client.poll_event() == {
            "method": "deskd/requestDenied",
            "params": {"method": "item/commandExecution/requestApproval"},
        }


def test_fragments_with_ping_are_assembled():
    def handler(conn, message):
        if message["method"] == "initialize":
            encoded = json.dumps(
                {
                    "id": message["id"],
                    "result": {"codexHome": "/private/fresh", "platformOs": "linux"},
                }
            ).encode()
            conn.sendall(
                frame(encoded[:20], fin=False)
                + frame(b"ping", opcode=9)
                + frame(encoded[20:], opcode=0)
            )
            opcode, payload = receive(conn)
            assert opcode == 10 and payload == b"ping"
            return True
        return False

    with daemon(handler) as (client, _):
        client.connect()
        assert client.peer_pid == os.getpid()


@pytest.mark.parametrize(
    "transform",
    [
        lambda value: value.replace(b"101 Switching", b"200 Switching"),
        lambda value: value.replace(b"Sec-WebSocket-Accept:", b"Incorrect-Accept:"),
        lambda value: value.replace(
            b"\r\n\r\n", b"\r\nSec-WebSocket-Extensions: permessage-deflate\r\n\r\n"
        ),
        lambda value: value.replace(
            b"Upgrade: websocket", b"Upgrade: websocket\r\nUpgrade: websocket"
        ),
    ],
)
def test_invalid_handshake_closes_connection(transform):
    with daemon(handshake=transform) as (client, _):
        with pytest.raises(RuntimeUnavailable):
            client.connect()
        assert client._socket is None


def test_real_peer_uid_is_required():
    with daemon() as (client, _):
        client.expected_uid = UID + 1
        with pytest.raises(RuntimePolicyError, match="daemon_peer_mismatch"):
            client.connect()


@pytest.mark.parametrize(
    "wire",
    [
        b"\x81\x80",  # Server frames cannot be masked.
        b"\x81\x7f" + struct.pack("!Q", MAX_MESSAGE + 1),
        frame(b"\xff"),
        frame(b'{"id":"deskd-1","result":{},"result":{}}'),
        frame(b'{"id":"deskd-1","result":{"bad":NaN}}'),
        frame({"id": "different", "result": {}}),
        frame(b"{}", opcode=2),
        frame(b"{}", opcode=0),
        frame(b"{}", opcode=9, fin=False),
        b"\x81\x7e\x00\x01{",  # Nonminimal length encoding.
    ],
)
def test_malformed_messages_fail_closed(wire):
    def handler(conn, message):
        conn.sendall(wire)
        return True

    with daemon(handler) as (client, _):
        with pytest.raises(RuntimeUnavailable):
            client.connect()
        assert client._socket is None


def test_rpc_deadline_does_not_reset_for_trickle():
    def handler(conn, message):
        conn.sendall(b"\x81\x7e")
        time.sleep(0.2)
        return True

    with daemon(handler, timeout=0.05) as (client, _):
        begin = time.monotonic()
        with pytest.raises(RuntimeUnavailable):
            client.connect()
        assert time.monotonic() - begin < 0.15


@pytest.mark.parametrize(
    "change",
    [
        {"approvalPolicy": "on-request"},
        {"approvalsReviewer": "guardian_subagent"},
        {"sandbox": {"type": "dangerFullAccess"}},
        {"cwd": "/elsewhere"},
        {"model": "other"},
        {"modelProvider": "other"},
        {"thread": thread_info(sessionId="different")},
        {"thread": thread_info(parentThreadId="parent")},
        {"thread": thread_info(forkedFromId="parent")},
        {"thread": thread_info(ephemeral=True)},
    ],
)
def test_root_settings_and_identity_are_exact(change):
    def handler(conn, message):
        if message["method"] == "thread/start":
            conn.sendall(frame({"id": message["id"], "result": settings(**change)}))
            return True
        return False

    with daemon(handler) as (client, _):
        client.connect()
        with pytest.raises(RuntimePolicyError):
            client.start_root(config())
        assert client._socket is None


def test_named_permission_profile_is_verified_and_never_combined_with_sandbox():
    def handler(conn, message):
        if message["method"] == "thread/start":
            assert message["params"]["permissions"] == "operator"
            assert "sandbox" not in message["params"]
            conn.sendall(
                frame(
                    {
                        "id": message["id"],
                        "result": settings(
                            activePermissionProfile={"id": "operator", "extends": None}
                        ),
                    }
                )
            )
            return True
        return False

    with daemon(handler) as (client, _):
        client.connect()
        client.start_root(config(sandbox=None, permissions="operator"))


def test_permission_profile_missing_is_denied():
    with daemon() as (client, _):
        client.connect()
        with pytest.raises(RuntimePolicyError, match="permission_profile"):
            client.start_root(config(sandbox=None, permissions="operator"))


def test_initialize_home_is_verified():
    with daemon() as (client, _):
        client.expected_codex_home = "/wrong"
        with pytest.raises(RuntimePolicyError, match="installation_mismatch"):
            client.connect()


def test_request_error_has_only_code_and_no_server_message():
    def handler(conn, message):
        if message["method"] == "thread/start":
            conn.sendall(
                frame(
                    {
                        "id": message["id"],
                        "error": {"code": -32001, "message": "synthetic-private-text"},
                    }
                )
            )
            return True
        return False

    with daemon(handler) as (client, _):
        client.connect()
        with pytest.raises(RuntimeRequestError) as caught:
            client.start_root(config())
        assert str(caught.value) == "daemon_rejected_-32001"
        assert client._socket is not None


def test_settings_changed_notification_invalidates_binding():
    def handler(conn, message):
        if message["method"] == "thread/read":
            altered = settings()
            altered["sandboxPolicy"] = {"type": "dangerFullAccess"}
            conn.sendall(
                frame(
                    {
                        "method": "thread/settings/updated",
                        "params": {"threadId": "root-1", "threadSettings": altered},
                    }
                )
            )
            return True
        return False

    with daemon(handler) as (client, _):
        client.connect()
        client.start_root(config())
        with pytest.raises(RuntimePolicyError):
            client.read_root("root-1")
        assert client._socket is None


def test_unbound_root_cannot_receive_or_read_a_turn():
    with daemon() as (client, _):
        client.connect()
        with pytest.raises(RuntimePolicyError, match="root_not_bound"):
            client.start_turn("foreign", [])
        with pytest.raises(RuntimePolicyError, match="root_not_bound"):
            client.read_turn("foreign", "turn-1")


def test_expected_settings_are_copied_at_binding():
    role = config()
    with daemon() as (client, _):
        client.connect()
        client.start_root(role)
        role.expected_sandbox["networkAccess"] = True
        assert client._roots["root-1"].expected_sandbox["networkAccess"] is False


def test_response_lost_after_start_is_never_retried():
    def handler(conn, message):
        if message["method"] == "turn/start":
            conn.shutdown(socket.SHUT_RDWR)
            return True
        return False

    with daemon(handler) as (client, server):
        client.connect()
        client.start_root(config())
        with pytest.raises(RuntimeUnavailable):
            client.start_turn("root-1", [])
        assert sum(m.get("method") == "turn/start" for m in server.messages) == 1
        assert client._socket is None


def test_oversized_turn_input_does_not_reach_daemon():
    with daemon() as (client, server):
        client.connect()
        client.start_root(config())
        with pytest.raises(RuntimeUnavailable):
            client.start_turn("root-1", "x" * MAX_MESSAGE)
        assert not any(m.get("method") == "turn/start" for m in server.messages)
