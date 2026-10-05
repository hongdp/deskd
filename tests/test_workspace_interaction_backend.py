"""Operator interaction against fresh SQLite stores and synthetic role evidence.

No runtime, listener, credentials, real model or external service is used.
"""

import json
import sqlite3

import pytest

from deskd.gateway.actions import WORKFLOW_ACTIONS
from deskd.gateway.identity import IdentityError, PrincipalId, TransportEvidence
from deskd.gateway.wire import encode_frame
from deskd.workspace.exchange import ACTIONS, WorkspaceExchange
from deskd.workspace.service import WorkspaceGateway
from deskd.workspace.store import WorkspaceError, WorkspaceStore


@pytest.fixture
def desk(tmp_path):
    gateway = WorkspaceGateway(
        gateway_db=tmp_path / "gateway.db",
        coordination_db=tmp_path / "workspace.db",
        harness_uid=26002, business_gid=26003,
        business_path=tmp_path / "business", admin_path=tmp_path / "admin",
        principals=["demo/analyst", "demo/trader"], activation_check=lambda: None,
    )
    generation = gateway.registry.start_service()
    gateway.registry.trusted_activate_service(expected_service_generation=generation)
    peers = {}
    for role in ("analyst", "trader"):
        principal = PrincipalId("demo", role)
        root = "root-" + role
        gateway.store.register_seat(principal.value, root, "a" * 64)
        gateway.registry.trusted_bind(
            principal, root_session_id=root, manifest_hash="a" * 64,
            capabilities=frozenset({*ACTIONS, *WORKFLOW_ACTIONS}),
            expected_binding_generation=0,
        )
        gateway.registry.trusted_grant_lease(
            principal, connection_id="channel-" + role,
            service_generation=generation, root_session_id=root,
            binding_generation=1, manifest_hash="a" * 64, ttl_seconds=30,
        )
        peers[role] = TransportEvidence(26002, "channel-" + role, generation)
    gateway.registry.trusted_activate_service(expected_service_generation=generation)
    return gateway, peers


def admin(desk, method, **params):
    return desk[0]._handlers()["workspace.console." + method](params)


def call(desk, role, action, arguments, request_id):
    reply = desk[0].commands.execute(
        request_id,
        {"name": action, "arguments": arguments,
         "_meta": {"sessionId": "root-" + role, "threadId": "root-" + role}},
        desk[1][role],
    )
    desk[0].exchange.pump()
    return reply


def test_human_task_has_distinct_creator_idempotency_and_real_role_lifecycle(desk):
    params = dict(assignee="demo/analyst", title="Research question", body="Full brief", request_id="h1")
    task = admin(desk, "task", **params)
    assert task["creator"] == "@supervisor"
    assert admin(desk, "task", **params) == task
    assert len(desk[0].store.inbox("demo/analyst")) == 1
    with pytest.raises(IdentityError, match="request_conflict"):
        admin(desk, "task", **{**params, "body": "Different brief"})
    assert desk[0].store.tasks_page("demo/trader")["tasks"] == []
    assigned = desk[0].store.tasks_page("demo/analyst")["tasks"][0]
    assert assigned["detail"] == "Full brief"
    call(desk, "analyst", "task.update", {
        "task_id": task["id"], "status": "done", "expected_version": 1,
    }, "done-1")
    assert admin(desk, "snapshot")["tasks"][0]["status"] == "done"
    assert {row["principal"] for row in desk[0].store.snapshot()["seats"]} == {
        "demo/analyst", "demo/trader",
    }
    with pytest.raises(WorkspaceError, match="unknown_principal"):
        desk[0].store.add_task("@supervisor", "demo/analyst", "spoof", request_id="spoof")


def test_human_task_cancel_is_owned_version_checked_and_does_not_pause_role(desk):
    task = admin(desk, "task", assignee="demo/analyst", title="One", body="Brief", request_id="one")
    with pytest.raises(IdentityError, match="version_conflict"):
        admin(desk, "cancel", task_id=task["id"], expected_version=2)
    assert admin(desk, "cancel", task_id=task["id"], expected_version=1)["status"] == "cancelled"
    assert not admin(desk, "snapshot")["seats"][0]["paused"]
    owned = desk[0].store.add_task("demo/trader", "demo/analyst", "Private owner", request_id="role-1")
    with pytest.raises(IdentityError, match="task_not_owned"):
        admin(desk, "cancel", task_id=owned["id"], expected_version=1)


