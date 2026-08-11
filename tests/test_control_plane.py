"""Container control-plane identity, atomicity and streaming contracts."""

from __future__ import annotations

import json
import hashlib
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deskd import mailbox, meetings, orchestration as orch
from deskd.config import CONFIG
from deskd.control import commands, store
from deskd.control.auth import ControlAuthError, Principal, TokenStore
from deskd.web.app import create_app


ALPHA_TOKEN = "alpha-token-000000000000000000000001"
BETA_TOKEN = "beta-token-0000000000000000000000002"
SERVICE_TOKEN = "service-token-0000000000000000000001"


def role_principal(role: str = "alpha") -> Principal:
    return Principal(f"role:{role}", role, frozenset({"agent"}))


def service_principal(*scopes: str) -> Principal:
    return Principal("control", None, frozenset(scopes))


def tokens() -> TokenStore:
    return TokenStore.from_tokens({
        ALPHA_TOKEN: role_principal("alpha"),
        BETA_TOKEN: role_principal("beta"),
        SERVICE_TOKEN: service_principal(
            "read", "directive", "orchestrator", "scheduler"),
    })


def headers(token: str = ALPHA_TOKEN, request_id: str | None = None) -> dict:
    out = {"Authorization": f"Bearer {token}"}
    if request_id:
        out["Idempotency-Key"] = request_id
    return out


def post(client: TestClient, request_id: str, verb: str, params: dict,
         token: str = ALPHA_TOKEN):
    return client.post(
        "/api/commands", headers=headers(token, request_id),
        json={"request_id": request_id, "verb": verb, "params": params})


def test_native_command_receipt_mutation_and_event_are_atomic(desk):
    principal = role_principal()
    response = commands.execute(
        principal, "task-create-0001", "task.add", {"title": "one"})
    assert response["accepted"] is True
    task_id = response["result"]["task_id"]
    assert orch.tasks(assignee_role="alpha")[0]["id"] == task_id

    # Same principal/request/content is an exact receipt replay, not a second
    # mutation; changing content under the id is a conflict.
    assert commands.execute(
        principal, "task-create-0001", "task.add", {"title": "one"}) == response
    assert len(orch.tasks(assignee_role="alpha")) == 1
    with pytest.raises(commands.CommandConflict):
        commands.execute(
            principal, "task-create-0001", "task.add", {"title": "different"})

    class SimulatedProcessDeath(BaseException):
        pass

    with pytest.raises(SimulatedProcessDeath):
        commands.execute(
            principal, "meeting-crash-001", "meeting.call",
            {"agenda": "must roll back", "attendees": ["alpha", "beta"]},
            before_complete=lambda: (_ for _ in ()).throw(SimulatedProcessDeath()))
    assert meetings.list_meetings(include_closed=True) == []
    with orch.connect() as conn:
        assert store.command_row(conn, principal.subject,
                                 "meeting-crash-001") is None
        assert conn.execute(
            "SELECT 1 FROM control_events WHERE resource_id='meeting-crash-001'"
        ).fetchone() is None


def test_role_identity_message_and_explicit_task_delegation(desk):
    alpha = role_principal()
    sent = commands.execute(alpha, "message-send-001", "message.send", {
        "target_role": "beta", "subject": "handoff:42", "body": "please review",
    })["result"]
    assert sent["message"]["sender"] == "alpha"
    assert mailbox.inbox("beta")[0]["body"] == "please review"
    with pytest.raises(commands.CommandError, match="forbidden"):
        commands.execute(alpha, "message-spoof-01", "message.send", {
            "target_role": "beta", "subject": "x", "body": "x",
            "sender": "gamma",
        })

    own = commands.execute(alpha, "task-own-00001", "task.add", {
        "title": "delegate me"})["result"]["task_id"]
    with pytest.raises(commands.ControlAuthError, match="delegation"):
        commands.execute(alpha, "task-move-0001", "task.update", {
            "task_id": own, "assignee_role": "beta"})
    CONFIG.task_delegations = (("alpha", "beta"),)
    moved = commands.execute(alpha, "task-move-0002", "task.update", {
        "task_id": own, "assignee_role": "beta"})
    assert moved["result"]["updated"] is True


