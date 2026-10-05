"""Lifecycle integration with real durable state and synthetic runtime/admin RPCs."""

from collections import deque

import pytest

from deskd.workspace.controller import Seat, WorkspaceController
from deskd.workspace.runtime import RootBinding, RootConfig, RuntimeUnavailable
from deskd.workspace.store import WorkspaceError, WorkspaceStore


class FakeRuntime:
    def __init__(self, trace):
        self.trace = trace
        self.events = deque()
        self.busy = set()
        self.resume_wrong = False
        self.send_error = False
        self.sent = []
        self.closed = False

    def resume_root(self, root_id, config):
        self.trace.append(("resume", root_id))
        return RootBinding(
            root_id, "foreign" if self.resume_wrong else root_id, config.cwd
        )

    def read_root(self, root_id):
        return {"status": {"type": "active" if root_id in self.busy else "idle"}}

    def start_turn(self, root_id, events):
        self.sent.append((root_id, events))
        if self.send_error:
            raise RuntimeUnavailable("synthetic_unknown")
        return "turn-" + str(len(self.sent))

    def read_turn(self, root_id, turn_id):
        return "completed"

    def poll_event(self, timeout=0):
        return self.events.popleft() if self.events else None

    def close(self):
        self.closed = True


@pytest.fixture
def controlled(tmp_path):
    trace = []
    store = WorkspaceStore(tmp_path / "workspace.db")
    roles = []
    for name in ("operator", "reviewer"):
        root = "root-" + name
        store.register_seat("demo/" + name, root, "a" * 64)
        roles.append(
            Seat(
                "demo/" + name,
                root,
                "a" * 64,
                1,
                RootConfig(
                    cwd="/work/" + name,
                    model="mock",
                    model_provider="mock",
                    expected_sandbox={"type": "readOnly", "networkAccess": False},
                ),
            )
        )
    runtime = FakeRuntime(trace)
    state = {
        "alive": True,
        "tampered": False,
        "now": 10.0,
        "connections": [],
        "bindings": [
            {
                "principal": seat.principal,
                "root_id": seat.root_id,
                "manifest_hash": seat.manifest_hash,
                "binding_generation": 1,
                "status": "bound",
            }
            for seat in roles
        ],
    }

    def admin(method, params):
        trace.append((method, params))
        if method == "workspace.bindings":
            return {"ok": True, "result": state["bindings"]}
        if method == "workspace.revoke":
            binding = next(
                row
                for row in state["bindings"]
                if row["principal"] == params["principal"]
            )
            binding.update(status="revoked", binding_generation=2)
            row = next(
                row
                for row in store.snapshot()["seats"]
                if row["principal"] == params["principal"]
            )
            if not row["revoked"]:
                store.revoke(
                    params["principal"], expected_version=params["expected_version"]
                )
        return {
            "ok": True,
            "result": state["connections"] if method == "connections" else {},
        }

    def attest():
        trace.append(("attest", None))
        if state["tampered"]:
            raise WorkspaceError("installation_changed")

    controller = WorkspaceController(
        store,
        runtime,
        admin,
        tuple(roles),
        attest=attest,
        daemon_alive=lambda: state["alive"],
        accepts_pid=lambda pid: pid == 4242,
        clock=lambda: state["now"],
    )
    return controller, store, runtime, state, trace


def test_recovery_fences_before_resuming_and_never_rebinds(controlled):
    controller, store, _, _, trace = controlled
    controller.start()
    kinds = [entry[0] for entry in trace]
    assert kinds[0] == "fence"
    assert kinds.count("resume") == 2
    assert kinds.index("fence") < kinds.index("resume") < kinds.index("activate")
    assert "bind" not in kinds and "lease" not in kinds
    assert store.snapshot()["service"]["active"] == 1


def test_wrong_root_identity_keeps_both_services_fenced(controlled):
    controller, store, runtime, _, trace = controlled
    runtime.resume_wrong = True
    with pytest.raises(WorkspaceError, match="root_identity_changed"):
        controller.start()
    assert not any(kind == "activate" for kind, _ in trace)
    assert trace[-1][0] == "fence"
    assert store.snapshot()["service"]["active"] == 0


