"""Opt-in Linux UID acceptance, using only synthetic local memo data.

Run only on a disposable root-capable runner with DESKD_UID_TEST_ROOT pointing
at an existing root-owned scratchpad. No users, groups, services, credentials,
network clients, or machine configuration are created or inspected. This checks
real UID/DAC and peer authentication, NOT a role shell sandbox, official Codex,
protected runtime attestation, or readiness for credentials.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import sqlite3
import stat
import sys
import tempfile
import time
import uuid

import pytest

from deskd.gateway.actions import MemoWorkflow, WORKFLOW_ACTIONS
from deskd.gateway.bridge import run_bridge
from deskd.gateway.commands import GatewayCommands
from deskd.gateway.events import GatewayEventStore
from deskd.gateway.registry import Registry
from deskd.gateway.transport import GatewayTransport

GATEWAY_UID = 26001
HARNESS_UID = 26002
BUSINESS_GID = 26003
THIRD_UID = 26004
MANIFEST = "d" * 64
IO_TIMEOUT = 5
FRAME_LIMIT = 64 * 1024


class JsonPipe:
    """Small synthetic JSON frames with deadlines on both reading and writing."""

    def __init__(self, read_fd, write_fd):
        self.read_fd = read_fd
        self.write_fd = write_fd
        self.pending = b""
        for fd in {read_fd, write_fd}:
            os.set_blocking(fd, False)

    def send(self, value):
        payload = json.dumps(value, separators=(",", ":")).encode() + b"\n"
        assert len(payload) <= FRAME_LIMIT
        deadline = time.monotonic() + IO_TIMEOUT
        while payload:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "synthetic channel write timed out"
            assert select.select([], [self.write_fd], [], remaining)[1]
            try:
                payload = payload[os.write(self.write_fd, payload) :]
            except BlockingIOError:
                continue

    def receive(self):
        deadline = time.monotonic() + IO_TIMEOUT
        while b"\n" not in self.pending:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "synthetic channel read timed out"
            assert select.select([self.read_fd], [], [], remaining)[0]
            try:
                chunk = os.read(self.read_fd, 4096)
            except BlockingIOError:
                continue
            assert chunk, "synthetic child or channel closed prematurely"
            self.pending += chunk
            assert len(self.pending) <= FRAME_LIMIT
        line, self.pending = self.pending.split(b"\n", 1)
        return json.loads(line)


class Processes:
    """Own only explicitly forked children and descriptors created by this test."""

    def __init__(self):
        self.children = set()
        self.fds = set()

    def pipe(self):
        pair = os.pipe()
        self.fds.update(pair)
        return pair

    def close_fd(self, fd):
        if fd in self.fds:
            self.fds.remove(fd)
            os.close(fd)

    def fork(self, uid, groups, keep, body):
        pid = os.fork()
        if pid:
            self.children.add(pid)
            return pid
        # No inherited parent control endpoints may keep another child alive.
        for fd in self.fds - set(keep):
            os.close(fd)
        try:
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            signal.alarm(60)
            os.setgroups(groups)
            os.setgid(uid)
            os.setuid(uid)
            assert os.getresuid() == (uid, uid, uid)
            assert os.getresgid() == (uid, uid, uid)
            assert set(os.getgroups()) == set(groups)
            body()
        except BaseException:
            # Never print inherited state, exception locals, or process environment.
            os._exit(1)
        os._exit(0)

    def wait(self, pid, timeout=IO_TIMEOUT):
        deadline = time.monotonic() + timeout
        while True:
            found, status = os.waitpid(pid, os.WNOHANG)
            if found:
                self.children.remove(pid)
                return os.waitstatus_to_exitcode(status)
            assert time.monotonic() < deadline, "owned child failed to exit"
            time.sleep(0.01)

    def cleanup(self):
        for fd in tuple(self.fds):
            self.close_fd(fd)
        # Poll before every signal. Unreaped children cannot have recycled PIDs.
        for sig in (None, signal.SIGTERM, signal.SIGKILL):
            for pid in tuple(self.children):
                found, _ = os.waitpid(pid, os.WNOHANG)
                if found:
                    self.children.remove(pid)
                elif sig is not None:
                    os.kill(pid, sig)
            deadline = time.monotonic() + 1
            while self.children and time.monotonic() < deadline:
                for pid in tuple(self.children):
                    found, _ = os.waitpid(pid, os.WNOHANG)
                    if found:
                        self.children.remove(pid)
                if self.children:
                    time.sleep(0.01)
        assert not self.children, "owned child survived bounded cleanup"


@pytest.fixture
def isolated_uid_tree():
    supplied = os.environ.get("DESKD_UID_TEST_ROOT")
    if not supplied:
        pytest.skip("opt-in disposable Linux root runner required")
    if sys.platform != "linux" or not hasattr(os, "fork"):
        pytest.fail("configured UID acceptance requires Linux fork")
    if os.geteuid() != 0:
        pytest.skip("UID acceptance requires an explicitly provisioned root runner")
    root = Path(supplied)
    assert root.is_absolute() and ".." not in root.parts
    assert root.name == "scratchpad", "UID artifacts must stay in scratchpad"
    for ancestor in (*reversed(root.parents), root):
        info = ancestor.lstat()
        assert stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
        assert info.st_uid == 0 and not info.st_mode & 0o022
    tree = Path(tempfile.mkdtemp(prefix="dg-", dir=root))
    processes = Processes()
    try:
        os.chmod(tree, 0o755)
        for name, group, mode in (
            ("b", BUSINESS_GID, 0o750),
            ("a", GATEWAY_UID, 0o700),
            ("g", GATEWAY_UID, 0o700),
        ):
            directory = tree / name
            directory.mkdir(mode=mode)
            os.chown(directory, GATEWAY_UID, group)
            os.chmod(directory, mode)
        assert len(os.fsencode(tree / "b/s")) <= 107, "UDS test path too long"
        secret = tree / "synthetic-secret"
        fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, b"synthetic fixture only; no real credential\n")
            os.fchown(fd, GATEWAY_UID, GATEWAY_UID)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        yield tree, processes
    finally:
        processes.cleanup()
        # Only this mkdtemp-created child is removed, never the supplied root.
        shutil.rmtree(tree)


def start_gateway(tree, processes):
    stop_read, stop_write = processes.pipe()
    ready_read, ready_write = processes.pipe()

    def serve():
        os.umask(0o077)
        registry = Registry(
            tree / "g/state.db", harness_uid=HARNESS_UID, actions=WORKFLOW_ACTIONS
        )
        events = GatewayEventStore(registry.db_path)
        memos = MemoWorkflow(registry.db_path)
        commands = GatewayCommands(registry, events, handlers=memos.handlers())
        server = GatewayTransport(
            registry,
            commands,
            business_path=tree / "b/s",
            admin_path=tree / "a/s",
            business_gid=BUSINESS_GID,
            # This fixture enables only local synthetic memo effects. This no-op
            # is explicitly NOT protected-runtime or sandbox attestation.
            activation_check=lambda: None,
            idle_timeout=45,
            frame_timeout=IO_TIMEOUT,
        ).start()
        try:
            JsonPipe(ready_write, ready_write).send({"ready": True})
            assert select.select([stop_read], [], [], 50)[0]
            assert os.read(stop_read, 1) == b""
        finally:
            server.close()

    pid = processes.fork(GATEWAY_UID, [BUSINESS_GID], {stop_read, ready_write}, serve)
    processes.close_fd(stop_read)
    processes.close_fd(ready_write)
    assert JsonPipe(ready_read, ready_read).receive() == {"ready": True}
    processes.close_fd(ready_read)
    return pid, stop_write


def check_dac(tree, processes, uid, groups, *, secret=False):
    read_fd, write_fd = processes.pipe()

    def probe():
        if secret:
            try:
                descriptor = os.open(tree / "synthetic-secret", os.O_RDONLY)
            except PermissionError as exc:
                assert exc.errno in {errno.EACCES, errno.EPERM}
            else:
                os.close(descriptor)
                raise AssertionError("harness read gateway-only synthetic file")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(IO_TIMEOUT)
            endpoint = tree / ("a/s" if secret else "b/s")
            try:
                peer.connect(str(endpoint))
            except PermissionError as exc:
                assert exc.errno in {errno.EACCES, errno.EPERM}
            else:
                raise AssertionError("unexpected socket access")
        JsonPipe(write_fd, write_fd).send({"denied": True})

    pid = processes.fork(uid, groups, {write_fd}, probe)
    processes.close_fd(write_fd)
    assert JsonPipe(read_fd, read_fd).receive() == {"denied": True}
    processes.close_fd(read_fd)
    assert processes.wait(pid) == 0


def check_untrusted_peer_uid(tree, processes):
    read_fd, write_fd = processes.pipe()

    def probe():
        # Deliberately grant the socket's group: DAC permits the connection,
        # while the gateway must independently reject the actual kernel UID.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(IO_TIMEOUT)
            peer.connect(str(tree / "b/s"))
            reply = JsonPipe(peer.fileno(), peer.fileno()).receive()
            assert reply == {
                "id": None,
                "ok": False,
                "error": {"code": "untrusted_peer"},
            }
        JsonPipe(write_fd, write_fd).send({"peer_uid_rejected": True})

    pid = processes.fork(THIRD_UID, [BUSINESS_GID], {write_fd}, probe)
    processes.close_fd(write_fd)
    assert JsonPipe(read_fd, read_fd).receive() == {"peer_uid_rejected": True}
    processes.close_fd(read_fd)
    assert processes.wait(pid) == 0


def start_bridge(tree, processes):
    source_read, source_write = processes.pipe()
    target_read, target_write = processes.pipe()

    def bridge():
        with os.fdopen(source_read, "rb", buffering=0) as source:
            with os.fdopen(target_write, "wb", buffering=0) as target:
                run_bridge(
                    tree / "b/s", gateway_uid=GATEWAY_UID, source=source, target=target
                )

    pid = processes.fork(
        HARNESS_UID, [BUSINESS_GID], {source_read, target_write}, bridge
    )
    processes.close_fd(source_read)
    processes.close_fd(target_write)
    channel = JsonPipe(target_read, source_write)
    channel.send(
        {
            "jsonrpc": "2.0",
            "id": "initialize",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "synthetic-harness", "version": "test"},
            },
        }
    )
    response = channel.receive()
    assert response["id"] == "initialize" and "result" in response
    channel.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return pid, channel


def admin_call(channel, method, params):
    request_id = uuid.uuid4().hex
    channel.send({"id": request_id, "method": method, "params": params})
    reply = channel.receive()
    assert reply["id"] == request_id and reply["ok"] is True, reply
    return reply["result"]


def call_tool(channel, seat, name, arguments, request_id, *, root=None):
    channel.send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": {"request_id": request_id, **arguments},
                "_meta": {
                    "sessionId": root or "root-" + seat,
                    "threadId": root or "root-" + seat,
                },
            },
        }
    )
    reply = channel.receive()
    assert reply["id"] == request_id and "result" in reply, reply
    result = reply["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


def test_public_transport_bridge_and_distinct_linux_uids(isolated_uid_tree):
    tree, processes = isolated_uid_tree
    gateway_pid, stop_fd = start_gateway(tree, processes)
    check_dac(tree, processes, HARNESS_UID, [BUSINESS_GID], secret=True)
    check_dac(tree, processes, THIRD_UID, [])
    check_untrusted_peer_uid(tree, processes)
    operator_pid, operator = start_bridge(tree, processes)
    reviewer_pid, reviewer = start_bridge(tree, processes)
    assert len({gateway_pid, operator_pid, reviewer_pid}) == 3

    # Open management only after forks so children cannot inherit root authority.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as manager:
        manager.settimeout(IO_TIMEOUT)
        manager.connect(str(tree / "a/s"))
        admin = JsonPipe(manager.fileno(), manager.fileno())
        accepted = admin_call(admin, "connections", {})
        assert {row["pid"] for row in accepted} == {operator_pid, reviewer_pid}
        assert all(row["uid"] == HARNESS_UID for row in accepted)
        assert all(row["gid"] == HARNESS_UID for row in accepted)
        by_pid = {row["pid"]: row for row in accepted}
        assert len({row["connection_id"] for row in accepted}) == 2
        assert len({row["service_generation"] for row in accepted}) == 1
        for seat, pid in (("operator", operator_pid), ("reviewer", reviewer_pid)):
            admin_call(
                admin,
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
        admin_call(admin, "activate", {})
        proposal_args = {
            "executor_principal": "demo/operator",
            "body": "Synthetic UID acceptance memo",
        }
        error, result = call_tool(
            operator, "operator", "proposal.create", proposal_args, "before-lease"
        )
        assert error and result["error"]["code"] == "inactive_channel"
        for seat, pid in (("operator", operator_pid), ("reviewer", reviewer_pid)):
            admin_call(
                admin,
                "lease",
                {
                    "desk_id": "demo",
                    "seat_id": seat,
                    "connection_id": by_pid[pid]["connection_id"],
                    "root_session_id": "root-" + seat,
                    "binding_generation": 1,
                    "manifest_hash": MANIFEST,
                    "ttl_seconds": 30,
                },
            )
        error, result = call_tool(
            operator,
            "operator",
            "proposal.create",
            proposal_args,
            "forged-root",
            root="root-reviewer",
        )
        assert error and result["error"]["code"] == "binding_mismatch"
        error, proposed = call_tool(
            operator, "operator", "proposal.create", proposal_args, "proposal-1"
        )
        assert not error
        approval_args = {
            "proposal_id": proposed["result"]["proposal_id"],
            "body_sha256": proposed["result"]["body_sha256"],
            "ttl_seconds": 30,
        }
        error, result = call_tool(
            operator, "operator", "approval.issue", approval_args, "self-approve"
        )
        assert error and result["error"]["code"] == "independent_approver_required"
        error, approved = call_tool(
            reviewer, "reviewer", "approval.issue", approval_args, "approval-1"
        )
        assert not error and approved["result"]["issuer_principal"] == "demo/reviewer"
        execution_args = {"approval_id": approved["result"]["approval_id"]}
        error, published = call_tool(
            operator, "operator", "action.execute", execution_args, "execution-1"
        )
        assert not error and published["result"]["body"] == proposal_args["body"]
        error, replay = call_tool(
            operator, "operator", "action.execute", execution_args, "execution-1"
        )
        assert not error and replay == published
        # A different request ID must not turn a consumed grant into a second effect.
        error, result = call_tool(
            operator, "operator", "action.execute", execution_args, "execution-2"
        )
        assert error and result["error"]["code"] == "approval_not_active"
        admin_call(admin, "fence", {})
        error, result = call_tool(
            operator, "operator", "action.execute", execution_args, "execution-1"
        )
        assert error and result["error"]["code"] == "service_fenced"
    for pid, bridge in ((operator_pid, operator), (reviewer_pid, reviewer)):
        processes.close_fd(bridge.write_fd)
        assert processes.wait(pid) == 0
        processes.close_fd(bridge.read_fd)
    processes.close_fd(stop_fd)
    assert processes.wait(gateway_pid) == 0
    assert not (tree / "b/s").exists() and not (tree / "a/s").exists()
    # Synthetic DB inspection only after all owned service processes have stopped.
    with sqlite3.connect(tree / "g/state.db") as conn:
        assert conn.execute("SELECT count(*) FROM memo_published").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM events_outbox").fetchone()[0] == 3
        assert (
            conn.execute("SELECT status FROM memo_approvals").fetchone()[0]
            == "consumed"
        )
