"""Integration of authorization, domain SQL and two-database delivery.

All principals and transport evidence are synthetic. No harness, socket,
credential, model, or broker is involved in these tests.
"""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Event

import pytest

from deskd.gateway.commands import CommandHandler, GatewayCommands
from deskd.gateway.events import EventConflict, GatewayEventStore
from deskd.gateway.identity import ActionPolicy, IdentityError, PrincipalId, TransportEvidence
from deskd.gateway.registry import Registry


def apply_note(conn, arguments, identity):
    amount = arguments["amount"]
    if type(amount) is not int or amount <= 0:
        raise ValueError("amount must be positive")
    conn.execute("CREATE TABLE IF NOT EXISTS notes (principal TEXT PRIMARY KEY, amount INTEGER)")
    conn.execute("INSERT INTO notes VALUES (?, ?) ON CONFLICT(principal) "
                 "DO UPDATE SET amount=amount+excluded.amount",
                 (identity.principal.value, amount))
    return {"principal": identity.principal.value, "accepted": amount}


def request(root="root-a", *, thread=None, amount=2, **arguments):
    return {"name": "note.create", "arguments": {"amount": amount, **arguments},
            "_meta": {"sessionId": root, "threadId": root if thread is None else thread}}


def lease(registry, binding, service, channel):
    registry.trusted_grant_lease(
        binding.principal, connection_id=channel, service_generation=service,
        root_session_id=binding.root_session_id,
        binding_generation=binding.binding_generation,
        manifest_hash=binding.manifest_hash, ttl_seconds=30)
    return TransportEvidence(12001, channel, service)


@pytest.fixture
def gateway(tmp_path):
    registry = Registry(tmp_path / "gateway.db", harness_uid=12001,
                        actions={"note.create": ActionPolicy("note.write", root_only=True)})
    service = registry.start_service()
    principal = PrincipalId("demo", "seat-a")
    binding = registry.trusted_bind(
        principal, root_session_id="root-a", manifest_hash="a" * 64,
        capabilities=frozenset({"note.write"}), expected_binding_generation=0)
    registry.trusted_activate_service(expected_service_generation=service)
    transport = lease(registry, binding, service, "connection-a")
    events = GatewayEventStore(registry.db_path)
    commands = GatewayCommands(registry, events, handlers={
        "note.create": CommandHandler("note.created", apply_note)})
    return registry, events, commands, transport, principal


def test_committed_identity_is_server_owned_and_replay_does_not_reapply(gateway):
    registry, events, commands, transport, principal = gateway
    params = request(role="seat-b", sessionId="root-b", principal="demo/seat-b")
    first = commands.execute("request-1", params, transport)
    assert commands.execute("request-1", params, transport) == first
    assert first.result == {"principal": principal.value, "accepted": 2}
    assert first.event["principal_id"] == principal.value
    assert first.event["provenance"]["root_session_id"] == "root-a"
    assert events.events() == [{"sequence": first.sequence, "event": first.event}]
    with sqlite3.connect(registry.db_path) as conn:
        assert conn.execute("SELECT * FROM notes").fetchall() == [(principal.value, 2)]


def test_changed_content_cannot_reuse_request_identity(gateway):
    registry, events, commands, transport, _ = gateway
    commands.execute("request-1", request(), transport)
    with pytest.raises(EventConflict):
        commands.execute("request-1", request(amount=3), transport)
    assert len(events.events()) == 1
    with sqlite3.connect(registry.db_path) as conn:
        assert conn.execute("SELECT amount FROM notes").fetchone() == (2,)


def test_child_and_forged_top_level_root_cannot_mutate(gateway):
    _, events, commands, transport, _ = gateway
    for params, reason in [(request(thread="child-a"), "root_required"),
                           (request("root-b"), "binding_mismatch")]:
        with pytest.raises(IdentityError, match=reason):
            commands.execute("request-1", params, transport)
    assert events.events() == []


def test_revoked_principal_cannot_read_a_private_replay_receipt(gateway):
    registry, _, commands, transport, principal = gateway
    commands.execute("request-1", request(), transport)
    registry.trusted_revoke(principal, expected_binding_generation=1)
    with pytest.raises(IdentityError):
        commands.execute("request-1", request(), transport)


