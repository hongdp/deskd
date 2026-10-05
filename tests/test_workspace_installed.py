"""Full fresh-install acceptance: official daemon, MCP, real UIDs and recovery.

All model replies are fixed local scripts, all effects are SQLite mock memos,
and every process belongs to the newly installed scratchpad workspace.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import sqlite3
import stat
import subprocess
import struct
import sys
import tempfile
import termios
import threading
import time
import tomllib
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
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            raise AssertionError(
                "non-JSON synthetic tool text: " + repr(value[:1200])
            ) from None
        return _object(decoded)
    if isinstance(value, list):
        # Stock 0.160.0 MCP output has a timing preamble plus one text body.
        # Strip only this exact observed envelope; never ignore an extra result
        # or an error block while looking for a convenient JSON payload.
        if len(value) == 2:
            header = value[0]
            assert isinstance(header, dict) and header.get("type") == "input_text"
            assert re.fullmatch(
                r"Wall time: [0-9]+(?:\.[0-9]+)? seconds\nOutput:",
                header.get("text", ""),
            ), "unexpected synthetic tool preamble"
            return _object(value[1])
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
            for tool in entry.get("tools", []):
                name = tool.get("name", "")
                if ("deskd" in entry.get("name", "") or "deskd" in name) and re.sub(
                    r"[^a-z0-9]", "", name.rsplit("__", 1)[-1]
                ) == expected:
                    return entry["name"], name
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
                root_id = None
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
                    state = owner.states.get(root_id, {})
                    context = {
                        "seat": owner.roles.get(root_id),
                        "action": state.get("action", [None])[0],
                    }
                    owner.failure = (
                        json.dumps(context)
                        + " "
                        + type(exc).__name__
                        + ": "
                        + str(exc)[:2400]
                    )
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
                "discovered": [],
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
            if waiting[1] == "search":
                discovered = outputs[-1].get("tools")
                assert isinstance(discovered, list) and len(discovered) <= 100, (
                    "official tool search did not return bounded definitions"
                )
                assert all(isinstance(tool, dict) for tool in discovered)
                state["discovered"].extend(discovered)
            else:
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
        advertised = [*body.get("tools", []), *state["discovered"]]
        selected = _tool(advertised, action)
        call_id = f"synthetic-{self.calls}"
        if selected is None:
            state["searches"] += 1
            assert state["searches"] <= 2, (
                "mock deskd tool was not exposed after discovery: "
                + json.dumps(
                    {
                        "action": action,
                        "catalog": [
                            {
                                "type": entry.get("type"),
                                "name": entry.get("name"),
                                "tools": [
                                    tool.get("name") for tool in entry.get("tools", [])
                                ],
                            }
                            for entry in advertised
                        ],
                    }
                )[:2000]
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


def _native_attach(deployment, manager, mock, roots):
    """Real terminal; only local /status and /quit, never a model prompt."""
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 48, 140, 0, 0))
    entry = (
        "import fcntl,sys,termios;fcntl.ioctl(0,termios.TIOCSCTTY,0);"
        "sys.path.insert(0,sys.argv.pop(1));"
        "from deskd.workspace.__main__ import main;raise SystemExit(main())"
    )
    process = None
    output = bytearray()
    replies = {}
    probes = {
        b"\x1b[6n": b"\x1b[1;1R",
        b"\x1b]10;?\x1b\\": b"\x1b]10;rgb:ffff/ffff/ffff\x1b\\",
        b"\x1b]11;?\x1b\\": b"\x1b]11;rgb:0000/0000/0000\x1b\\",
        b"\x1b[?u": b"\x1b[?0u",
        b"\x1b[c": b"\x1b[?1;2c",
    }
    daemon_pid = manager.pids["daemon"]
    before_calls = mock.calls

    def read_terminal():
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                data = os.read(master, 65536)
            except OSError:
                data = b""
            output.extend(data)
            assert len(output) <= 2 * 1024 * 1024, "synthetic terminal output limit"
            for query, answer in probes.items():
                count = output.count(query)
                for _ in range(count - replies.get(query, 0)):
                    os.write(master, answer)
                replies[query] = count
        visible = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", bytes(output))
        assert not any(
            marker in visible
            for marker in (
                b"Meet GPT-6 Sol",
                b"Try new model",
                b"Codex just got an upgrade.",
            )
        ), "native terminal offered a model migration instead of a ready composer"
        manager.tick()
        assert mock.failure is None and mock.calls == before_calls
        return visible

    try:
        process = subprocess.Popen(
            [
                deployment.value["python"]["path"],
                "-I",
                "-B",
                "-c",
                entry,
                str(deployment.prefix / "lib"),
                "attach",
                "--deployment",
                str(deployment.path),
                "--seat",
                "analyst",
            ],
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(deployment.prefix / "base"),
                "TERM": "xterm-256color",
                "LANG": "C.UTF-8",
            },
            cwd=deployment.prefix / "base",
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave)
        slave = None
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            visible = read_terminal()
            assert process.poll() is None, "native terminal exited before ready"
            if b"shortcuts" in visible:
                break
        else:
            pytest.fail("native terminal did not display its ready composer")
        status_offset = len(output)
        os.write(master, b"/status")
        # Let the native paste-burst guard settle before pressing Enter.
        time.sleep(0.15)
        os.write(master, b"\r")
        expected_root = roots["desk/analyst"]["root_id"].encode()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            read_terminal()
            status_output = re.sub(
                rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", bytes(output[status_offset:])
            )
            if expected_root in status_output and b"Session" in status_output:
                break
            assert process.poll() is None, "native terminal exited before /status"
        else:
            pytest.fail("native /status did not show the registered root")
        os.write(master, b"/quit")
        time.sleep(0.15)
        os.write(master, b"\r")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process.poll() is None:
            read_terminal()
        assert process.poll() == 0, "native /quit did not exit cleanly"
        assert manager.pids["daemon"] == daemon_pid and mock.calls == before_calls
        assert json.loads(deployment.roots_path.read_text()) == roots
        assert deployment.admin("workspace.status")["result"]["service"]["active"]
        runtime = deployment.runtime()
        try:
            assert runtime.peer_pid == daemon_pid
            seat = next(s for s in deployment.seats() if s.principal == "desk/analyst")
            binding = runtime.resume_root(seat.root_id, seat.config)
            assert binding.thread_id == expected_root.decode()
            assert runtime.read_root(binding.thread_id)["id"] == binding.thread_id
        finally:
            runtime.close()
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        os.close(master)
        if slave is not None:
            os.close(slave)


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
    from deskd.workspace.board import make_server, read_installed_snapshot
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
    # Observe only fresh fixture children. The original entry, identity,
    # environment, protocol and authorization decisions still run unchanged.
    # These mock-only files cannot contain a real key or provider response.
    diagnostics = []
    actual_popen = subprocess.Popen

    def observed_popen(argv, **kwargs):
        if (
            isinstance(argv, list)
            and str(prefix / "lib") in argv
            and any("run_installed(" in value for value in argv)
        ):
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
            provider="mock",
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

        board = make_server(lambda: read_installed_snapshot(deployment))
        board_thread = threading.Thread(target=board.serve_forever, daemon=True)
        board_thread.start()
        base = f"http://127.0.0.1:{board.server_port}"
        with urllib.request.urlopen(base + "/status", timeout=5) as response:
            raw = response.read()
            visible = json.loads(raw)
            assert response.headers["Cache-Control"] == "no-store"
        assert len(visible["seats"]) == 3 and MEMO.encode() not in raw
        assert visible["live_observation"] and not visible["fenced"]
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
            "workspace.store.schedule_timer",
            {
                "actor": "desk/engineer",
                "due_at": time.time() + 0.1,
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
        with pytest.raises(urllib.error.HTTPError) as disconnected:
            urllib.request.urlopen(base + "/status", timeout=5)
        assert disconnected.value.code == 503
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
        analyst = next(
            seat
            for seat in after_revoke["seats"]
            if seat["principal"] == "desk/analyst"
        )
        assert deployment.admin(
            "workspace.pause",
            {
                "principal": "desk/analyst",
                "paused": True,
                "expected_version": analyst["version"],
            },
        )["ok"]
        previous_pids = manager.pids
        calls_before_crash = mock.calls
        # Only this manager's just-created child is signalled. Do not poll or
        # reap it here: the supervisor reserves the leader PID with WNOWAIT.
        assert manager.children["controller"].pid == previous_pids["controller"]
        os.kill(previous_pids["controller"], signal.SIGKILL)
        deadline = time.monotonic() + 30
        while manager.restarts == 0 and time.monotonic() < deadline:
            manager.tick()
            time.sleep(0.01)
        assert manager.restarts == 1 and manager.state == "running"
        assert all(manager.pids[name] != pid for name, pid in previous_pids.items())
        assert json.loads(deployment.roots_path.read_text()) == roots
        crash_recovered = deployment.admin("workspace.status")["result"]
        assert crash_recovered["service"]["active"]
        assert (
            crash_recovered["service"]["generation"]
            != after_revoke["service"]["generation"]
        )
        analyst = next(
            seat
            for seat in crash_recovered["seats"]
            if seat["principal"] == "desk/analyst"
        )
        engineer = next(
            seat
            for seat in crash_recovered["seats"]
            if seat["principal"] == "desk/engineer"
        )
        assert analyst["paused"] and engineer["revoked"]
        assert mock.calls == calls_before_crash
        with sqlite3.connect(
            deployment.gateway_db.as_uri() + "?mode=ro", uri=True
        ) as db:
            assert db.execute("SELECT count(*) FROM memo_published").fetchone()[0] == 1
        assert deployment.admin(
            "workspace.pause",
            {
                "principal": "desk/analyst",
                "paused": False,
                "expected_version": analyst["version"],
            },
        )["ok"]
        _native_attach(deployment, manager, mock, roots)
        assert deployment.admin("fence")["ok"]
        with urllib.request.urlopen(base + "/status", timeout=5) as response:
            fenced = json.load(response)
        assert fenced["live_observation"] and fenced["fenced"]
        assert lifecycle
    except Exception:
        if "deployment" in locals():
            # Only this fresh mock installation's generated non-secret config.
            # Print differing key names, never values or another host's config.
            config_path = prefix / "harness/config.toml"
            expected_config = next(
                item["content"]
                for item in deployment.plan["files"]
                if item["path"] == str(config_path)
            )
            before = tomllib.loads(expected_config)
            after = tomllib.loads(config_path.read_text())

            def changed_keys(left, right, stem=""):
                if isinstance(left, dict) and isinstance(right, dict):
                    return [
                        key
                        for name in sorted(left.keys() | right.keys())
                        for key in changed_keys(
                            left.get(name), right.get(name), stem + "/" + name
                        )
                    ]
                return [stem] if left != right else []

            print("SYNTHETIC CONFIG CHANGED KEYS", changed_keys(before, after))
            for name in deployment.value["inventory"]:
                path = Path(name)
                assert prefix in path.parents
                info = path.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != 0
                    or info.st_mode & 0o022
                    or info.st_nlink != 1
                ):
                    print(
                        "SYNTHETIC INVENTORY METADATA",
                        {
                            "path": str(path.relative_to(prefix)),
                            "uid": info.st_uid,
                            "mode": oct(info.st_mode),
                            "nlink": info.st_nlink,
                        },
                    )
        for path in diagnostics:
            with path.open("rb") as log:
                data = log.read(4096)
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
