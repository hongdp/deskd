"""Real Linux UDS tests, NOT proof of a provisioned multi-UID deployment.

Fixtures call the private listener seam solely because CI has one UID. Production
start's distinct-UID rejection is tested separately; there is no public bypass.
No existing socket, process, credentials or database is used.
"""

import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import stat
import threading
import tempfile
import time

import pytest

from deskd.gateway.commands import CommandHandler, GatewayCommands
from deskd.gateway.events import GatewayEventStore, MAX_JSON_BYTES, canonical_json
from deskd.gateway.registry import Registry
from deskd.gateway.transport import (
    GatewayTransport,
    TransportError,
    _Endpoint,
    validate_service_domains,
)
from deskd.gateway.wire import MAX_FRAME_BYTES, encode_frame


MANIFEST = "b" * 64
pytestmark = pytest.mark.skipif(
    not hasattr(socket, "SO_PEERCRED"), reason="Linux SO_PEERCRED required"
)


class Client:
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(3)
        self.socket.connect(str(path))
        self.file = self.socket.makefile("rwb")

    def call(self, method, params, request_id="wire-1"):
        self.file.write(
            encode_frame({"id": request_id, "method": method, "params": params})
        )
        self.file.flush()
        return self.read()

    def read(self):
        raw = self.file.readline()
        assert raw, "server disconnected without response"
        return json.loads(raw)

    def close(self):
        self.file.close()
        self.socket.close()


@pytest.fixture
def directory(monkeypatch):
    # Honour TMPDIR so callers choose an isolated scratch area. Keep the name
    # short enough for Linux's 107-byte pathname socket limit.
    path = Path(tempfile.mkdtemp(prefix="dg-"))
    (path / "b").mkdir(mode=0o750)
    (path / "a").mkdir(mode=0o700)
    os.chmod(path / "b", 0o750)
    os.chmod(path / "a", 0o700)
    # The developer workspace has group-writable pre-existing ancestors. Do
    # not alter them: only skip those outside this newly owned fixture tree.
    # Production retains the strict ancestor policy with no configuration bypass.
    check = _Endpoint._check_ancestor
    monkeypatch.setattr(
        _Endpoint,
        "_check_ancestor",
        staticmethod(
            lambda candidate: (
                check(candidate)
                if candidate == path or path in candidate.parents
                else None
            )
        ),
    )
    try:
        yield path
    finally:
        shutil.rmtree(path)


