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
from deskd.workspace.board import HTML, JS, make_server, public_snapshot
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
        "messages": []
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
