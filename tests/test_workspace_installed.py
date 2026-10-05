"""Full fresh-install acceptance: official daemon, MCP, real UIDs and recovery.

All model replies are fixed local scripts, all effects are SQLite mock memos,
and every process belongs to the newly installed scratchpad workspace.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import pytest

from test_workspace_linux_sandbox import _assert_root_tree

HARNESS_UID = 27002
GATEWAY_UID = 27001
BUSINESS_GID = 27003

MEMO = "PUBLIC MOCK MEMO"
MEMO_HASH = hashlib.sha256(MEMO.encode()).hexdigest()


def _object(value):
    """Unwrap only this mock's MCP text/receipt envelopes, never host data."""
    if isinstance(value, str):
        return _object(json.loads(value))
    if isinstance(value, list):
        assert len(value) == 1, "unexpected synthetic tool output"
        return _object(value[0])
    assert isinstance(value, dict)
    if value.get("type") in {"text", "output_text", "input_text"}:
        return _object(value["text"])
    if "content" in value:
        assert value.get("isError") is not True, "synthetic MCP call rejected"
        return _object(value["content"])
    assert "error" not in value, "synthetic gateway call rejected"
    return value.get("result", value)


def _tool(tools, action):
    expected = re.sub(r"[^a-z0-9]", "", action)
    for entry in tools:
        if entry.get("type") == "namespace":
            if "deskd" in entry.get("name", ""):
                for tool in entry.get("tools", []):
                    if re.sub(r"[^a-z0-9]", "", tool.get("name", "")) == expected:
                        return entry["name"], tool["name"]
        elif "deskd" in entry.get("name", ""):
            name = entry["name"]
            if re.sub(r"[^a-z0-9]", "", name.rsplit("__", 1)[-1]) == expected:
                return None, name
    return None


class CollaborationMock:
    """Deterministic per-root scripts; another root independently authorizes."""

    def __init__(self):
        self.roles = {}
        self.states = {}
        self.calls = 0
        self.finished = {}
        self.failure = None
        self.proposal = None
        self.approval = None
        self.memo = None
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    assert 0 < length < 4 * 1024 * 1024
                    body = json.loads(self.rfile.read(length))
                    # This specific non-secret header identifies only a new mock root.
                    root_id = self.headers.get("thread-id")
                    with owner.lock:
                        item = owner.reply(root_id, body)
                    events = [
                        {"type": "response.created", "response": {"id": "mock"}},
                        {"type": "response.output_item.done", "item": item},
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "mock",
                                "usage": {
                                    "input_tokens": 1,
                                    "output_tokens": 1,
                                    "total_tokens": 2,
                                },
                            },
                        },
                    ]
                    output = "".join(
                        "event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n"
                        for e in events
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(output)))
                    self.end_headers()
                    self.wfile.write(output)
                except Exception as exc:
                    # Every string here comes from this synthetic fixture. No request
                    # body, authorization header, or inherited environment is printed.
                    owner.failure = type(exc).__name__ + ": " + str(exc)[:300]
                    self.send_error(500)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()

    def script(self, seat):
        inbox = yield "inbox.read", {}
        messages = inbox["messages"]
        assert messages, "wake must expose a durable inbox message"
        ids = [str(m["id"]) for m in messages]
        payloads = []
        for message in messages:
            try:
                payloads.append(json.loads(message["body"]))
            except json.JSONDecodeError:
                pass
        if seat == "trader":
            approved = next((x for x in payloads if "approval_id" in x), None)
            if approved:
                args = {
                    "approval_id": approved["approval_id"],
                    "request_id": "publish-mock-memo",
                }
                first = yield "action.execute", args
                replay = yield "action.execute", args
                assert first["memo_id"] == replay["memo_id"]
                self.memo = first
            else:
                proposal = yield (
                    "proposal.create",
                    {"executor_principal": "desk/trader", "body": MEMO},
                )
                self.proposal = proposal
                yield (
                    "mail.send",
                    {
                        "recipient": "desk/analyst",
                        "body": json.dumps(
                            {
                                "proposal_id": proposal["proposal_id"],
                                "body_sha256": proposal["body_sha256"],
                            }
                        ),
                    },
                )
                yield (
                    "task.create",
                    {
                        "assignee": "desk/engineer",
                        "title": "Review synthetic fixture",
                        "body": "MOCK TASK BODY MUST NOT APPEAR ON BOARD",
                        "depends_on": [],
                    },
                )
        elif seat == "analyst":
            proposal = next(x for x in payloads if "proposal_id" in x)
            # Independent mock review checks a predetermined memo digest; message
            # text itself never serves as gateway authorization.
            assert proposal["body_sha256"] == MEMO_HASH
            approval = yield (
                "approval.issue",
                {
                    "proposal_id": proposal["proposal_id"],
                    "body_sha256": MEMO_HASH,
                    "ttl_seconds": 120,
                },
            )
            self.approval = approval
            yield (
                "mail.send",
                {
                    "recipient": "desk/trader",
                    "body": json.dumps({"approval_id": approval["approval_id"]}),
                },
            )
        elif seat == "engineer" and any("task_id" in x for x in payloads):
            tasks = yield "tasks.read", {}
            task = next(x for x in tasks["tasks"] if x["status"] != "done")
            yield (
                "task.update",
                {
                    "task_id": task["id"],
                    "status": "done",
                    "expected_version": task["version"],
                },
            )
        yield "inbox.ack", {"message_ids": ids}

    def reply(self, root_id, body):
        assert root_id in self.roles, "unknown mock root"
        self.calls += 1
        seat = self.roles[root_id]
        state = self.states.get(root_id)
        if state is None:
            generator = self.script(seat)
            state = self.states[root_id] = {
                "generator": generator,
                "action": next(generator),
                "waiting": None,
                "searches": 0,
            }
        waiting = state["waiting"]
        if waiting:
            outputs = [
                item
                for item in body.get("input", [])
                if item.get("call_id") == waiting[0]
                and item.get("type") in ("function_call_output", "tool_search_output")
            ]
            assert outputs, "tool result must arrive before the next scripted step"
            if waiting[1] != "search":
                result = _object(outputs[-1]["output"])
                try:
                    state["action"] = state["generator"].send(result)
                    state["searches"] = 0
                except StopIteration:
                    del self.states[root_id]
                    self.finished[seat] = self.finished.get(seat, 0) + 1
                    return {
                        "type": "message",
                        "id": "m",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "Synthetic work complete."}
                        ],
                    }
        action, args = state["action"]
        selected = _tool(body.get("tools", []), action)
        call_id = f"synthetic-{self.calls}"
        if selected is None:
            state["searches"] += 1
            assert state["searches"] <= 2, (
                "mock deskd tool was not exposed after discovery"
            )
            state["waiting"] = (call_id, "search")
            return {
                "type": "tool_search_call",
                "execution": "client",
                "call_id": call_id,
                "arguments": {"query": "deskd " + action, "limit": 20},
            }
        namespace, name = selected
        state["waiting"] = (call_id, action)
        args = {"request_id": "request-" + call_id, **args}
        item = {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(args),
        }
        if namespace:
            item["namespace"] = namespace
        return item


