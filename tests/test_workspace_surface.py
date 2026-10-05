"""Authenticated outbox, gateway extensions and real local HTTP surface.

TransportEvidence below is synthetic in-process unit evidence; genuine Linux
peer and role-sandbox boundaries are covered by dedicated acceptance fixtures.
"""

from contextlib import contextmanager
import http.client
import json
from types import SimpleNamespace
import threading

import pytest

from deskd.gateway.commands import GatewayCommands
from deskd.gateway.events import GatewayEventStore
from deskd.gateway.identity import (
    ActionPolicy,
    IdentityError,
    PrincipalId,
    TransportEvidence,
)
from deskd.gateway.registry import Registry
from deskd.gateway.transport import GatewayTransport
from deskd.workspace.board import (
    HTML,
    JS,
    make_server,
    public_snapshot,
    read_installed_snapshot,
)
from deskd.workspace.exchange import ACTIONS, WorkspaceExchange
from deskd.workspace.store import WorkspaceStore


@pytest.fixture
def exchange(tmp_path):
    store = WorkspaceStore(tmp_path / "workspace.db")
    registry = Registry(
        tmp_path / "gateway.db",
        harness_uid=12001,
        actions={**ACTIONS, "state.read": ActionPolicy("state.read")},
    )
    generation = registry.start_service()
    registry.trusted_activate_service(expected_service_generation=generation)
    peers = {}
    for seat in ("operator", "reviewer"):
        principal = PrincipalId("demo", seat)
        root = "root-" + seat
        store.register_seat(principal.value, root, "a" * 64)
        registry.trusted_bind(
            principal,
            root_session_id=root,
            manifest_hash="a" * 64,
            capabilities=frozenset({*ACTIONS, "state.read"}),
            expected_binding_generation=0,
        )
        registry.trusted_grant_lease(
            principal,
            connection_id="channel-" + seat,
            service_generation=generation,
            root_session_id=root,
            binding_generation=1,
            manifest_hash="a" * 64,
            ttl_seconds=30,
        )
        peers[seat] = TransportEvidence(12001, "channel-" + seat, generation)
    registry.trusted_activate_service(expected_service_generation=generation)
    events = GatewayEventStore(registry.db_path)
    exchange = WorkspaceExchange(
        events, store, principals={"demo/operator", "demo/reviewer"}
    )
    commands = GatewayCommands(registry, events, handlers=exchange.handlers())
    transport = GatewayTransport(
        registry,
        commands,
        business_path=tmp_path / "business/s",
        admin_path=tmp_path / "admin/s",
        business_gid=12003,
        activation_check=lambda: None,
        readers=exchange.readers(),
        allow_identify=True,
    )
    return exchange, registry, events, commands, peers, transport


def request(name="mail.send", args=None, seat="operator"):
    return {
        "name": name,
        "arguments": args
        if args is not None
        else {"recipient": "demo/reviewer", "body": "synthetic-private-message"},
        "_meta": {"sessionId": "root-" + seat, "threadId": "root-" + seat},
    }


def test_gateway_receipt_precedes_projection_and_replay_is_once(exchange):
    outbox, _, events, commands, peers, _ = exchange
    first = commands.execute("message-1", request(), peers["operator"])
    assert first.result["queued"] is True
    assert outbox.store.inbox("demo/reviewer") == []
    assert commands.execute("message-1", request(), peers["operator"]) == first
    assert outbox.pump() == 1
    assert len(outbox.store.inbox("demo/reviewer")) == 1
    restarted = WorkspaceExchange(events, outbox.store, principals=outbox.principals)
    assert restarted.pump() == 1
    assert len(outbox.store.inbox("demo/reviewer")) == 1
    assert outbox.store.inbox("demo/reviewer")[0]["sender"] == "demo/operator"


@pytest.mark.parametrize(
    "args",
    [
        {"recipient": "demo/reviewer", "body": "ok", "actor": "demo/reviewer"},
        {"recipient": ["demo/reviewer"], "body": "ok"},
        {"recipient": "foreign/seat", "body": "ok"},
        {"recipient": "demo/reviewer", "body": {"nested": "wrong"}},
    ],
)
def test_malformed_or_spoofed_mail_never_commits(exchange, args):
    _, _, events, commands, peers, _ = exchange
    with pytest.raises(IdentityError):
        commands.execute("bad", request(args=args), peers["operator"])
    assert events.events() == []


