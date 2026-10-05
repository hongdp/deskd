"""Offline rehearsal of the real durable workspace APIs with a fixed runtime.

Synthetic transport evidence is supplied in process. This exercises application
logic, not official model behavior, operating-system identity or role sandboxes.
"""

from __future__ import annotations

from dataclasses import asdict
from html import escape
import json
from pathlib import Path

from deskd.gateway.actions import ActionError, MemoWorkflow, WORKFLOW_ACTIONS
from deskd.gateway.commands import GatewayCommands
from deskd.gateway.events import GatewayEventStore
from deskd.gateway.identity import IdentityError, PrincipalId, TransportEvidence
from deskd.gateway.registry import Registry

from .board import public_snapshot
from .exchange import ACTIONS, WorkspaceExchange
from .scheduler import WorkspaceScheduler
from .store import WorkspaceStore


LIMITATIONS = [
    "The runtime is a deterministic in-process double; no model is called.",
    "Synthetic identity evidence exercises authorization logic, not OS isolation.",
    "No credentials, broker, network endpoint or real trade is used.",
    "The only authorized action publishes one memo in a fresh local database.",
    "The offline board is a snapshot; the separate board command serves live local status.",
    "Lost start acknowledgments need a human to associate the batch with a turn; the mock simulates this explicit confirmation from its recorded batch.",
]


class MockRuntime:
    """Records exact tool-output inputs and can lose a reply after acceptance."""

    def __init__(self):
        self.calls: list[dict] = []
        self.turns: dict[tuple[str, str], str] = {}
        self.fail_next = False

    def start_turn(self, root_id: str, events: list[dict]) -> str:
        turn_id = "mock-turn-" + str(len(self.calls) + 1)
        self.calls.append(
            {
                "root_id": root_id,
                "turn_id": turn_id,
                "input": [],
                "toolOutput": {
                    "namespace": "deskd",
                    "name": "workspace_events",
                    "output": json.dumps(events, ensure_ascii=False),
                },
            }
        )
        self.turns[(root_id, turn_id)] = "completed"
        if self.fail_next:
            self.fail_next = False
            raise OSError("synthetic lost reply after acceptance")
        return turn_id

    def read_turn(self, root_id: str, turn_id: str) -> str:
        return self.turns.get((root_id, turn_id), "unknown")


def render_board(report: dict) -> str:
    """Standalone, script-free snapshot; escape every variable value."""

    def text(value):
        return escape(str(value), quote=True)

    cards = "".join(
        "<article><h2>"
        + text(seat["principal"])
        + "</h2><p>Turns used: "
        + text(seat["turns_used"])
        + " / "
        + text(seat["budget_turns"])
        + "</p><p>Paused: "
        + text(bool(seat["paused"]))
        + "</p></article>"
        for seat in report["snapshot"]["seats"]
    )
    checks = "".join(
        "<tr><td>"
        + text(item["scenario"])
        + "</td><td>"
        + text(item["outcome"])
        + "</td></tr>"
        for item in report["checks"]
    )
    limitations = "".join(
        "<li>" + text(item) + "</li>" for item in report["limitations"]
    )
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width'>"
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
        "<title>deskd workspace rehearsal</title><style>"
        "body{font:16px system-ui;background:#101820;color:#edf0eb;max-width:1050px;margin:48px auto;padding:24px;line-height:1.6}"
        "h1{font-size:42px;font-weight:500}.seats{display:flex;flex-wrap:wrap;gap:20px}article{flex:1;min-width:220px;border:1px solid #52625f;border-radius:12px;padding:20px}"
        "td,th{text-align:left;padding:12px;border-bottom:1px solid #52625f}table{width:100%;border-collapse:collapse}.notice{color:#bbd6b0}"
        "</style><main><p>DESKD / OFFLINE WORKSPACE REHEARSAL</p>"
        "<h1>Separate seats. Durable work.</h1><p class='notice'>"
        "The rehearsal finished fenced. No model or external action is running.</p>"
        "<section class='seats'>" + cards + "</section>"
        "<h2>Collaboration, authorization and recovery</h2><table><thead>"
        "<tr><th>Scenario</th><th>Observed result</th></tr></thead><tbody>"
        + checks
        + "</tbody></table><h2>Validation limits</h2><ul>"
        + limitations
        + "</ul>"
        "<p>Private message bodies and model transcripts are omitted from this board.</p></main></html>"
    )