def test_typed_mailbox_broadcast_read_reply_and_stop_are_one_workflow(desk):
    alpha = role_principal("alpha")
    beta = role_principal("beta")
    gamma = role_principal("gamma")
    thread = commands.execute(alpha, "mailbox-open-0001", "mailbox.open", {
        "subject": "bounded handoff", "max_messages": 12,
    })["result"]
    thread_id = thread["id"]

    sent_all = commands.execute(alpha, "mailbox-send-all1", "mailbox.send", {
        "thread_id": thread_id, "target_role": "all", "kind": "note",
        "body": "broadcast one",
    })["result"]
    sent_both = commands.execute(alpha, "mailbox-send-both", "mailbox.send", {
        "thread_id": thread_id, "target_role": "both", "kind": "note",
        "body": "broadcast two",
    })["result"]
    assert sent_all["recipients"] == sent_both["recipients"] == ["beta", "gamma"]
    assert sent_all["message"]["recipient"] == mailbox.BROADCAST
    with orch.connect() as conn:
        wake_targets = {row["target_role"] for row in conn.execute(
            "SELECT target_role FROM agent_inbox WHERE ref LIKE ?",
            (f"{thread_id}:%",)).fetchall()}
    assert wake_targets == {"beta", "gamma"}

    for index, principal in enumerate((beta, gamma)):
        unread = commands.execute(
            principal, f"mailbox-inbox-{index}a", "mailbox.inbox", {
                "thread_id": thread_id})["result"]
        assert [row["body"] for row in unread["messages"]] == [
            "broadcast one", "broadcast two"]
        marked = commands.execute(
            principal, f"mailbox-inbox-{index}b", "mailbox.inbox", {
                "thread_id": thread_id, "mark_read": True})["result"]
        assert len(marked["messages"]) == 2 and marked["marked_read"] is True
        empty = commands.execute(
            principal, f"mailbox-inbox-{index}c", "mailbox.inbox", {
                "thread_id": thread_id})["result"]
        assert empty["messages"] == []

    question = commands.execute(alpha, "mailbox-question1", "mailbox.send", {
        "thread_id": thread_id, "target_role": "beta", "kind": "question",
        "body": "acknowledge the handoff", "requires_reply": True,
    })["result"]["message"]
    commands.execute(beta, "mailbox-reply-001", "mailbox.send", {
        "thread_id": thread_id, "target_role": "alpha", "kind": "answer",
        "body": "handoff acknowledged", "reply_to": question["id"],
    })
    with orch.connect() as conn:
        debt = conn.execute(
            "SELECT resolved_at FROM mailbox_messages WHERE id=?",
            (question["id"],)).fetchone()
    assert debt["resolved_at"] is not None

    stopped = commands.execute(alpha, "mailbox-stop-0001", "mailbox.stop", {
        "thread_id": thread_id, "action": "close", "reason": "handoff complete",
    })["result"]
    assert stopped["status"] == "closed"


