"""Durable goal orchestration using synthetic authoritative facts, no network."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from deskd.workspace.goals import GoalEngine
from deskd.workspace.store import WorkspaceError, WorkspaceStore


@pytest.fixture
def desk(tmp_path):
    clock = [1000.0]
    store = WorkspaceStore(tmp_path / "workspace.sqlite", clock=lambda: clock[0])
    for role in ("researcher", "reviewer", "executor", "stranger"):
        store.register_seat("demo/" + role, "root-" + role, "a" * 64)
    artifacts = {}
    evidence = {}

    def read_artifact(kind, artifact_id):
        if kind == "memo_for_proposal":
            return next((item for (name, _), item in artifacts.items()
                         if name == "memo" and item["proposal_id"] == artifact_id), None)
        return artifacts.get((kind, artifact_id))

    def read_evidence(actor, evidence_id):
        item = evidence.get(evidence_id)
        if item and (actor == "demo/researcher" or item.get("shared")):
            return item
        return None

    engine = GoalEngine(store, artifact_reader=read_artifact, evidence_reader=read_evidence)
    return engine, store, clock, artifacts, evidence


def create(desk, **kwargs):
    return desk[0].create(**{
        "title": "Release brief", "objective": "Describe verified changes and uncertainty.",
        "researcher": "demo/researcher", "reviewer": "demo/reviewer", "executor": "demo/executor",
        "source_ids": ["release-notes"], "request_id": "create", "followup_seconds": 60,
        **kwargs,
    })


def current(desk, goal_id):
    return desk[0].read(goal_id=goal_id)["goals"][0]


def research(desk, goal, *, proposal_id="proposal", evidence_id="evidence"):
    _, _, clock, artifacts, evidence = desk
    body = "Release brief: a change was observed. Source: release-notes."
    artifacts["proposal", proposal_id] = {
        "proposal_id": proposal_id, "author_principal": "demo/researcher",
        "executor_principal": "demo/executor", "body": body,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "created_at": clock[0],
    }
    evidence[evidence_id] = {
        "source_id": "release-notes", "sha256": "e" * 64, "fetched_at": clock[0], "shared": True,
    }
    return desk[0].report(
        "demo/researcher", goal_id=goal["id"], cycle=goal["cycle"], stage="research",
        artifact_id=proposal_id, evidence_ids=[evidence_id], request_id="report-" + proposal_id,
    )


def review(desk, goal, *, approval_id="approval"):
    cycle = goal["cycles"][0]
    desk[3]["approval", approval_id] = {
        "approval_id": approval_id, "proposal_id": cycle["proposal_id"],
        "body_sha256": cycle["body_sha256"], "issuer_principal": "demo/reviewer",
        "executor_principal": "demo/executor", "status": "active", "expires_at": desk[2][0] + 300,
    }
    return desk[0].report(
        "demo/reviewer", goal_id=goal["id"], cycle=goal["cycle"], stage="review",
        artifact_id=approval_id, evidence_ids=[], request_id="report-" + approval_id,
    )


def publish(desk, goal, *, memo_id="memo", report=True):
    cycle = goal["cycles"][0]
    proposal = desk[3]["proposal", cycle["proposal_id"]]
    desk[3]["memo", memo_id] = {
        **proposal, "memo_id": memo_id, "approval_id": cycle["approval_id"],
        "issuer_principal": "demo/reviewer", "published_at": desk[2][0],
    }
    if report:
        return desk[0].report(
            "demo/executor", goal_id=goal["id"], cycle=goal["cycle"], stage="delivery",
            artifact_id=memo_id, evidence_ids=[], request_id="report-" + memo_id,
        )


def error(code):
    return pytest.raises(WorkspaceError, match="^" + code + "$")


def drain(store, actor):
    ids = [row["id"] for row in store.inbox(actor)]
    store.acknowledge(actor, ids)


def test_goal_delegates_only_ready_stage_and_requires_exact_delivery(desk):
    engine, store, _, _, _ = desk
    goal = create(desk)
    assert len(store.tasks("demo/researcher")) == 1
    assert store.tasks("demo/reviewer") == []
    assert store.tasks("demo/executor") == []
    assert json.loads(store.tasks("demo/researcher")[0]["detail"])["source_ids"] == ["release-notes"]
    goal = research(desk, goal)
    assert goal["cycles"][0]["phase"] == "review"
    reviewer_task = store.tasks("demo/reviewer")[0]
    assert reviewer_task["creator"] == "@supervisor"
    review_messages = store.inbox("demo/reviewer")
    assert any(row["kind"] == "review_body" for row in review_messages)
    goal = review(desk, goal)
    delivery = store.tasks("demo/executor")[0]
    detail = json.loads(delivery["detail"])
    body_message = next(row for row in store.inbox("demo/executor") if row["id"] == detail["proposal_body_message_id"])
    assert hashlib.sha256(body_message["body"].encode()).hexdigest() == goal["cycles"][0]["body_sha256"]
    store.update_task("demo/executor", delivery["id"], "done", expected_version=1)
    assert engine.tick()["deliveries_recovered"] == 0
    assert current(desk, goal["id"])["state"] == "active"
    goal = publish(desk, goal)
    assert goal["state"] == "completed"
    assert goal["cycles"][0]["memo_id"] == "memo"
    assert engine.read()["notices"][0]["kind"] == "completion"
    # No completion message is invented under a role's identity.
    with store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM operator_messages").fetchone()[0] == 0


def test_no_authentication_from_goal_id_or_role_argument(desk):
    engine = desk[0]
    goal = create(desk)
    assert engine.read("demo/stranger")["goals"] == []
    with error("goal_not_visible"):
        engine.read("demo/stranger", goal_id=goal["id"])
    with error("goal_role_mismatch"):
        engine.report("demo/reviewer", goal_id=goal["id"], cycle=1, stage="research",
                      artifact_id="fake", evidence_ids=["fake"], request_id="forged")
    with error("goal_requires_independent_participants"):
        create(desk, request_id="second", reviewer="demo/researcher")
    with error("goal_requires_independent_participants"):
        create(desk, request_id="third", reviewer="other/reviewer")


@pytest.mark.parametrize("field,value", [
    ("author_principal", "demo/executor"),
    ("executor_principal", "demo/researcher"),
    ("body_sha256", "f" * 64),
    ("body", "different bytes"),
])
def test_research_refuses_proposal_not_from_designated_participants(desk, field, value):
    goal = create(desk)
    research(desk, goal)
    # New goal must not adopt a foreign or altered proposal, even from a trusted reader.
    second = create(desk, request_id="second")
    desk[3]["proposal", "proposal"][field] = value
    with error("goal_proposal_mismatch"):
        desk[0].report("demo/researcher", goal_id=second["id"], cycle=1, stage="research",
                       artifact_id="proposal", evidence_ids=["evidence"], request_id="bad-proposal")
    assert current(desk, second["id"])["cycles"][0]["phase"] == "research"


def test_unshared_stale_or_incomplete_evidence_cannot_advance(desk):
    engine, store, _, artifacts, evidence = desk
    first = create(desk)
    research(desk, first)
    second = create(desk, request_id="second")
    artifacts["proposal", "second"] = {**artifacts["proposal", "proposal"], "proposal_id": "second"}
    evidence["evidence"]["shared"] = False
    args = dict(goal_id=second["id"], cycle=1, stage="research", artifact_id="second",
                evidence_ids=["evidence"], request_id="second-report")
    with error("goal_evidence_not_shared"):
        engine.report("demo/researcher", **args)
    evidence["evidence"]["shared"] = True
    evidence["evidence"]["fetched_at"] = 999
    with error("goal_evidence_stale"):
        engine.report("demo/researcher", **args)
    evidence["evidence"]["fetched_at"] = 1000
    evidence["evidence"]["source_id"] = "other-source"
    with error("goal_evidence_mismatch"):
        engine.report("demo/researcher", **args)
    evidence["evidence"]["source_id"] = "release-notes"
    assert engine.report("demo/researcher", **args)["cycles"][0]["phase"] == "review"
    assert len(store.tasks("demo/reviewer")) == 2


@pytest.mark.parametrize("field,value", [
    ("issuer_principal", "demo/researcher"), ("executor_principal", "demo/reviewer"),
    ("proposal_id", "other-proposal"), ("body_sha256", "0" * 64),
    ("status", "revoked"), ("expires_at", 1000),
])
def test_wrong_or_expired_approval_never_delegates_execution(desk, field, value):
    goal = research(desk, create(desk))
    cycle = goal["cycles"][0]
    approval = {
        "approval_id": "bad", "proposal_id": cycle["proposal_id"],
        "body_sha256": cycle["body_sha256"], "issuer_principal": "demo/reviewer",
        "executor_principal": "demo/executor", "status": "active", "expires_at": 1100,
        field: value,
    }
    desk[3]["approval", "bad"] = approval
    with error("goal_approval_mismatch"):
        desk[0].report("demo/reviewer", goal_id=goal["id"], cycle=1, stage="review",
                       artifact_id="bad", evidence_ids=[], request_id="bad-approval")
    assert desk[1].tasks("demo/executor") == []


@pytest.mark.parametrize("field,value", [
    ("body", "changed"), ("issuer_principal", "demo/researcher"),
    ("approval_id", "other-approval"), ("executor_principal", "demo/reviewer"),
])
def test_delivery_report_does_not_substitute_a_different_result(desk, field, value):
    goal = review(desk, research(desk, create(desk)))
    publish(desk, goal, report=False)
    desk[3]["memo", "memo"][field] = value
    with error("goal_delivery_mismatch"):
        desk[0].report("demo/executor", goal_id=goal["id"], cycle=1, stage="delivery",
                       artifact_id="memo", evidence_ids=[], request_id="wrong-delivery")
    assert current(desk, goal["id"])["state"] == "active"


def test_same_request_concurrent_replay_has_one_goal_and_assignment(desk):
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: create(desk), range(8)))
    assert len({row["id"] for row in rows}) == 1
    assert len(desk[1].tasks("demo/researcher")) == 1
    with error("request_conflict"):
        create(desk, title="Changed")


def test_restart_recovers_delivery_without_role_impersonation_or_reexecution(desk):
    engine, store, clock, _, _ = desk
    goal = review(desk, research(desk, create(desk)))
    publish(desk, goal, report=False)
    restarted_store = WorkspaceStore(store.db_path, clock=lambda: clock[0])
    restarted = GoalEngine(restarted_store, artifact_reader=engine.artifact_reader,
                           evidence_reader=engine.evidence_reader)
    assert restarted.tick()["deliveries_recovered"] == 1
    assert restarted.tick()["deliveries_recovered"] == 0
    assert restarted.read(goal_id=goal["id"])["goals"][0]["state"] == "completed"
    assert restarted_store.tasks("demo/executor")[0]["status"] == "done"
    with restarted_store._connect() as conn:
        row = conn.execute("SELECT actor FROM events WHERE kind='task.goal_verified'").fetchone()
        assert row["actor"] == "controller"


def test_delivery_report_after_recovery_and_later_cycle_is_harmless(desk):
    goal = create(desk, max_cycles=2, interval_seconds=60)
    goal = review(desk, research(desk, goal))
    publish(desk, goal, report=False)
    desk[0].tick()
    desk[2][0] += 61
    desk[0].tick()
    recovered = desk[0].report(
        "demo/executor", goal_id=goal["id"], cycle=1, stage="delivery",
        artifact_id="memo", evidence_ids=[], request_id="late-report",
    )
    assert recovered["cycle"] == 2 and recovered["state"] == "active"
    assert len(desk[0].read()["notices"]) == 1


def test_goal_read_bounds_whole_records_and_preserves_selected_objective(desk):
    objective = "x" * 16384
    goals = []
    for index in range(20):
        goal = create(desk, request_id="goal-" + str(index), objective=objective)
        desk[0].ask("demo/researcher", goal_id=goal["id"], question="q" * 4096, request_id="ask-" + str(index))
        goals.append(goal)
    value = desk[0].read("demo/researcher")
    assert value["has_more"] is True
    assert value["notices_has_more"] is True
    assert len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) < 256 * 1024
    assert all(goal["objective"] == objective for goal in value["goals"])
    selected = desk[0].read("demo/researcher", goal_id=goals[0]["id"])
    assert selected["goals"][0]["objective"] == objective


def test_long_history_is_bounded_and_always_includes_current_cycle(desk):
    goal = create(desk, max_cycles=100, interval_seconds=60)
    references = json.dumps([str(index).zfill(256) for index in range(100)])
    with desk[1]._connect(write=True) as conn:
        conn.execute("UPDATE workspace_goals SET cycle=100 WHERE id=?", (goal["id"],))
        for number in range(2, 101):
            conn.execute(
                "INSERT INTO workspace_goal_cycles(goal_id,number,phase,research_task,evidence_ids,next_followup,started_at) "
                "VALUES(?,?,'research',?,?,?,?)",
                (goal["id"], number, goal["cycles"][0]["research_task"], references, 1100, 1000),
            )
    value = desk[0].read("demo/researcher", goal_id=goal["id"])
    selected = value["goals"][0]
    assert selected["cycles"][0]["number"] == 100
    assert selected["cycles_has_more"] is True
    assert len(json.dumps(value).encode()) < 256 * 1024


def test_periodic_goal_coalesces_missed_intervals_and_finishes_at_budget(desk):
    engine, store, clock, _, _ = desk
    goal = create(desk, max_cycles=2, interval_seconds=60)
    goal = publish(desk, review(desk, research(desk, goal)))
    assert goal["state"] == "scheduled"
    assert engine.tick()["cycles_started"] == 0
    clock[0] += 10000
    assert engine.tick()["cycles_started"] == 1
    assert engine.tick()["cycles_started"] == 0
    goal = current(desk, goal["id"])
    assert goal["cycle"] == 2
    goal = research(desk, goal, proposal_id="proposal-2", evidence_id="evidence-2")
    goal = review(desk, goal, approval_id="approval-2")
    goal = publish(desk, goal, memo_id="memo-2")
    clock[0] += 10000
    assert engine.tick()["cycles_started"] == 0
    assert goal["state"] == "completed"
    assert len(store.tasks("demo/researcher")) == 2


def test_followups_are_bounded_event_driven_and_do_not_repeat_unchanged_notices(desk):
    engine, store, clock, _, _ = desk
    goal = create(desk, max_followups=1)
    clock[0] += 61
    # Unconsumed initial task already supplies one wake demand.
    assert engine.tick()["followups"] == 0
    drain(store, "demo/researcher")
    assert engine.tick()["followups"] == 1
    assert engine.tick()["followups"] == 0
    drain(store, "demo/researcher")
    clock[0] += 61
    assert engine.tick()["blocked"] == 1
    assert engine.tick()["blocked"] == 0
    assert current(desk, goal["id"])["blocked_reason"] == "followup_budget_exhausted"
    assert len(engine.read()["notices"]) == 1


def test_seat_budget_exhaustion_escalates_without_wake_loop(desk):
    goal = create(desk)
    desk[1].set_budget("demo/researcher", 0, expected_version=1)
    desk[2][0] += 61
    assert desk[0].tick()["blocked"] == 1
    assert current(desk, goal["id"])["blocked_reason"] == "seat_budget_exhausted"
    assert desk[0].read()["notices"][0]["kind"] == "budget"


def test_human_question_waits_persistently_and_answer_is_not_approval(desk):
    engine, store, clock, _, _ = desk
    goal = create(desk)
    waiting = engine.ask("demo/researcher", goal_id=goal["id"], question="Which scope?", request_id="ask")
    assert waiting["state"] == "waiting_human"
    with store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM messages WHERE state='queued'").fetchone()[0] == 0
    clock[0] += 10000
    assert engine.tick() == {"cycles_started": 0, "deliveries_recovered": 0, "followups": 0, "blocked": 0}
    with error("version_conflict"):
        engine.answer(goal["id"], body="Public releases only", expected_version=1, request_id="answer")
    answered = engine.answer(goal["id"], body="Public releases only", expected_version=waiting["version"], request_id="answer")
    assert answered["state"] == "active"
    assert answered["cycles"][0]["approval_id"] is None
    assert answered == engine.answer(goal["id"], body="Public releases only", expected_version=waiting["version"], request_id="answer")
    assert any("goal_answer" in row["body"] for row in store.inbox("demo/researcher"))


@pytest.mark.parametrize("reason", ["approval_expired", "approval_revoked"])
def test_stale_approval_stops_executor_and_explicit_resume_requires_new_review(desk, reason):
    engine, store, clock, artifacts, _ = desk
    goal = review(desk, research(desk, create(desk)))
    original_delivery = goal["cycles"][0]["delivery_task"]
    if reason == "approval_expired":
        artifacts["approval", "approval"]["expires_at"] = clock[0] + 10
    else:
        artifacts["approval", "approval"]["status"] = "revoked"
    clock[0] += 61
    assert engine.tick()["blocked"] == 1
    blocked = current(desk, goal["id"])
    assert blocked["blocked_reason"] == reason
    with store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM messages WHERE state='queued' AND recipient='demo/executor'").fetchone()[0] == 0
    assert engine.tick()["blocked"] == 0
    resumed = engine.update(goal["id"], action="resume", expected_version=blocked["version"], request_id="resume-review")
    assert resumed["cycles"][0]["phase"] == "review"
    assert resumed["cycles"][0]["approval_id"] is None
    assert resumed["cycles"][0]["review_task"] != goal["cycles"][0]["review_task"]
    assert any(item["kind"] == "review_body" for item in store.inbox("demo/reviewer"))
    renewed = review(desk, resumed, approval_id="fresh-approval")
    assert renewed["cycles"][0]["delivery_task"] != original_delivery
    assert next(task for task in store.tasks("demo/executor") if task["id"] == original_delivery)["status"] == "cancelled"
    assert publish(desk, renewed)["state"] == "completed"


def test_pause_suppresses_only_goal_wakes_and_resume_restores_context(desk):
    engine, store, _, _, _ = desk
    goal = create(desk)
    unrelated = store.trusted_enqueue("demo/researcher", "Other work", request_id="unrelated")
    paused = engine.update(goal["id"], action="pause", expected_version=1, request_id="pause")
    with store._connect() as conn:
        queued = conn.execute("SELECT id FROM messages WHERE state='queued'").fetchall()
        assert [row[0] for row in queued] == [unrelated["id"]]
    resumed = engine.update(goal["id"], action="resume", expected_version=paused["version"], request_id="resume")
    assert resumed["state"] == "active"
    with store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM messages WHERE state='goal_paused'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM messages WHERE state='queued'").fetchone()[0] == 3


def test_cancel_stops_work_without_marking_goal_success(desk):
    engine, store, clock, _, _ = desk
    goal = create(desk)
    cancelled = engine.update(goal["id"], action="cancel", expected_version=goal["version"], request_id="cancel")
    assert cancelled["state"] == "cancelled"
    assert cancelled["cycles"][0]["memo_id"] is None
    assert store.tasks("demo/researcher")[0]["status"] == "cancelled"
    clock[0] += 10000
    assert engine.tick()["followups"] == 0
    with error("goal_closed"):
        engine.update(goal["id"], action="resume", expected_version=cancelled["version"], request_id="resume")


@pytest.mark.parametrize("changes,code", [
    ({"max_cycles": 0}, "invalid_max_cycles"),
    ({"max_cycles": 2}, "goal_interval_required"),
    ({"interval_seconds": float("inf")}, "invalid_interval_seconds"),
    ({"followup_seconds": 0}, "invalid_followup_seconds"),
    ({"source_ids": []}, "invalid_source_ids"),
    ({"max_followups": True}, "invalid_max_followups"),
])
def test_goal_limits_are_explicit_and_finite(desk, changes, code):
    with error(code):
        create(desk, **changes)
