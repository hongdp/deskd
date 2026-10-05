"""Opt-in official-runtime isolation acceptance on an ephemeral Linux VM.

No model credential is used: a loopback Responses mock emits only deterministic
shell probes against synthetic files. The CI wrapper provides a private mount
namespace with /tmp backed by its scratchpad. No host installation is performed.
"""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

import pytest

from deskd.workspace.installation import (
    Installation,
    OFFICIAL_LINUX_X64_SHA256,
    OFFICIAL_BWRAP_SHA256,
    RoleInstallation,
)

HARNESS_UID = 26002
GATEWAY_UID = 26001
BUSINESS_GID = 26003


class ModelMock:
    """A fixed tool call followed by a fixed answer; never forwards requests."""

    def __init__(self):
        self.command = None
        self.patch = None
        self.image_path = None
        self.image_output = None
        self.image_call_id = None
        self.sent = False
        self.calls = 0
        self.failure = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length < 4 * 1024 * 1024:
                        raise ValueError("invalid mock request size")
                    body = json.loads(self.rfile.read(length))
                    names = [
                        t.get("name", t.get("type")) for t in body.get("tools", [])
                    ]
                    owner.calls += 1
                    if not owner.sent:
                        if owner.image_path is not None:
                            if "view_image" not in names:
                                raise ValueError("expected official view_image tool")
                            owner.image_call_id = f"image-probe-{owner.calls}"
                            item = {
                                "type": "function_call",
                                "name": "view_image",
                                "arguments": json.dumps({"path": owner.image_path}),
                                "call_id": owner.image_call_id,
                            }
                        elif owner.patch is not None:
                            if "apply_patch" not in names:
                                raise ValueError("expected official apply_patch tool")
                            item = {
                                "type": "custom_tool_call",
                                "name": "apply_patch",
                                "input": owner.patch,
                                "call_id": f"patch-probe-{owner.calls}",
                            }
                        else:
                            if "exec_command" not in names or owner.command is None:
                                raise ValueError("expected official exec_command tool")
                            item = {
                                "type": "function_call",
                                "call_id": f"isolation-probe-{owner.calls}",
                                "name": "exec_command",
                                "arguments": json.dumps(
                                    {
                                        "cmd": owner.command,
                                        "yield_time_ms": 10000,
                                        "max_output_tokens": 500,
                                    }
                                ),
                            }
                        owner.sent = True
                    else:
                        if owner.image_path is not None:
                            outputs = [
                                value
                                for value in body.get("input", [])
                                if value.get("type") == "function_call_output"
                                and value.get("call_id") == owner.image_call_id
                            ]
                            if len(outputs) != 1:
                                raise ValueError("missing synthetic image result")
                            owner.image_output = outputs[0]["output"]
                        item = {
                            "type": "message",
                            "role": "assistant",
                            "id": "m",
                            "content": [
                                {"type": "output_text", "text": "probe complete"}
                            ],
                        }
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
                    payload = "".join(
                        "event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n"
                        for e in events
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception as exc:
                    owner.failure = type(exc).__name__
                    self.send_error(500)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()


def _assert_root_tree(path):
    assert path.is_absolute() and ".." not in path.parts
    for ancestor in (*reversed(path.parents), path):
        info = ancestor.lstat()
        assert stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
        assert info.st_uid == 0 and not info.st_mode & 0o022


@pytest.fixture
def runtime_tree():
    supplied = os.environ.get("DESKD_SANDBOX_TEST_ROOT")
    if not supplied:
        pytest.skip("opt-in ephemeral Linux root runner required")
    assert sys.platform == "linux" and os.geteuid() == 0
    root = Path(supplied)
    _assert_root_tree(root)
    assert root.name == "scratchpad"
    # The wrapper binds its fresh scratchpad/tmp over /tmp in a private mount
    # namespace. Stock Codex intentionally ignores TMPDIR for daemon sockets.
    assert os.path.samefile(root / "system-tmp", "/tmp")
    binary = root / "official/codex"
    info = binary.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == OFFICIAL_LINUX_X64_SHA256
    tree = Path(tempfile.mkdtemp(prefix="dw-", dir=root))
    os.chmod(tree, 0o755)
    roles = tuple(
        RoleInstallation(
            seat, str(tree / "roles" / seat), str(tree / "roles" / seat / "data")
        )
        for seat in ("operator", "reviewer")
    )
    installation = Installation(
        str(tree), HARNESS_UID, GATEWAY_UID, BUSINESS_GID, roles
    )
    mock = ModelMock()
    plan = installation.plan(mock_port=mock.server.server_port)
    for entry in plan["directories"]:
        path = Path(entry["path"])
        path.mkdir(exist_ok=path == tree)
        os.chown(path, entry["uid"], entry["gid"])
        os.chmod(path, int(entry["mode"], 8))
    for entry in plan["files"]:
        path = Path(entry["path"])
        path.write_text(entry["content"])
        os.chmod(path, int(entry["mode"], 8))
    shutil.copyfile(binary, installation.binary)
    os.chmod(installation.binary, 0o755)
    helper = root / "official/codex-resources/bwrap"
    assert hashlib.sha256(helper.read_bytes()).hexdigest() == OFFICIAL_BWRAP_SHA256
    resources = tree / "bin/codex-resources"
    resources.mkdir()
    resources.chmod(0o755)
    shutil.copyfile(helper, resources / "bwrap")
    (resources / "bwrap").chmod(0o755)
    for role in roles:
        marker = Path(role.data) / "marker"
        marker.write_text("SYNTHETIC-ROLE-MARKER")
        os.chown(marker, HARNESS_UID, HARNESS_UID)
        os.chmod(marker, 0o600)

        # One deterministic RGB pixel, constructed as PNG chunks using stdlib.
        # This is test input, never a user image or a file from outside scratchpad.
        def chunk(kind, data):
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data))
            )

        png = Path(role.data) / "synthetic.png"
        png.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x80\xff"))
            + chunk(b"IEND", b"")
        )
        os.chown(png, HARNESS_UID, HARNESS_UID)
        png.chmod(0o600)
    for i, role in enumerate(roles):
        link = Path(role.data) / "synthetic-peer.png"
        link.symlink_to(Path(roles[1 - i].data) / "synthetic.png")
        os.lchown(link, HARNESS_UID, HARNESS_UID)
    for parent in ("harness", "tmp"):
        marker = tree / parent / "synthetic-private"
        marker.write_text("SYNTHETIC-PARENT-PRIVATE")
        os.chown(marker, HARNESS_UID, HARNESS_UID)
        os.chmod(marker, 0o600)
    fake_secret = tree / "gateway/fake-secret"
    fake_secret.write_text("PUBLIC-MOCK-NOT-A-CREDENTIAL")
    os.chown(fake_secret, GATEWAY_UID, GATEWAY_UID)
    os.chmod(fake_secret, 0o600)
    channel_dir = tree / "business"
    channel_dir.mkdir(mode=0o750)
    os.chown(channel_dir, GATEWAY_UID, BUSINESS_GID)
    os.chmod(channel_dir, 0o750)
    channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    channel.bind(str(channel_dir / "s"))
    channel.listen(8)
    os.chown(channel_dir / "s", GATEWAY_UID, BUSINESS_GID)
    os.chmod(channel_dir / "s", 0o660)
    # A gateway boundary test separately runs the real gateway UID. This test
    # targets the additional same-harness-UID role sandbox, including this path.
    processes = []
    try:
        yield installation, plan, mock, processes, channel_dir / "s"
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        channel.close()
        mock.close()
        # Keep only this synthetic tree for failure diagnosis until VM teardown.


