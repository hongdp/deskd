"""Real UDS + command transactions + memo workflow + MCP loop integration.

This single-UID fixture uses only private listener/bootstrap seams. It proves
neither a production three-UID deployment nor public run_bridge authentication.
All data are synthetic and all effects remain within this fixture's SQLite DB.
"""

import io
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import time
import tempfile
import uuid

import pytest

from deskd.gateway.actions import MemoWorkflow, WORKFLOW_ACTIONS, tool_catalog
from deskd.gateway.bridge import _serve_stdio
from deskd.gateway.commands import GatewayCommands
from deskd.gateway.events import GatewayEventStore
from deskd.gateway.identity import IdentityError
from deskd.gateway.registry import Registry
from deskd.gateway.transport import GatewayTransport, _Endpoint
from deskd.gateway.wire import MAX_FRAME_BYTES, MAX_RESPONSE_BYTES, encode_frame

MANIFEST = "d" * 64
pytestmark = pytest.mark.skipif(
    not hasattr(socket, "SO_PEERCRED"), reason="Linux UDS required"
)


class Peer:
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(3)
        self.socket.connect(str(path))
        self.stream = self.socket.makefile("rb")
        self.received_sizes = []
        self.channel = self.rpc("hello", {})["result"]["connection_id"]

    def rpc(self, method, params):
        request_id = uuid.uuid4().hex
        self.socket.sendall(
            encode_frame({"id": request_id, "method": method, "params": params})
        )
        frame = self.stream.readline(MAX_RESPONSE_BYTES + 2)
        assert frame.endswith(b"\n") and len(frame) <= MAX_RESPONSE_BYTES + 1
        self.received_sizes.append(len(frame))
        response = json.loads(frame)
        assert response["id"] == request_id
        return response

    def close(self):
        self.stream.close()
        self.socket.close()


@pytest.fixture
def workflow(monkeypatch):
    path = Path(tempfile.mkdtemp(prefix="dg-"))
    for name, mode in [("b", 0o750), ("a", 0o700)]:
        (path / name).mkdir(mode=mode)
        os.chmod(path / name, mode)
    # Existing workspace ancestors are 0775; leave them unchanged. Production
    # rejects them. Only this freshly created test subtree uses real validation.
    check = _Endpoint._check_ancestor
    monkeypatch.setattr(
        _Endpoint,
        "_check_ancestor",
        staticmethod(
            lambda candidate: (
                check(candidate)
                if candidate == path or path in candidate.parents
                else None
            )
        ),
    )
    registry = Registry(
        path / "g.db", harness_uid=os.getuid(), actions=WORKFLOW_ACTIONS
    )
    events = GatewayEventStore(registry.db_path)
    memos = MemoWorkflow(registry.db_path)
    commands = GatewayCommands(registry, events, handlers=memos.handlers())
    server = GatewayTransport(
        registry,
        commands,
        business_path=path / "b/s",
        admin_path=path / "a/s",
        business_gid=os.getgid(),
        admin_uids=frozenset({os.getuid() + 10000}),
        activation_check=lambda: None,
    )._start_listeners()
    peers = [Peer(path / "b/s"), Peer(path / "b/s")]
    try:
        for seat in ("operator", "reviewer"):
            server._admin_call(
                "bind",
                {
                    "desk_id": "demo",
                    "seat_id": seat,
                    "root_session_id": "root-" + seat,
                    "manifest_hash": MANIFEST,
                    "capabilities": list(WORKFLOW_ACTIONS),
                    "expected_binding_generation": 0,
                },
            )
        server._admin_call("activate", {})
        yield server, peers, path
    finally:
        for peer in peers:
            peer.close()
        server.close()
        shutil.rmtree(path)


def lease(server, peer, seat):
    return server._admin_call(
        "lease",
        {
            "connection_id": peer.channel,
            "desk_id": "demo",
            "seat_id": seat,
            "root_session_id": "root-" + seat,
            "binding_generation": 1,
            "manifest_hash": MANIFEST,
            "ttl_seconds": 30,
        },
    )


