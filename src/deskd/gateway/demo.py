"""Credential-free workflow demonstration, with explicitly synthetic identities."""

from __future__ import annotations

from dataclasses import asdict
from html import escape
import json
from pathlib import Path
import time
from typing import Any

from .actions import ActionError, MemoWorkflow, WORKFLOW_ACTIONS
from .commands import GatewayCommands
from .events import EventConflict, GatewayEventStore
from .identity import ActionPolicy, IdentityError, PrincipalId, TransportEvidence
from .registry import Registry


LIMITATIONS = [
    "Synthetic role identities; no OS isolation or official harness is exercised.",
    "The only effect is a memo row in this new local database.",
    "No model, credentials, broker, network service or real trade is used.",
    "This snapshot is a workflow demonstration, not a live supervisor console.",
]


def render_board(report: dict[str, Any]) -> str:
    """Render an offline human snapshot. All untrusted content is escaped."""

    def cell(value):
        return escape(str(value), quote=True)

    roles = "".join(
        f"<article><h2>{cell(role['seat'])}</h2><p>{cell(role['responsibility'])}</p>"
        f"<code>{cell(role['principal'])}</code></article>"
        for role in report["roles"]
    )
    memos = "".join(
        f"<article><h2>Published memo</h2><pre>{cell(memo['body'])}</pre>"
        "<dl>"
        + "".join(
            f"<dt>{cell(label)}</dt><dd>{cell(memo[key])}</dd>"
            for label, key in (
                ("Proposed by", "author_principal"),
                ("Independently authorized by", "issuer_principal"),
                ("Executed by", "executor_principal"),
                ("Exact content SHA-256", "body_sha256"),
                ("Memo", "memo_id"),
            )
        )
        + "</dl></article>"
        for memo in report["snapshot"]["memos"]
    )
    checks = "".join(
        f"<tr><td>{cell(item['scenario'])}</td>"
        f"<td>{cell(item['outcome'])}</td><td>{cell(item['code'])}</td></tr>"
        for item in report["checks"]
    )
    limits = "".join(f"<li>{cell(item)}</li>" for item in report["limitations"])
    return (
        """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>deskd — local workflow</title><style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif}body{max-width:1080px;margin:3rem auto;padding:0 1.25rem;line-height:1.55}
h1{font-size:2.3rem;margin-bottom:.25rem}h2{font-size:1.1rem}.banner{padding:1rem;border:2px solid #bd821e;border-radius:10px}
.roles{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:1rem}article{border:1px solid #8886;border-radius:12px;padding:1.1rem;margin:1rem 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;padding:1rem;background:#8881}code,dd{overflow-wrap:anywhere}
dt{font-weight:600;margin-top:.7rem}dd{margin-left:0}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:.65rem;border-bottom:1px solid #8885}small{opacity:.8}
</style><main><small>DESKD / LOCAL WORKFLOW</small><h1>One desk. Separate responsibilities.</h1>
<p>Propose → independent authorization → execute → durable receipt.</p>
<div class="banner"><strong>Offline mock demonstration</strong><ul>"""
        + limits
        + """</ul></div>
<section class="roles">"""
        + roles
        + """</section>"""
        + memos
        + """
<h2>Adversarial checks and recovery</h2><table><thead><tr><th>Scenario</th><th>Outcome</th><th>Evidence</th></tr></thead><tbody>"""
        + checks
        + """</tbody></table>
<p>Effects: """
        + cell(len(report["snapshot"]["memos"]))
        + """ published memo. The same authorized request returns its original receipt.</p>
</main></html>"""
    )