def test_reserved_human_reply_mailbox_is_idempotent_durable_and_never_wakes_a_role(desk):
    first = call(desk, "analyst", "mail.send", {
        "recipient": "@supervisor", "body": "Here is the result; please clarify next step.",
    }, "reply-1")
    again = call(desk, "analyst", "mail.send", {
        "recipient": "@supervisor", "body": "Here is the result; please clarify next step.",
    }, "reply-1")
    assert first == again
    assert desk[0].store.inbox("demo/analyst") == desk[0].store.inbox("demo/trader") == []
    snapshot = admin(desk, "snapshot")
    assert snapshot["unread_count"] == 1
    assert snapshot["messages"][0]["sender"] == "demo/analyst"
    assert snapshot["messages"][0]["state"] == "unread"
    # Replaying the retained outbox cannot duplicate a human notification.
    replay = WorkspaceExchange(desk[0].events, desk[0].store, principals=desk[0].exchange.principals)
    replay.pump()
    reopened = WorkspaceStore(desk[0].store.db_path)
    assert reopened.console_snapshot()["unread_count"] == 1
    reply_id = snapshot["messages"][0]["reply_id"]
    assert admin(desk, "read", message_ids=[reply_id]) == {"read": [reply_id]}
    assert admin(desk, "read", message_ids=[reply_id]) == {"read": [reply_id]}
    assert admin(desk, "snapshot")["unread_count"] == 0


def test_console_projection_keeps_private_mail_roots_and_leases_out(desk):
    admin(desk, "message", recipient="demo/analyst", body="Human brief", request_id="brief")
    call(desk, "analyst", "mail.send", {"recipient": "demo/trader", "body": "PRIVATE-PEER-BODY"}, "peer")
    call(desk, "trader", "mail.send", {"recipient": "@supervisor", "body": "Public result"}, "result")
    result = admin(desk, "snapshot")
    encoded = json.dumps(result)
    for excluded in ("PRIVATE-PEER-BODY", "root-analyst", "channel-analyst", "manifest_hash", "binding_generation"):
        assert excluded not in encoded
    assert {row["body"] for row in result["messages"]} == {"Human brief", "Public result"}
    assert "approval.issue" in result["seats"][0]["capabilities"]
    # No privileged content appears on the existing metadata-only board path.
    assert "Public result" not in json.dumps(desk[0].store.snapshot())


def test_read_ack_validates_all_ids_atomically_and_cannot_ack_role_mail(desk):
    role = admin(desk, "message", recipient="demo/analyst", body="Human", request_id="h")
    reply = desk[0].store.enqueue("demo/analyst", "@supervisor", "Result", request_id="r")
    with pytest.raises(IdentityError, match="unknown_operator_message"):
        admin(desk, "read", message_ids=[reply["id"], 9999])
    assert admin(desk, "snapshot")["unread_count"] == 1
    admin(desk, "read", message_ids=[reply["id"]])
    assert desk[0].store.inbox("demo/analyst")[0]["id"] == role["id"]
    assert desk[0].store.inbox("demo/analyst")[0]["state"] == "queued"
    for ids in ([True], [1, 1], ["1"], None):
        with pytest.raises(IdentityError):
            admin(desk, "read", message_ids=ids)


@pytest.mark.parametrize("method", ["snapshot", "task", "message", "cancel", "read", "review"])
def test_console_is_closed_admin_protocol_never_a_role_endpoint(desk, method):
    with pytest.raises(IdentityError, match="invalid_management_params"):
        admin(desk, method, arbitrary="caller-supplied")
    with pytest.raises(IdentityError, match="unknown_business_method"):
        desk[0].transport._business_call("workspace.console." + method, {}, None)


@pytest.mark.parametrize("action,args", [
    ("task.create", {"assignee": "@supervisor", "title": "x", "body": "x", "depends_on": []}),
    ("mail.send", {"recipient": "@someone", "body": "x"}),
    ("mail.send", {"recipient": "@supervisor", "body": "x", "sender": "@supervisor"}),
])
def test_reserved_mailbox_does_not_create_a_general_identity_escape(desk, action, args):
    with pytest.raises(IdentityError):
        call(desk, "analyst", action, args, "bad")
    assert admin(desk, "snapshot")["unread_count"] == 0


def test_memo_results_and_exact_independent_approval_remain_real_gateway_actions(desk):
    proposal = call(desk, "trader", "proposal.create", {
        "executor_principal": "demo/trader", "body": "A reviewed local result",
    }, "proposal").result
    summary = admin(desk, "snapshot")
    assert summary["proposals"][0]["body_sha256"] == proposal["body_sha256"]
    assert summary["proposals"][0]["status"] == "pending"
    # Asking for review is correspondence, never approval by the human UI.
    admin(desk, "message", recipient="demo/analyst", body="Review " + proposal["proposal_id"], request_id="review")
    assert admin(desk, "snapshot")["approvals"] == []
    approval = call(desk, "analyst", "approval.issue", {
        "proposal_id": proposal["proposal_id"], "body_sha256": proposal["body_sha256"], "ttl_seconds": 60,
    }, "approve").result
    call(desk, "trader", "action.execute", {"approval_id": approval["approval_id"]}, "execute")
    summary = admin(desk, "snapshot")
    assert summary["proposals"][0]["status"] == "executed"
    assert summary["memos"][0]["body"] == "A reviewed local result"
    assert summary["memos"][0]["issuer_principal"] == "demo/analyst"