def test_configuration_tamper_fences_before_new_work(controlled):
    controller, store, runtime, state, trace = controlled
    controller.start()
    store.trusted_enqueue("demo/operator", "queued work", request_id="work-1")
    state["tampered"] = True
    state["now"] += 2
    with pytest.raises(WorkspaceError, match="installation_changed"):
        controller.tick()
    assert not runtime.sent
    assert trace[-1][0] == "fence"
    assert store.snapshot()["service"]["active"] == 0


def test_dead_daemon_fences_before_leases_or_dispatch(controlled):
    controller, store, runtime, state, trace = controlled
    controller.start()
    state["alive"] = False
    before = len(trace)
    with pytest.raises(WorkspaceError, match="managed_daemon_unavailable"):
        controller.tick()
    assert [kind for kind, _ in trace[before:]] == ["fence"]
    assert not runtime.sent
    assert store.snapshot()["service"]["active"] == 0


def test_only_known_root_and_managed_descendant_receive_lease(controlled):
    controller, _, _, state, trace = controlled
    controller.start()
    state["connections"] = [
        {"connection_id": "wrong-pid", "requested_root": "root-operator", "pid": 4243},
        {"connection_id": "wrong-root", "requested_root": "foreign", "pid": 4242},
        {"connection_id": "unannounced", "pid": 4242},
        {"connection_id": "valid", "requested_root": "root-operator", "pid": 4242},
    ]
    controller.tick()
    leases = [params for kind, params in trace if kind == "lease"]
    assert len(leases) == 1
    assert leases[0]["connection_id"] == "valid"
    assert leases[0]["binding_generation"] == 1
    assert leases[0]["root_session_id"] == "root-operator"
    assert leases[0]["ttl_seconds"] <= 3


def test_unknown_send_fences_and_never_replays_automatically(controlled):
    controller, store, runtime, _, trace = controlled
    controller.start()
    store.trusted_enqueue("demo/operator", "untrusted event", request_id="work-1")
    runtime.send_error = True
    result = controller.tick()
    assert result["state"] == "unknown"
    assert len(runtime.sent) == 1
    assert store.snapshot()["service"]["active"] == 0
    assert trace[-1][0] == "fence"
    with pytest.raises(WorkspaceError):
        controller.tick()
    assert len(runtime.sent) == 1


def test_busy_native_turn_defers_that_seat_without_blocking_other_seat(controlled):
    controller, store, runtime, _, _ = controlled
    controller.start()
    store.trusted_enqueue("demo/operator", "operator work", request_id="work-1")
    store.trusted_enqueue("demo/reviewer", "reviewer work", request_id="work-2")
    runtime.busy.add("root-operator")
    result = controller.tick()
    assert result["principal"] == "demo/reviewer"
    assert runtime.sent[0][0] == "root-reviewer"
    assert store.inbox("demo/operator")[0]["state"] == "queued"


def test_native_turn_completion_does_not_ack_inbox_or_fence(controlled):
    controller, store, runtime, _, trace = controlled
    controller.start()
    runtime.events.append(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "root-operator",
                "turn": {"id": "native-turn", "status": "completed"},
            },
        }
    )
    assert controller.tick() is None
    assert store.snapshot()["service"]["active"] == 1
    assert sum(kind == "fence" for kind, _ in trace) == 1


def test_owned_turn_completion_releases_busy_without_marking_work_handled(controlled):
    controller, store, runtime, _, _ = controlled
    controller.start()
    store.trusted_enqueue("demo/operator", "operator work", request_id="work-1")
    dispatch = controller.tick()
    runtime.events.append(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "root-operator",
                "turn": {"id": dispatch["turn_id"], "status": "completed"},
            },
        }
    )
    assert controller.tick() is None
    assert store.inbox("demo/operator")[0]["state"] == "delivered"
    assert store.snapshot()["dispatches"][0]["state"] == "completed"


