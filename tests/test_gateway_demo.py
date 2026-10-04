"""Public entry-point acceptance with only fresh, synthetic local state."""

from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import struct
import sys

import pytest

from deskd.gateway.demo import render_board, run_demo
import deskd.gateway.__main__ as gateway_cli


ROOT = Path(__file__).resolve().parents[1]


def no_network(*args, **kwargs):
    raise AssertionError("offline acceptance must not create a socket")


def test_demo_publishes_once_and_writes_complete_fenced_artifacts(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(socket, "socket", no_network)
    output = tmp_path / "fresh-demo"
    report = run_demo(output)
    assert json.loads((output / "report.json").read_text()) == report
    assert report["mode"] == "offline-mock"
    assert report["isolation_verified"] is report["ready_for_credentials"] is False
    assert {role["seat"] for role in report["roles"]} == {
        "operator",
        "reviewer",
        "engineer",
    }
    assert len(report["snapshot"]["memos"]) == 1
    memo = report["snapshot"]["memos"][0]
    assert memo["issuer_principal"] != memo["executor_principal"]
    assert report["receipts"][0]["result"]["body"] == memo["body"]
    evidence = {item["code"] for item in report["checks"]}
    assert {
        "independent_approver_required",
        "binding_mismatch",
        "approval_content_mismatch",
        "root_required",
        "capability_denied",
        "no_duplicate_effect",
        "approval_not_active",
        "service_fenced",
        "stale_service_generation",
        "inactive_channel",
    } <= evidence
    assert len(report["checks"]) >= 11
    with sqlite3.connect(output / "workflow.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM memo_published").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM events_outbox").fetchone() == (3,)
        assert conn.execute("SELECT active FROM service").fetchone() == (0,)
    board = (output / "board.html").read_text()
    assert "Offline mock demonstration" in board
    assert "Content-Security-Policy" in board
    assert "default-src 'none'" in board


def test_demo_refuses_existing_output_without_modifying_contents(tmp_path):
    output = tmp_path / "already-there"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_bytes(b"untouched synthetic data")
    before = sentinel.stat()
    with pytest.raises(FileExistsError):
        run_demo(output)
    assert list(output.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"untouched synthetic data"
    assert sentinel.stat().st_mtime_ns == before.st_mtime_ns


def test_board_escapes_all_dynamic_text_instead_of_rendering_active_markup():
    attack = '<script>alert("mock")</script><img src=x onerror="mock()">&'
    report = {
        "roles": [{"seat": attack, "responsibility": attack, "principal": attack}],
        "snapshot": {
            "memos": [
                {
                    key: attack
                    for key in (
                        "body",
                        "author_principal",
                        "issuer_principal",
                        "executor_principal",
                        "body_sha256",
                        "memo_id",
                    )
                }
            ]
        },
        "checks": [{"scenario": attack, "outcome": attack, "code": attack}],
        "limitations": [attack],
    }
    board = render_board(report)

    class Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.names = []
            self.attributes = []

        def handle_starttag(self, tag, attrs):
            self.names.append(tag)
            self.attributes.extend(attrs)

    tags = Tags()
    tags.feed(board)
    assert not {"script", "img", "iframe"} & set(tags.names)
    assert not any(name.startswith("on") for name, _ in tags.attributes)
    assert "&lt;script&gt;" in board
    assert "&quot;mock&quot;" in board
    assert attack not in board


@pytest.fixture
def isolated_cli(tmp_path):
    """A fresh interpreter with a hostile mock host config and no socket access.

    The supplied environment never inherits an actual host config or secret.
    These helper files are synthetic and live under pytest's scratch directory.
    """
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    host_marker = tmp_path / "unexpected-host-import"
    network_marker = tmp_path / "unexpected-socket-use"
    (helpers / "synthetic_forbidden_host.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(host_marker)!r}).write_text('unexpected import')\n"
        "raise RuntimeError('legacy host must not load')\n"
    )
    (helpers / "sitecustomize.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "def deny_socket(event, args):\n"
        "    if event == 'socket.__new__':\n"
        f"        Path({str(network_marker)!r}).write_text('unexpected socket')\n"
        "        raise RuntimeError('offline CLI attempted socket access')\n"
        "sys.addaudithook(deny_socket)\n"
    )
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": os.pathsep.join([str(helpers), str(ROOT / "src")]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DESKD_CONFIG_MODULE": "synthetic_forbidden_host",
    }

    def run(*arguments):
        result = subprocess.run(
            [sys.executable, "-m", "deskd.gateway", *map(str, arguments)],
            cwd=tmp_path,
            env=environment,
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert not host_marker.exists(), (
            "independent CLI imported legacy host configuration"
        )
        assert not network_marker.exists(), "offline CLI attempted to create a socket"
        assert "offline CLI attempted socket access" not in result.stderr
        return result

    return run


@pytest.fixture
def negative_manifest(tmp_path):
    # Preserve the published negative fixture's schema, placeholder pin and
    # relationships, but direct all metadata inspection into this scratch tree.
    public = json.loads(
        (ROOT / "docs/fixtures/local-install-manifest.json").read_text()
    )

    def relocate(value):
        if isinstance(value, dict):
            return {key: relocate(item) for key, item in value.items()}
        if isinstance(value, list):
            return [relocate(item) for item in value]
        if isinstance(value, str) and value.startswith("/"):
            return str(tmp_path / "declared-metadata" / value.lstrip("/"))
        return value

    fixture = relocate(public)
    path = tmp_path / "non-secret-manifest.json"
    path.write_text(json.dumps(fixture))
    return path, fixture


def test_module_cli_demo_is_independent_of_host_and_network(tmp_path, isolated_cli):
    output = tmp_path / "cli-demo"
    result = isolated_cli("demo", "--output", output)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    summary = json.loads(result.stdout)
    assert summary["mode"] == "offline-mock"
    assert summary["published_memos"] == 1
    assert summary["checks"] >= 11
    assert summary["isolation_verified"] is summary["ready_for_credentials"] is False
    assert Path(summary["board"]) == output / "board.html"
    assert (output / "report.json").is_file()
    assert (output / "workflow.db").is_file()


def test_module_cli_existing_output_rejects_without_touching_old_data(
    tmp_path, isolated_cli
):
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "sentinel.bin"
    sentinel.write_bytes(b"existing mock state")
    result = isolated_cli("demo", "--output", output)
    assert result.returncode == 2
    assert json.loads(result.stderr) == {"error": "output_or_socket_already_exists"}
    assert result.stdout == ""
    assert list(output.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"existing mock state"


def test_module_cli_preflight_negative_fixture_has_no_host_or_network_use(
    isolated_cli, negative_manifest
):
    path, _ = negative_manifest
    result = isolated_cli("preflight", path)
    assert result.returncode == 2
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["metadata_ok"] is report["ready_for_credentials"] is False
    assert report["faults"]


def test_serve_memo_rejects_negative_fixture_before_creating_state(
    isolated_cli, negative_manifest
):
    path, fixture = negative_manifest
    gateway_state = Path(fixture["paths"]["gateway_state"])
    assert not gateway_state.exists()
    result = isolated_cli("serve-memo", "--manifest", path)
    assert result.returncode == 2
    assert result.stdout == ""
    assert json.loads(result.stderr) == {"error": "request_or_configuration_rejected"}
    assert not gateway_state.exists()
    assert not (gateway_state / "memo.db").exists()


@pytest.mark.parametrize(
    "response",
    [
        {"ok": True},
        {"ok": False, "error": "not-an-error-object"},
        {"ok": False, "error": {"code": "storage_error"}},
    ],
    ids=["incomplete-success", "malformed-error", "uncertain-commit-error"],
)
def test_control_rejects_ambiguous_ack_without_retry(tmp_path, monkeypatch, response):
    gateway_uid = os.geteuid() + 10001
    connections = []
    submissions = []

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, timeout):
            assert timeout > 0

        def connect(self, path):
            connections.append(path)

        def getsockopt(self, *args):
            return struct.pack("3i", 123, gateway_uid, 0)

        def sendall(self, raw):
            submissions.append(json.loads(raw))

        def makefile(self, mode):
            assert mode == "rb"
            return io.BytesIO(
                json.dumps({"id": submissions[-1]["id"], **response}).encode() + b"\n"
            )

    monkeypatch.setattr(gateway_cli.socket, "socket", lambda *args: FakeSocket())
    with pytest.raises(OSError, match="unknown_admin_outcome"):
        gateway_cli.control(tmp_path / "synthetic-admin.sock", gateway_uid, "bind", {})
    assert len(connections) == len(submissions) == 1
    assert not (tmp_path / "synthetic-admin.sock").exists()


@pytest.mark.parametrize("kind", ["fifo", "symlink", "directory", "hardlink"])
def test_manifest_rejects_nonregular_or_aliased_files_without_blocking(
    tmp_path, isolated_cli, kind
):
    manifest = tmp_path / "invalid-manifest"
    if kind == "fifo":
        os.mkfifo(manifest)
    elif kind == "directory":
        manifest.mkdir()
    else:
        target = tmp_path / "synthetic-target.json"
        target.write_text('{"synthetic": true}')
        if kind == "symlink":
            manifest.symlink_to(target)
        else:
            os.link(target, manifest)
    # A timeout in isolated_cli fails the test: opening an unwritten FIFO must
    # be nonblocking and rejected before reading. No writer or service runs.
    result = isolated_cli("preflight", manifest)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "error" in json.loads(result.stderr)
    assert "synthetic" not in result.stderr
