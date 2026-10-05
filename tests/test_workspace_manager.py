"""Supervisor lifecycle mocks plus owned local subprocess/file-lock checks.

Actual UID dropping and official daemon restart run only in disposable Linux CI.
These tests never inspect or stop unrelated processes and use explicit fixtures.
"""

import fcntl
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from deskd.workspace import manager as module
from deskd.workspace.manager import WorkspaceManager, _Endpoint, _stop_owned_process
from deskd.workspace.store import WorkspaceError


class Deployment:
    def __init__(self, path):
        self.path = Path(path)
        self.prefix = self.path.parent.parent
        self.installation = SimpleNamespace(
            harness_uid=27002, gateway_uid=27001, business_gid=27003
        )
        self.admin_path = self.prefix / "admin/s"
        self.business_path = self.prefix / "business/s"
        self.roots_path = self.prefix / "policy/roots.json"
        self.value = {
            "mock_port": 12345,
            "python": {"path": str(Path("/usr/bin/python3").resolve())},
        }
        self.plan = {
            "launch": {
                "argv": [
                    str(self.prefix / "bin/codex"),
                    "app-server",
                    "--listen",
                    "unix://",
                    "--managed-daemon",
                    "--strict-config",
                ],
                "environment": {
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(self.prefix / "home"),
                    "CODEX_HOME": str(self.prefix / "harness"),
                    "TMPDIR": str(self.prefix / "tmp"),
                    "LANG": "C.UTF-8",
                },
            }
        }
        self.trace = []

    def attest(self):
        self.trace.append("attest")

    def seats(self):
        self.trace.append("existing-roots")
        return [SimpleNamespace(principal="demo/analyst")]

    def admin(self, method, params=None):
        self.trace.append(method)
        if method == "workspace.bindings":
            return {"ok": True, "result": [{"principal": "demo/analyst"}]}
        if method == "workspace.status":
            return {"ok": True, "result": {"seats": [{"principal": "demo/analyst"}]}}
        return {"ok": True, "result": {"fenced": True}}


class Process:
    next_pid = 123450

    def __init__(self, args, **kwargs):
        self.pid = Process.next_pid
        Process.next_pid += 1
        self.returncode = None
        self.args = args
        self.kwargs = kwargs

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = self.returncode or 0
        return self.returncode


@pytest.fixture
def manager(tmp_path, monkeypatch):
    policy = tmp_path / "policy"
    policy.mkdir()
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module, "Deployment", Deployment)
    monkeypatch.setattr(module.subprocess, "Popen", Process)
    monkeypatch.setattr(
        module, "_has_exited", lambda child: child.returncode is not None
    )
    observed = []
    value = WorkspaceManager(
        policy / "deployment.json", on_event=observed.append, backoff_seconds=0
    )
    value.test_events = observed
    value.test_stops = []
    monkeypatch.setattr(value, "_lock", lambda: None)
    monkeypatch.setattr(value, "_check_empty_endpoints", lambda: None)
    monkeypatch.setattr(
        value,
        "_wait",
        lambda name, ready: value.deployment.trace.append("ready-" + name),
    )

    def stop(process):
        value.test_stops.append(process.pid)
        process.returncode = -15

    monkeypatch.setattr(module, "_stop_owned_process", stop)
    yield value
    value.close()


def test_fixed_launch_environment_privileges_and_fence_before_daemon(
    manager, monkeypatch
):
    monkeypatch.setenv("SYNTHETIC_PRIVATE_ENV", "fixture-only")
    manager.start()
    assert list(manager.children) == ["gateway", "daemon", "controller"]
    gateway, daemon, controller = manager.children.values()
    assert gateway.kwargs["user"] == 27001
    assert daemon.kwargs["user"] == 27002
    assert controller.kwargs["user"] == 0
    for process in manager.children.values():
        assert "SYNTHETIC_PRIVATE_ENV" not in process.kwargs["env"]
        assert process.kwargs["start_new_session"] is True
        assert process.kwargs["close_fds"] is True
        assert process.kwargs["umask"] == 0o077
        assert process.kwargs["stdin"] == subprocess.DEVNULL
        assert process.kwargs["stderr"] == subprocess.DEVNULL
        assert process.kwargs["stdout"] == subprocess.DEVNULL
    assert gateway.args[1:4] == ["-I", "-B", "-c"]
    assert gateway.args[5] == str(manager.deployment.prefix / "lib")
    assert controller.args[-1] == str(daemon.pid)
    assert "--managed-daemon" in daemon.args
    trace = manager.deployment.trace
    assert trace.index("fence") < trace.index("ready-daemon")
    assert manager.state == "running"