def tool(peer, seat, name, arguments, request_id, *, root=None, thread=None):
    root = root or "root-" + seat
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "synthetic-harness", "version": "test"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": {"request_id": request_id, **arguments},
                "_meta": {"sessionId": root, "threadId": thread or root},
            },
        },
    ]
    source = io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in messages))
    target = io.BytesIO()
    _serve_stdio(source, target, peer.rpc, tool_catalog())
    output = target.getvalue().splitlines()
    response = json.loads(output[-1])["result"]
    return (
        response["isError"],
        json.loads(response["content"][0]["text"]),
        len(output[-1]),
    )


@pytest.mark.parametrize(
    "body", ["Synthetic memo", "\\" * 65536], ids=["small", "max-escaped"]
)
def test_real_uds_approval_execution_and_mcp_output(workflow, body):
    server, (operator, reviewer), path = workflow
    accepted = server._admin_call("connections", {})
    assert {row["connection_id"] for row in accepted} == {
        operator.channel,
        reviewer.channel,
    }
    assert all(
        row["pid"] == os.getpid() and row["uid"] == os.getuid() for row in accepted
    )
    proposed_args = {"executor_principal": "demo/operator", "body": body}
    error, result, _ = tool(
        operator, "operator", "proposal.create", proposed_args, "proposal-1"
    )
    assert error and result["error"]["code"] == "inactive_channel"
    for peer, seat in ((operator, "operator"), (reviewer, "reviewer")):
        lease(server, peer, seat)
    error, result, _ = tool(
        operator,
        "operator",
        "proposal.create",
        proposed_args,
        "forged-root",
        root="root-reviewer",
    )
    assert error and result["error"]["code"] == "binding_mismatch"
    error, proposed, stdio_size = tool(
        operator, "operator", "proposal.create", proposed_args, "proposal-1"
    )
    assert not error
    proposal = proposed["result"]
    approve_args = {
        "proposal_id": proposal["proposal_id"],
        "body_sha256": proposal["body_sha256"],
        "ttl_seconds": 30,
    }
    error, result, _ = tool(
        operator, "operator", "approval.issue", approve_args, "self-approve"
    )
    assert error and result["error"]["code"] == "independent_approver_required"
    error, result, _ = tool(
        reviewer,
        "reviewer",
        "approval.issue",
        approve_args,
        "child-approve",
        thread="child-reviewer",
    )
    assert error and result["error"]["code"] == "root_required"
    error, approved, _ = tool(
        reviewer, "reviewer", "approval.issue", approve_args, "approval-1"
    )
    assert not error and approved["result"]["issuer_principal"] == "demo/reviewer"
    execute_args = {"approval_id": approved["result"]["approval_id"]}
    error, published, _ = tool(
        operator, "operator", "action.execute", execute_args, "execution-1"
    )
    assert not error and published["result"]["body"] == body
    error, replay, _ = tool(
        operator, "operator", "action.execute", execute_args, "execution-1"
    )
    assert not error and replay == published
    with sqlite3.connect(path / "g.db") as conn:
        assert conn.execute("SELECT count(*) FROM memo_published").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM events_outbox").fetchone()[0] == 3
        assert (
            conn.execute("SELECT status FROM memo_approvals").fetchone()[0]
            == "consumed"
        )
    if len(body) == 65536:
        assert max(operator.received_sizes) > MAX_FRAME_BYTES
        assert stdio_size > max(operator.received_sizes)


def test_eof_channel_cannot_be_leased_again_or_reused(workflow):
    server, (operator, reviewer), path = workflow
    lease(server, operator, "operator")
    operator.close()
    deadline = time.monotonic() + 2
    while any(
        row["connection_id"] == operator.channel
        for row in server._admin_call("connections", {})
    ):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    with pytest.raises(IdentityError, match="unknown_live_connection"):
        lease(server, operator, "operator")
    replacement = Peer(path / "b/s")
    try:
        assert replacement.channel not in {operator.channel, reviewer.channel}
        error, result, _ = tool(
            replacement,
            "operator",
            "proposal.create",
            {"executor_principal": "demo/operator", "body": "Synthetic replacement"},
            "replacement-1",
        )
        assert error and result["error"]["code"] == "inactive_channel"
    finally:
        replacement.close()