def make_server(directory, *, admin_test=False, **kwargs):
    current = os.getuid()
    harness = current + 10000 if admin_test else current
    admin = frozenset({current}) if admin_test else frozenset({current + 20000})
    registry = Registry(directory / "g.db", harness_uid=harness)
    events = GatewayEventStore(registry.db_path)
    with sqlite3.connect(registry.db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS effects(value TEXT)")

    def apply(conn, args, identity):
        conn.execute("INSERT INTO effects VALUES (?)", (identity.principal.value,))
        return {"principal_id": identity.principal.value}

    commands = GatewayCommands(
        registry,
        events,
        handlers={
            "proposal.create": CommandHandler("proposal.created", apply),
            "order.submit": CommandHandler("paper.submitted", apply),
        },
    )
    return GatewayTransport(
        registry,
        commands,
        business_path=directory / "b/s",
        admin_path=directory / "a/s",
        business_gid=os.getgid(),
        admin_uids=admin,
        activation_check=kwargs.pop("activation_check", lambda: None),
        **kwargs,
    )


@pytest.fixture
def server(directory):
    server = make_server(directory)._start_listeners()
    try:
        yield server
    finally:
        server.close()


def bootstrap(server, channel, root="root-alpha"):
    # Only this trusted in-memory fixture invokes management; the business
    # protocol cannot access these methods, even from the same real UID.
    binding = server._admin_call(
        "bind",
        {
            "desk_id": "demo",
            "seat_id": "alpha",
            "root_session_id": root,
            "manifest_hash": MANIFEST,
            "capabilities": ["state.read", "proposal.create", "order.submit"],
            "expected_binding_generation": 0,
        },
    )
    server._admin_call("activate", {})
    server._admin_call(
        "lease",
        {
            "connection_id": channel,
            "desk_id": "demo",
            "seat_id": "alpha",
            "root_session_id": root,
            "binding_generation": binding["binding_generation"],
            "manifest_hash": MANIFEST,
            "ttl_seconds": 30,
        },
    )


def execute_params(root="root-alpha", thread=None, **args):
    return {
        "request_id": "business-1",
        "mcp": {
            "name": "order.submit",
            "arguments": args,
            "_meta": {"sessionId": root, "threadId": thread or root},
        },
    }


def test_production_start_requires_real_distinct_domains(directory):
    server = make_server(directory)
    with pytest.raises(TransportError, match="gateway_harness_uid_must_differ"):
        server.start()
    assert not (directory / "b/s").exists()
    with pytest.raises(TransportError, match="admin_uid_must_be_independent"):
        make_server(directory, admin_test=True).start()


@pytest.mark.parametrize(
    "gateway,harness", [(0, 12001), (12001, 0), (True, 12001), (12001, False)]
)
def test_production_services_must_be_nonroot(gateway, harness):
    with pytest.raises(TransportError, match="service_uid_must_be_nonroot"):
        validate_service_domains(gateway, harness, frozenset({9999}))


def test_real_peer_credentials_come_from_kernel(server, directory):
    client = Client(directory / "b/s")
    try:
        hello = client.call("hello", {})["result"]
        row = server._admin_call("connections", {})[0]
        assert row["connection_id"] == hello["connection_id"]
        assert row["uid"] == os.getuid() and row["pid"] == os.getpid()
        assert row["service_generation"] == server.service_generation
        assert (
            client.call("execute", execute_params())["error"]["code"]
            == "service_fenced"
        )
        bootstrap(server, hello["connection_id"])
        reply = client.call(
            "execute", execute_params(peer_uid=0, connection_id="fake", epoch=99)
        )
        assert reply["ok"] is True
        assert reply["result"]["result"]["principal_id"] == "demo/alpha"
        status = client.call(
            "status", {"_meta": {"sessionId": "root-alpha", "threadId": "root-alpha"}}
        )
        assert status["result"]["principal_id"] == "demo/alpha"
        assert "connections" not in status["result"]
        replay = client.call(
            "execute", execute_params(peer_uid=0, connection_id="fake", epoch=99)
        )
        assert replay["result"] == reply["result"]
    finally:
        client.close()


def test_business_cannot_manage_or_supply_transport_envelope(server, directory):
    client = Client(directory / "b/s")
    try:
        for method in ("bind", "activate", "lease", "connections", "fence", "revoke"):
            result = client.call(method, {})
            assert result["error"]["code"] == "unknown_business_method"
        bad = execute_params()
        bad["service_generation"] = server.service_generation
        assert client.call("execute", bad)["error"]["code"] == "invalid_method_params"
    finally:
        client.close()


def test_harness_uid_cannot_enter_management_socket(server, directory):
    client = Client(directory / "a/s")
    try:
        assert client.read()["error"]["code"] == "untrusted_peer"
    finally:
        client.close()


def test_admin_socket_acl_and_exact_accepted_connection(directory):
    server = make_server(directory, admin_test=True)._start_listeners()
    admin = Client(directory / "a/s")
    business = Client(directory / "b/s")
    try:
        assert business.read()["error"]["code"] == "untrusted_peer"
        assert admin.call("status", {})["result"]["fenced"] is True
        assert admin.call("activate", {})["ok"] is True
        assert admin.call("status", {})["result"]["fenced"] is False
        assert admin.call("connections", {})["result"] == []
        lease = {
            "connection_id": "client-invented",
            "desk_id": "demo",
            "seat_id": "alpha",
            "root_session_id": "root-alpha",
            "binding_generation": 1,
            "manifest_hash": MANIFEST,
            "ttl_seconds": 20,
        }
        assert admin.call("lease", lease)["error"]["code"] == "unknown_live_connection"
        assert admin.call("execute", {})["error"]["code"] == "unknown_admin_method"
        lease["peer_uid"] = server.registry.harness_uid
        assert admin.call("lease", lease)["error"]["code"] == "invalid_method_params"
    finally:
        admin.close()
        business.close()
        server.close()


def test_failed_activation_check_keeps_service_fenced(directory):
    def fail():
        raise RuntimeError("private diagnostics must never be returned")

    server = make_server(
        directory, admin_test=True, activation_check=fail
    )._start_listeners()
    client = Client(directory / "a/s")
    try:
        response = client.call("activate", {})
        assert response["error"]["code"] == "activation_check_failed"
        assert "private" not in json.dumps(response)
        assert client.call("status", {})["result"]["fenced"] is True
    finally:
        client.close()
        server.close()


def test_later_fence_supersedes_inflight_activation(directory):
    entered, finish = threading.Event(), threading.Event()

    def check():
        entered.set()
        assert finish.wait(3)

    server = make_server(
        directory, admin_test=True, activation_check=check
    )._start_listeners()
    activator, controller = Client(directory / "a/s"), Client(directory / "a/s")
    replies = []
    worker = threading.Thread(
        target=lambda: replies.append(activator.call("activate", {}))
    )
    worker.start()
    try:
        assert entered.wait(2)
        assert controller.call("fence", {})["ok"]
        finish.set()
        worker.join(3)
        assert not worker.is_alive()
        assert replies[0]["error"]["code"] == "activation_superseded"
        assert controller.call("status", {})["result"]["fenced"]
    finally:
        finish.set()
        worker.join(3)
        activator.close()
        controller.close()
        server.close()


def test_child_and_other_root_cannot_use_an_approved_channel(server, directory):
    client = Client(directory / "b/s")
    try:
        bootstrap(server, client.call("hello", {})["result"]["connection_id"])
        assert (
            client.call("execute", execute_params(thread="child-trader"))["error"][
                "code"
            ]
            == "root_required"
        )
        assert (
            client.call("execute", execute_params(root="root-other"))["error"]["code"]
            == "binding_mismatch"
        )
        server._admin_call("fence", {})
        assert (
            client.call("execute", execute_params())["error"]["code"]
            == "service_fenced"
        )
    finally:
        client.close()


@pytest.mark.parametrize("kind", ["file", "symlink", "socket", "hardlink"])
def test_bind_never_removes_an_existing_path(directory, kind):
    path = directory / "b/s"
    existing = None
    if kind in ("socket", "hardlink"):
        existing = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        existing.bind(str(path if kind == "socket" else directory / "b/x"))
        if kind == "hardlink":
            os.link(directory / "b/x", path)
    elif kind == "symlink":
        (directory / "b/target").write_text("FAKE FILE")
        path.symlink_to(directory / "b/target")
    else:
        path.write_text("FAKE FILE")
    before = path.lstat()
    endpoint = _Endpoint(path, gid=os.getgid(), directory_mode=0o750, socket_mode=0o660)
    try:
        with pytest.raises(TransportError, match="socket_path_exists"):
            endpoint.bind()
        assert (path.lstat().st_dev, path.lstat().st_ino) == (
            before.st_dev,
            before.st_ino,
        )
    finally:
        endpoint.close()
        if existing is not None:
            existing.close()


def test_close_only_removes_its_own_socket_inode(directory):
    endpoint = _Endpoint(
        directory / "b/s", gid=os.getgid(), directory_mode=0o750, socket_mode=0o660
    )
    endpoint.bind()
    path = directory / "b/s"
    path.unlink()
    path.write_text("REPLACEMENT MUST SURVIVE")
    endpoint.close()
    assert path.read_text() == "REPLACEMENT MUST SURVIVE"


def test_socket_modes_gid_and_runtime_hardlink_change(server, directory):
    business = (directory / "b/s").lstat()
    admin = (directory / "a/s").lstat()
    assert stat.S_IMODE(business.st_mode) == 0o660 and business.st_gid == os.getgid()
    assert stat.S_IMODE(admin.st_mode) == 0o600
    os.link(directory / "b/s", directory / "b/h")
    with pytest.raises(TransportError, match="socket_identity_changed"):
        server._business.check()


def test_unsafe_directory_permissions_and_symlink_ancestor_rejected(directory):
    os.chmod(directory / "b", 0o770)
    with pytest.raises(TransportError, match="unsafe_socket_ancestor"):
        _Endpoint(
            directory / "b/s", gid=os.getgid(), directory_mode=0o750, socket_mode=0o660
        ).bind()
    os.chmod(directory / "b", 0o750)
    (directory / "l").symlink_to(directory / "b")
    with pytest.raises(TransportError, match="unsafe_socket_ancestor"):
        _Endpoint(
            directory / "l/s", gid=os.getgid(), directory_mode=0o750, socket_mode=0o660
        ).bind()


@pytest.mark.parametrize(
    "raw,code",
    [
        (b'{"id":"x","id":"y","method":"hello","params":{}}\n', "duplicate_json_key"),
        (b'{"id":"x","method":"hello","params":{"n":NaN}}\n', "invalid_json_number"),
        (b"[]\n", "request_must_be_object"),
        (b'{"id":false,"method":"hello","params":{}}\n', "invalid_wire_request_id"),
        (b"\xff\n", "invalid_json"),
    ],
)
def test_wire_rejects_malformed_frames(server, directory, raw, code):
    client = Client(directory / "b/s")
    try:
        client.socket.sendall(raw)
        assert client.read()["error"]["code"] == code
    finally:
        client.close()


def test_partial_frame_timeout_is_bounded(directory):
    server = make_server(directory, frame_timeout=0.1)._start_listeners()
    client = Client(directory / "b/s")
    try:
        client.socket.sendall(b"{")
        assert client.read()["error"]["code"] == "frame_timeout"
    finally:
        client.close()
        server.close()


def test_oversized_input_is_rejected_without_unbounded_buffer(server, directory):
    client = Client(directory / "b/s")
    try:
        client.socket.sendall(b"x" * (MAX_FRAME_BYTES + 1))
        assert client.read()["error"]["code"] == "frame_too_large"
    finally:
        client.close()


def test_maximum_stored_receipt_fits_response_envelope():
    receipt = {"result": "x" * (MAX_JSON_BYTES - len(canonical_json({"result": ""})))}
    assert len(canonical_json(receipt)) == MAX_JSON_BYTES
    frame = encode_frame({"id": "x", "ok": True, "result": receipt}, response=True)
    assert json.loads(frame)["result"] == receipt


def test_unencodable_response_closes_with_unknown_outcome_code():
    left, right = socket.socketpair()
    try:
        GatewayTransport._reply(left, {"id": "x", "ok": True, "result": object()})
        right.settimeout(1)
        with right.makefile("rb") as reader:
            assert (
                json.loads(reader.readline())["error"]["code"]
                == "response_encoding_error"
            )
            assert reader.readline() == b""
    finally:
        left.close()
        right.close()


def test_max_business_connections_does_not_consume_admin_capacity(directory):
    server = make_server(directory, max_connections=1)._start_listeners()
    first = Client(directory / "b/s")
    try:
        first.call("hello", {})
        excess = Client(directory / "b/s")
        admin = Client(directory / "a/s")
        try:
            assert excess.read()["error"]["code"] == "server_busy"
            assert admin.read()["error"]["code"] == "untrusted_peer"
        finally:
            excess.close()
            admin.close()
    finally:
        first.close()
        server.close()


def test_short_connections_do_not_accumulate_thread_history(server, directory):
    for _ in range(80):
        client = Client(directory / "b/s")
        try:
            assert client.call("hello", {})["ok"]
        finally:
            client.close()
        with server._lock:
            assert len(server._threads) <= 18  # 16 live business peers + 2 listeners.
    deadline = time.monotonic() + 3
    while True:
        with server._lock:
            remaining = len(server._threads)
        if remaining == 2:
            break
        assert time.monotonic() < deadline
        time.sleep(0.01)
    server.close()
    assert not server._threads