def test_owned_child_death_fences_stops_then_restarts_same_roots(manager):
    manager.start()
    old = manager.pids
    manager.children["daemon"].returncode = -9
    status = manager.tick()
    assert status["state"] == "running"
    assert status["restarts"] == 1
    assert manager.test_stops == [old["controller"], old["daemon"], old["gateway"]]
    assert "existing-roots" in manager.deployment.trace
    assert all(manager.pids[key] != old[key] for key in old)
    assert sum(event.get("child") == "bootstrap" for event in manager.test_events) == 1


def test_restart_limit_closes_owned_children_and_fails_closed(manager):
    manager.max_restarts = 0
    manager.start()
    manager.children["controller"].returncode = 1
    with pytest.raises(WorkspaceError, match="managed_restart_limit_reached"):
        manager.tick()
    assert manager.children == {}
    assert manager.state == "stopped"
    assert any(
        event["state"] == "restart_limit_reached" for event in manager.test_events
    )


def test_existing_roots_skip_bootstrap_even_on_explicit_up(manager):
    manager.deployment.roots_path.write_text("synthetic protected record stand-in")
    manager.start(bootstrap=True)
    assert "existing-roots" in manager.deployment.trace
    assert not any(event.get("child") == "bootstrap" for event in manager.test_events)


def test_explicit_up_repairs_incomplete_registration_but_restart_does_not(manager):
    manager.deployment.roots_path.write_text("synthetic protected record stand-in")
    original = manager.deployment.admin

    def partial(method, params=None):
        if method == "workspace.status":
            return {"ok": True, "result": {"seats": []}}
        return original(method, params)

    manager.deployment.admin = partial
    manager.start(bootstrap=True)
    assert sum(event.get("child") == "bootstrap" for event in manager.test_events) == 1
    manager.children["daemon"].returncode = -9
    manager.tick()
    assert sum(event.get("child") == "bootstrap" for event in manager.test_events) == 1


def test_start_failure_stops_only_spawned_children(manager, monkeypatch):
    def fail(name, ready):
        if name == "daemon":
            raise WorkspaceError("synthetic_start_failure")

    monkeypatch.setattr(manager, "_wait", fail)
    with pytest.raises(WorkspaceError, match="synthetic_start_failure"):
        manager.start()
    assert len(manager.test_stops) == 2
    assert manager.children == {}
    assert manager.state == "stopped"


def test_callback_failure_cannot_disable_supervision(manager):
    def broken_callback(event):
        raise ValueError("synthetic display failure")

    manager.on_event = broken_callback
    manager.start()
    assert manager.tick()["state"] == "running"
    manager.close()
    assert manager.children == {}


def test_cleanup_still_attempts_all_owned_children_after_stop_failure(
    manager, monkeypatch
):
    manager.start()
    pids = manager.pids
    attempted = []

    def failing_stop(child):
        attempted.append(child.pid)
        if child.pid == pids["controller"]:
            raise subprocess.TimeoutExpired("synthetic", 1)
        child.returncode = 0

    monkeypatch.setattr(module, "_stop_owned_process", failing_stop)
    with pytest.raises(WorkspaceError, match="managed_child_stop_incomplete"):
        manager.close()
    assert attempted == [pids["controller"], pids["daemon"], pids["gateway"]]
    assert manager.state == "stop_incomplete"