def test_same_principal_new_root_returns_original_receipt_and_provenance(gateway):
    registry, _, commands, transport, principal = gateway
    first = commands.execute("request-1", request(), transport)
    binding = registry.trusted_bind(
        principal, root_session_id="root-a-next", manifest_hash="a" * 64,
        capabilities=frozenset({"note.write"}), expected_binding_generation=1)
    new_transport = lease(registry, binding, transport.service_generation, "connection-next")
    assert commands.execute("request-1", request("root-a-next"), new_transport) == first
    assert first.event["provenance"]["root_session_id"] == "root-a"


def test_fence_between_initial_check_and_transaction_blocks_effect(gateway, monkeypatch):
    registry, events, commands, transport, _ = gateway
    original = events.publish

    def fence_then_publish(*args, **kwargs):
        registry.fence(expected_service_generation=transport.service_generation)
        return original(*args, **kwargs)

    monkeypatch.setattr(events, "publish", fence_then_publish)
    with pytest.raises(IdentityError, match="service_fenced"):
        commands.execute("request-1", request(), transport)
    assert events.events() == []


def test_fence_serializes_after_already_committing_effect(gateway):
    registry, events, _, transport, _ = gateway
    effect_entered, release_effect, fence_started, fence_done = (Event() for _ in range(4))

    def delayed_effect(conn, arguments, identity):
        effect_entered.set()
        assert release_effect.wait(timeout=5)
        return apply_note(conn, arguments, identity)

    commands = GatewayCommands(registry, events, handlers={
        "note.create": CommandHandler("note.created", delayed_effect)})

    def fence():
        fence_started.set()
        registry.fence(expected_service_generation=transport.service_generation)
        fence_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        committed = pool.submit(commands.execute, "request-1", request(), transport)
        try:
            assert effect_entered.wait(timeout=5)
            fenced = pool.submit(fence)
            assert fence_started.wait(timeout=5)
            assert not fence_done.wait(timeout=0.05)
        finally:
            release_effect.set()
        receipt = committed.result(timeout=5)
        fenced.result(timeout=5)
    assert events.events() == [{"sequence": receipt.sequence, "event": receipt.event}]
    with pytest.raises(IdentityError, match="service_fenced"):
        commands.execute("request-2", request(), transport)


def test_projection_failure_and_ack_loss_do_not_repeat_producer_effect(gateway, tmp_path):
    registry, _, commands, transport, _ = gateway
    receipt = commands.execute("request-1", request(), transport)
    consumer = GatewayEventStore(tmp_path / "coordination.db")

    def project(conn, event):
        conn.execute("CREATE TABLE IF NOT EXISTS projection (value INTEGER)")
        conn.execute("INSERT INTO projection VALUES (?)", (event["payload"]["arguments"]["amount"],))
        return {"applied": event["event_id"]}

    def failed_project(conn, event):
        project(conn, event)
        raise RuntimeError("synthetic consumer crash")

    with pytest.raises(RuntimeError, match="synthetic consumer crash"):
        consumer.consume("board", receipt.event, project=failed_project)
    assert consumer.projection_events("board") == []
    first_ack = consumer.consume("board", receipt.event, project=project)
    assert consumer.consume("board", receipt.event, project=project) == first_ack
    with sqlite3.connect(consumer.db_path) as conn:
        assert conn.execute("SELECT value FROM projection").fetchall() == [(2,)]
    with sqlite3.connect(registry.db_path) as conn:
        assert conn.execute("SELECT amount FROM notes").fetchall() == [(2,)]


def test_split_authorization_and_producer_database_is_rejected(gateway, tmp_path):
    registry, _, _, _, _ = gateway
    other = GatewayEventStore(tmp_path / "wrong-producer.db")
    with pytest.raises(ValueError, match="share one database"):
        GatewayCommands(registry, other, handlers={})


def test_domain_validation_failure_rolls_back_receipt_and_outbox(gateway):
    _, events, commands, transport, _ = gateway
    with pytest.raises(ValueError, match="positive"):
        commands.execute("request-1", request(amount=-1), transport)
    assert events.events() == []
    assert commands.execute("request-1", request(), transport).result["accepted"] == 2