def run_demo(output: str | Path) -> dict:
    """Use a fresh 0700 directory only; never load host configuration or auth."""
    output = Path(output)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    store = WorkspaceStore(output / "workspace.sqlite", clock=lambda: 100.0)
    registry = Registry(
        output / "gateway.sqlite",
        harness_uid=12001,
        actions={**WORKFLOW_ACTIONS, **ACTIONS},
        clock=lambda: 50.0,
    )
    events = GatewayEventStore(output / "gateway.sqlite")
    workflow = MemoWorkflow(output / "gateway.sqlite", clock=lambda: 100.0)
    principals = {"demo/" + name for name in ("operator", "reviewer", "engineer")}
    exchange = WorkspaceExchange(events, store, principals=principals)
    commands = GatewayCommands(
        registry, events, handlers={**workflow.handlers(), **exchange.handlers()}
    )
    service = registry.start_service()
    registry.trusted_activate_service(expected_service_generation=service)
    bindings, channels = {}, {}
    for name in ("operator", "reviewer", "engineer"):
        principal = PrincipalId("demo", name)
        capabilities = set(ACTIONS)
        if name == "operator":
            capabilities.update(("proposal.create", "action.execute", "approval.issue"))
        elif name == "reviewer":
            capabilities.update(("approval.issue", "approval.revoke"))
        binding = registry.trusted_bind(
            principal,
            root_session_id="root-" + name,
            manifest_hash="a" * 64,
            capabilities=frozenset(capabilities),
            expected_binding_generation=0,
        )
        bindings[name] = binding
        store.register_seat(
            principal.value, binding.root_session_id, binding.manifest_hash
        )

    def lease():
        for name, binding in bindings.items():
            channel = "synthetic-" + name + "-" + service
            registry.trusted_grant_lease(
                binding.principal,
                connection_id=channel,
                service_generation=service,
                root_session_id=binding.root_session_id,
                binding_generation=binding.binding_generation,
                manifest_hash=binding.manifest_hash,
                ttl_seconds=60,
            )
            channels[name] = TransportEvidence(12001, channel, service)

    lease()
    attestations = {
        b.principal.value: {
            "root_id": b.root_session_id,
            "manifest_hash": b.manifest_hash,
            "binding_generation": b.binding_generation,
        }
        for b in bindings.values()
    }
    generation = store.start_service()
    store.activate(generation, attestations)
    runtime = MockRuntime()
    scheduler = WorkspaceScheduler(store, runtime, generation)
    checks = []

    def check(scenario, condition, outcome):
        if not condition:
            raise RuntimeError("workspace_demo_check_failed")
        checks.append({"scenario": scenario, "outcome": outcome})

    def call(name, action, arguments, request_id, *, root=None):
        root = root or bindings[name].root_session_id
        return commands.execute(
            request_id,
            {
                "name": action,
                "arguments": arguments,
                "_meta": {"sessionId": root, "threadId": root},
            },
            channels[name],
        )

    def deny(scenario, expected, operation):
        before = len(events.events())
        try:
            operation()
        except (IdentityError, ActionError) as exc:
            check(
                scenario,
                exc.code == expected and len(events.events()) == before,
                exc.code,
            )
        else:
            raise RuntimeError("workspace_demo_expected_denial")

    def acknowledge(name, request_id):
        ids = [str(item["id"]) for item in store.inbox("demo/" + name)]
        call(name, "inbox.ack", {"message_ids": ids}, request_id)
        exchange.pump()

    # Real gateway authorization -> outbox -> separately committed coordination.
    message = call(
        "operator",
        "mail.send",
        {"recipient": "demo/reviewer", "body": "Review the synthetic release memo."},
        "mail-1",
    )
    check(
        "Gateway receipt precedes delivery",
        message.result["queued"] and store.inbox("demo/reviewer") == [],
        "queued intent only",
    )
    exchange.pump()
    exchange.cursor = 0
    exchange.pump()
    check(
        "Private application receipt",
        store.gateway_receipt("demo/operator", message.event["event_id"])["applied"]
        and store.gateway_receipt("demo/reviewer", message.event["event_id"]) is None,
        "visible only to the submitting identity",
    )
    check(
        "Retained outbox replay",
        len(store.inbox("demo/reviewer")) == 1,
        "one durable message",
    )
    call(
        "operator",
        "task.create",
        {
            "assignee": "demo/reviewer",
            "title": "Review memo",
            "body": "Review the exact local-only memo content.",
            "depends_on": [],
        },
        "task-1",
    )
    exchange.pump()
    task = store.tasks("demo/reviewer")[0]
    delivered = scheduler.tick()
    check(
        "Busy-seat batch coalescing",
        delivered["principal"] == "demo/reviewer"
        and len(json.loads(runtime.calls[-1]["toolOutput"]["output"])) == 2,
        "mail and task delivered in one turn",
    )
    scheduler.complete_turn("root-reviewer", delivered["turn_id"])
    check(
        "Completion is not acknowledgment",
        all(item["state"] == "delivered" for item in store.inbox("demo/reviewer")),
        "messages remain unhandled",
    )
    acknowledge("reviewer", "ack-reviewer")
    call(
        "reviewer",
        "task.update",
        {"task_id": task["id"], "status": "done", "expected_version": 1},
        "task-done",
    )
    exchange.pump()
    check(
        "Recipient handling and task completion",
        not store.inbox("demo/reviewer")
        and store.tasks("demo/reviewer")[0]["status"] == "done",
        "explicit authenticated acknowledgment",
    )

    proposal = call(
        "operator",
        "proposal.create",
        {
            "executor_principal": "demo/operator",
            "body": "Release readiness reviewed. This memo has no external effect.",
        },
        "proposal-1",
    )
    approve_args = {
        "proposal_id": proposal.result["proposal_id"],
        "body_sha256": proposal.result["body_sha256"],
        "ttl_seconds": 600,
    }
    deny(
        "Self-approval",
        "independent_approver_required",
        lambda: call("operator", "approval.issue", approve_args, "self-approval"),
    )
    deny(
        "Forged reviewer root",
        "binding_mismatch",
        lambda: call(
            "operator", "approval.issue", approve_args, "forged", root="root-reviewer"
        ),
    )
    approval = call("reviewer", "approval.issue", approve_args, "approval-1")
    args = {"approval_id": approval.result["approval_id"]}
    deny(
        "Engineer execution",
        "capability_denied",
        lambda: call("engineer", "action.execute", args, "engineer-execute"),
    )
    memo = call("operator", "action.execute", args, "execute-1")
    check(
        "Independent approval and one effect",
        asdict(memo) == asdict(call("operator", "action.execute", args, "execute-1"))
        and len(workflow.summary()["memos"]) == 1,
        "one memo; original receipt on replay",
    )

    store.set_paused("demo/engineer", True, expected_version=1)
    call(
        "operator",
        "mail.send",
        {
            "recipient": "demo/engineer",
            "body": "Inspect the synthetic workspace report.",
        },
        "engineer-mail",
    )
    exchange.pump()
    check(
        "Pause preserves queued work",
        scheduler.tick() is None and len(store.inbox("demo/engineer")) == 1,
        "no model turn while paused",
    )
    store.set_paused("demo/engineer", False, expected_version=2)
    delivered = scheduler.tick()
    scheduler.complete_turn("root-engineer", delivered["turn_id"])
    acknowledge("engineer", "ack-engineer")

    call(
        "reviewer",
        "mail.send",
        {"recipient": "demo/operator", "body": "Record the completed review."},
        "operator-mail",
    )
    exchange.pump()
    runtime.fail_next = True
    unknown = scheduler.tick()
    unknown_turn = runtime.calls[-1]["turn_id"]
    call(
        "reviewer",
        "mail.send",
        {
            "recipient": "demo/operator",
            "body": "Publish the final local report after reconciliation.",
        },
        "operator-later",
    )
    exchange.pump()
    count = len(runtime.calls)
    check(
        "Lost runtime reply",
        unknown["state"] == "unknown"
        and scheduler.tick() is None
        and len(runtime.calls) == count,
        "UNKNOWN blocks automatic retry",
    )

    # Reopen the durable state, preserving every root; activation is explicit.
    store.fence(generation)
    registry.fence(expected_service_generation=service)
    service = registry.start_service()
    registry.trusted_activate_service(expected_service_generation=service)
    lease()
    store = WorkspaceStore(output / "workspace.sqlite", clock=lambda: 100.0)
    generation = store.start_service()
    check(
        "Restart is fenced",
        store.snapshot()["service"]["active"] == 0,
        "explicit recovery required",
    )
    scheduler = WorkspaceScheduler(store, runtime, generation)
    # Explicitly simulate independent management confirmation using the mock's
    # recorded causal batch. Merely reading a completed root turn is insufficient.
    recorded_ids = [
        event["id"] for event in json.loads(runtime.calls[-1]["toolOutput"]["output"])
    ]
    check(
        "Explicit lost-ACK association",
        recorded_ids == json.loads(store.dispatch(unknown["id"])["message_ids"]),
        "simulated human confirmation; not automatic runtime proof",
    )
    store.reconcile(
        unknown["id"],
        generation=generation,
        root_id="root-operator",
        turn_id=unknown_turn,
        outcome="completed",
        human_confirmed=True,
    )
    store.activate(generation, attestations)
    exchange = WorkspaceExchange(events, store, principals=principals)
    exchange.pump()
    check(
        "Same-root recovery",
        {seat["root_id"] for seat in store.snapshot()["seats"]}
        == {binding.root_session_id for binding in bindings.values()},
        "registered roots unchanged",
    )
    delivered = scheduler.tick()
    # Now restart with a durably known turn ID. Its authoritative runtime status
    # can be reconciled directly without guessing which turn received the batch.
    store = WorkspaceStore(output / "workspace.sqlite", clock=lambda: 100.0)
    generation = store.start_service()
    scheduler = WorkspaceScheduler(store, runtime, generation)
    scheduler.reconcile_turn(delivered["id"], "root-operator", delivered["turn_id"])
    store.activate(generation, attestations)
    exchange = WorkspaceExchange(events, store, principals=principals)
    exchange.pump()
    check(
        "Known-turn restart reconciliation",
        store.dispatch(delivered["id"])["state"] == "completed",
        "authoritative status for the exact durable turn ID",
    )
    acknowledge("operator", "ack-operator")
    check(
        "Reconciled work remains separate from later demand",
        len(runtime.calls) == count + 1 and not store.inbox("demo/operator"),
        "one later turn, then explicit inbox acknowledgment",
    )

    store.fence(generation)
    registry.fence(expected_service_generation=service)
    snapshot = store.snapshot()
    report = {
        "kind": "offline-mock-workspace",
        "checks": checks,
        "limitations": LIMITATIONS,
        "snapshot": snapshot,
        "public_snapshot": public_snapshot(snapshot),
        "memo": workflow.summary()["memos"][0],
        "runtime_calls": len(runtime.calls),
        "tool_output_only": all(
            call["input"] == [] and "toolOutput" in call for call in runtime.calls
        ),
        "files": {
            "workspace": "workspace.sqlite",
            "gateway": "gateway.sqlite",
            "board": "board.html",
            "report": "report.json",
        },
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "board.html").write_text(render_board(report), encoding="utf-8")
    return report
