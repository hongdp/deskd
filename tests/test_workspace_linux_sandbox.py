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
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from deskd.workspace.installation import (
    Installation,
    OFFICIAL_LINUX_X64_SHA256,
    RoleInstallation,
)

HARNESS_UID = 26002
GATEWAY_UID = 26001
BUSINESS_GID = 26003


class ModelMock:
    """A fixed tool call followed by a fixed answer; never forwards requests."""

    def __init__(self):
        self.command = None
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
                        if "exec_command" not in names or owner.command is None:
                            raise ValueError("expected official exec_command tool")
                        owner.sent = True
                        item = {
                            "type": "function_call",
                            "call_id": f"isolation-probe-{owner.calls}",
                            "name": "exec_command",
                            "arguments": json.dumps(
                                {
                                    "cmd": owner.command,
                                    "yield_time_ms": 1000,
                                    "max_output_tokens": 500,
                                }
                            ),
                        }
                    else:
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
    for role in roles:
        marker = Path(role.data) / "marker"
        marker.write_text("SYNTHETIC-ROLE-MARKER")
        os.chown(marker, HARNESS_UID, HARNESS_UID)
        os.chmod(marker, 0o600)
    fake_secret = tree / "gateway/fake-secret"
    fake_secret.write_text("PUBLIC-MOCK-NOT-A-CREDENTIAL")
    os.chown(fake_secret, GATEWAY_UID, GATEWAY_UID)
    os.chmod(fake_secret, 0o600)
    channel_dir = tree / "business"
    channel_dir.mkdir(mode=0o750)
    os.chown(channel_dir, GATEWAY_UID, BUSINESS_GID)
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
    installation, role, other, gateway_socket, daemon_socket, port, suffix
):
    paths = {
        "other_read": str(Path(other.data) / "marker"),
        "secret_read": installation.path("gateway/fake-secret"),
    }
    script = f"""
import json, os, pathlib, socket
results = {{}}
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
denied('other_write', lambda: pathlib.Path({str(Path(other.data) / "injected")!r}).write_text('bad'))
denied('config_write', lambda: pathlib.Path({str(Path(role.root) / ".codex/config.toml")!r}).write_text('bad'))
denied('config_rename', lambda: os.rename({role.root!r}, {role.root + "-moved"!r}))
denied('hardlink', lambda: os.link({str(Path(other.data) / "marker")!r}, {str(Path(role.data) / "linked")!r}))
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
    mock.command = _probe_command(
        installation,
        role,
        other,
        gateway_socket,
        daemon_socket,
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
                completed = True
                break
    assert completed, "official sandbox turn did not complete"
    assert mock.failure is None and mock.calls >= before + 2 and mock.sent
    result = Path(role.data) / f"result-{suffix}.json"
    assert result.exists(), (
        "model tool ran without producing the synthetic sandbox receipt"
    )
    checks = json.loads(result.read_text())
    assert checks and all(value is True for value in checks.values()), checks
    assert (Path(role.data) / f"own-{suffix}").read_text() == "allowed"


def test_official_named_profiles_isolate_roles_and_survive_restart(runtime_tree):
    # Imported after the opt-in fixture: ordinary developer runs do not need a
    # daemon, privileges, a downloaded runtime, or network access.
    from deskd.workspace.runtime import CodexRuntime, RootConfig

    installation, plan, mock, processes, gateway_socket = runtime_tree
    advertised, proc = _start_daemon(installation, plan, processes)
    client = CodexRuntime(
        str(advertised),
        expected_uid=HARNESS_UID,
        expected_codex_home=installation.path("harness"),
        timeout=10,
    ).connect()
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
    finally:
        client.close()
    proc.terminate()
    proc.wait(timeout=15)
    advertised, _ = _start_daemon(installation, plan, processes)
    client = CodexRuntime(
        str(advertised),
        expected_uid=HARNESS_UID,
        expected_codex_home=installation.path("harness"),
        timeout=10,
    ).connect()
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
