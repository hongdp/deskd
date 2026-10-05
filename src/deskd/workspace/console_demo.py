"""Fresh synthetic data for the interactive console; no model or worker runs."""

from pathlib import Path

from deskd.gateway.identity import IdentityError, PrincipalId, TransportEvidence
from .console import ConsoleBackend
from .exchange import ACTIONS
from .service import WorkspaceGateway


def create_demo(output):
    output = Path(output)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    gateway = WorkspaceGateway(
        gateway_db=output / "gateway.sqlite",
        coordination_db=output / "workspace.sqlite",
        harness_uid=26002,
        business_gid=26003,
        business_path=output / "unused-business",
        admin_path=output / "unused-admin",
        principals=["demo/" + role for role in ("analyst", "trader", "engineer")],
        activation_check=lambda: None,
    )
    service = gateway.registry.start_service()
    gateway.registry.trusted_activate_service(expected_service_generation=service)
    identities, attestations = {}, {}
    for role in ("analyst", "trader", "engineer"):
        principal = PrincipalId("demo", role)
        root = "mock-root-" + role
        capabilities = set(ACTIONS) | {"proposal.create", "state.read"}
        if role == "analyst":
            capabilities |= {"approval.issue", "approval.revoke"}
        if role == "trader":
            capabilities.add("action.execute")
        gateway.registry.trusted_bind(
            principal, root_session_id=root, manifest_hash="a" * 64,
            capabilities=frozenset(capabilities), expected_binding_generation=0,
        )
        gateway.store.register_seat(principal.value, root, "a" * 64)
        attestations[principal.value] = {"root_id": root, "manifest_hash": "a" * 64, "binding_generation": 1}
        channel = "synthetic-console-" + role
        gateway.registry.trusted_grant_lease(
            principal, connection_id=channel, service_generation=service,
            root_session_id=root, binding_generation=1,
            manifest_hash="a" * 64, ttl_seconds=60,
        )
        identities[role] = TransportEvidence(26002, channel, service)
    gateway.registry.trusted_activate_service(expected_service_generation=service)
    generation = gateway.store.start_service()
    gateway.store.activate(generation, attestations)

    def call(role, action, args, request_id):
        root = "mock-root-" + role
        receipt = gateway.commands.execute(request_id, {
            "name": action, "arguments": args,
            "_meta": {"sessionId": root, "threadId": root},
        }, identities[role])
        gateway.exchange.pump()
        return receipt.result

    task = gateway.store.trusted_add_task(
        "demo/analyst", "整理本周研究摘要", "示例任务：说明信息来源、主要变化和仍待确认的问题。",
        request_id="seed-task-1",
    )
    call("analyst", "task.update", {"task_id": task["id"], "status": "done", "expected_version": 1}, "seed-done")
    gateway.store.trusted_add_task(
        "demo/engineer", "检查交付清单", "示例任务：核对界面的空状态、失败提示和小屏幕布局。",
        request_id="seed-task-2",
    )
    gateway.store.trusted_enqueue("demo/analyst", "请把研究结论和需要我决定的事项发到这里。", request_id="seed-message")
    call("analyst", "mail.send", {
        "recipient": "@supervisor",
        "body": "示例回复：摘要已整理完成。下一步需要确认研究范围是最近一周还是最近一个月。此内容由演示预置，不是模型生成。",
    }, "seed-reply")
    proposal = call("trader", "proposal.create", {
        "executor_principal": "demo/trader",
        "body": "示例成果 · 工作台交付记录\n\n角色独立复核后，这份备忘录写入了本地账本。没有调用外部服务，也没有真实交易。",
    }, "seed-proposal-completed")
    approval = call("analyst", "approval.issue", {
        "proposal_id": proposal["proposal_id"], "body_sha256": proposal["body_sha256"], "ttl_seconds": 600,
    }, "seed-approval")
    call("trader", "action.execute", {"approval_id": approval["approval_id"]}, "seed-publish")
    call("trader", "proposal.create", {
        "executor_principal": "demo/trader", "body": "待复核示例：将下周工作计划记录为一份本地备忘录。仅影响此演示账本。",
    }, "seed-proposal-pending")
    # Explicit synthetic examples only. No source worker, model or dispatcher runs.
    gateway.automation.sources.configure("demo-news", "https://example.test/demo-news")
    note = gateway.automation.knowledge.remember(
        "demo/engineer", "示例共享知识：交付前检查", "这是合成示例笔记。交付前核对来源、独立复核结论与成果记录。",
        sources=[], request_id="seed-shared-note",
    )
    gateway.automation.knowledge.publish(
        "demo/engineer", note["id"], expected_version=note["version"], request_id="seed-publish-note",
    )
    gateway.automation.knowledge.remember(
        "demo/trader", "演示私人笔记", "PRIVATE_SYNTHETIC_NOTE_NOT_SHARED",
        sources=[], request_id="seed-private-note",
    )
    goal = gateway.automation.create_goal(
        title="示例目标：整理公开动态", objective="这是预置的模拟目标。调研公开资料，经独立复核后交付一份共享备忘录。",
        researcher="demo/engineer", reviewer="demo/analyst", executor="demo/trader",
        source_ids=["demo-news"], request_id="seed-goal", interval_seconds=None,
        max_cycles=1, followup_seconds=3600, max_followups=2,
    )
    gateway.automation.goals.ask(
        "demo/engineer", goal_id=goal["id"], question="示例问题：这次应关注最近一周还是最近一个月？回答后只更新演示账本，不会调用模型。",
        request_id="seed-goal-question",
    )
    gateway.automation.tick()
    handlers = gateway._handlers()

    def admin(method, params):
        try:
            if method == "status":
                return {"ok": True, "result": {"fenced": False}}
            return {"ok": True, "result": handlers[method](params)}
        except IdentityError as exc:
            return {"ok": False, "error": {"code": exc.code}}

    backend = ConsoleBackend(admin, mode="demo")
    backend._demo_gateway = gateway  # Synthetic-only fixture; never an HTTP route.
    return backend