def _wait(manager, deployment, mock, predicate, *, seconds=60):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        manager.tick()
        assert mock.failure is None, mock.failure
        reply = deployment.admin("workspace.status")
        assert reply.get("ok") is True
        last = reply["result"]
        if predicate(last):
            return last
        time.sleep(0.05)
    pytest.fail(
        "installed synthetic workspace did not reach its expected durable state: "
        + json.dumps(
            {
                "finished": mock.finished,
                "calls": mock.calls,
                "active": last.get("service") if last else None,
            }
        )
    )


def test_installed_workspace_collaborates_recovers_and_exposes_readonly_board(
    monkeypatch,
):
    supplied = os.environ.get("DESKD_SANDBOX_TEST_ROOT")
    if not supplied:
        pytest.skip("opt-in ephemeral Linux root runner required")
    assert sys.platform == "linux" and os.geteuid() == 0
    root = Path(supplied)
    _assert_root_tree(root)
    assert root.name == "scratchpad" and os.path.samefile(root / "system-tmp", "/tmp")
    from deskd.workspace.board import make_server
    from deskd.workspace.deployment import Deployment, install
    from deskd.workspace.manager import WorkspaceManager

    mock = CollaborationMock()
    # Reserve a unique name, then remove only that empty directory because the
    # public installer must create a fresh prefix itself.
    prefix = Path(tempfile.mkdtemp(prefix="wi-", dir=root))
    prefix.rmdir()
    manager = None
    board = None
    board_thread = None
    lifecycle = []
    # Observe only fresh fixture children; do not change their executable,
    # arguments, identity, environment, protocol or authorization decisions.
    # These mock-only files cannot contain a real key or provider response.
    diagnostics = []
    actual_popen = subprocess.Popen

    def observed_popen(argv, **kwargs):
        if isinstance(argv, list) and str(prefix / "lib") in argv:
            path = prefix / f"synthetic-child-{len(diagnostics)}.log"
            diagnostics.append(path)
            with path.open("wb") as log:
                return actual_popen(argv, **{**kwargs, "stderr": log})
        return actual_popen(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", observed_popen)
    try:
        manifest = install(
            prefix,
            binary=root / "official/codex",
            python=Path(os.environ["DESKD_SANDBOX_TEST_PYTHON"]),
            harness_uid=HARNESS_UID,
            gateway_uid=GATEWAY_UID,
            business_gid=BUSINESS_GID,
            mock_port=mock.server.server_port,
        )
        deployment = Deployment(manifest)
        deployment.attest()
        manager = WorkspaceManager(manifest, on_event=lifecycle.append)
        manager.start(bootstrap=True)
        roots = json.loads(deployment.roots_path.read_text())
        mock.roles = {
            record["root_id"]: principal.split("/")[1]
            for principal, record in roots.items()
        }
        assert len(mock.roles) == 3
        assert deployment.admin(
            "workspace.enqueue",
            {
                "recipient": "desk/trader",
                "body": "Begin the fixed synthetic collaboration.",
                "request_id": "start-synthetic-cycle",
            },
        )["ok"]

        def complete(status):
            return (
                mock.memo is not None
                and status["tasks"]
                and all(t["status"] == "done" for t in status["tasks"])
                and all(
                    s["active_dispatch"] is None and set(s["inbox"]) <= {"handled"}
                    for s in status["seats"]
                )
            )

        status = _wait(manager, deployment, mock, complete)
        assert mock.finished.get("trader") == 2
        assert mock.finished.get("analyst") == 1
        assert mock.finished.get("engineer") == 1
        with sqlite3.connect(
            deployment.gateway_db.as_uri() + "?mode=ro", uri=True
        ) as db:
            assert db.execute("SELECT count(*) FROM memo_published").fetchone()[0] == 1
            issuer, executor = db.execute(
                "SELECT issuer_principal,executor_principal FROM memo_approvals"
            ).fetchone()
            assert issuer == "desk/analyst" and executor == "desk/trader"

        board = make_server(lambda: deployment.admin("workspace.status")["result"])
        board_thread = threading.Thread(target=board.serve_forever, daemon=True)
        board_thread.start()
        base = f"http://127.0.0.1:{board.server_port}"
        with urllib.request.urlopen(base + "/status", timeout=5) as response:
            raw = response.read()
            visible = json.loads(raw)
            assert response.headers["Cache-Control"] == "no-store"
        assert len(visible["seats"]) == 3 and MEMO.encode() not in raw
        assert b"MOCK TASK BODY" not in raw and b"proposal_id" not in raw
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(
                urllib.request.Request(base + "/status", data=b"{}", method="POST"),
                timeout=5,
            )
        assert denied.value.code == 405

        engineer = next(s for s in status["seats"] if s["principal"] == "desk/engineer")
        assert deployment.admin(
            "workspace.pause",
            {
                "principal": "desk/engineer",
                "paused": True,
                "expected_version": engineer["version"],
            },
        )["ok"]
        assert deployment.admin(
            "workspace.enqueue",
            {
                "recipient": "desk/engineer",
                "body": "Resume after independent unpause.",
                "request_id": "paused-work",
            },
        )["ok"]
        paused = _wait(
            manager,
            deployment,
            mock,
            lambda s: any(
                x["principal"] == "desk/engineer" and x["inbox"].get("queued") == 1
                for x in s["seats"]
            ),
        )
        assert mock.finished["engineer"] == 1
        before_generation = paused["service"]["generation"]
        manager.close()
        manager = WorkspaceManager(manifest, on_event=lifecycle.append)
        manager.start(bootstrap=True)
        assert json.loads(deployment.roots_path.read_text()) == roots
        recovered = deployment.admin("workspace.status")["result"]
        assert recovered["service"]["generation"] != before_generation
        engineer = next(
            s for s in recovered["seats"] if s["principal"] == "desk/engineer"
        )
        assert engineer["paused"] and engineer["inbox"].get("queued") == 1
        assert deployment.admin(
            "workspace.pause",
            {
                "principal": "desk/engineer",
                "paused": False,
                "expected_version": engineer["version"],
            },
        )["ok"]
        final = _wait(
            manager,
            deployment,
            mock,
            lambda s: (
                mock.finished.get("engineer") == 2
                and all(
                    x["active_dispatch"] is None and set(x["inbox"]) <= {"handled"}
                    for x in s["seats"]
                )
            ),
        )
        assert final["service"]["active"]
        with sqlite3.connect(
            deployment.gateway_db.as_uri() + "?mode=ro", uri=True
        ) as db:
            assert db.execute("SELECT count(*) FROM memo_published").fetchone()[0] == 1
        engineer = next(s for s in final["seats"] if s["principal"] == "desk/engineer")
        assert deployment.admin(
            "workspace.revoke",
            {
                "principal": "desk/engineer",
                "expected_binding_generation": 1,
                "expected_version": engineer["version"],
            },
        )["ok"]
        manager.close()
        manager = WorkspaceManager(manifest, on_event=lifecycle.append)
        manager.start(bootstrap=True)
        assert json.loads(deployment.roots_path.read_text()) == roots
        after_revoke = deployment.admin("workspace.status")["result"]
        assert after_revoke["service"]["active"]
        assert len(after_revoke["seats"]) == 3
        assert all(
            bool(seat["revoked"]) is (seat["principal"] == "desk/engineer")
            for seat in after_revoke["seats"]
        )
        assert mock.finished["engineer"] == 2
        assert lifecycle
    except Exception:
        for path in diagnostics:
            data = path.read_bytes()[:4096]
            if data:
                print("SYNTHETIC CHILD DIAGNOSTIC", data.decode(errors="replace"))
        raise
    finally:
        if board is not None:
            board.shutdown()
            board.server_close()
        if board_thread is not None:
            board_thread.join(timeout=5)
            assert not board_thread.is_alive()
        if manager is not None:
            manager.close()
        mock.close()
