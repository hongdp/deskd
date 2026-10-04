"""Synthetic local memo workflow; no harness, credential or external effect."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier

import pytest

from deskd.gateway.actions import (
    ActionConflict,
    ActionError,
    MAX_MEMO_BYTES,
    MemoWorkflow,
    WORKFLOW_ACTIONS,
    tool_catalog,
)
from deskd.gateway.commands import GatewayCommands
from deskd.gateway.events import EventConflict, GatewayEventStore
from deskd.gateway.identity import (
    ActionPolicy,
    IdentityError,
    PrincipalId,
    TransportEvidence,
)
from deskd.gateway.registry import Registry


class Desk:
    def __init__(self, path, *, policies=None):
        self.now = 1000.0
        self.registry = Registry(
            path,
            harness_uid=12001,
            clock=lambda: 10,
            actions=WORKFLOW_ACTIONS if policies is None else policies,
        )
        self.service = self.registry.start_service()
        self.registry.trusted_activate_service(expected_service_generation=self.service)
        self.bindings = {}
        self.transports = {}
        # Deliberately give operator approval capability as well: independence
        # must come from principal identity, never a convenient role fixture.
        for seat, caps in {
            "operator": {"proposal.create", "action.execute", "approval.issue"},
            "reviewer": {"approval.issue", "approval.revoke"},
            "other": {"action.execute", "approval.issue", "approval.revoke"},
            "controller": {"approval.revoke_any"},
            "engineer": {"state.read"},
        }.items():
            self.bind(seat, caps)
        self.events = GatewayEventStore(path)
        self.workflow = MemoWorkflow(path, clock=lambda: self.now)
        self.commands = GatewayCommands(
            self.registry, self.events, handlers=self.workflow.handlers()
        )

    def bind(self, seat, caps, *, root=None):
        old = self.bindings.get(seat)
        binding = self.registry.trusted_bind(
            PrincipalId("demo", seat),
            root_session_id=root or "root-" + seat,
            manifest_hash="a" * 64,
            capabilities=frozenset(caps),
            expected_binding_generation=old.binding_generation if old else 0,
        )
        channel = f"channel-{seat}-{binding.binding_generation}"
        self.registry.trusted_grant_lease(
            binding.principal,
            connection_id=channel,
            service_generation=self.service,
            root_session_id=binding.root_session_id,
            binding_generation=binding.binding_generation,
            manifest_hash=binding.manifest_hash,
            ttl_seconds=30,
        )
        self.bindings[seat] = binding
        self.transports[seat] = TransportEvidence(12001, channel, self.service)

    def call(self, seat, action, args, request, *, child=False):
        root = self.bindings[seat].root_session_id
        return self.commands.execute(
            request,
            {
                "name": action,
                "arguments": args,
                "_meta": {
                    "sessionId": root,
                    "threadId": "child-thread" if child else root,
                },
            },
            self.transports[seat],
        )

    def propose(self, body="Exact memo\nwith whitespace.  ", request="proposal-1"):
        return self.call(
            "operator",
            "proposal.create",
            {"executor_principal": "demo/operator", "body": body},
            request,
        )

    def approve(self, proposal, *, seat="reviewer", request="approval-1", ttl=30):
        return self.call(
            seat,
            "approval.issue",
            {
                "proposal_id": proposal.result["proposal_id"],
                "body_sha256": proposal.result["body_sha256"],
                "ttl_seconds": ttl,
            },
            request,
        )

    def execute(self, approval, *, request="execution-1", seat="operator", child=False):
        return self.call(
            seat,
            "action.execute",
            {"approval_id": approval.result["approval_id"]},
            request,
            child=child,
        )

    def revoke(
        self, approval, *, seat="reviewer", request="revocation-1", control=False
    ):
        return self.call(
            seat,
            "approval.revoke_any" if control else "approval.revoke",
            {"approval_id": approval.result["approval_id"]},
            request,
        )


@pytest.fixture
def desk(tmp_path):
    return Desk(tmp_path / "workflow.db")


def test_complete_workflow_exact_body_and_server_owned_identity(desk):
    proposal = desk.propose()
    approval = desk.approve(proposal)
    published = desk.execute(approval)
    assert published.result["body"] == proposal.result["body"]
    assert published.result["body_sha256"] == approval.result["body_sha256"]
    assert published.result["author_principal"] == "demo/operator"
    assert published.result["issuer_principal"] == "demo/reviewer"
    assert published.result["executor_principal"] == "demo/operator"
    assert published.event["principal_id"] == "demo/operator"
    board = desk.workflow.summary()
    assert board["proposals"][0]["status"] == "executed"
    assert board["approvals"][0]["status"] == "consumed"
    assert (
        len(board["memos"])
        == len(
            [
                e
                for e in desk.events.events()
                if e["event"]["event_type"] == "memo.published"
            ]
        )
        == 1
    )


def test_self_approval_is_not_fixed_by_new_root_or_capability(desk):
    proposal = desk.propose()
    with pytest.raises(ActionError, match="independent_approver_required"):
        desk.approve(proposal, seat="operator")
    desk.bind(
        "operator",
        {"proposal.create", "approval.issue", "action.execute"},
        root="new-operator-root",
    )
    with pytest.raises(ActionError, match="independent_approver_required"):
        desk.approve(proposal, seat="operator", request="approval-2")
    assert len(desk.events.events()) == 1


@pytest.mark.parametrize(
    "action,args",
    [
        (
            "approval.issue",
            {"proposal_id": "absent", "body_sha256": "a" * 64, "ttl_seconds": 10},
        ),
        ("action.execute", {"approval_id": "absent"}),
        ("approval.revoke", {"approval_id": "absent"}),
    ],
)
def test_engineer_without_business_capabilities_cannot_mutate(desk, action, args):
    with pytest.raises(IdentityError):
        desk.call("engineer", action, args, "denied")
    assert desk.events.events() == []


def test_child_can_propose_but_cannot_approve_or_execute(desk):
    proposal = desk.call(
        "operator",
        "proposal.create",
        {"executor_principal": "demo/operator", "body": "child-authored draft"},
        "child-proposal",
        child=True,
    )
    with pytest.raises(IdentityError, match="root_required"):
        desk.call(
            "reviewer",
            "approval.issue",
            {
                "proposal_id": proposal.result["proposal_id"],
                "body_sha256": proposal.result["body_sha256"],
                "ttl_seconds": 10,
            },
            "child-approval",
            child=True,
        )
    approval = desk.approve(proposal)
    with pytest.raises(IdentityError, match="root_required"):
        desk.execute(approval, child=True)


def test_handler_defends_root_even_if_policy_is_accidentally_weaker(tmp_path):
    policies = dict(WORKFLOW_ACTIONS)
    policies["action.execute"] = ActionPolicy("action.execute", root_only=False)
    desk = Desk(tmp_path / "weak-policy.db", policies=policies)
    approval = desk.approve(desk.propose())
    with pytest.raises(ActionError, match="root_required"):
        desk.execute(approval, child=True)
    assert desk.workflow.summary()["memos"] == []


def test_wrong_executor_and_unknown_or_cross_desk_target(desk):
    approval = desk.approve(desk.propose())
    with pytest.raises(ActionError, match="designated_executor_required"):
        desk.execute(approval, seat="other")
    for executor, reason in [
        ("demo/absent", "executor_not_bound"),
        ("another/operator", "cross_desk_executor"),
        ("arbitrary", "invalid_executor_principal"),
    ]:
        with pytest.raises(ActionError, match=reason):
            desk.call(
                "operator",
                "proposal.create",
                {"executor_principal": executor, "body": "memo"},
                "bad-target",
            )


def test_approval_requires_executor_still_bound(desk):
    proposal = desk.propose()
    desk.registry.trusted_revoke(
        PrincipalId("demo", "operator"), expected_binding_generation=1
    )
    with pytest.raises(ActionError, match="executor_not_bound"):
        desk.approve(proposal)


def test_digest_mismatch_and_identity_or_body_rewrite_fields_rejected(desk):
    proposal = desk.propose()
    with pytest.raises(ActionError, match="approval_content_mismatch"):
        desk.call(
            "reviewer",
            "approval.issue",
            {
                "proposal_id": proposal.result["proposal_id"],
                "body_sha256": "0" * 64,
                "ttl_seconds": 20,
            },
            "mismatch",
        )
    with pytest.raises(ActionError, match="invalid_action_arguments"):
        desk.call(
            "operator",
            "proposal.create",
            {
                "executor_principal": "demo/operator",
                "body": "memo",
                "author_principal": "demo/reviewer",
            },
            "spoof",
        )
    approval = desk.approve(proposal)
    for extra in [
        {"body": "rewritten"},
        {"issuer_principal": "demo/other"},
        {"executor_principal": "demo/other"},
    ]:
        with pytest.raises(ActionError, match="invalid_action_arguments"):
            desk.call(
                "operator",
                "action.execute",
                {"approval_id": approval.result["approval_id"], **extra},
                "rewrite",
            )
    assert len(desk.events.events()) == 2


@pytest.mark.parametrize(
    "body",
    ["", "   \n", "中" * (MAX_MEMO_BYTES // 3 + 1)],
    ids=["empty", "whitespace", "utf8-byte-overflow"],
)
def test_body_limits_are_utf8_bytes(desk, body):
    with pytest.raises(ActionError):
        desk.propose(body)
    assert desk.events.events() == []


@pytest.mark.parametrize(
    "ttl",
    [True, 0, -1, 3601, 10**400],
    ids=["bool", "zero", "negative", "too-long", "integer-overflow"],
)
def test_approval_ttl_limits(desk, ttl):
    with pytest.raises(ActionError, match="invalid_approval_ttl"):
        desk.approve(desk.propose(), ttl=ttl)


def test_replay_same_semantics_returns_original_even_after_consumption(desk):
    proposal = desk.propose()
    assert desk.propose() == proposal
    with pytest.raises(EventConflict):
        desk.propose("Changed body")
    approval = desk.approve(proposal)
    published = desk.execute(approval)
    assert desk.approve(proposal) == approval
    assert desk.execute(approval) == published
    with pytest.raises(ActionConflict, match="approval_not_active"):
        desk.execute(approval, request="execution-2")
    assert len(desk.events.events()) == 3


def test_new_root_replay_requires_current_authority_and_retains_original_provenance(
    desk,
):
    approval = desk.approve(desk.propose())
    published = desk.execute(approval)
    desk.bind("operator", {"action.execute"}, root="replacement-root")
    assert desk.execute(approval) == published
    assert published.event["provenance"]["root_session_id"] == "root-operator"
    desk.bind("operator", {"proposal.create"}, root="less-authority-root")
    with pytest.raises(IdentityError):
        desk.execute(approval)
    assert len(desk.workflow.summary()["memos"]) == 1


def test_approval_survives_executor_root_replacement_but_not_revocation(desk):
    approval = desk.approve(desk.propose())
    desk.bind("operator", {"action.execute"}, root="replacement-root")
    assert desk.execute(approval).result["executor_principal"] == "demo/operator"
    desk.registry.trusted_revoke(
        PrincipalId("demo", "operator"), expected_binding_generation=2
    )
    with pytest.raises(IdentityError):
        desk.execute(approval)


@pytest.mark.parametrize("now", [1030, 999])
def test_expiry_boundary_and_backwards_clock_reject_execution(desk, now):
    approval = desk.approve(desk.propose(), ttl=30)
    desk.now = now
    with pytest.raises(ActionError, match="approval_expired_or_clock_invalid"):
        desk.execute(approval)
    assert desk.workflow.summary()["memos"] == []


def test_revoke_monotonic_and_issuer_or_explicit_control_only(desk):
    approval = desk.approve(desk.propose())
    with pytest.raises(ActionError, match="approval_issuer_required"):
        desk.revoke(approval, seat="other")
    with pytest.raises(IdentityError):
        desk.revoke(approval, seat="other", control=True)
    revoked = desk.revoke(approval, seat="controller", control=True)
    assert revoked.result["revoked_by"] == "demo/controller"
    assert desk.revoke(approval, seat="controller", control=True) == revoked
    with pytest.raises(ActionConflict, match="approval_not_active"):
        desk.revoke(approval, request="again")
    with pytest.raises(ActionConflict, match="approval_not_active"):
        desk.execute(approval)
    assert desk.workflow.summary()["approvals"][0]["status"] == "revoked"


def test_issuer_revoke_before_execute_and_execute_before_revoke(desk):
    approval = desk.approve(desk.propose())
    desk.revoke(approval)
    with pytest.raises(ActionConflict):
        desk.execute(approval)
    proposal2 = desk.propose("second", request="proposal-2")
    approval2 = desk.approve(proposal2, request="approval-2")
    desk.execute(approval2, request="execution-2")
    with pytest.raises(ActionConflict):
        desk.revoke(approval2, request="revocation-2")
    assert len(desk.workflow.summary()["memos"]) == 1


def test_two_approvals_do_not_allow_proposal_to_publish_twice(desk):
    proposal = desk.propose()
    first = desk.approve(proposal)
    second = desk.approve(proposal, seat="other", request="approval-2")
    desk.execute(first)
    with pytest.raises(ActionConflict, match="proposal_already_executed"):
        desk.execute(second, request="execution-2")
    states = {
        row["approval_id"]: row["status"]
        for row in desk.workflow.summary()["approvals"]
    }
    assert states[second.result["approval_id"]] == "superseded"


def test_concurrent_execution_consumes_approval_once(desk):
    approval = desk.approve(desk.propose())
    barrier = Barrier(2)

    def attempt(request):
        barrier.wait(timeout=5)
        try:
            return desk.execute(approval, request=request)
        except ActionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["execute-a", "execute-b"]))
    assert sum(isinstance(result, ActionConflict) for result in results) == 1
    assert len(desk.workflow.summary()["memos"]) == 1
    assert len(desk.events.events()) == 3


def test_concurrent_revocation_and_execution_have_one_linearized_winner(desk):
    approval = desk.approve(desk.propose())
    barrier = Barrier(2)

    def attempt(operation):
        barrier.wait(timeout=5)
        try:
            return operation(approval)
        except ActionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [desk.execute, desk.revoke]))
    assert sum(isinstance(result, ActionConflict) for result in results) == 1
    board = desk.workflow.summary()
    state = board["approvals"][0]["status"]
    assert (state, len(board["memos"])) in [("revoked", 0), ("consumed", 1)]
    assert len(desk.events.events()) == 3


def test_outbox_failure_rolls_back_memo_and_approval_consumption(desk):
    approval = desk.approve(desk.propose())
    with sqlite3.connect(desk.registry.db_path) as conn:
        conn.execute(
            "CREATE TRIGGER fail_publish BEFORE INSERT ON events_requests "
            "WHEN NEW.request_id='execution-1' BEGIN SELECT RAISE(ABORT,'injected failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        desk.execute(approval)
    board = desk.workflow.summary()
    assert board["approvals"][0]["status"] == "active"
    assert board["memos"] == []
    assert len(desk.events.events()) == 2
    with sqlite3.connect(desk.registry.db_path) as conn:
        conn.execute("DROP TRIGGER fail_publish")
    assert desk.execute(approval).result["status"] == "published"


def test_reopening_workflow_and_readonly_summary_do_not_replay_effects(desk):
    desk.execute(desk.approve(desk.propose()))
    before = desk.events.events()
    reopened = MemoWorkflow(desk.registry.db_path, clock=lambda: desk.now)
    assert reopened.summary() == desk.workflow.summary()
    assert desk.events.events() == before
    assert len(reopened.summary()["memos"]) == 1


def test_summary_expiry_is_derived_without_state_mutation(desk):
    desk.approve(desk.propose())
    desk.now = 1031
    board = desk.workflow.summary()
    assert board["approvals"][0]["status"] == "expired"
    assert board["proposals"][0]["status"] == "pending"
    with sqlite3.connect(desk.registry.db_path) as conn:
        assert conn.execute("SELECT status FROM memo_approvals").fetchone() == (
            "active",
        )
    assert len(desk.events.events()) == 2


def test_filtered_summary_only_shares_participant_drafts_and_desk_memos(desk):
    proposal = desk.propose()
    engineer = PrincipalId("demo", "engineer")
    reviewer = PrincipalId("demo", "reviewer")
    assert desk.workflow.summary_for(engineer)["proposals"] == []
    assert desk.workflow.summary_for(reviewer)["proposals"] == []
    approval = desk.approve(proposal)
    review_board = desk.workflow.summary_for(reviewer)
    assert len(review_board["proposals"]) == len(review_board["approvals"]) == 1
    assert desk.workflow.summary_for(engineer)["approvals"] == []
    desk.execute(approval)
    assert len(desk.workflow.summary_for(engineer)["memos"]) == 1
    outside = desk.workflow.summary_for(PrincipalId("outside", "engineer"))
    assert outside["memos"] == outside["proposals"] == outside["approvals"] == []
    with pytest.raises(ActionError, match="invalid_summary_principal"):
        desk.workflow.summary_for("demo/operator")


def test_static_catalog_excludes_control_and_does_not_expose_identity_fields():
    catalog = tool_catalog()
    assert {item["name"] for item in catalog} == {
        "proposal.create",
        "approval.issue",
        "action.execute",
        "approval.revoke",
    }
    for item in catalog:
        schema = item["inputSchema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert "issuer_principal" not in schema["properties"]
        assert "request_id" not in schema["properties"]
    catalog[0]["inputSchema"]["properties"].clear()
    assert tool_catalog()[0]["inputSchema"]["properties"]


def test_workflow_refuses_missing_and_nonregistry_database(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(
        ActionError, match="workflow_requires_existing_registry_database"
    ):
        MemoWorkflow(path)
    assert not path.exists()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unrelated(value)")
    with pytest.raises(
        ActionError, match="workflow_requires_existing_registry_database"
    ):
        MemoWorkflow(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("unrelated",)]
