"""Actual SQLite coordination failure and isolation cases, no runtime services."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from deskd.gateway.events import canonical_json
from deskd.workspace.store import WorkspaceError, WorkspaceStore


@pytest.fixture
def store(tmp_path):
    value = WorkspaceStore(tmp_path / "workspace.sqlite", clock=lambda: 100.0)
    for role in ("operator", "reviewer", "engineer"):
        value.register_seat("demo/" + role, "root-" + role, "a" * 64)
    return value


def activate(store):
    generation = store.start_service()
    seats = store.snapshot()["seats"]
    store.activate(
        generation,
        {
            seat["principal"]: {
                key: seat[key]
                for key in ("root_id", "manifest_hash", "binding_generation")
            }
            for seat in seats
            if not seat["revoked"]
        },
    )
    return generation


def expect(code):
    return pytest.raises(WorkspaceError, match="^" + code + "$")


def event(actor="demo/operator", event_id="e1", action="mail.send", arguments=None):
    value = {
        "event_id": event_id,
        "principal_id": actor,
        "event_type": "workspace." + action,
        "payload": {
            "action": action,
            "arguments": arguments or {"recipient": "demo/reviewer", "body": "hello"},
        },
    }
    value["fingerprint"] = hashlib.sha256(canonical_json(value).encode()).hexdigest()
    return value


def test_rejects_legacy_database_without_modifying_it(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE private_legacy(id INTEGER)")
    with expect("incompatible_database"):
        WorkspaceStore(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("private_legacy",)]


def test_fixed_root_and_reserved_management_identity(store):
    original = store.register_seat("demo/operator", "root-operator", "a" * 64)
    assert original["root_id"] == "root-operator"
    with expect("binding_conflict"):
        store.register_seat("demo/operator", "root-new", "a" * 64)
    with expect("root_already_bound"):
        store.register_seat("demo/impostor", "root-operator", "a" * 64)
    with expect("reserved_principal"):
        store.register_seat("@supervisor", "root-new", "a" * 64)
    activate(store)
    with expect("registration_requires_fence"):
        store.register_seat("demo/new", "root-new", "a" * 64)


def test_service_start_fenced_and_exact_attestation(store):
    generation = store.start_service()
    with expect("service_fenced"):
        store.claim_next(generation)
    with expect("attestation_mismatch"):
        store.activate(generation, {})
    assert store.snapshot()["service"]["active"] == 0
    active = activate(store)
    with expect("stale_service_generation"):
        store.claim_next(generation)
    store.fence(active)
    with expect("service_fenced"):
        store.claim_next(active)


def test_sender_receipts_visibility_and_atomic_ack(store):
    one = store.enqueue("demo/operator", "demo/reviewer", "hello", request_id="m1")
    two = store.enqueue("demo/operator", "demo/engineer", "secret", request_id="m2")
    assert (
        store.enqueue("demo/operator", "demo/reviewer", "hello", request_id="m1") == one
    )
    with expect("request_conflict"):
        store.enqueue("demo/operator", "demo/reviewer", "changed", request_id="m1")
    assert store.inbox("demo/operator") == []
    assert [item["body"] for item in store.inbox("demo/reviewer")] == ["hello"]
    assert store.inbox("demo/reviewer")[0]["sender"] == "demo/operator"
    with expect("message_not_owned"):
        store.acknowledge("demo/reviewer", [one["id"], two["id"]])
    assert store.inbox("demo/reviewer")[0]["state"] == "queued"
    store.acknowledge("demo/reviewer", [one["id"]])
    assert store.inbox("demo/reviewer") == []
    assert store.acknowledge("demo/reviewer", [one["id"]]) == {"handled": [one["id"]]}


def test_trusted_management_sender_and_read_denied(store):
    message = store.trusted_enqueue("demo/operator", "human task", request_id="human-1")
    assert message["sender"] == "@supervisor"
    with expect("unknown_principal"):
        store.enqueue("@supervisor", "demo/operator", "forged", request_id="x")
    with expect("unknown_principal"):
        store.inbox("@supervisor")


def test_pause_resume_budget_and_revoke_are_distinct(store):
    store.enqueue("demo/operator", "demo/reviewer", "hello", request_id="m1")
    generation = activate(store)
    paused = store.set_paused("demo/reviewer", True, expected_version=1)
    assert paused["revoked"] == 0
    assert store.claim_next(generation) is None
    with expect("version_conflict"):
        store.set_paused("demo/reviewer", False, expected_version=1)
    store.set_paused("demo/reviewer", False, expected_version=2)
    store.set_budget("demo/reviewer", 0, expected_version=3)
    assert store.claim_next(generation) is None
    store.set_budget("demo/reviewer", 1, expected_version=4)
    dispatch = store.claim_next(generation)
    store.mark_delivered(dispatch["id"], "turn-1", generation)
    store.complete_turn("root-reviewer", "turn-1", generation)
    store.enqueue("demo/operator", "demo/reviewer", "next", request_id="m2")
    assert store.claim_next(generation) is None
    store.revoke("demo/reviewer", expected_version=5)
    with expect("principal_revoked"):
        store.inbox("demo/reviewer")
    with expect("principal_revoked"):
        store.set_paused("demo/reviewer", False, expected_version=6)


def test_exact_batch_busy_coalescing_and_handled_independence(store):
    generation = activate(store)
    first = store.enqueue("demo/operator", "demo/reviewer", "one", request_id="1")
    second = store.enqueue("demo/operator", "demo/reviewer", "two", request_id="2")
    dispatch = store.claim_next(generation)
    assert [item["id"] for item in dispatch["events"]] == [first["id"], second["id"]]
    later = store.enqueue("demo/operator", "demo/reviewer", "later", request_id="3")
    assert store.claim_next(generation) is None
    store.acknowledge("demo/reviewer", [first["id"]])
    store.mark_delivered(dispatch["id"], "turn-1", generation)
    store.complete_turn("root-reviewer", "turn-1", generation)
    assert [
        item["state"] for item in store.inbox("demo/reviewer", include_handled=True)
    ] == ["handled", "delivered", "queued"]
    assert [item["id"] for item in store.claim_next(generation)["events"]] == [
        later["id"]
    ]


def test_urgent_events_precede_other_seats_but_do_not_interrupt_busy(store):
    generation = activate(store)
    store.enqueue("demo/operator", "demo/reviewer", "regular", request_id="1")
    store.enqueue(
        "demo/operator", "demo/engineer", "urgent", priority=2, request_id="2"
    )
    assert store.claim_next(generation)["principal"] == "demo/engineer"
    assert store.claim_next(generation)["principal"] == "demo/reviewer"


def test_delivery_and_completion_duplicate_events_preserve_first_fact(store):
    generation = activate(store)
    store.enqueue("demo/operator", "demo/reviewer", "hello", request_id="m1")
    dispatch = store.claim_next(generation)
    store.mark_delivered(dispatch["id"], "turn-1", generation)
    store.mark_delivered(dispatch["id"], "turn-1", generation)
    store.complete_turn("root-reviewer", "turn-1", generation)
    store.complete_turn("root-reviewer", "turn-1", generation)
    with expect("turn_conflict"):
        store.complete_turn("root-reviewer", "turn-1", generation, status="failed")
    events = store.snapshot()["events"]
    assert sum(item["kind"] == "dispatch.delivered" for item in events) == 1
    assert sum(item["kind"] == "turn.completed" for item in events) == 1


@pytest.mark.parametrize("delivered", [False, True])
def test_restart_quarantines_inflight_without_auto_retry(store, delivered):
    generation = activate(store)
    store.enqueue("demo/operator", "demo/reviewer", "hello", request_id="m1")
    dispatch = store.claim_next(generation)
    if delivered:
        store.mark_delivered(dispatch["id"], "turn-1", generation)
    restored = WorkspaceStore(store.db_path)
    new_generation = activate(restored)
    assert restored.dispatch(dispatch["id"])["state"] == "unknown"
    assert restored.inbox("demo/reviewer")[0]["state"] == "unknown"
    assert restored.claim_next(new_generation) is None
    with expect("stale_service_generation"):
        store.mark_delivered(dispatch["id"], "turn-1", generation)
    with expect("reconciliation_conflict"):
        restored.reconcile(
            dispatch["id"],
            generation=new_generation,
            root_id="wrong-root",
            turn_id="turn-1",
            outcome="completed",
        )
    restored.reconcile(
        dispatch["id"],
        generation=new_generation,
        root_id="root-reviewer",
        turn_id="turn-1",
        outcome="completed",
        human_confirmed=not delivered,
    )
    assert restored.inbox("demo/reviewer")[0]["state"] == "delivered"
    assert restored.snapshot()["seats"][2]["active_dispatch"] is None


def test_reconciliation_known_turn_must_not_be_replaced(store):
    generation = activate(store)
    store.enqueue("demo/operator", "demo/reviewer", "hello", request_id="m1")
    dispatch = store.claim_next(generation)
    store.mark_delivered(dispatch["id"], "turn-1", generation)
    store.mark_unknown(dispatch["id"], generation)
    with expect("turn_conflict"):
        store.reconcile(
            dispatch["id"],
            generation=generation,
            root_id="root-reviewer",
            turn_id="turn-fake",
            outcome="completed",
        )
    with expect("turn_conflict"):
        store.reconcile(
            dispatch["id"],
            generation=generation,
            root_id="root-reviewer",
            turn_id=None,
            outcome="not_started",
        )


def test_explicit_proven_non_dispatch_requeues_but_keeps_budget_charge(store):
    generation = activate(store)
    store.enqueue("demo/operator", "demo/reviewer", "hello", request_id="m1")
    dispatch = store.claim_next(generation)
    store.mark_unknown(dispatch["id"], generation)
    store.reconcile(
        dispatch["id"],
        generation=generation,
        root_id="root-reviewer",
        turn_id=None,
        outcome="not_started",
        human_confirmed=True,
    )
    assert store.inbox("demo/reviewer")[0]["state"] == "queued"
    seat = next(
        item
        for item in store.snapshot()["seats"]
        if item["principal"] == "demo/reviewer"
    )
    assert seat["turns_used"] == 1


def test_concurrent_claim_and_enqueue_ack_never_lose_messages(store):
    generation = activate(store)
    first = store.enqueue("demo/operator", "demo/reviewer", "first", request_id="first")
    second_handle = WorkspaceStore(store.db_path)

    def enqueue(index):
        return second_handle.enqueue(
            "demo/operator", "demo/reviewer", str(index), request_id=str(index)
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(enqueue, index) for index in range(30)]
        ack = pool.submit(store.acknowledge, "demo/reviewer", [first["id"]])
        for future in futures:
            future.result()
        ack.result()
        claims = list(
            pool.map(
                lambda _: WorkspaceStore(store.db_path).claim_next(generation), range(5)
            )
        )
    assert sum(item is not None for item in claims) == 1
    assert len(store.inbox("demo/reviewer")) == 30
    assert store.inbox("demo/reviewer", include_handled=True)[0]["state"] == "handled"


def test_enqueue_failure_rolls_back_message_and_receipt(store, monkeypatch):
    original = store._event

    def fail(*args):
        raise RuntimeError("injected write fault")

    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(RuntimeError):
        store.enqueue("demo/operator", "demo/reviewer", "hello", request_id="m1")
    monkeypatch.setattr(store, "_event", original)
    assert store.inbox("demo/reviewer") == []
    assert (
        store.enqueue("demo/operator", "demo/reviewer", "hello", request_id="m1")["id"]
        == 1
    )


def test_timers_coalesce_overdue_ticks_and_require_owner(store):
    generation = activate(store)
    timer = store.schedule_timer(
        "demo/reviewer", due_at=1, body="check", interval_seconds=10, request_id="t1"
    )
    assert store.fire_timers(generation) == 1
    assert store.fire_timers(generation) == 0
    assert store.inbox("demo/reviewer")[0]["kind"] == "timer"
    assert (
        next(
            item
            for item in store.snapshot()["seats"]
            if item["principal"] == "demo/reviewer"
        )["next_trigger_at"]
        == 110
    )
    with expect("timer_not_owned"):
        store.cancel_timer("demo/operator", timer["id"])
    store.cancel_timer("demo/reviewer", timer["id"])


def test_tasks_dependencies_versions_and_visibility(store):
    first = store.add_task("demo/operator", "demo/reviewer", "review", request_id="t1")
    second = store.add_task(
        "demo/operator",
        "demo/operator",
        "publish",
        depends_on=[first["id"]],
        request_id="t2",
    )
    assert store.tasks("demo/engineer") == []
    with expect("task_blocked"):
        store.update_task("demo/operator", second["id"], "done", expected_version=1)
    with expect("task_not_owned"):
        store.update_task("demo/engineer", first["id"], "done", expected_version=1)
    store.update_task("demo/reviewer", first["id"], "done", expected_version=1)
    assert any(
        json.loads(item["body"]).get("dependencies_ready") is True
        for item in store.inbox("demo/operator")
    )
    with expect("version_conflict"):
        store.update_task("demo/reviewer", first["id"], "done", expected_version=1)
    store.update_task("demo/operator", second["id"], "done", expected_version=1)
    assert store.tasks("demo/operator")[1]["status"] == "done"


def test_projection_event_and_receipt_are_atomic_and_deduplicated(store, monkeypatch):
    value = event()
    original = store._event

    def fail_projection(conn, kind, actor, ref):
        if kind == "gateway.projected":
            raise RuntimeError("crash before outer commit")
        original(conn, kind, actor, ref)

    monkeypatch.setattr(store, "_event", fail_projection)
    with pytest.raises(RuntimeError):
        store.apply_gateway_event(value)
    assert store.inbox("demo/reviewer") == []
    monkeypatch.setattr(store, "_event", original)
    first = store.apply_gateway_event(value)
    assert first["applied"] is True
    assert store.apply_gateway_event(value) == first
    assert len(store.inbox("demo/reviewer")) == 1
    with expect("event_conflict"):
        store.apply_gateway_event(
            event(arguments={"recipient": "demo/reviewer", "body": "changed"})
        )


def test_projection_rejection_durable_and_does_not_stall_other_events(store):
    rejected = store.apply_gateway_event(
        event(arguments={"recipient": "demo/missing", "body": "hello"})
    )
    assert rejected == {"applied": False, "error": "unknown_principal"}
    assert (
        store.apply_gateway_event(
            event(arguments={"recipient": "demo/missing", "body": "hello"})
        )
        == rejected
    )
    assert store.apply_gateway_event(event(event_id="next"))["applied"] is True


def test_projection_receipt_is_filtered_to_actor_and_exposes_rejection(store):
    assert store.gateway_receipt("demo/operator", "e1") is None
    applied = store.apply_gateway_event(event())
    assert store.gateway_receipt("demo/operator", "e1") == applied
    assert store.gateway_receipt("demo/reviewer", "e1") is None
    rejected = store.apply_gateway_event(
        event(
            event_id="rejected",
            arguments={"recipient": "demo/missing", "body": "hello"},
        )
    )
    assert store.gateway_receipt("demo/operator", "rejected") == rejected
    assert store.gateway_receipt("demo/reviewer", "rejected") is None


def test_dispatch_byte_budget_splits_dense_escaped_messages_without_loss(store):
    generation = activate(store)
    for index in range(10):
        store.enqueue(
            "demo/operator", "demo/reviewer", "\x01" * 60_000, request_id=str(index)
        )
    first = store.claim_next(generation)
    assert len(first["events"]) == 1
    assert len(canonical_json(first["events"]).encode()) < 512 * 1024
    outer = canonical_json(
        {
            "input": [],
            "toolOutput": {
                "name": "workspace_events",
                "namespace": "deskd",
                "output": canonical_json(first["events"]),
            },
        }
    )
    assert len(outer.encode()) < 2 * 1024 * 1024
    store.mark_delivered(first["id"], "t1", generation)
    store.complete_turn("root-reviewer", "t1", generation)
    second = store.claim_next(generation)
    assert len(second["events"]) == 1
    assert second["events"][0]["id"] != first["events"][0]["id"]
    assert len(store.inbox("demo/reviewer")) == 10


def test_projection_fingerprint_and_ack_ids_are_checked(store):
    tampered = event()
    tampered["payload"]["arguments"]["body"] = "tampered"
    with expect("event_fingerprint_mismatch"):
        store.apply_gateway_event(tampered)
    message = store.apply_gateway_event(event())["result"]
    invalid = store.apply_gateway_event(
        event(
            actor="demo/reviewer",
            event_id="invalid",
            action="inbox.ack",
            arguments={"message_ids": ["01"]},
        )
    )
    assert invalid["error"] == "invalid_ids"
    ack = store.apply_gateway_event(
        event(
            actor="demo/reviewer",
            event_id="ack",
            action="inbox.ack",
            arguments={"message_ids": [str(message["id"])]},
        )
    )
    assert ack["applied"] is True
    assert store.inbox("demo/reviewer") == []


def test_projection_task_commands(store):
    create = event(
        action="task.create",
        arguments={
            "assignee": "demo/reviewer",
            "title": "task",
            "body": "detail",
            "depends_on": [],
        },
    )
    task = store.apply_gateway_event(create)["result"]
    update = event(
        actor="demo/reviewer",
        event_id="update",
        action="task.update",
        arguments={"task_id": task["id"], "status": "done", "expected_version": 1},
    )
    assert store.apply_gateway_event(update)["result"]["status"] == "done"
    assert store.apply_gateway_event(update)["result"]["version"] == 2


def test_snapshot_does_not_expose_private_prose(store):
    store.enqueue("demo/operator", "demo/reviewer", "PRIVATE-MESSAGE", request_id="m1")
    store.add_task(
        "demo/operator",
        "demo/reviewer",
        "PRIVATE-TITLE",
        detail="PRIVATE-DETAIL",
        request_id="t1",
    )
    store.schedule_timer(
        "demo/operator", due_at=100, body="PRIVATE-TIMER", request_id="timer"
    )
    snapshot = json.dumps(store.snapshot())
    assert "PRIVATE" not in snapshot


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf")])
def test_invalid_timer_times_rejected(store, value):
    with expect("invalid_due_at"):
        store.schedule_timer(
            "demo/operator", due_at=value, body="x", request_id="timer"
        )
