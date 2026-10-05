"""Closed management protocol and gateway-only SQLite writer composition.

The management transport here is synthetic; kernel access is a separate Linux
acceptance test. No service listener or existing process is started.
"""

import pytest

from deskd.gateway.identity import IdentityError, PrincipalId
from deskd.workspace.scheduler import WorkspaceScheduler
from deskd.workspace.service import RemoteWorkspaceStore, WorkspaceGateway
from deskd.workspace.store import WorkspaceError


@pytest.fixture
def service(tmp_path):
    gateway = WorkspaceGateway(
        gateway_db=tmp_path / "gateway.db",
        coordination_db=tmp_path / "workspace.db",
        harness_uid=26002,
        business_gid=26003,
        business_path=tmp_path / "business",
        admin_path=tmp_path / "admin",
        principals=["demo/analyst"],
        activation_check=lambda: None,
    )
    gateway.registry.start_service()
    handlers = gateway._handlers()
    calls = []

    def admin(method, params):
        calls.append((method, params))
        try:
            return {"ok": True, "result": handlers[method](params)}
        except IdentityError as exc:
            return {"ok": False, "error": {"code": exc.code}}

    return gateway, RemoteWorkspaceStore(admin), calls


def activate(store):
    store.register_seat("demo/analyst", "root-a", "a" * 64)
    generation = store.start_service()
    store.activate(
        generation,
        {
            "demo/analyst": {
                "root_id": "root-a",
                "manifest_hash": "a" * 64,
                "binding_generation": 1,
            }
        },
    )
    return generation


def test_remote_controller_lifecycle_and_scheduler_use_only_fixed_admin_rpc(service):
    gateway, store, calls = service
    generation = activate(store)
    store.trusted_enqueue("demo/analyst", "one", request_id="human-1")

    class Runtime:
        def start_turn(self, root_id, events):
            assert root_id == "root-a"
            assert events[0]["sender"] == "@supervisor"
            return "turn-1"

    scheduler = WorkspaceScheduler(store, Runtime(), generation)
    assert scheduler.tick(blocked_principals=frozenset({"demo/analyst"})) is None
    dispatch = scheduler.tick()
    assert dispatch["state"] == "delivered"
    scheduler.complete_turn("root-a", "turn-1")
    assert store.dispatch(dispatch["id"])["state"] == "completed"
    assert gateway.store.inbox("demo/analyst")[0]["state"] == "delivered"
    store.fence(generation)
    assert store.snapshot()["service"]["active"] == 0
    assert all(method.startswith("workspace.store.") for method, _ in calls)
    assert not hasattr(store, "db_path")


def test_remote_management_preserves_codes_and_cas_and_timer_ownership(service):
    gateway, store, _ = service
    generation = activate(store)
    store.set_paused("demo/analyst", True, expected_version=1)
    with pytest.raises(WorkspaceError, match="version_conflict"):
        store.set_paused("demo/analyst", False, expected_version=1)
    timer = store.schedule_timer(
        "demo/analyst", due_at=0, body="timer", request_id="timer-1"
    )
    assert store.fire_timers(generation) == 1
    store.cancel_timer("demo/analyst", timer["id"])
    store.set_budget("demo/analyst", 50, expected_version=2)
    assert store.snapshot()["seats"][0]["budget_turns"] == 50


def test_management_endpoint_rejects_unknown_methods_fields_and_business_access(
    service,
):
    gateway, store, calls = service
    with pytest.raises(AttributeError):
        store.execute_sql("DELETE FROM seats")
    with pytest.raises(WorkspaceError, match="invalid_management_params"):
        store.register_seat("demo/a", "root-a", "a" * 64, arbitrary=True)
    assert calls == []
    handlers = gateway._handlers()
    with pytest.raises(IdentityError, match="invalid_management_params"):
        handlers["workspace.store.start_service"]({"query": "arbitrary"})
    with pytest.raises(IdentityError, match="unknown_business_method"):
        gateway.transport._business_call("workspace.store.start_service", {}, None)
    with pytest.raises(IdentityError, match="unknown_business_method"):
        gateway.transport._business_call("workspace.bindings", {}, None)


