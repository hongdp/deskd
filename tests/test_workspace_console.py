"""Real loopback HTTP + fresh SQLite, with explicitly synthetic role evidence."""

import http.client
import json
import secrets
import threading

import pytest

from deskd.workspace.console import BrowserSessions, ConsoleBackend, ConsoleError, make_server
from deskd.workspace.console_demo import create_demo


@pytest.fixture
def console(tmp_path):
    backend = create_demo(tmp_path / "console-demo")
    sessions = BrowserSessions()
    server = make_server(backend, sessions=sessions)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    proof = secrets.token_hex(32)

    def request(path, value=None, *, authenticated=True):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        headers = {"X-Deskd-Session": proof} if authenticated else {}
        if value is not None:
            headers.update({"Origin": f"http://127.0.0.1:{server.server_port}",
                            "X-Deskd-Console": "1", "Content-Type": "application/json"})
        conn.request("GET" if value is None else "POST", path,
                     None if value is None else json.dumps(value), headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw)

    yield backend, sessions, server, request
    server.shutdown()
    server.server_close()
    worker.join(timeout=3)


def pair(console):
    _, sessions, _, request = console
    code, state = request("/api/pair", {})
    assert code == 200 and state["state"] == "pending"
    assert sessions.approve(state["pairing_id"])
    assert request("/api/session") == (200, {"state": "paired"})


def test_http_human_task_round_trip_is_persistent_idempotent_and_version_checked(console):
    backend, _, _, request = console
    assert request("/api/snapshot")[0] == 401
    pair(console)
    command = {"command": "task", "params": {
        "assignee": "demo/engineer", "title": "Browser task", "body": "Synthetic only",
        "request_id": "browser-create-1",
    }}
    code, created = request("/api/commands", command)
    assert code == 200 and created["result"]["creator"] == "@supervisor"
    assert created["result"]["status"] == "queued"
    assert request("/api/commands", command) == (200, created)
    command["params"]["body"] = "Different work with reused id"
    assert request("/api/commands", command)[0] in (400, 409)
    _, snapshot = request("/api/snapshot")
    task = next(t for t in snapshot["result"]["tasks"] if t["id"] == created["result"]["id"])
    assert task["detail"] == "Synthetic only"
    cancel = {"command": "cancel", "params": {"task_id": task["id"], "expected_version": task["version"]}}
    assert request("/api/commands", cancel)[0] == 200
    assert request("/api/commands", cancel)[0] in (400, 409)
    assert next(t for t in backend.snapshot()["tasks"] if t["id"] == task["id"])["status"] == "cancelled"


def test_operator_conversation_and_read_state_do_not_fabricate_role_handling(console):
    _, _, _, request = console
    pair(console)
    command = {"command": "message", "params": {
        "recipient": "demo/analyst", "body": "Please clarify the synthetic scope.", "request_id": "human-message-1",
    }}
    assert request("/api/commands", command)[0] == 200
    _, result = request("/api/snapshot")
    snapshot = result["result"]
    sent = next(m for m in snapshot["messages"] if m["body"] == command["params"]["body"])
    assert sent["sender"] == "@supervisor" and sent["state"] == "queued"
    reply = next(m for m in snapshot["messages"] if "reply_id" in m)
    assert snapshot["unread_count"] == 1 and not reply["read"]
    assert request("/api/commands", {"command": "ack", "params": {"message_ids": [reply["reply_id"]]}})[0] == 200
    assert request("/api/snapshot")[1]["result"]["unread_count"] == 0
    assert request("/api/logout", {})[0] == 200
    assert request("/api/snapshot")[0] == 401


def test_pause_preserves_compare_and_swap_and_review_has_no_execute_route(console):
    _, _, _, request = console
    pair(console)
    snapshot = request("/api/snapshot")[1]["result"]
    seat = next(s for s in snapshot["seats"] if s["principal"] == "demo/analyst")
    pause = {"command": "pause", "params": {
        "principal": seat["principal"], "paused": True, "expected_version": seat["version"],
    }}
    assert request("/api/commands", pause)[0] == 200
    assert request("/api/commands", pause)[0] == 409
    for name in ("approval.issue", "action.execute", "bind", "model.auth", "workspace.store.activate"):
        assert request("/api/commands", {"command": name, "params": {}})[0] == 400
    assert len(request("/api/snapshot")[1]["result"]["memos"]) == 1