def test_first_start_refuses_preexisting_socket_metadata_without_removing_it(manager):
    path = manager.deployment.admin_path
    path.parent.mkdir()
    path.write_text("fixture predecessor marker")
    with pytest.raises(
        WorkspaceError, match="preexisting_service_endpoint_requires_management"
    ):
        WorkspaceManager._check_empty_endpoints(manager)
    assert path.read_text() == "fixture predecessor marker"


def test_real_exclusive_flock_and_no_symlink_lock_adoption(manager, monkeypatch):
    original = os.fstat

    def root_fixture(fd):
        info = original(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_nlink=info.st_nlink)

    monkeypatch.setattr(module.os, "fstat", root_fixture)
    WorkspaceManager._lock(manager)
    other = WorkspaceManager(manager.deployment.path)
    with pytest.raises(WorkspaceError, match="workspace_already_managed"):
        other._lock()
    manager._unlock()
    other._lock()
    other._unlock()
    lock = manager.deployment.prefix / "policy/manager.lock"
    lock.unlink()
    target = lock.parent / "target"
    target.write_text("preserve")
    lock.symlink_to(target)
    with pytest.raises(OSError):
        WorkspaceManager._lock(manager)
    assert target.read_text() == "preserve"


def socket_fixture(path):
    # /proc/self/fd avoids Linux's short sockaddr_un pathname limit while all
    # fixtures remain in the selected scratchpad pytest temporary directory.
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(f"/proc/self/fd/{fd}/{path.name}")
    finally:
        os.close(fd)
    path.chmod(0o600)
    return sock


def test_endpoint_cleanup_requires_exact_owned_inode_and_no_alias(tmp_path):
    path = tmp_path / "s"
    first = socket_fixture(path)
    try:
        endpoint = _Endpoint.capture(path, uid=os.getuid(), mode=0o600)
        alias = tmp_path / "alias"
        os.link(path, alias)
        with pytest.raises(WorkspaceError, match="managed_endpoint_changed"):
            endpoint.remove()
        alias.unlink()
        path.unlink()
        second = socket_fixture(path)
        try:
            with pytest.raises(WorkspaceError, match="managed_endpoint_changed"):
                endpoint.remove()
            assert path.exists()
        finally:
            second.close()
    finally:
        first.close()


def test_owned_daemon_lock_must_be_free_before_removal(tmp_path):
    path = tmp_path / "daemon.lock"
    path.touch(mode=0o600)
    record = _Endpoint.capture(path, uid=os.getuid(), mode=0o600, regular=True)
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(WorkspaceError, match="managed_daemon_lock_busy"):
            record.lock()
        assert path.is_file()
    finally:
        os.close(fd)
    owned = record.lock()
    try:
        record.remove()
        assert not path.exists()
    finally:
        os.close(owned)


def test_owned_subprocess_group_stops_without_using_process_discovery(tmp_path):
    child = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        _stop_owned_process(child, grace=2)
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)


def test_reaped_child_group_is_never_signaled(tmp_path, monkeypatch):
    child = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "pass"],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    child.wait(timeout=5)
    signaled = []
    monkeypatch.setattr(
        module.os, "killpg", lambda pid, sig: signaled.append((pid, sig))
    )
    with pytest.raises(WorkspaceError, match="managed_child_already_reaped"):
        _stop_owned_process(child, grace=1)
    assert signaled == []


def test_nonroot_manager_refuses_before_reading_deployment(monkeypatch):
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    with pytest.raises(WorkspaceError, match="independent_administrator_required"):
        WorkspaceManager("/synthetic/nonexistent/deployment.json")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_restarts": -1},
        {"max_restarts": True},
        {"backoff_seconds": float("nan")},
        {"startup_timeout": 0},
    ],
)
def test_manager_bounds_are_explicit(kwargs):
    with pytest.raises(WorkspaceError):
        WorkspaceManager("/synthetic/nonexistent/deployment.json", **kwargs)