def test_controller_close_fences_and_closes_own_runtime(controlled):
    controller, store, runtime, _, trace = controlled
    controller.start()
    controller.close()
    assert runtime.closed
    assert store.snapshot()["service"]["active"] == 0
    assert trace[-1][0] == "fence"


def test_interrupted_revocation_stays_revoked_without_resuming_or_leasing(controlled):
    controller, store, runtime, state, trace = controlled
    state["bindings"][0].update(status="revoked", binding_generation=2)
    controller.start()
    assert ("resume", "root-operator") not in trace
    assert store.snapshot()["seats"][0]["revoked"] == 1
    state["connections"] = [
        {"connection_id": "old", "requested_root": "root-operator", "pid": 4242}
    ]
    controller.tick()
    assert not any(method == "lease" for method, _ in trace)
    assert store.snapshot()["service"]["active"] == 1
    assert runtime.sent == []


def test_runtime_revocation_does_not_fence_other_seat(controlled):
    controller, store, _, state, trace = controlled
    controller.start()
    state["bindings"][0].update(status="revoked", binding_generation=2)
    store.trusted_enqueue("demo/reviewer", "independent work", request_id="other")
    assert controller.tick()["principal"] == "demo/reviewer"
    assert store.snapshot()["service"]["active"] == 1
    assert sum(method == "fence" for method, _ in trace) == 1


def test_admin_rebinding_is_not_accepted_as_a_new_role(controlled):
    controller, store, runtime, state, _ = controlled
    controller.start()
    state["bindings"][0]["root_id"] = "replacement"
    with pytest.raises(WorkspaceError, match="workspace_binding_mismatch"):
        controller.tick()
    assert not runtime.sent
    assert store.snapshot()["service"]["active"] == 0


def test_all_revoked_roots_recover_without_resuming_or_reauthorizing(controlled):
    controller, store, runtime, state, trace = controlled
    for binding in state["bindings"]:
        binding.update(status="revoked", binding_generation=2)
    controller.start()
    assert all(seat["revoked"] for seat in store.snapshot()["seats"])
    assert store.snapshot()["service"]["active"] == 1
    assert not any(method == "resume" for method, _ in trace)
    assert controller.tick() is None
    assert not any(method == "lease" for method, _ in trace)
    assert runtime.sent == []


def test_bridge_disconnect_between_listing_and_renewal_does_not_fence_workspace(
    controlled,
):
    controller, store, _, state, trace = controlled
    controller.start()
    state["connections"] = [
        {"connection_id": "departed", "requested_root": "root-operator", "pid": 4242},
        {"connection_id": "live", "requested_root": "root-reviewer", "pid": 4242},
    ]
    original = controller.admin

    def disconnect(method, params):
        if method == "lease" and params["connection_id"] == "departed":
            return {"ok": False, "error": {"code": "unknown_live_connection"}}
        return original(method, params)

    controller.admin = disconnect
    assert controller.tick() is None
    assert store.snapshot()["service"]["active"] == 1
    leases = [params for method, params in trace if method == "lease"]
    assert [params["connection_id"] for params in leases] == ["live"]
    assert sum(method == "fence" for method, _ in trace) == 1


@pytest.mark.parametrize(
    "code", ["binding_mismatch", "service_fenced", "channel_cannot_be_rebound"]
)
def test_only_disconnected_channel_error_is_tolerated_during_renewal(controlled, code):
    controller, store, _, state, trace = controlled
    controller.start()
    state["connections"] = [
        {"connection_id": "live", "requested_root": "root-operator", "pid": 4242}
    ]
    original = controller.admin

    def reject(method, params):
        if method == "lease":
            return {"ok": False, "error": {"code": code}}
        return original(method, params)

    controller.admin = reject
    with pytest.raises(WorkspaceError, match="gateway_control_rejected"):
        controller.tick()
    assert store.snapshot()["service"]["active"] == 0
    assert trace[-1][0] == "fence"