@pytest.mark.parametrize(
    "name,args",
    [
        ("inbox.ack", {"message_ids": [["1"]]}),
        ("inbox.ack", {"message_ids": [True]}),
        ("inbox.ack", {"message_ids": ["1", "1"]}),
        (
            "task.create",
            {
                "assignee": "demo/reviewer",
                "title": "t",
                "body": "b",
                "depends_on": [{}],
            },
        ),
        ("task.update", {"task_id": "task", "status": [], "expected_version": 1}),
    ],
)
def test_malformed_nested_values_return_stable_rejection(exchange, name, args):
    _, _, events, commands, peers, _ = exchange
    with pytest.raises(IdentityError):
        commands.execute("bad", request(name, args), peers["operator"])
    assert events.events() == []


def test_forged_root_does_not_change_sender(exchange):
    _, _, events, commands, peers, _ = exchange
    with pytest.raises(IdentityError, match="binding_mismatch"):
        commands.execute("forged", request(seat="reviewer"), peers["operator"])
    assert events.events() == []


def test_business_read_is_identity_scoped_and_rechecks_revocation(exchange):
    outbox, registry, _, commands, peers, transport = exchange
    commands.execute("message-1", request(), peers["operator"])
    outbox.pump()
    accepted = SimpleNamespace(evidence=peers["operator"], requested_root=None)
    assert transport._business_call("read", request("inbox.read", {}), accepted) == {
        "messages": [],
        "has_more": False,
        "next_cursor": None,
    }
    accepted = SimpleNamespace(evidence=peers["reviewer"], requested_root=None)
    result = transport._business_call(
        "read", request("inbox.read", {}, "reviewer"), accepted
    )
    assert len(result["messages"]) == 1
    registry.trusted_revoke(
        PrincipalId("demo", "reviewer"), expected_binding_generation=1
    )
    with pytest.raises(IdentityError):
        transport._business_call(
            "read", request("inbox.read", {}, "reviewer"), accepted
        )


def test_identify_cannot_reassign_an_existing_channel_root(exchange):
    _, _, _, _, peers, transport = exchange
    accepted = SimpleNamespace(evidence=peers["operator"], requested_root=None)
    assert transport._business_call(
        "identify", {"_meta": request()["_meta"]}, accepted
    ) == {"ready": True}
    with pytest.raises(IdentityError, match="channel_root_changed"):
        transport._business_call(
            "identify", {"_meta": request(seat="reviewer")["_meta"]}, accepted
        )


@pytest.mark.parametrize(
    "method", ["activate", "fence", "bind", "lease", "connections"]
)
def test_business_connection_never_calls_admin_method(exchange, method):
    _, _, _, _, peers, transport = exchange
    with pytest.raises(IdentityError, match="unknown_business_method"):
        transport._business_call(
            method, {}, SimpleNamespace(evidence=peers["operator"])
        )


def test_model_auth_is_absent_from_mcp_and_no_generic_tool_can_invoke_it(exchange):
    from deskd.workspace.exchange import tool_catalog

    _, _, events, commands, peers, transport = exchange
    calls = []
    transport.auth_provider = lambda: calls.append(True) or "SYNTHETIC-NOT-A-REAL-KEY"
    assert "model.auth" not in {tool["name"] for tool in tool_catalog()}
    with pytest.raises(IdentityError):
        commands.execute("auth-spoof", request("model.auth", {}), peers["operator"])
    assert calls == [] and events.events() == []
    with pytest.raises(IdentityError, match="unknown_admin_method"):
        transport._admin_call("model.auth", {})
    assert calls == []