def test_snapshot_bounds_escaped_large_content_without_cutting_a_result(desk):
    body = "\x01" * 65_536
    for number in range(8):
        desk[0].store.enqueue("demo/analyst", "@supervisor", body, request_id=f"big-{number}")
        admin(desk, "task", assignee="demo/analyst", title=str(number), body=body, request_id=f"task-{number}")
    result = admin(desk, "snapshot")
    assert len(encode_frame({"ok": True, "result": result}, response=True)) < 1024 * 1024
    assert result["truncated"]["messages"] and result["truncated"]["tasks"]
    assert result["unread_count"] == 8
    assert result["messages"]
    for row in result["messages"]:
        assert row["body"] == body
    for row in result["tasks"]:
        assert row["detail"] == body


def test_oldest_unread_is_reachable_past_hundred_recent_messages(desk):
    for number in range(105):
        desk[0].store.enqueue("demo/analyst", "@supervisor", str(number), request_id=f"reply-{number}")
    first = admin(desk, "snapshot")
    assert first["messages"][0]["body"] == "0"
    assert first["truncated"]["messages"]
    assert len(first["messages"]) == 100
    admin(desk, "read", message_ids=[row["reply_id"] for row in first["messages"]])
    second = admin(desk, "snapshot")
    assert second["unread_count"] == 5
    assert [row["body"] for row in second["messages"] if not row.get("read", True)] == [str(n) for n in range(100, 105)]


def test_byte_limit_preserves_the_only_unread_reply_over_large_task_content(desk):
    reply_body = "\x01" * 65_536
    desk[0].store.enqueue("demo/analyst", "@supervisor", reply_body, request_id="reply")
    admin(desk, "task", assignee="demo/analyst", title="T", body="\x01" * 65_480, request_id="task")
    result = admin(desk, "snapshot")
    assert result["messages"][0]["body"] == reply_body
    assert result["tasks"] == [] and result["truncated"]["tasks"]
    assert len(encode_frame({"ok": True, "result": result}, response=True)) < 1024 * 1024


def test_v1_migration_preserves_existing_roles_receipts_and_queued_messages(tmp_path):
    path = tmp_path / "old.db"
    old = WorkspaceStore(path)
    old.register_seat("demo/analyst", "root-a", "a" * 64)
    sent = old.trusted_enqueue("demo/analyst", "Pending work", request_id="same")
    before = old.snapshot()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE operator_messages")
        conn.execute("PRAGMA user_version=1")
    upgraded = WorkspaceStore(path)
    assert upgraded.snapshot() == before
    assert upgraded.trusted_enqueue("demo/analyst", "Pending work", request_id="same") == sent
    assert upgraded.console_snapshot()["unread_count"] == 0
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def review_proposal(desk, *, body="Exact local result"):
    proposal = call(desk, "trader", "proposal.create", {
        "executor_principal": "demo/trader", "body": body,
    }, "proposal").result
    return {
        "proposal_id": proposal["proposal_id"], "body_sha256": proposal["body_sha256"],
        "reviewer": "demo/analyst", "request_id": "human-review",
    }


@pytest.mark.parametrize("body", ["x" * 65_536, "Untrusted\x00memo\nwith exact whitespace.  "])
def test_review_loads_exact_legal_memo_and_atomically_queues_metadata_and_body(desk, body):
    params = review_proposal(desk, body=body)
    requested = admin(desk, "review", **params)
    assert requested["queued"] is True
    assert admin(desk, "review", **params) == requested
    messages = desk[0].store.inbox("demo/analyst")
    assert len(messages) == 2
    assert [row["id"] for row in messages] == requested["message_ids"]
    assert [row["kind"] for row in messages] == ["review_request", "review_body"]
    metadata = json.loads(messages[0]["body"])
    assert metadata["body_message_id"] == messages[1]["id"]
    assert metadata["proposal_id"] == params["proposal_id"]
    assert metadata["body_sha256"] == params["body_sha256"]
    assert "untrusted" in metadata["instruction"]
    assert messages[1]["body"] == body
    assert admin(desk, "snapshot")["approvals"] == []
    assert all(row["sender"] == "@supervisor" and row["priority"] == 0 for row in messages)
    with pytest.raises(IdentityError, match="request_conflict"):
        admin(desk, "review", **{**params, "body_sha256": "a" * 64})


