"""Supervise only this explicitly installed workspace and our own child groups.

The privileged manager is administrator code, never a model tool. It discovers
no processes, reads no credentials, creates no users and stops no pre-existing
services. A fresh manager refuses predecessor endpoints it cannot prove it owns.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import subprocess
import time
from typing import Callable

from .deployment import Deployment
from .store import WorkspaceError


_PYTHON_ENTRY = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from deskd.workspace.deployment import run_installed; "
    "run_installed(sys.argv[2], sys.argv[3], "
    "daemon_pid=int(sys.argv[4]) if len(sys.argv)>4 else None)"
)


@dataclass(frozen=True)
class _Endpoint:
    path: Path
    device: int
    inode: int
    parent_device: int
    parent_inode: int
    uid: int
    mode: int
    link_target: str | None = None
    regular: bool = False

    @classmethod
    def capture(
        cls, path: Path, *, uid: int, mode: int, link_target=None, regular=False
    ):
        info = path.lstat()
        parent = path.parent.stat()
        expected_type = (
            stat.S_ISREG
            if regular
            else stat.S_ISLNK
            if link_target is not None
            else stat.S_ISSOCK
        )
        if (
            not expected_type(info.st_mode)
            or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != mode
            or info.st_nlink != 1
            or (link_target is not None and os.readlink(path) != link_target)
        ):
            raise WorkspaceError("unsafe_managed_endpoint")
        return cls(
            path,
            info.st_dev,
            info.st_ino,
            parent.st_dev,
            parent.st_ino,
            uid,
            mode,
            link_target,
            regular,
        )

    def remove(self):
        """Only after the manager has stopped its corresponding owned process."""
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        parent = self.path.parent.stat()
        expected_type = (
            stat.S_ISREG
            if self.regular
            else stat.S_ISLNK
            if self.link_target is not None
            else stat.S_ISSOCK
        )
        if (
            (info.st_dev, info.st_ino) != (self.device, self.inode)
            or (parent.st_dev, parent.st_ino) != (self.parent_device, self.parent_inode)
            or not expected_type(info.st_mode)
            or info.st_uid != self.uid
            or stat.S_IMODE(info.st_mode) != self.mode
            or info.st_nlink != 1
            or (
                self.link_target is not None
                and os.readlink(self.path) != self.link_target
            )
        ):
            raise WorkspaceError("managed_endpoint_changed")
        self.path.unlink()

    def lock(self):
        if not self.regular:
            raise WorkspaceError("invalid_managed_lock")
        fd = os.open(self.path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (
                (info.st_dev, info.st_ino) != (self.device, self.inode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != self.uid
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != self.mode
            ):
                raise WorkspaceError("managed_daemon_lock_changed")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkspaceError("managed_daemon_lock_busy") from exc
            return fd
        except BaseException:
            os.close(fd)
            raise


def _peer(path: Path, uid: int, pid: int):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        sock.connect(str(path))
        observed_pid, observed_uid, _ = struct.unpack(
            "3i",
            sock.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            ),
        )
        if (observed_uid, observed_pid) != (uid, pid):
            raise WorkspaceError("managed_service_peer_mismatch")


def _has_exited(process: subprocess.Popen) -> bool:
    """Observe without reaping: the leader PID must remain reserved for cleanup."""
    if process.returncode is not None:
        raise WorkspaceError("managed_child_already_reaped")
    try:
        return (
            os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            is not None
        )
    except ChildProcessError as exc:
        raise WorkspaceError("managed_child_ownership_lost") from exc


def _stop_owned_process(process: subprocess.Popen, *, grace: float = 5):
    """Only Popen groups created by this manager are ever passed here.

    Keep the leader alive or unreaped until ALL group signals have been sent.
    Its reserved PID cannot be reused for an unrelated group during cleanup.
    No group identity is adopted from a PID file or a previous manager instance.
    """
    _has_exited(process)
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace
    while not _has_exited(process) and time.monotonic() < deadline:
        time.sleep(0.05)
    # The unreaped leader still reserves the PID even if it has already died.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=grace)


class WorkspaceManager:
    """A bounded supervisor with an exclusive lock per protected installation.

    Initial `start(bootstrap=True)` is an explicit management action. Automatic
    recovery never bootstraps, changes a root, or resolves UNKNOWN deliveries.
    Closing stops only children created by this manager, in controller/daemon/
    gateway order. No plaintext process output enters status or event callbacks.
    """

    def __init__(
        self,
        deployment_path: str | Path,
        *,
        on_event: Callable[[dict], None] | None = None,
        max_restarts: int = 3,
        backoff_seconds: float = 0.5,
        startup_timeout: float = 30,
    ):
        if type(max_restarts) is not int or not 0 <= max_restarts <= 10:
            raise WorkspaceError("invalid_restart_limit")
        if type(backoff_seconds) not in (int, float) or not 0 <= backoff_seconds <= 10:
            raise WorkspaceError("invalid_restart_backoff")
        if type(startup_timeout) not in (int, float) or not 1 <= startup_timeout <= 60:
            raise WorkspaceError("invalid_startup_timeout")
        if os.geteuid() != 0:
            raise WorkspaceError("independent_administrator_required")
        self.deployment = Deployment(deployment_path)
        self.max_restarts = max_restarts
        self.backoff_seconds = float(backoff_seconds)
        self.startup_timeout = float(startup_timeout)
        self.on_event = on_event or (lambda _: None)
        self.children: dict[str, subprocess.Popen] = {}
        self._lock_fd: int | None = None
        self._gateway_endpoints: list[_Endpoint] = []
        self._daemon_endpoints: list[_Endpoint] = []
        self._daemon_directory: tuple[Path, int, int] | None = None
        self._daemon_lock: _Endpoint | None = None
        self.restarts = 0
        self.state = "stopped"
        self._started = False
        self._closed = False

    @property
    def pids(self) -> dict[str, int]:
        return {name: child.pid for name, child in self.children.items()}

    def snapshot(self) -> dict:
        return {"state": self.state, "restarts": self.restarts, "pids": self.pids}

    def _emit(self, state: str, *, child: str | None = None):
        self.state = state
        event = self.snapshot()
        if child is not None:
            event["child"] = child
        try:
            self.on_event(event)
        except Exception:
            # Display failure cannot disable supervision or expose child output.
            pass

    def _lock(self):
        lock_path = self.deployment.prefix / "policy/manager.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise WorkspaceError("unsafe_manager_lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkspaceError("workspace_already_managed") from exc
        except BaseException:
            os.close(fd)
            raise
        self._lock_fd = fd

    def _unlock(self):
        if self._lock_fd is not None:
            fd, self._lock_fd = self._lock_fd, None
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _python_argv(self, command: str, daemon_pid: int | None = None) -> list[str]:
        if command not in {"gateway", "bootstrap", "controller"}:
            raise WorkspaceError("invalid_managed_command")
        python = Path(self.deployment.value["python"]["path"])
        for path in (*reversed(python.parents), python):
            info = path.lstat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise WorkspaceError("unprotected_system_python")
        if not python.is_file() or not os.access(python, os.X_OK):
            raise WorkspaceError("unusable_system_python")
        argv = [
            str(python),
            "-I",
            "-B",
            "-c",
            _PYTHON_ENTRY,
            str(self.deployment.prefix / "lib"),
            command,
            str(self.deployment.path),
        ]
        if daemon_pid is not None:
            argv.append(str(daemon_pid))
        return argv

    def _spawn(self, name: str, *, daemon_pid: int | None = None):
        installation = self.deployment.installation
        if name == "daemon":
            launch = self.deployment.plan["launch"]
            argv = list(launch["argv"])
            env = dict(launch["environment"])
            uid = installation.harness_uid
        else:
            argv = self._python_argv(name, daemon_pid)
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(self.deployment.prefix / "base"),
                "LANG": "C.UTF-8",
            }
            uid = installation.gateway_uid if name == "gateway" else 0
        if self.deployment.value.get("mock_port") is not None:
            env.update(
                {
                    "HTTP_PROXY": "http://127.0.0.1:9",
                    "HTTPS_PROXY": "http://127.0.0.1:9",
                    "ALL_PROXY": "http://127.0.0.1:9",
                    "NO_PROXY": "127.0.0.1,localhost",
                }
            )
        env.update({"TERM": "dumb", "RUST_LOG": "off"})
        process = subprocess.Popen(
            argv,
            cwd=str(self.deployment.prefix / "base"),
            env=env,
            user=uid,
            group=uid,
            extra_groups=[installation.business_gid] if uid else [],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            umask=0o077,
        )
        self.children[name] = process
        self._emit("starting", child=name)
        return process

    def _wait(self, name: str, ready: Callable[[], bool]):
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if _has_exited(self.children[name]):
                raise WorkspaceError("managed_" + name + "_exited")
            try:
                if ready():
                    return
            except (FileNotFoundError, ConnectionRefusedError):
                pass
            time.sleep(0.05)
        raise WorkspaceError("managed_" + name + "_startup_timeout")

    def _gateway_ready(self):
        deployment = self.deployment
        _peer(
            deployment.admin_path,
            deployment.installation.gateway_uid,
            self.children["gateway"].pid,
        )
        reply = deployment.admin("status")
        if (
            reply.get("ok") is not True
            or reply.get("result", {}).get("fenced") is not True
        ):
            raise WorkspaceError("gateway_not_initially_fenced")
        self._gateway_endpoints = [
            _Endpoint.capture(
                deployment.admin_path,
                uid=deployment.installation.gateway_uid,
                mode=0o600,
            ),
            _Endpoint.capture(
                deployment.business_path,
                uid=deployment.installation.gateway_uid,
                mode=0o660,
            ),
        ]
        return True

    def _daemon_ready(self):
        deployment = self.deployment
        advertised = (
            deployment.prefix / "harness/app-server-control/app-server-control.sock"
        )
        _peer(
            advertised, deployment.installation.harness_uid, self.children["daemon"].pid
        )
        directory = Path(f"/tmp/codex-daemon-{deployment.installation.harness_uid}")
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != deployment.installation.harness_uid
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise WorkspaceError("unsafe_managed_daemon_directory")
        target = os.readlink(advertised)
        real_socket = Path(target)
        if real_socket.parent != directory or real_socket.name in ("", ".", ".."):
            raise WorkspaceError("unexpected_daemon_socket_target")
        self._daemon_directory = (directory, info.st_dev, info.st_ino)
        self._daemon_lock = _Endpoint.capture(
            real_socket.with_suffix(".lock"),
            uid=deployment.installation.harness_uid,
            mode=0o600,
            regular=True,
        )
        self._daemon_endpoints = [
            _Endpoint.capture(
                real_socket, uid=deployment.installation.harness_uid, mode=0o600
            ),
            _Endpoint.capture(
                advertised,
                uid=deployment.installation.harness_uid,
                mode=0o777,
                link_target=target,
            ),
        ]
        return True

    def _controller_ready(self):
        reply = self.deployment.admin("status")
        if reply.get("ok") is not True:
            raise WorkspaceError("gateway_status_unavailable")
        if reply.get("result", {}).get("fenced") is not False:
            return False
        workspace = self.deployment.admin("workspace.status")
        return (
            workspace.get("ok") is True
            and workspace.get("result", {}).get("service", {}).get("active") == 1
        )

    def _fence(self) -> bool:
        gateway = self.children.get("gateway")
        if gateway is None or _has_exited(gateway):
            self._emit("gateway_unavailable")
            return False
        try:
            reply = self.deployment.admin("fence")
            if (
                reply.get("ok") is not True
                or reply.get("result", {}).get("fenced") is not True
            ):
                raise WorkspaceError("gateway_fence_failed")
        except (OSError, ValueError):
            self._emit("fence_unconfirmed_lease_expiry_bound")
            return False
        self._emit("fenced")
        return True

    def _check_empty_endpoints(self):
        candidates = (
            self.deployment.admin_path,
            self.deployment.business_path,
            self.deployment.prefix
            / "harness/app-server-control/app-server-control.sock",
            Path(f"/tmp/codex-daemon-{self.deployment.installation.harness_uid}"),
        )
        for path in candidates:
            if os.path.lexists(path):
                raise WorkspaceError("preexisting_service_endpoint_requires_management")

    def _start_cycle(self, *, bootstrap: bool):
        self.deployment.attest()
        self._check_empty_endpoints()
        if not bootstrap:
            # Protected exact roots must pre-exist; automatic recovery may not
            # create new ones or repair a changed administrative declaration.
            self.deployment.seats()
        self._spawn("gateway")
        self._wait("gateway", self._gateway_ready)
        if not self._fence():
            raise WorkspaceError("gateway_fence_failed")
        self._spawn("daemon")
        self._wait("daemon", self._daemon_ready)
        if bootstrap:
            child = self._spawn("bootstrap")
            try:
                code = child.wait(timeout=self.startup_timeout)
            except subprocess.TimeoutExpired as exc:
                raise WorkspaceError("managed_bootstrap_timeout") from exc
            self.children.pop("bootstrap")
            if code != 0:
                raise WorkspaceError("managed_bootstrap_failed")
        self._spawn("controller", daemon_pid=self.children["daemon"].pid)
        self._wait("controller", self._controller_ready)
        self._emit("running")

    def start(self, *, bootstrap: bool = True):
        if self._started or self._closed or type(bootstrap) is not bool:
            raise WorkspaceError("manager_already_started")
        self.deployment.attest()
        self._lock()
        self._started = True
        try:
            if self.deployment.roots_path.exists():
                bootstrap = False
            self._start_cycle(bootstrap=bootstrap)
        except BaseException:
            self.close()
            raise
        return self

    def _cleanup_endpoints(self):
        lock_fd = self._daemon_lock.lock() if self._daemon_lock is not None else None
        try:
            for endpoint in self._daemon_endpoints:
                endpoint.remove()
            self._daemon_endpoints.clear()
            if self._daemon_lock is not None:
                self._daemon_lock.remove()
                self._daemon_lock = None
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
        for endpoint in self._gateway_endpoints:
            endpoint.remove()
        self._gateway_endpoints.clear()
        if self._daemon_directory is not None:
            directory, device, inode = self._daemon_directory
            try:
                info = directory.lstat()
            except FileNotFoundError:
                self._daemon_directory = None
                return
            if (
                (info.st_dev, info.st_ino) != (device, inode)
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != self.deployment.installation.harness_uid
            ):
                raise WorkspaceError("managed_daemon_directory_changed")
            try:
                directory.rmdir()  # Empty only. Never recurse or remove its contents.
            except OSError as exc:
                raise WorkspaceError("managed_daemon_directory_not_empty") from exc
            self._daemon_directory = None

    def _stop_cycle(self):
        self._fence()
        failed = False
        for name in ("controller", "bootstrap", "daemon", "gateway"):
            child = self.children.get(name)
            if child is not None:
                try:
                    _stop_owned_process(child)
                except (OSError, WorkspaceError, subprocess.TimeoutExpired):
                    failed = True
                else:
                    self.children.pop(name)
        if failed:
            raise WorkspaceError("managed_child_stop_incomplete")
        self._cleanup_endpoints()

    def tick(self) -> dict:
        if not self._started or self._closed:
            raise WorkspaceError("manager_not_running")
        failed = next(
            (name for name, child in self.children.items() if _has_exited(child)),
            None,
        )
        if failed is None:
            return self.snapshot()
        self._emit("child_exited", child=failed)
        try:
            self._stop_cycle()
        except BaseException:
            self.close()
            raise
        if self.restarts >= self.max_restarts:
            self._emit("restart_limit_reached")
            self.close()
            raise WorkspaceError("managed_restart_limit_reached")
        self.restarts += 1
        self._emit("recovering")
        time.sleep(min(self.backoff_seconds * 2 ** (self.restarts - 1), 10))
        try:
            self._start_cycle(bootstrap=False)
        except BaseException:
            self.close()
            raise
        return self.snapshot()

    def close(self):
        if self._closed:
            return
        try:
            self._stop_cycle()
        finally:
            self._closed = True
            self._unlock()
            self._emit("stop_incomplete" if self.children else "stopped")

    def run(self, *, bootstrap: bool = True):
        stopping = False

        def stop(_signum, _frame):
            nonlocal stopping
            stopping = True

        previous = {
            sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            self.start(bootstrap=bootstrap)
            while not stopping:
                self.tick()
                time.sleep(0.1)
        finally:
            self.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