def test_typed_review_preserves_gates_and_keeps_artifacts_host_private(
        desk, tmp_path):
    alpha = role_principal("alpha")
    beta = role_principal("beta")
    desk.review_artifact_root = tmp_path / "private-review-artifacts"

    started = commands.execute(alpha, "review-start-0001", "review.start", {
        "subject": "container boundary", "attendees": ["alpha", "beta"],
    })["result"]
    thread_id = started["meeting"]["thread_id"]
    assert started["thread"]["owner_role"] == "alpha"
    commands.execute(beta, "review-checkin-01", "meeting.check_in", {
        "meeting_id": thread_id,
    })

    def artifact_params(stage: str, name: str, content: str) -> dict:
        return {
            "thread_id": thread_id, "stage": stage, "name": name,
            "content": content,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        }

    with pytest.raises(ValueError, match="safe basename"):
        commands.execute(alpha, "review-bad-name01", "review.submit",
                         artifact_params("report", "../report.md", "bad name"))
    mismatch = artifact_params("report", "bad.md", "digest mismatch")
    mismatch["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="does not match"):
        commands.execute(alpha, "review-bad-sha001", "review.submit", mismatch)
    with pytest.raises(commands.CommandError, match="unknown command params"):
        commands.execute(alpha, "review-client-path", "review.submit", {
            **artifact_params("report", "bad.md", "never trust a path"),
            "path": "/tmp/client-controlled",
        })

    report_a = artifact_params("report", "alpha-report.md", "alpha report")
    uploaded = commands.execute(
        alpha, "review-report-a01", "review.submit", report_a)["result"]
    assert uploaded["artifact"] == {
        "name": "alpha-report.md", "sha256": report_a["sha256"],
        "size_bytes": len("alpha report"),
        "content_type": "text/plain; charset=utf-8",
    }
    updates = commands.execute(beta, "review-updates-01", "meeting.updates", {
        "meeting_id": thread_id,
    })["result"]
    assert updates["messages"]
    assert all("artifact_path" not in row for row in updates["messages"])
    assert str(desk.review_artifact_root) not in json.dumps(updates)

    report_b = artifact_params("report", "beta-report.md", "beta report")
    report_b.pop("stage")
    commands.execute(beta, "review-report-b01", "review.report", report_b)
    assert mailbox.get_thread(thread_id)["phase"] == "cross_review"

    review_a = artifact_params("review", "alpha-review.md", "alpha review")
    review_a.pop("stage")
    commands.execute(alpha, "review-cross-a001", "review.review", review_a)
    review_b = artifact_params("review", "beta-review.md", "beta review")
    review_b.pop("stage")
    commands.execute(beta, "review-cross-b001", "review.review", review_b)
    assert mailbox.get_thread(thread_id)["phase"] == "discussion"

    commands.execute(alpha, "review-agree-a001", "review.agree", {
        "thread_id": thread_id, "body": "alpha agrees",
    })
    commands.execute(beta, "review-agree-b001", "review.agree", {
        "thread_id": thread_id, "body": "beta agrees",
    })
    assert mailbox.get_thread(thread_id)["phase"] == "ready_to_finalize"

    beta_final = artifact_params("final", "beta-final.md", "not finalizer")
    beta_final.pop("stage")
    with pytest.raises(ValueError, match="only alpha"):
        commands.execute(beta, "review-final-beta", "review.finalize", beta_final)
    alpha_final = artifact_params("final", "final.md", "final synthesis")
    alpha_final.pop("stage")
    commands.execute(alpha, "review-final-alpha", "review.finalize", alpha_final)

    status = commands.execute(
        beta, "review-status-001", "review.status", {
            "thread_id": thread_id})["result"]
    assert status["thread"]["phase"] == "finalized"
    assert len(status["artifacts"]) == 5
    assert all(a["managed"] and "path" not in a and "stored_path" not in a
               for a in status["artifacts"])
    with orch.connect() as conn:
        stored = [dict(row) for row in conn.execute(
            "SELECT * FROM control_review_artifacts WHERE thread_id=?",
            (thread_id,)).fetchall()]
    assert len(stored) == 5
    for row in stored:
        path = Path(row["stored_path"])
        assert path.is_relative_to(desk.review_artifact_root)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_wake_claim_session_bind_and_land_are_role_cas(desk):
    alpha = role_principal()
    orch.task_add("wake me", assignee_role="alpha", priority="urgent")
    assert orch.plan_wakes(record=True)["actions"]

    first = commands.execute(
        alpha, "wake-claim-0001", "agent.wake.claim", {})["result"]
    claim = first["claim"]
    assert claim["role"] == "alpha" and claim["attempt_ids"]
    recovered = commands.execute(
        alpha, "wake-claim-0002", "agent.wake.claim", {})["result"]
    assert recovered["recovered"] is True
    assert recovered["claim"]["claim_id"] == claim["claim_id"]

    start = commands.execute(alpha, "session-start-01", "agent.session.start", {
        "session_id": "provisional-1", "mode": claim["mode"],
        "provider": "claude", "image_digest": CONFIG.image_digest,
        "build_revision": CONFIG.build_revision,
        "config_version": CONFIG.config_version,
        "prompt_version": CONFIG.prompt_version,
    })["result"]
    assert start["provenance"]["provisional_session_id"] == "provisional-1"
    bound = commands.execute(alpha, "session-bind-001", "agent.session.bind", {
        "provisional_session_id": "provisional-1",
        "actual_session_id": "provider-thread-99",
    })["result"]
    assert bound["session_id"] == "provider-thread-99"
    with pytest.raises(commands.CommandConflict):
        commands.execute(alpha, "heartbeat-bad-01", "agent.session.heartbeat", {
            "session_id": "someone-else"})
    landed = commands.execute(alpha, "wake-land-00001", "agent.wake.land", {
        "claim_id": claim["claim_id"], "outcome": "landed",
        "session_id": "provider-thread-99",
    })["result"]
    assert landed["state"] == "landed"


def test_indeterminate_wake_quarantines_role_until_operator_reconciles(desk):
    alpha = role_principal()
    orch.task_add("unsafe retry", assignee_role="alpha", priority="urgent")
    orch.plan_wakes(record=True)
    claim = commands.execute(
        alpha, "wake-claim-quarantine", "agent.wake.claim", {})["result"]["claim"]
    quarantined = commands.execute(
        alpha, "wake-land-quarantine", "agent.wake.land", {
            "claim_id": claim["claim_id"], "outcome": "indeterminate",
            "error": "worker died after an external tool may have run",
        })["result"]
    assert quarantined["state"] == "indeterminate"
    with pytest.raises(commands.CommandConflict, match="quarantined"):
        commands.execute(alpha, "wake-claim-blocked", "agent.wake.claim", {})

    operator = service_principal("read", "operator")
    reconciled = commands.execute(
        operator, "wake-reconcile-001", "wake.reconcile", {
            "claim_id": claim["claim_id"], "resolution": "retry",
            "note": "operator verified no external effect committed",
        })["result"]
    assert reconciled["state"] == "reconciled"
    assert reconciled["resolution"] == "retry"


def test_operator_landed_reconciliation_delivers_claimed_inbox(desk):
    alpha = role_principal()
    inbox_id = orch.inbox_enqueue(
        "alpha", "system", "uncertain delivery", priority="urgent")
    orch.plan_wakes(record=True)
    claim = commands.execute(
        alpha, "wake-claim-landed", "agent.wake.claim", {})["result"]["claim"]
    assert inbox_id in claim["inbox_ids"]
    commands.execute(alpha, "wake-land-unknown", "agent.wake.land", {
        "claim_id": claim["claim_id"], "outcome": "indeterminate",
        "error": "lost worker acknowledgement",
    })
    commands.execute(
        service_principal("operator"), "wake-reconcile-landed",
        "wake.reconcile", {"claim_id": claim["claim_id"],
                           "resolution": "landed",
                           "note": "operator proved the worker started"})
    with orch.connect() as conn:
        row = conn.execute(
            "SELECT delivered_at FROM agent_inbox WHERE id=?", (inbox_id,)
        ).fetchone()
    assert row["delivered_at"] is not None


def test_external_duplicate_has_one_live_execution(desk):
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def invoke(ctx, params):
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(5)
        return {"echo": params}

    CONFIG.command_handlers = (
        commands.HostCommand("tool.invoke", invoke, transactional=False),)
    principal = role_principal()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(commands.execute, principal, "tool-job-000001",
                            "tool.invoke", {"tool": "scan"})
        assert entered.wait(5)
        duplicate = commands.execute(
            principal, "tool-job-000001", "tool.invoke", {"tool": "scan"})
        assert duplicate["job"]["status"] == "running"
        release.set()
        completed = first.result(timeout=5)
    assert calls == 1
    assert completed["result"] == {"echo": {"tool": "scan"}}


def test_nonrecoverable_external_lost_receipt_is_terminal_indeterminate(desk):
    calls = 0

    def invoke(ctx, params):
        nonlocal calls
        calls += 1
        return {"side_effect": "may have happened"}

    CONFIG.command_handlers = (
        commands.HostCommand("tool.invoke", invoke, transactional=False),)
    principal = role_principal()
    with pytest.raises(RuntimeError, match="lost response"):
        commands.execute(
            principal, "tool-indeterminate", "tool.invoke", {"tool": "send"},
            before_complete=lambda: (_ for _ in ()).throw(
                RuntimeError("lost response")))
    with pytest.raises(commands.CommandConflict, match="indeterminate"):
        commands.execute(
            principal, "tool-indeterminate", "tool.invoke", {"tool": "send"})
    assert calls == 1


def test_cursor_rejects_future_and_retention_gap(desk):
    store.ensure_schema()
    current = store.current_cursor()
    prefix = current.rsplit("_", 1)[0]
    with pytest.raises(store.CursorAhead):
        store.events_after(f"{prefix}_ffffffffffffffff")
    with pytest.raises(store.CursorWrongServer):
        store.events_after("evt_0000000000000000_0000000000000000")


def test_http_snapshot_command_scoping_and_idempotency(desk):
    client = TestClient(create_app(token_store=tokens()))
    denied = client.get("/api/snapshot")
    assert denied.status_code == 401

    alpha = client.get("/api/snapshot", headers=headers()).json()
    service = client.get(
        "/api/snapshot", headers=headers(SERVICE_TOKEN)).json()
    expected = {"cursor", "generated_at", "server_version", "board", "tasks",
                "inbox", "meetings", "hooks", "wake", "runtime", "meta"}
    assert set(alpha) == expected == set(service)
    assert set(alpha["tasks"]) == {"tasks", "stalled_ids"}
    assert "attempts" in alpha["wake"]
    assert alpha["runtime"]["roles"][0]["role"] == "alpha"
    assert "message.send" in alpha["meta"]["allowed_verbs"]
    assert "directive.send" not in alpha["meta"]["allowed_verbs"]
    assert "directive.send" in service["meta"]["allowed_verbs"]

    mismatch = client.post(
        "/api/commands", headers=headers(request_id="different-id"),
        json={"request_id": "http-task-00001", "verb": "task.add",
              "params": {"title": "x"}})
    assert mismatch.status_code == 400
    made = post(client, "http-task-00001", "task.add", {"title": "x"})
    assert made.status_code == 202
    replay = post(client, "http-task-00001", "task.add", {"title": "x"})
    assert replay.json() == made.json()
    cross = client.get("/api/inbox?role=beta", headers=headers())
    assert cross.status_code == 403


def test_api_only_disables_legacy_and_scopes_detail(desk):
    meetings.call_meeting(
        agenda="private room", called_by="alpha", attendees=["alpha", "beta"])
    meeting_id = meetings.list_meetings()[0]["meeting"]["thread_id"]
    CONFIG.control_api_only = True
    client = TestClient(create_app(token_store=tokens()))

    assert client.get("/api/board", headers=headers()).status_code == 404
    assert client.get("/api/agent/alpha/feed").status_code == 401
    assert client.get("/api/agent/alpha/feed", headers=headers()).status_code == 200
    assert client.get("/api/agent/beta/feed", headers=headers()).status_code == 403
    assert client.get(
        f"/api/meetings/{meeting_id}", headers=headers()).status_code == 200
    gamma_token = "gamma-token-00000000000000000000001"
    scoped = TokenStore.from_tokens({
        gamma_token: role_principal("gamma"), ALPHA_TOKEN: role_principal("alpha")})
    other = TestClient(create_app(token_store=scoped))
    assert other.get(
        f"/api/meetings/{meeting_id}", headers=headers(gamma_token)).status_code == 403


def test_snapshot_and_sse_advance_across_hidden_role_event(desk):
    client = TestClient(create_app(token_store=tokens()))
    cursor = client.get("/api/snapshot", headers=headers()).json()["cursor"]
    orch.inbox_enqueue("beta", "system", "beta secret", body="do not leak")
    # A hidden beta event advances alpha's cursor through one coalesced cursor
    # invalidation; it never serializes the secret or beta resource id.
    response = client.get(
        f"/api/events?after={cursor}&once=true", headers=headers())
    assert response.status_code == 200
    chunk = response.text
    assert "event: cursor" in chunk and "redacted" in chunk
    assert "beta secret" not in chunk


def test_sse_redacts_every_other_role_and_nonparticipant_thread_event(desk):
    client = TestClient(create_app(token_store=tokens()))
    cursor = client.get("/api/snapshot", headers=headers()).json()["cursor"]
    with orch.connect(write=True) as conn:
        conn.execute(
            "UPDATE agent_registry SET display_name=? WHERE role='beta'",
            ("private beta registry change",))
    called = meetings.call_meeting(
        agenda="beta gamma private room", called_by="beta",
        attendees=["beta", "gamma"])
    meeting_id = called["meeting"]["thread_id"]
    with orch.connect(write=True) as conn:
        conn.execute(
            """INSERT INTO control_review_artifacts
               (thread_id,role,stage,name,sha256,size_bytes,stored_path,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (meeting_id, "beta", "report", "secret.md", "a" * 64, 6,
             "/host/private/secret-artifact", store.now_iso()))

    response = client.get(
        f"/api/events?after={cursor}&once=true", headers=headers())
    assert response.status_code == 200
    chunk = response.text
    assert "event: cursor" in chunk and '"redacted":true' in chunk
    for private in (
            "private beta registry change", "beta gamma private room",
            meeting_id, "secret.md", "/host/private/secret-artifact",
            '"resource":"registry"', '"resource":"meeting"',
            '"resource":"review_artifact"'):
        assert private not in chunk


def test_secret_files_and_manifest_must_be_owner_only(desk, tmp_path, monkeypatch):
    role_dir = tmp_path / "roles"
    role_dir.mkdir(mode=0o700)
    alpha_file = role_dir / "alpha.token"
    alpha_file.write_text(ALPHA_TOKEN)
    alpha_file.chmod(0o644)
    monkeypatch.setenv("DESKD_ROLE_TOKENS_DIR", str(role_dir))
    with pytest.raises(ControlAuthError, match="group/world"):
        TokenStore.from_environment()
    alpha_file.chmod(0o600)
    loaded = TokenStore.from_environment()
    assert loaded.authenticate(f"Bearer {ALPHA_TOKEN}").role == "alpha"
    real_secret = tmp_path / "real-alpha-token"
    real_secret.write_text(ALPHA_TOKEN)
    real_secret.chmod(0o600)
    alpha_file.unlink()
    alpha_file.symlink_to(real_secret)
    with pytest.raises(ControlAuthError, match="non-symlink"):
        TokenStore.from_environment()

    monkeypatch.delenv("DESKD_ROLE_TOKENS_DIR")
    manifest = tmp_path / "services.json"
    manifest.write_text(json.dumps({
        "version": 1,
        "principals": [{
            "subject": "scheduler", "scopes": ["scheduler"],
            "token_sha256": hashlib.sha256(
                SERVICE_TOKEN.encode()).hexdigest(),
        }],
    }))
    manifest.chmod(0o644)
    monkeypatch.setenv("DESKD_SERVICE_TOKENS_FILE", str(manifest))
    with pytest.raises(ControlAuthError, match="group/world"):
        TokenStore.from_environment()
    manifest.chmod(0o600)
    assert TokenStore.from_environment().authenticate(
        f"Bearer {SERVICE_TOKEN}").subject == "scheduler"
    real_manifest = tmp_path / "real-services.json"
    manifest.rename(real_manifest)
    manifest.symlink_to(real_manifest)
    with pytest.raises(ControlAuthError, match="non-symlink"):
        TokenStore.from_environment()


def test_new_private_projection_tables_have_event_triggers(desk):
    store.ensure_schema()
    expected = {
        "session_feed", "session_todos", "message_delivery",
        "mailbox_notifications", "wake_escalations", "meeting_events",
        "meeting_response_obligations", "meeting_escalations",
    }
    with orch.connect() as conn:
        trigger_tables = {
            row["tbl_name"] for row in conn.execute(
                "SELECT DISTINCT tbl_name FROM sqlite_master "
                "WHERE type='trigger' AND name LIKE 'trg_control_%'")
        }
    assert expected <= trigger_tables

    before = store.current_cursor()
    orch.set_status("alpha", state="working", session_id="feed-session")
    orch.feed_append("alpha", "feed-session", "narration", "working")
    orch.record_todos("alpha", [{"content": "one", "status": "in_progress"}])
    resources = {event["resource"] for event in store.events_after(before)}
    assert {"presence", "feed", "todos"} <= resources