def test_bindings_projection_excludes_channels_and_caller_paths(service):
    gateway, _, _ = service
    gateway.registry.trusted_bind(
        PrincipalId("demo", "analyst"),
        root_session_id="root-a",
        manifest_hash="a" * 64,
        capabilities=frozenset({"inbox.read"}),
        expected_binding_generation=0,
    )
    result = gateway._handlers()["workspace.bindings"]({})
    assert result == [
        {
            "principal": "demo/analyst",
            "root_id": "root-a",
            "binding_generation": 1,
            "manifest_hash": "a" * 64,
            "status": "bound",
            "capabilities": ["inbox.read"],
        }
    ]


def test_remote_does_not_retry_unknown_management_outcome():
    calls = []

    def dropped(method, params):
        calls.append(method)
        raise OSError("synthetic lost ACK")

    with pytest.raises(OSError):
        RemoteWorkspaceStore(dropped).start_service()
    assert len(calls) == 1
    with pytest.raises(WorkspaceError, match="unknown_management_outcome"):
        RemoteWorkspaceStore(lambda *_: {"ok": True}).start_service()


def test_remote_reconcile_known_turn_and_revocation(service):
    gateway, store, _ = service
    generation = activate(store)
    store.trusted_enqueue("demo/analyst", "one", request_id="human-1")
    dispatch = store.claim_next(generation)
    store.mark_unknown(dispatch["id"], generation)
    store.reconcile(
        dispatch["id"],
        generation=generation,
        root_id="root-a",
        turn_id="turn-1",
        outcome="completed",
        human_confirmed=True,
    )
    store.revoke("demo/analyst", expected_version=1)
    with pytest.raises(WorkspaceError, match="principal_revoked"):
        store.trusted_enqueue("demo/analyst", "later", request_id="human-2")


def bind_for_revoke(gateway, store):
    activate(store)
    gateway.registry.trusted_bind(
        PrincipalId("demo", "analyst"),
        root_session_id="root-a",
        manifest_hash="a" * 64,
        capabilities=frozenset({"inbox.read"}),
        expected_binding_generation=0,
    )
    return {
        "principal": "demo/analyst",
        "expected_binding_generation": 1,
        "expected_version": 1,
    }


def test_public_revoke_removes_gateway_authority_and_scheduling_idempotently(service):
    gateway, store, _ = service
    params = bind_for_revoke(gateway, store)
    handler = gateway._handlers()["workspace.revoke"]
    first = handler(params)
    assert first == {
        "principal": "demo/analyst",
        "revoked": True,
        "binding_generation": 2,
        "version": 2,
    }
    assert handler(params) == first
    assert gateway._handlers()["workspace.bindings"]({})[0]["status"] == "revoked"
    assert store.snapshot()["seats"][0]["revoked"] == 1


def test_public_revoke_partial_failure_keeps_authority_revoked_and_can_finish(
    service, monkeypatch
):
    gateway, store, _ = service
    params = bind_for_revoke(gateway, store)
    handler = gateway._handlers()["workspace.revoke"]
    original = gateway.store.revoke

    def fail(*args, **kwargs):
        raise WorkspaceError("synthetic_store_failure")

    monkeypatch.setattr(gateway.store, "revoke", fail)
    with pytest.raises(IdentityError, match="synthetic_store_failure"):
        handler(params)
    assert gateway._handlers()["workspace.bindings"]({})[0]["status"] == "revoked"
    assert store.snapshot()["seats"][0]["revoked"] == 0
    monkeypatch.setattr(gateway.store, "revoke", original)
    assert handler(params)["revoked"] is True


def test_public_revoke_checks_both_versions_before_removing_authority(service):
    gateway, store, _ = service
    params = bind_for_revoke(gateway, store)
    with pytest.raises(IdentityError, match="version_conflict"):
        gateway._handlers()["workspace.revoke"]({**params, "expected_version": 99})
    assert gateway._handlers()["workspace.bindings"]({})[0]["status"] == "bound"