def run_demo(output: Path | str) -> dict[str, Any]:
    """Create a fresh directory; never read user configuration or existing state."""
    output = Path(output)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    db_path = output / "workflow.db"
    policies = {**WORKFLOW_ACTIONS, "state.read": ActionPolicy("state.read")}
    registry = Registry(db_path, harness_uid=12001, actions=policies)
    events = GatewayEventStore(db_path)
    workflow = MemoWorkflow(db_path)
    commands = GatewayCommands(registry, events, handlers=workflow.handlers())
    service = registry.start_service()
    registry.trusted_activate_service(expected_service_generation=service)
    bindings, channels = {}, {}
    role_specs = {
        # Deliberately allow approval.issue so self-approval tests the independent
        # principal check, not an incidental missing capability.
        "operator": (
            {"proposal.create", "action.execute", "approval.issue"},
            "Proposes work and executes exactly what another identity approves.",
        ),
        "reviewer": (
            {"approval.issue", "approval.revoke"},
            "Reviews exact content and grants a bounded, single-use approval.",
        ),
        "engineer": (
            {"state.read"},
            "Observes status without authority to approve or execute.",
        ),
    }

    def bind_channels(*, renewed=False):
        for seat, (caps, _) in role_specs.items():
            old = bindings.get(seat)
            binding = registry.trusted_bind(
                PrincipalId("demo", seat),
                root_session_id=f"root-{seat}-{'renewed' if renewed else 'initial'}",
                manifest_hash="a" * 64,
                capabilities=frozenset(caps),
                expected_binding_generation=old.binding_generation if old else 0,
            )
            channel = f"synthetic-{seat}-{binding.binding_generation}"
            registry.trusted_grant_lease(
                binding.principal,
                connection_id=channel,
                service_generation=service,
                root_session_id=binding.root_session_id,
                binding_generation=binding.binding_generation,
                manifest_hash=binding.manifest_hash,
                ttl_seconds=60,
            )
            bindings[seat] = binding
            channels[seat] = TransportEvidence(12001, channel, service)

    bind_channels()
    checks = []

    def call(seat, action, args, request, *, child=False, impersonate=None):
        root = bindings[impersonate or seat].root_session_id
        return commands.execute(
            request,
            {
                "name": action,
                "arguments": args,
                "_meta": {
                    "sessionId": root,
                    "threadId": "child-thread" if child else root,
                },
            },
            channels[seat],
        )

    def must_deny(scenario, expected, operation):
        before = len(events.events())
        try:
            operation()
        except (IdentityError, ActionError, EventConflict) as exc:
            code = getattr(exc, "code", "request_conflict")
            if code != expected:
                raise RuntimeError(f"Unexpected denial in {scenario}: {code}") from exc
            if len(events.events()) != before:
                raise RuntimeError("Rejected action changed the event log")
            checks.append({"scenario": scenario, "outcome": "rejected", "code": code})
        else:
            raise RuntimeError(f"Expected rejection in {scenario}")

    proposal = call(
        "operator",
        "proposal.create",
        {
            "executor_principal": "demo/operator",
            "body": "Publish the release readiness memo.\nThis is synthetic content with no external effect.",
        },
        "proposal-1",
    )
    approval_args = {
        "proposal_id": proposal.result["proposal_id"],
        "body_sha256": proposal.result["body_sha256"],
        "ttl_seconds": 600,
    }
    must_deny(
        "Self-approval",
        "independent_approver_required",
        lambda: call("operator", "approval.issue", approval_args, "self-approval"),
    )
    must_deny(
        "Forged reviewer root on operator channel",
        "binding_mismatch",
        lambda: call(
            "operator",
            "approval.issue",
            approval_args,
            "forged-root",
            impersonate="reviewer",
        ),
    )
    must_deny(
        "Approve different content",
        "approval_content_mismatch",
        lambda: call(
            "reviewer",
            "approval.issue",
            {**approval_args, "body_sha256": "0" * 64},
            "changed-content",
        ),
    )
    approval = call("reviewer", "approval.issue", approval_args, "approval-1")
    execution_args = {"approval_id": approval.result["approval_id"]}
    must_deny(
        "Child thread executes",
        "root_required",
        lambda: call(
            "operator", "action.execute", execution_args, "child-execute", child=True
        ),
    )
    must_deny(
        "Engineer executes",
        "capability_denied",
        lambda: call("engineer", "action.execute", execution_args, "engineer-execute"),
    )
    published = call("operator", "action.execute", execution_args, "execute-1")
    repeated = call("operator", "action.execute", execution_args, "execute-1")
    if asdict(repeated) != asdict(published):
        raise RuntimeError("Replay did not return the original receipt")
    checks.append(
        {
            "scenario": "Lost-ACK retry with same request ID",
            "outcome": "original receipt",
            "code": "no_duplicate_effect",
        }
    )
    must_deny(
        "Reuse approval with a new request ID",
        "approval_not_active",
        lambda: call("operator", "action.execute", execution_args, "execute-again"),
    )
    old_channel = channels["operator"]
    old_root = bindings["operator"].root_session_id
    service = registry.start_service()
    channels["operator"] = TransportEvidence(12001, "unleased-after-restart", service)
    must_deny(
        "Gateway restart before activation",
        "service_fenced",
        lambda: call("operator", "action.execute", execution_args, "execute-1"),
    )
    registry.trusted_activate_service(expected_service_generation=service)
    bind_channels(renewed=True)
    must_deny(
        "Old connection after restart",
        "stale_service_generation",
        lambda: commands.execute(
            "execute-1",
            {
                "name": "action.execute",
                "arguments": execution_args,
                "_meta": {"sessionId": old_root, "threadId": old_root},
            },
            old_channel,
        ),
    )
    recovered = call("operator", "action.execute", execution_args, "execute-1")
    if asdict(recovered) != asdict(published):
        raise RuntimeError("Authorized recovery did not return the original receipt")
    checks.append(
        {
            "scenario": "Explicit trusted rebind to a new root after restart",
            "outcome": "original receipt",
            "code": "no_duplicate_effect",
        }
    )
    registry.trusted_revoke(
        bindings["operator"].principal,
        expected_binding_generation=bindings["operator"].binding_generation,
    )
    must_deny(
        "Revoked identity replays a previous receipt",
        "inactive_channel",
        lambda: call("operator", "action.execute", execution_args, "execute-1"),
    )
    registry.fence(expected_service_generation=service)
    snapshot = workflow.summary()
    if len(snapshot["memos"]) != 1 or len(events.events()) != 3:
        raise RuntimeError("Unexpected effects after workflow demonstration")
    report = {
        "schema_version": 1,
        "mode": "offline-mock",
        "created_at": time.time(),
        "isolation_verified": False,
        "ready_for_credentials": False,
        "limitations": LIMITATIONS,
        "roles": [
            {"seat": seat, "principal": f"demo/{seat}", "responsibility": text}
            for seat, (_, text) in role_specs.items()
        ],
        "checks": checks,
        "snapshot": snapshot,
        "receipts": [asdict(x) for x in (proposal, approval, published)],
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "board.html").write_text(render_board(report), encoding="utf-8")
    return report