def test_model_auth_requires_explicit_configuration_and_empty_fixed_arguments(exchange):
    from deskd.gateway.wire import WireError

    _, _, events, _, peers, transport = exchange
    accepted = SimpleNamespace(evidence=peers["operator"])
    with pytest.raises(IdentityError, match="model_auth_not_configured"):
        transport._business_call("model.auth", {}, accepted)
    calls = []
    transport.auth_provider = lambda: calls.append(True) or "SYNTHETIC-NOT-A-REAL-KEY"
    for params in (
        {"path": "/synthetic/other"},
        {"endpoint": "https://invalid.example"},
        {"role": "analyst"},
    ):
        with pytest.raises(WireError, match="invalid_method_params"):
            transport._business_call("model.auth", params, accepted)
    assert calls == []
    assert transport._business_call("model.auth", {}, accepted) == {
        "token": "SYNTHETIC-NOT-A-REAL-KEY"
    }
    assert calls == [True] and events.events() == []
    status = transport._business_call("status", {"_meta": request()["_meta"]}, accepted)
    assert "SYNTHETIC-NOT-A-REAL-KEY" not in json.dumps(status)


@contextmanager
def board(snapshot):
    server = make_server(snapshot)
    worker = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
        assert not worker.is_alive()


def fetch(server, path, *, method="GET", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def snapshot():
    return {
        "service": {"active": 1},
        "seats": [
            {
                "principal": "<img src=x onerror=alert(1)>",
                "paused": 0,
                "root_id": "private-root",
                "manifest_hash": "private-manifest",
                "body": "PRIVATE-INBOX-BODY",
                "inbox": {"queued": 2, "unknown": 1},
            }
        ],
        "dispatches": [{"private": "PRIVATE-DISPATCH"}],
        "counts": {"queued": 2, "body": "PRIVATE-COUNT"},
    }


def test_http_status_uses_explicit_metadata_allowlist_and_safe_rendering():
    with board(snapshot) as server:
        status, headers, body = fetch(server, "/status")
        assert status == 200
        public = json.loads(body)
        assert public["delivery"] == {"queued": 2}
        assert public["fenced"] is False
        assert public["live_observation"] is False
        assert not any(
            marker in body
            for marker in (b"PRIVATE-", b"private-root", b"private-manifest")
        )
        assert headers["Cache-Control"] == "no-store"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert b"innerHTML" not in JS and b"textContent" in JS
        assert b"<img" not in HTML


@pytest.mark.parametrize(
    "headers", [{"Host": "attacker.example"}, {"Origin": "https://attacker.example"}]
)
def test_http_rejects_dns_rebinding_and_foreign_origin(headers):
    with board(snapshot) as server:
        status, _, body = fetch(server, "/status", headers=headers)
        assert status == 403 and b"PRIVATE" not in body


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_http_has_no_mutating_endpoint(method):
    called = []
    with board(lambda: called.append(True) or snapshot()) as server:
        assert fetch(server, "/control", method=method)[0] == 405
    assert not called


def test_http_snapshot_failure_does_not_leak_exception():
    def failing():
        raise RuntimeError("PRIVATE-EXCEPTION")

    with board(failing) as server:
        status, _, body = fetch(server, "/status")
        assert status == 503 and b"PRIVATE" not in body


def test_unknown_service_state_is_visibly_fenced():
    assert public_snapshot({})["fenced"] is True


def test_recorded_ledger_is_never_presented_as_live_health():
    recorded = snapshot()
    recorded["service"]["active"] = 1
    assert public_snapshot(recorded)["live_observation"] is False
    assert b"Recorded ledger state. Live health is unverified." in JS
    assert b"Running:" not in JS
    assert b"Last status may be stale" in JS


@pytest.fixture
def observer():
    calls = []
    responses = {
        "status": {
            "ok": True,
            "result": {"fenced": False, "token": "PRIVATE-UNEXPECTED"},
        },
        "workspace.status": {"ok": True, "result": snapshot()},
    }

    def admin(method):
        calls.append(method)
        result = responses[method]
        if isinstance(result, Exception):
            raise result
        return result

    deployment = SimpleNamespace(
        attest=lambda: calls.append("attest"),
        admin=admin,
    )
    return deployment, responses, calls


@pytest.mark.parametrize(
    "gateway_fenced,workspace_active,expected_fenced",
    [
        (False, 1, False),
        (True, 1, True),
        (False, 0, True),
        (True, 0, True),
    ],
)
def test_installed_board_observes_gateway_and_ledger_before_marking_live(
    observer,
    gateway_fenced,
    workspace_active,
    expected_fenced,
):
    deployment, responses, calls = observer
    responses["status"]["result"]["fenced"] = gateway_fenced
    responses["workspace.status"]["result"]["service"]["active"] = workspace_active
    with board(lambda: read_installed_snapshot(deployment)) as server:
        status, _, body = fetch(server, "/status")
    assert status == 200
    result = json.loads(body)
    assert result["live_observation"] is True
    assert result["fenced"] is expected_fenced
    assert result["seats"][0]["queued"] == 2
    assert "PRIVATE" not in body.decode() and b"private-root" not in body
    assert set(result) == {"live_observation", "fenced", "seats", "delivery"}
    assert calls == ["attest", "status", "workspace.status"]


@pytest.mark.parametrize(
    "method,response",
    [
        ("status", OSError("PRIVATE-CONNECTION-FAILURE")),
        ("status", {"ok": False, "error": {"code": "PRIVATE-DENIED"}}),
        ("status", {"ok": True, "result": {"fenced": 0}}),
        ("workspace.status", OSError("PRIVATE-CONNECTION-FAILURE")),
        ("workspace.status", {"ok": False}),
        (
            "workspace.status",
            {"ok": True, "result": {"service": {"active": True}, "seats": []}},
        ),
        ("workspace.status", {"ok": True, "result": {"service": {"active": 1}}}),
    ],
)
def test_installed_board_never_falls_back_to_active_ledger_on_observation_failure(
    observer, method, response
):
    deployment, responses, _ = observer
    with board(lambda: read_installed_snapshot(deployment)) as server:
        first = fetch(server, "/status")
        assert json.loads(first[2])["live_observation"] is True
        responses[method] = response
        status, _, body = fetch(server, "/status")
    assert status == 503 and body == b"Status unavailable"


def test_installed_board_attestation_failure_calls_no_gateway_method(observer):
    deployment, _, calls = observer

    def reject():
        raise ValueError("PRIVATE-POLICY-CHANGE")

    deployment.attest = reject
    with board(lambda: read_installed_snapshot(deployment)) as server:
        status, _, body = fetch(server, "/status")
    assert status == 503 and body == b"Status unavailable" and calls == []


@pytest.mark.parametrize(
    "arguments",
    [[], ["--state", "/synthetic/db", "--deployment", "/synthetic/deployment"]],
)
def test_board_cli_requires_exactly_one_observation_source(arguments):
    from deskd.workspace.__main__ import main

    with pytest.raises(SystemExit) as error:
        main(["board", *arguments])
    assert error.value.code == 2


@pytest.mark.parametrize("mode", ["state", "deployment"])
def test_board_cli_selects_read_only_observer(mode, tmp_path, monkeypatch, capsys):
    from deskd.workspace import board as board_module, deployment as deployment_module
    from deskd.workspace.__main__ import main

    path = tmp_path / "synthetic"
    path.write_text("synthetic fixture, never opened as a real database")
    calls = []
    installed = object()
    monkeypatch.setattr(
        deployment_module,
        "Deployment",
        lambda candidate: calls.append(("deployment", candidate)) or installed,
    )
    monkeypatch.setattr(
        board_module,
        "read_installed_snapshot",
        lambda candidate: calls.append(("live", candidate)) or snapshot(),
    )
    monkeypatch.setattr(
        board_module,
        "read_snapshot",
        lambda candidate: calls.append(("recorded", candidate)) or snapshot(),
    )

    class Server:
        server_port = 12345

        def __init__(self, callback):
            self.callback = callback

        def serve_forever(self, **_):
            self.callback()

        def server_close(self):
            calls.append(("closed",))

    monkeypatch.setattr(
        board_module, "make_server", lambda callback, **_: Server(callback)
    )
    assert main(["board", "--" + mode, str(path)]) == 0
    if mode == "deployment":
        assert calls == [("deployment", path), ("live", installed), ("closed",)]
    else:
        assert calls == [("recorded", path), ("recorded", path), ("closed",)]
    assert capsys.readouterr().err == ""
