"""Durable wake scheduling with a deterministic in-process runtime double."""

import pytest

from deskd.workspace.scheduler import WorkspaceScheduler
from deskd.workspace.store import WorkspaceError, WorkspaceStore


class Runtime:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.status = "unknown"
        self.observer = None

    def start_turn(self, root_id, events):
        self.calls.append((root_id, events))
        if self.observer:
            self.observer()
        if self.fail:
            raise OSError("synthetic upstream private diagnostic")
        return "turn-" + str(len(self.calls))

    def read_turn(self, root_id, turn_id):
        return self.status


@pytest.fixture
def environment(tmp_path):
    store = WorkspaceStore(tmp_path / "workspace.sqlite", clock=lambda: 100.0)
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
    runtime = Runtime()
    return store, runtime, WorkspaceScheduler(store, runtime, generation)


def enqueue(store, request_id="1"):
    return store.enqueue(
        "demo/analyst",
        "demo/analyst",
        "untrusted message " + request_id,
        request_id=request_id,
    )


def test_intent_committed_before_external_call_without_open_write_lock(environment):
    store, runtime, scheduler = environment
    enqueue(store)

    def observer():
        other = WorkspaceStore(store.db_path)
        assert other.snapshot()["dispatches"][0]["state"] == "delivering"
        # A second connection can write while the external call is pending.
        enqueue(other, "concurrent")

    runtime.observer = observer
    result = scheduler.tick()
    assert result["state"] == "delivered"
    assert runtime.calls[0][0] == "root-a"
    assert runtime.calls[0][1][0]["body"] == "untrusted message 1"
    assert scheduler.tick() is None
    assert len(runtime.calls) == 1
    scheduler.complete_turn("root-a", result["turn_id"])
    assert store.inbox("demo/analyst")[0]["state"] == "delivered"
    runtime.observer = None
    assert scheduler.tick()["state"] == "delivered"


def test_ambiguous_start_remains_unknown_and_never_auto_retries(environment):
    store, runtime, scheduler = environment
    enqueue(store)
    runtime.fail = True
    result = scheduler.tick()
    assert result["state"] == "unknown"
    assert "private" not in str(store.snapshot())
    runtime.fail = False
    enqueue(store, "later")
    assert scheduler.tick() is None
    assert len(runtime.calls) == 1
    with pytest.raises(WorkspaceError, match="reconciliation_unproven"):
        scheduler.reconcile_turn(result["id"], "root-a", "turn-1")
    assert scheduler.tick() is None
    runtime.status = "completed"
    # A turn on the same root, even completed, does not associate this dispatch.
    with pytest.raises(WorkspaceError, match="reconciliation_unproven"):
        scheduler.reconcile_turn(result["id"], "root-a", "turn-1")
    store.reconcile(
        result["id"],
        generation=scheduler.generation,
        root_id="root-a",
        turn_id="turn-1",
        outcome="completed",
        human_confirmed=True,
    )
    assert scheduler.tick()["state"] == "delivered"
    assert len(runtime.calls) == 2


def test_forged_reconcile_root_rejected_before_read(environment):
    store, runtime, scheduler = environment
    enqueue(store)
    runtime.fail = True
    dispatch = scheduler.tick()
    runtime.status = "completed"
    with pytest.raises(WorkspaceError, match="reconciliation_conflict"):
        scheduler.reconcile_turn(dispatch["id"], "other-root", "turn-1")


def test_idle_polling_does_not_call_model(environment):
    store, runtime, scheduler = environment
    for _ in range(5):
        assert scheduler.tick() is None
    store.schedule_timer("demo/analyst", due_at=101, body="future", request_id="timer")
    assert scheduler.tick() is None
    assert runtime.calls == []


def test_native_busy_root_is_skipped_without_charging_budget(environment):
    store, runtime, scheduler = environment
    enqueue(store)
    assert scheduler.tick(blocked_principals=frozenset({"demo/analyst"})) is None
    assert store.snapshot()["seats"][0]["turns_used"] == 0
    assert runtime.calls == []
    assert scheduler.tick()["state"] == "delivered"


def test_due_timer_wakes_and_completion_never_auto_acknowledges(environment):
    store, runtime, scheduler = environment
    store.schedule_timer("demo/analyst", due_at=99, body="due", request_id="timer")
    result = scheduler.tick()
    assert result["state"] == "delivered"
    scheduler.complete_turn("root-a", result["turn_id"], status="failed")
    assert store.inbox("demo/analyst")[0]["state"] == "delivered"
    assert scheduler.tick() is None
    assert len(runtime.calls) == 1


def test_bad_turn_response_is_unknown(environment):
    store, runtime, scheduler = environment
    enqueue(store)
    runtime.start_turn = lambda root, events: None
    assert scheduler.tick()["state"] == "unknown"
    assert scheduler.tick() is None


def test_reconcile_running_turn_keeps_busy_until_completion(environment):
    store, runtime, scheduler = environment
    enqueue(store)
    dispatch = scheduler.tick()
    store.mark_unknown(dispatch["id"], scheduler.generation)
    runtime.status = "inProgress"
    scheduler.reconcile_turn(dispatch["id"], "root-a", "turn-1")
    enqueue(store, "later")
    assert scheduler.tick() is None
    scheduler.complete_turn("root-a", "turn-1")
    runtime.fail = False
    assert scheduler.tick()["state"] == "delivered"


def test_unknown_start_cannot_be_resolved_without_human_association(environment):
    store, runtime, scheduler = environment
    enqueue(store)
    runtime.fail = True
    dispatch = scheduler.tick()
    for outcome, turn_id in (("completed", "old-turn"), ("not_started", None)):
        with pytest.raises(WorkspaceError, match="reconciliation_unproven"):
            store.reconcile(
                dispatch["id"],
                generation=scheduler.generation,
                root_id="root-a",
                turn_id=turn_id,
                outcome=outcome,
            )
    assert store.dispatch(dispatch["id"])["state"] == "unknown"


def test_known_turn_reconciliation_rejects_other_turn_before_runtime_read(environment):
    store, runtime, scheduler = environment
    enqueue(store)
    dispatch = scheduler.tick()
    store.mark_unknown(dispatch["id"], scheduler.generation)
    with pytest.raises(WorkspaceError, match="turn_conflict"):
        scheduler.reconcile_turn(dispatch["id"], "root-a", "different-turn")