def test_review_two_message_failure_rolls_back_entire_request_and_receipt(desk, monkeypatch):
    params = review_proposal(desk)
    store = desk[0].store
    original = store._insert_message
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise WorkspaceError("inbox_full")
        return original(*args, **kwargs)

    before = store.snapshot()["events"]
    monkeypatch.setattr(store, "_insert_message", fail_second)
    with pytest.raises(IdentityError, match="inbox_full"):
        admin(desk, "review", **params)
    assert store.inbox("demo/analyst") == []
    assert store.snapshot()["events"] == before
    assert store.trusted_review_receipt(
        params["reviewer"], params["proposal_id"], params["body_sha256"], request_id=params["request_id"]
    ) is None
    monkeypatch.setattr(store, "_insert_message", original)
    assert len(admin(desk, "review", **params)["message_ids"]) == 2


@pytest.mark.parametrize("changed,error", [
    ({"body_sha256": "a" * 64}, "proposal_content_mismatch"),
    ({"reviewer": "demo/trader"}, "independent_reviewer_required"),
    ({"reviewer": "demo/unknown"}, "reviewer_not_authorized"),
    ({"reviewer": "foreign/analyst"}, "reviewer_not_authorized"),
    ({"proposal_id": "proposal_missing"}, "proposal_not_found"),
])
def test_review_rejects_wrong_content_or_independent_identity_without_enqueue(desk, changed, error):
    params = review_proposal(desk)
    with pytest.raises(IdentityError, match=error):
        admin(desk, "review", **{**params, **changed})
    assert desk[0].store.inbox("demo/analyst") == []
    assert desk[0].store.inbox("demo/trader") == []
    assert admin(desk, "snapshot")["approvals"] == []


@pytest.mark.parametrize("revocation", ["binding", "seat", "capability"])
def test_review_checks_reviewer_binding_capability_and_coordination_revocation(desk, revocation):
    params = review_proposal(desk)
    if revocation == "binding":
        desk[0].registry.trusted_revoke(PrincipalId("demo", "analyst"), expected_binding_generation=1)
    elif revocation == "seat":
        desk[0].store.revoke("demo/analyst", expected_version=1)
    else:
        desk[0].registry.trusted_bind(
            PrincipalId("demo", "analyst"), root_session_id="root-analyst", manifest_hash="a" * 64,
            capabilities=frozenset({"mail.send"}), expected_binding_generation=1,
        )
    with pytest.raises(IdentityError, match="principal_revoked" if revocation == "seat" else "reviewer_not_authorized"):
        admin(desk, "review", **params)
    assert desk[0].store.console_snapshot()["messages"] == []


def test_review_replay_after_execution_returns_original_receipt_but_new_request_fails(desk):
    params = review_proposal(desk)
    queued = admin(desk, "review", **params)
    approval = call(desk, "analyst", "approval.issue", {
        "proposal_id": params["proposal_id"], "body_sha256": params["body_sha256"], "ttl_seconds": 60,
    }, "approval").result
    call(desk, "trader", "action.execute", {"approval_id": approval["approval_id"]}, "execute")
    assert admin(desk, "review", **params) == queued
    with pytest.raises(IdentityError, match="proposal_already_executed"):
        admin(desk, "review", **{**params, "request_id": "second-review"})
    assert len(desk[0].store.inbox("demo/analyst")) == 2


@pytest.mark.parametrize("revocation", ["binding", "seat"])
def test_review_replay_after_reviewer_revocation_is_only_receipt_observation(desk, revocation):
    params = review_proposal(desk)
    queued = admin(desk, "review", **params)
    if revocation == "binding":
        desk[0].registry.trusted_revoke(PrincipalId("demo", "analyst"), expected_binding_generation=1)
    else:
        desk[0].store.revoke("demo/analyst", expected_version=1)
    assert admin(desk, "review", **params) == queued
    with pytest.raises(IdentityError):
        admin(desk, "review", **{**params, "request_id": "fresh-review"})
    assert len(desk[0].store.console_snapshot()["messages"]) == 2
    assert admin(desk, "snapshot")["approvals"] == []


def test_review_at_real_inbox_capacity_rolls_back_header_and_receipt(desk):
    params = review_proposal(desk)
    store = desk[0].store
    with sqlite3.connect(store.db_path) as conn:
        conn.executemany(
            "INSERT INTO messages(sender,recipient,kind,body,priority,created_at) "
            "VALUES('@supervisor','demo/analyst','message','synthetic existing mail',0,1)",
            [()] * 9999,
        )
    before = store.snapshot()["events"]
    with pytest.raises(IdentityError, match="inbox_full"):
        admin(desk, "review", **params)
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 9999
        assert conn.execute("SELECT count(*) FROM messages WHERE kind LIKE 'review_%'").fetchone()[0] == 0
    assert store.snapshot()["events"] == before
    assert store.trusted_review_receipt(
        params["reviewer"], params["proposal_id"], params["body_sha256"], request_id=params["request_id"]
    ) is None