def test_lost_reply_never_retries_or_claims_failure():
    calls = []

    def admin(method, params):
        calls.append(method)
        raise OSError("synthetic after-commit disconnect")

    backend = ConsoleBackend(admin)
    with pytest.raises(ConsoleError, match="outcome_unknown"):
        backend.command({"command": "message", "params": {
            "recipient": "demo/analyst", "body": "synthetic", "request_id": "stable-id",
        }})
    assert calls == ["workspace.console.message"]


def test_browser_review_queues_exact_content_once_without_granting_authority(console):
    _, _, _, request = console
    pair(console)
    before = request("/api/snapshot")[1]["result"]
    proposal = next(p for p in before["proposals"] if p["status"] == "pending")
    command = {"command": "review", "params": {
        "proposal_id": proposal["proposal_id"], "body_sha256": proposal["body_sha256"],
        "reviewer": "demo/analyst", "request_id": "browser-review-1",
    }}
    status, result = request("/api/commands", command)
    assert status == 200 and result["result"]["queued"]
    assert request("/api/commands", command) == (status, result)
    after = request("/api/snapshot")[1]["result"]
    assert len(after["messages"]) == len(before["messages"]) + 2
    assert any(m.get("kind") == "review_body" and m["body"] == proposal["body"] for m in after["messages"])
    assert after["approvals"] == before["approvals"]
    assert after["memos"] == before["memos"]


def test_demo_is_explicit_fresh_and_never_starts_a_runtime(tmp_path):
    directory = tmp_path / "demo"
    backend = create_demo(directory)
    snapshot = backend.snapshot()
    assert snapshot["mode"] == "demo" and not snapshot["live_observation"]
    assert len(snapshot["proposals"]) == 2 and len(snapshot["memos"]) == 1
    assert all(s["turns_used"] == 0 for s in snapshot["seats"])
    assert not (directory / "unused-admin").exists()
    with pytest.raises(FileExistsError):
        create_demo(directory)


def test_extended_console_requires_pairing_and_preserves_human_goal_authority(console):
    _, _, _, request = console
    assert request("/api/workspace")[0] == 401
    pair(console)
    before = request("/api/workspace")[1]["result"]
    assert before["health"]["observation"] == "synthetic"
    source = {"command": "source_configure", "params": {
        "name": "console-fixture", "url": "https://example.test/brief", "max_bytes": 8192, "timeout_seconds": 1,
    }}
    assert request("/api/commands", source)[0] == 200
    goal = {"command": "goal_create", "params": {
        "title": "HTTP goal", "objective": "Synthetic scope", "researcher": "demo/engineer",
        "reviewer": "demo/analyst", "executor": "demo/trader", "source_ids": ["console-fixture"],
        "request_id": "http-goal", "interval_seconds": None, "max_cycles": 1,
        "followup_seconds": 60, "max_followups": 2,
    }}
    code, created = request("/api/commands", goal)
    assert code == 200 and created["result"]["state"] == "active"
    assert request("/api/commands", goal) == (200, created)
    value = created["result"]
    pause = {"command": "goal_update", "params": {
        "goal_id": value["id"], "action": "pause", "expected_version": value["version"], "request_id": "http-goal-pause",
    }}
    assert request("/api/commands", pause)[1]["result"]["state"] == "paused"
    assert request("/api/commands", pause)[0] == 200
    assert request("/api/commands", {"command": "goal.report", "params": {}})[0] == 400
    assert request("/api/commands", {"command": "notification_configure", "params": {}})[0] == 400
    assert request("/api/logout", {})[0] == 200
    assert request("/api/workspace")[0] == 401


def test_console_source_validation_never_fetches_and_cannot_read_role_memory(console):
    _, _, _, request = console
    pair(console)
    command = {"command": "source_configure", "params": {
        "name": "private", "url": "http://127.0.0.1/", "max_bytes": 1000, "timeout_seconds": 1,
    }}
    assert request("/api/commands", command)[0] == 400
    command = {"command": "memory_search", "params": {"query": "", "limit": 20}}
    code, found = request("/api/commands", command)
    assert code == 200
    assert all(m["shared"] for m in found["result"]["memories"])
    command["params"]["actor"] = "demo/engineer"
    assert request("/api/commands", command)[0] == 400