def _start_daemon(installation, plan, processes):
    launch = plan["launch"]
    env = dict(launch["environment"])
    env.update(
        {
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1,localhost",
            "TERM": "dumb",
            "RUST_LOG": "off",
            "DESKD_SYNTHETIC_PARENT_ONLY": "PUBLIC-MOCK-MARKER",
        }
    )
    log = Path(installation.prefix) / f"daemon-{len(processes)}.log"
    with log.open("wb") as target:
        proc = subprocess.Popen(
            launch["argv"],
            cwd=launch["cwd"],
            env=env,
            user=HARNESS_UID,
            group=HARNESS_UID,
            extra_groups=[BUSINESS_GID],
            stdout=target,
            stderr=target,
            start_new_session=True,
        )
    processes.append(proc)
    advertised = Path(
        installation.path("harness/app-server-control/app-server-control.sock")
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        assert proc.poll() is None, (
            "official daemon exited; synthetic log retained in scratchpad"
        )
        if advertised.exists():
            return advertised, proc
        time.sleep(0.05)
    pytest.fail("official daemon did not publish its private socket")


def _probe_command(
    installation, role, other, gateway_socket, daemon_socket, daemon_pid, port, suffix
):
    paths = {
        "other_read": str(Path(other.data) / "marker"),
        "secret_read": installation.path("gateway/fake-secret"),
        "harness_read": installation.path("harness/synthetic-private"),
        "shared_tmp_read": installation.path("tmp/synthetic-private"),
        "owned_daemon_proc": f"/proc/{daemon_pid}/environ",
        "owned_daemon_mem": f"/proc/{daemon_pid}/mem",
        "owned_daemon_root_file": f"/proc/{daemon_pid}/root{installation.path('harness/synthetic-private')}",
    }
    script = f"""
import ctypes, errno, json, os, pathlib, socket
results = {{"parent_environment": os.environ.get("DESKD_SYNTHETIC_PARENT_ONLY") is None}}
def denied(name, action):
    try:
        value = action()
        if hasattr(value, 'close'): value.close()
    except OSError:
        results[name] = True
    else:
        results[name] = False
for name, path in {paths!r}.items():
    denied(name, lambda path=path: open(path, 'rb'))
def directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    os.close(fd)
for name, path in {{'owned_daemon_root': '/proc/{daemon_pid}/root', 'owned_daemon_fd': '/proc/{daemon_pid}/fd'}}.items():
    denied(name, lambda path=path: directory(path))
# This is exclusively the fixture daemon PID, verified using SO_PEERCRED.
# SEIZE neither stops the target nor reads memory. If it unexpectedly succeeds,
# fail the receipt; tracer exit releases it without sending a signal.
libc = ctypes.CDLL(None, use_errno=True)
ptrace = libc.ptrace
ptrace.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
ptrace.restype = ctypes.c_long
ctypes.set_errno(0)
seized = ptrace(0x4206, {daemon_pid}, None, None)
results['owned_daemon_ptrace'] = seized == -1 and ctypes.get_errno() in (errno.EPERM, errno.ESRCH, errno.EACCES)
denied('owned_daemon_signal_zero', lambda: os.kill({daemon_pid}, 0))
denied('other_write', lambda: pathlib.Path({str(Path(other.data) / "injected")!r}).write_text('bad'))
denied('config_write', lambda: pathlib.Path({str(Path(role.root) / ".codex/config.toml")!r}).write_text('bad'))
denied('shadow_config_mkdir', lambda: pathlib.Path({str(Path(role.data) / ".codex")!r}).mkdir())
# bwrap may leave an empty mount target. Probe the file independently so an
# existing directory cannot short-circuit the actual policy write attempt.
shadow = pathlib.Path({str(Path(role.data) / ".codex")!r})
denied('shadow_config', lambda: (shadow / 'config.toml').write_text('default_permissions = "locked"'))
denied('shadow_config_file_symlink', lambda: os.symlink({str(Path(role.root) / ".codex/config.toml")!r}, shadow / 'config.toml'))
denied('shadow_config_symlink', lambda: os.symlink({str(Path(role.root) / ".codex")!r}, shadow))
denied('shadow_config_remove', lambda: shadow.rmdir())
replacement = pathlib.Path({str(Path(role.data) / ("shadow-replacement-" + suffix))!r})
replacement.mkdir()
denied('shadow_config_replace', lambda: os.replace(replacement, shadow))
denied('config_rename', lambda: os.rename({role.root!r}, {role.root + "-moved"!r}))
denied('hardlink', lambda: os.link({str(Path(other.data) / "marker")!r}, {str(Path(role.data) / "linked")!r}))
link = pathlib.Path({str(Path(role.data) / ("peer-link-" + suffix))!r})
link.symlink_to({str(Path(other.data) / "marker")!r})
denied('symlink_read', lambda: link.open('rb'))
for name, path in {{'gateway_socket': {str(gateway_socket)!r}, 'daemon_socket': {str(daemon_socket)!r}}}.items():
    def connect(path=path):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(1)
            sock.connect(path)
    denied(name, connect)
def network():
    with socket.create_connection(('127.0.0.1', {port}), timeout=1): pass
denied('loopback_network', network)
pathlib.Path({str(Path(role.data) / ("own-" + suffix))!r}).write_text('allowed')
results['own_write'] = True
pathlib.Path({str(Path(role.data) / ("result-" + suffix + ".json"))!r}).write_text(json.dumps(results))
print('SYNTHETIC-SANDBOX-PROBE-COMPLETE')
"""
    return "/usr/bin/python3 -c " + shlex.quote(script)


def _assert_no_shadow_policy(role):
    shadow = Path(role.data) / ".codex"
    # Official bwrap can create a harmless empty mount target. The marker is
    # the protected config file, so a placeholder must never supply policy.
    assert not shadow.is_symlink()
    if shadow.exists():
        assert shadow.is_dir() and not any(shadow.iterdir())
    assert not os.path.lexists(shadow / "config.toml")


def _run_probe(
    client,
    binding,
    installation,
    role,
    other,
    gateway_socket,
    daemon_socket,
    mock,
    suffix,
):
    _assert_no_shadow_policy(role)
    mock.patch = None
    mock.command = _probe_command(
        installation,
        role,
        other,
        gateway_socket,
        daemon_socket,
        client.peer_pid,
        mock.server.server_port,
        suffix,
    )
    mock.sent = False
    before = mock.calls
    turn_id = client.start_turn(
        binding.thread_id,
        [
            {
                "event_id": suffix,
                "type": "test.probe",
                "data": {"instruction": "Run the fixed sandbox probe."},
            }
        ],
    )
    deadline = time.monotonic() + 40
    completed = False
    while time.monotonic() < deadline:
        event = client.poll_event(timeout=1)
        if event and event.get("method") == "turn/completed":
            params = event.get("params", {})
            if (
                params.get("threadId") == binding.thread_id
                and params.get("turn", {}).get("id") == turn_id
            ):
                assert params["turn"].get("status") == "completed"
                completed = True
                break
    assert completed, "official sandbox turn did not complete"
    assert client.read_turn(binding.thread_id, turn_id) == "completed"
    assert mock.failure is None and mock.calls >= before + 2 and mock.sent
    result = Path(role.data) / f"result-{suffix}.json"
    assert result.exists(), (
        "model tool ran without producing the synthetic sandbox receipt"
    )
    checks = json.loads(result.read_text())
    assert checks and all(value is True for value in checks.values()), checks
    _assert_no_shadow_policy(role)
    assert (Path(role.data) / f"own-{suffix}").read_text() == "allowed"


def _run_patch_probe(client, binding, role, other, mock, suffix):
    for target_role, permitted in ((role, True), (other, False)):
        target = Path(target_role.data) / f"patch-{suffix}-{permitted}"
        mock.patch = f"*** Begin Patch\n*** Add File: {target}\n+SYNTHETIC-PATCH\n*** End Patch\n"
        mock.sent = False
        before = mock.calls
        turn_id = client.start_turn(
            binding.thread_id, [{"event_id": "patch-" + suffix, "type": "test.patch"}]
        )
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            event = client.poll_event(timeout=1)
            if event and event.get("method") == "turn/completed":
                params = event.get("params", {})
                if (
                    params.get("threadId") == binding.thread_id
                    and params.get("turn", {}).get("id") == turn_id
                ):
                    assert params["turn"]["status"] == "completed"
                    break
        else:
            pytest.fail("official apply_patch turn did not complete")
        assert client.read_turn(binding.thread_id, turn_id) == "completed"
        assert mock.sent and mock.calls >= before + 2 and mock.failure is None
        assert target.exists() is permitted
        if permitted:
            assert target.read_text().strip() == "SYNTHETIC-PATCH"
    mock.patch = None


def _run_image_probe(client, binding, role, other, mock, suffix):
    def contains_image(value):
        if isinstance(value, dict):
            return value.get("type") in ("input_image", "image_url") or any(
                contains_image(child) for child in value.values()
            )
        return isinstance(value, list) and any(contains_image(v) for v in value)

    for index, (target, permitted) in enumerate(
        (
            (Path(role.data) / "synthetic.png", True),
            (Path(other.data) / "synthetic.png", False),
            (Path(role.data) / "synthetic-peer.png", False),
        )
    ):
        mock.image_path = str(target)
        mock.image_output = None
        mock.sent = False
        before = mock.calls
        turn_id = client.start_turn(
            binding.thread_id,
            [{"event_id": f"image-{suffix}-{index}", "type": "test.image"}],
        )
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            event = client.poll_event(timeout=1)
            if event and event.get("method") == "turn/completed":
                params = event.get("params", {})
                if (
                    params.get("threadId") == binding.thread_id
                    and params.get("turn", {}).get("id") == turn_id
                ):
                    assert params["turn"]["status"] == "completed"
                    break
        else:
            pytest.fail("official view_image turn did not complete")
        assert client.read_turn(binding.thread_id, turn_id) == "completed"
        assert mock.sent and mock.calls >= before + 2 and mock.failure is None
        assert mock.image_output is not None
        assert contains_image(mock.image_output) is permitted, (
            "view_image did not enforce the expected synthetic image boundary"
        )
        if not permitted:
            text = json.dumps(mock.image_output).lower()
            assert any(word in text for word in ("error", "unable", "denied", "fail"))
    mock.image_path = None
    mock.image_output = None


def test_official_named_profiles_isolate_roles_and_survive_restart(runtime_tree):
    # Imported after the opt-in fixture: ordinary developer runs do not need a
    # daemon, privileges, a downloaded runtime, or network access.
    from deskd.workspace.runtime import CodexRuntime, RootConfig

    installation, plan, mock, processes, gateway_socket = runtime_tree
    # Prove the negative role probes are enforced by the sandbox rather than
    # the shared UID alone: this trusted baseline can read the peer's dummy
    # marker and connect to the mock gateway business socket.
    baseline = (
        "from pathlib import Path; import socket; "
        f"assert Path({str(Path(installation.roles[1].data) / 'marker')!r}).read_text() == 'SYNTHETIC-ROLE-MARKER'; "
        f"s=socket.socket(socket.AF_UNIX); s.connect({str(gateway_socket)!r}); s.close()"
    )
    subprocess.run(
        ["/usr/bin/python3", "-c", baseline],
        check=True,
        timeout=5,
        env={"PATH": "/usr/bin:/bin"},
        cwd=installation.path("base"),
        user=HARNESS_UID,
        group=HARNESS_UID,
        extra_groups=[BUSINESS_GID],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    advertised, proc = _start_daemon(installation, plan, processes)
    client = CodexRuntime(
        str(advertised),
        expected_uid=HARNESS_UID,
        expected_codex_home=installation.path("harness"),
        timeout=10,
    ).connect()
    assert client.peer_pid == proc.pid
    bindings = []
    configs = []
    try:
        for role in installation.roles:
            config = RootConfig(
                cwd=role.data,
                model="gpt-5.5",
                model_provider="deskd_mock",
                sandbox=None,
                permissions=role.seat,
                expected_sandbox={
                    "type": "workspaceWrite",
                    "writableRoots": [],
                    "networkAccess": False,
                    "excludeTmpdirEnvVar": True,
                    "excludeSlashTmp": True,
                },
            )
            bindings.append(client.start_root(config))
            configs.append(config)
        assert bindings[0].thread_id != bindings[1].thread_id
        assert bindings[0].session_id != bindings[1].session_id
        for i, role in enumerate(installation.roles):
            _run_probe(
                client,
                bindings[i],
                installation,
                role,
                installation.roles[1 - i],
                gateway_socket,
                advertised.resolve(),
                mock,
                f"initial-{i}",
            )
            _run_patch_probe(
                client,
                bindings[i],
                role,
                installation.roles[1 - i],
                mock,
                f"initial-{i}",
            )
            _run_image_probe(
                client,
                bindings[i],
                role,
                installation.roles[1 - i],
                mock,
                f"initial-{i}",
            )
    finally:
        client.close()
    proc.terminate()
    proc.wait(timeout=15)
    advertised, proc = _start_daemon(installation, plan, processes)
    client = CodexRuntime(
        str(advertised),
        expected_uid=HARNESS_UID,
        expected_codex_home=installation.path("harness"),
        timeout=10,
    ).connect()
    assert client.peer_pid == proc.pid
    try:
        for i, role in enumerate(installation.roles):
            resumed = client.resume_root(bindings[i].thread_id, configs[i])
            assert resumed.session_id == bindings[i].session_id
            _run_probe(
                client,
                resumed,
                installation,
                role,
                installation.roles[1 - i],
                gateway_socket,
                advertised.resolve(),
                mock,
                f"resumed-{i}",
            )
            _run_patch_probe(
                client, resumed, role, installation.roles[1 - i], mock, f"resumed-{i}"
            )
            _run_image_probe(
                client, resumed, role, installation.roles[1 - i], mock, f"resumed-{i}"
            )
    finally:
        client.close()


def test_installation_plan_is_inert_and_rejects_boundary_overlap():
    import tomllib

    roles = (
        RoleInstallation(
            "operator", "/opt/desk/roles/operator", "/opt/desk/roles/operator/data"
        ),
        RoleInstallation(
            "reviewer", "/opt/desk/roles/reviewer", "/opt/desk/roles/reviewer/data"
        ),
    )
    install = Installation("/opt/desk", 26002, 26001, 26003, roles)
    plan = install.plan(mock_port=12345)
    assert plan["ready_for_credentials"] is False
    config = tomllib.loads(plan["files"][0]["content"])
    assert "sandbox_mode" not in config
    assert config["permissions"]["operator"]["filesystem"][roles[1].root] == "deny"
    assert config["permissions"]["operator"]["filesystem"][roles[0].data] == "write"
    assert config["permissions"]["operator"]["network"]["enabled"] is False
    assert plan["launch"]["environment"]["CODEX_HOME"] == "/opt/desk/harness"
    assert all(file["uid"] == 0 for file in plan["files"])
    assert all(
        plan["launch"]["environment"].get(name) is None
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY")
    )
    with pytest.raises(ValueError):
        Installation("/opt/desk", 26002, 26002, 26003, roles)
    with pytest.raises(ValueError):
        Installation(
            "/opt/desk",
            26002,
            26001,
            26003,
            (
                roles[0],
                RoleInstallation("nested", roles[0].data, roles[0].data + "/data"),
            ),
        )
    with pytest.raises(ValueError):
        Installation("/opt/desk\nInjected=true", 26002, 26001, 26003, roles)
    with pytest.raises(ValueError):
        RoleInstallation("bad", "/opt/desk/bad", "/opt/desk/bad/.codex")


def test_fixed_bridge_config_is_present_only_in_role_project_layers():
    import tomllib

    roles = (
        RoleInstallation(
            "operator", "/opt/desk/roles/operator", "/opt/desk/roles/operator/data"
        ),
        RoleInstallation(
            "reviewer", "/opt/desk/roles/reviewer", "/opt/desk/roles/reviewer/data"
        ),
    )
    installation = Installation("/opt/desk", 26002, 26001, 26003, roles)
    plan = installation.plan(with_gateway_bridge=True)
    base = tomllib.loads(plan["files"][0]["content"])
    assert base["mcp_servers"]["deskd"]["enabled"] is False
    for entry in plan["files"][1:]:
        role = tomllib.loads(entry["content"])
        transport = role["mcp_servers"]["deskd"]
        assert transport == {
            "command": "/opt/desk/bin/deskd-bridge",
            "args": ["--socket", "/opt/desk/business/s", "--gateway-uid", "26001"],
            "enabled": True,
            "required": True,
        }
        assert entry["sha256"] == hashlib.sha256(entry["content"].encode()).hexdigest()
    assert "deskd" not in tomllib.loads(installation.configuration())["mcp_servers"]
