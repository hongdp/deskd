"""Credential-free application invariants; no OS authentication/sandbox claims."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from deskd.gateway.identity import (
    ActionPolicy, IdentityError, PrincipalId, RegistryConflict, TransportEvidence,
)
from deskd.gateway.registry import Registry


MANIFEST = "a" * 64
ALPHA = PrincipalId("sample", "alpha")
BETA = PrincipalId("sample", "beta")
CAPS = frozenset({"state.read", "order.submit", "grant.issue"})


def metadata(session="root-alpha", thread=None, **arguments):
    return {"_meta": {"sessionId": session, "threadId": thread or session},
            "arguments": arguments}


@pytest.fixture
def ready(tmp_path):
    now = [10.0]
    registry = Registry(tmp_path / "gateway.db", harness_uid=1234, clock=lambda: now[0])
    service = registry.start_service()
    binding = registry.trusted_bind(
        ALPHA, root_session_id="root-alpha", manifest_hash=MANIFEST,
        capabilities=CAPS, expected_binding_generation=0)
    registry.trusted_activate_service(expected_service_generation=service)
    registry.trusted_grant_lease(
        ALPHA, connection_id="channel-1", service_generation=service,
        root_session_id="root-alpha", binding_generation=binding.binding_generation,
        manifest_hash=MANIFEST, ttl_seconds=30)
    return registry, now, TransportEvidence(1234, "channel-1", service)


def test_top_level_metadata_and_server_action_are_the_only_identity_inputs(ready):
    registry, _, transport = ready
    call = registry.authorize("order.submit", metadata(
        role="beta", principal="sample/beta", epoch=999,
        _meta={"sessionId": "root-beta", "threadId": "root-beta"},
        capability="everything", root_only=False), transport)
    assert call.principal == ALPHA
    assert call.binding_generation == 1


@pytest.mark.parametrize("params", [None, [], {}, {"_meta": []},
    {"arguments": {"_meta": {"sessionId": "root-alpha", "threadId": "root-alpha"}}},
    {"_meta": {"sessionId": "root-alpha"}},
    {"_meta": {"sessionId": 123, "threadId": "root-alpha"}},
    {"_meta": {"sessionId": " root-alpha", "threadId": "root-alpha"}},
    {"_meta": {"sessionId": "root-alpha", "threadId": True}},
])
def test_missing_malformed_or_nested_metadata_is_rejected(ready, params):
    registry, _, transport = ready
    with pytest.raises(IdentityError):
        registry.authorize("order.submit", params, transport)


def test_child_can_read_but_cannot_make_critical_mutation(ready):
    registry, _, transport = ready
    params = metadata(thread="child-named-trader")
    assert registry.authorize("state.read", params, transport).principal == ALPHA
    with pytest.raises(IdentityError, match="root_required"):
        registry.authorize("order.submit", params, transport)


def test_critical_root_predicate_cannot_be_disabled_by_action_configuration(tmp_path):
    r = Registry(tmp_path / "g.db", harness_uid=1,
                 actions={"order.submit": ActionPolicy("submit", root_only=False)})
    service = r.start_service()
    r.trusted_bind(ALPHA, root_session_id="r", manifest_hash=MANIFEST,
                   capabilities=frozenset({"submit"}), expected_binding_generation=0)
    r.trusted_activate_service(expected_service_generation=service)
    r.trusted_grant_lease(ALPHA, connection_id="c", service_generation=service,
                          root_session_id="r", binding_generation=1,
                          manifest_hash=MANIFEST, ttl_seconds=10)
    with pytest.raises(IdentityError, match="root_required"):
        r.authorize("order.submit", metadata("r", "child"), TransportEvidence(1, "c", service))


def test_unknown_actions_capabilities_peers_and_channels_fail_closed(ready):
    r, _, t = ready
    for action, transport, reason in [
        ("arbitrary.execute", t, "unknown_action"),
        ("order.cancel", t, "capability_denied"),
        ("order.submit", TransportEvidence(999, t.connection_id, t.service_generation), "untrusted_peer"),
        ("order.submit", TransportEvidence(t.peer_uid, "unregistered", t.service_generation), "inactive_channel"),
        ("order.submit", TransportEvidence(t.peer_uid, t.connection_id, "old-service"), "stale_service_generation"),
    ]:
        with pytest.raises(IdentityError, match=reason):
            r.authorize(action, metadata(), transport)


def test_known_other_root_cannot_cross_the_registered_connection(ready):
    r, _, t = ready
    r.trusted_bind(BETA, root_session_id="root-beta", manifest_hash=MANIFEST,
                   capabilities=CAPS, expected_binding_generation=0)
    with pytest.raises(IdentityError, match="binding_mismatch"):
        r.authorize("order.submit", metadata("root-beta"), t)


def test_expiry_boundary_and_explicit_start_fence(ready):
    r, now, t = ready
    now[0] = 40.0
    with pytest.raises(IdentityError, match="inactive_channel"):
        r.authorize("order.submit", metadata(), t)
    service = r.start_service()
    with pytest.raises(IdentityError, match="service_fenced"):
        r.authorize("order.submit", metadata(), TransportEvidence(1234, "new", service))
    with pytest.raises(IdentityError, match="stale_service_generation"):
        r.authorize("order.submit", metadata(), t)


def test_new_instance_never_adopts_persisted_active_service(ready):
    r, _, t = ready
    reopened = Registry(r.db_path, harness_uid=1234)
    with pytest.raises(IdentityError, match="service_not_started"):
        reopened.authorize("order.submit", metadata(), t)
    new_service = reopened.start_service()
    assert new_service != t.service_generation
    with pytest.raises(IdentityError, match="stale_service_generation"):
        r.authorize("order.submit", metadata(), t)


def test_fence_reactivation_does_not_resurrect_an_old_channel(ready):
    r, _, t = ready
    r.fence(expected_service_generation=t.service_generation)
    with pytest.raises(IdentityError, match="service_fenced"):
        r.authorize("order.submit", metadata(), t)
    r.trusted_activate_service(expected_service_generation=t.service_generation)
    with pytest.raises(IdentityError, match="inactive_channel"):
        r.authorize("order.submit", metadata(), t)
    with pytest.raises(RegistryConflict, match="channel_cannot_be_rebound"):
        r.trusted_grant_lease(ALPHA, connection_id=t.connection_id,
            service_generation=t.service_generation, root_session_id="root-alpha",
            binding_generation=1, manifest_hash=MANIFEST, ttl_seconds=10)


def test_replace_preserves_principal_revokes_channel_and_prevents_root_reassignment(ready):
    r, _, t = ready
    replacement = r.trusted_bind(ALPHA, root_session_id="replacement-root", manifest_hash=MANIFEST,
                                capabilities=CAPS, expected_binding_generation=1)
    assert replacement.principal == ALPHA and replacement.binding_generation == 2
    with pytest.raises(IdentityError, match="inactive_channel"):
        r.authorize("order.submit", metadata(), t)
    for occupied in ("root-alpha", "replacement-root"):
        with pytest.raises(RegistryConflict, match="root_already_owned"):
            r.trusted_bind(BETA, root_session_id=occupied, manifest_hash=MANIFEST,
                           capabilities=CAPS, expected_binding_generation=0)
    with pytest.raises(RegistryConflict, match="binding_generation_conflict"):
        r.trusted_bind(ALPHA, root_session_id="third-root", manifest_hash=MANIFEST,
                       capabilities=CAPS, expected_binding_generation=1)


def test_parallel_compare_and_swap_has_one_winner(ready):
    r, _, _ = ready
    barrier = threading.Barrier(2)
    def replace(root):
        barrier.wait()
        try:
            return r.trusted_bind(ALPHA, root_session_id=root, manifest_hash=MANIFEST,
                                  capabilities=CAPS, expected_binding_generation=1)
        except RegistryConflict as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(replace, ["root-next-one", "root-next-two"]))
    assert sum(x == "binding_generation_conflict" for x in outcomes) == 1
    assert len([x for x in outcomes if not isinstance(x, str)]) == 1


@pytest.mark.parametrize("changes,reason", [
    ({"root_session_id": "other"}, "binding_mismatch"),
    ({"binding_generation": 2}, "binding_mismatch"),
    ({"manifest_hash": "b" * 64}, "binding_mismatch"),
    ({"ttl_seconds": 61}, "lease_too_long"),
    ({"ttl_seconds": float("inf")}, "invalid_lease_duration"),
    ({"ttl_seconds": True}, "invalid_lease_duration"),
])
def test_lease_requires_exact_binding_manifest_and_bounded_ttl(ready, changes, reason):
    r, _, t = ready
    args = dict(connection_id="fresh", service_generation=t.service_generation,
                root_session_id="root-alpha", binding_generation=1,
                manifest_hash=MANIFEST, ttl_seconds=10)
    args.update(changes)
    with pytest.raises(IdentityError, match=reason):
        r.trusted_grant_lease(ALPHA, **args)


def test_revoke_blocks_channel_and_stale_admin(ready):
    r, _, t = ready
    revoked = r.trusted_revoke(ALPHA, expected_binding_generation=1)
    assert revoked.status == "revoked" and revoked.binding_generation == 2
    with pytest.raises(IdentityError, match="inactive_channel"):
        r.authorize("state.read", metadata(), t)
    with pytest.raises(RegistryConflict, match="binding_generation_conflict"):
        r.trusted_revoke(ALPHA, expected_binding_generation=1)


def test_registry_refuses_existing_v1_database(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE agent_registry(role TEXT)")
    with pytest.raises(IdentityError, match="not_a_gateway_registry"):
        Registry(path, harness_uid=1)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("agent_registry",)]


def test_borrowed_authorization_shares_domain_transaction_without_committing(ready):
    r, _, t = ready
    with sqlite3.connect(r.db_path) as conn:
        conn.execute("CREATE TABLE effects(value TEXT)")
        conn.execute("BEGIN")
        r.authorize("order.submit", metadata(), t, connection=conn)
        conn.execute("INSERT INTO effects VALUES ('fake effect')")
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute("SELECT * FROM effects").fetchall() == []


def test_borrowed_authorization_rejects_wrong_database_and_no_transaction(ready, tmp_path):
    r, _, t = ready
    with sqlite3.connect(r.db_path) as conn:
        with pytest.raises(IdentityError, match="authorization_requires_transaction"):
            r.authorize("order.submit", metadata(), t, connection=conn)
    with sqlite3.connect(tmp_path / "wrong.db") as conn:
        conn.execute("BEGIN")
        with pytest.raises(IdentityError, match="authorization_database_mismatch"):
            r.authorize("order.submit", metadata(), t, connection=conn)


def test_stale_deferred_snapshot_cannot_authorize_after_fence(ready):
    r, _, t = ready
    conn = sqlite3.connect(r.db_path, isolation_level=None, timeout=.1)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN")
        assert conn.execute("SELECT active FROM service").fetchone()[0] == 1
        r.fence(expected_service_generation=t.service_generation)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            r.authorize("order.submit", metadata(), t, connection=conn)
        conn.rollback()
        conn.execute("BEGIN")
        with pytest.raises(IdentityError, match="service_fenced"):
            r.authorize("order.submit", metadata(), t, connection=conn)
    finally:
        conn.close()


@pytest.mark.parametrize("path", ["", "   ", ":memory:", "file::memory:"])
def test_registry_requires_explicit_file_path(path):
    with pytest.raises(IdentityError, match="invalid_registry_path"):
        Registry(path, harness_uid=1)


def test_principal_encoding_has_no_separator_ambiguity_and_fits_event_envelope():
    assert len(PrincipalId("a" * 127, "b" * 128).value) == 256
    for desk, seat in [("desk/other", "seat"), ("desk", "seat/other"),
                       ("a" * 128, "b" * 128)]:
        with pytest.raises(IdentityError):
            PrincipalId(desk, seat)


def test_trusted_admin_methods_validate_principal_type(ready):
    r, _, t = ready
    with pytest.raises(IdentityError, match="invalid_principal"):
        r.trusted_revoke("sample/alpha", expected_binding_generation=1)
    with pytest.raises(IdentityError, match="invalid_principal"):
        r.trusted_grant_lease("sample/alpha", connection_id="new",
            service_generation=t.service_generation, root_session_id="root-alpha",
            binding_generation=1, manifest_hash=MANIFEST, ttl_seconds=10)


def test_attached_business_database_is_rejected(ready, tmp_path):
    r, _, t = ready
    with sqlite3.connect(r.db_path) as conn:
        conn.execute("ATTACH DATABASE ? AS other", (str(tmp_path / "other.db"),))
        conn.execute("BEGIN")
        with pytest.raises(IdentityError, match="authorization_database_mismatch"):
            r.authorize("order.submit", metadata(), t, connection=conn)
