"""Only newly created synthetic marker keys and mock transports are used.

No real credential, model endpoint, runtime home, or existing process is read.
Rootless metadata-model tests relax only pre-existing ancestors outside their
own private scratch subtree; real service UID/kernel separation is tested in CI.
"""

import io
import json
import os
import struct
import time

import pytest

from deskd.workspace import model_auth
from deskd.workspace.model_auth import (
    ModelAuthError,
    ModelKeySource,
    fetch_token,
    helper_main,
)

SYNTHETIC = b"SYNTHETIC-ONLY-NOT-A-REAL-MODEL-KEY"


@pytest.fixture
def synthetic_source(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip(
            "rootless file model; distinct real service UIDs covered by root acceptance"
        )
    private = tmp_path / "gateway"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    key = private / "model.key"
    key.write_bytes(SYNTHETIC + b"\n")
    key.chmod(0o600)
    original = model_auth._directory

    def own_subtree_only(info, path, gateway_uid, *, final):
        if path == private or private in path.parents:
            original(info, path, gateway_uid, final=final)

    monkeypatch.setattr(model_auth, "_directory", own_subtree_only)
    checks = []

    def authorize():
        checks.append("authorized")

    source = ModelKeySource(key, os.geteuid(), authorize)
    return source, key, checks


def test_synthetic_file_is_read_only_after_authorization_and_never_logged(
    synthetic_source, capsys
):
    source, _, checks = synthetic_source
    assert source.read_token().encode() == SYNTHETIC
    assert checks == ["authorized"]
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("return_value", [False, True, {}, "synthetic"])
def test_ambiguous_authorization_callback_cannot_open_file(
    synthetic_source, monkeypatch, return_value
):
    source, _, _ = synthetic_source
    source._authorize = lambda: return_value
    monkeypatch.setattr(
        model_auth.os,
        "open",
        lambda *_args, **_kwargs: pytest.fail("denied callback must not open any path"),
    )
    with pytest.raises(ModelAuthError, match="model_auth_not_ready"):
        source.read_token()


def test_authorization_failure_does_not_echo_callback_payload(
    synthetic_source, monkeypatch, capsys
):
    source, _, _ = synthetic_source

    def denied():
        raise ValueError("SYNTHETIC-PRIVATE-ERROR-CONTENT")

    source._authorize = denied
    monkeypatch.setattr(
        model_auth.os,
        "open",
        lambda *_args, **_kwargs: pytest.fail("denied callback must not open any path"),
    )
    with pytest.raises(ModelAuthError) as caught:
        source.read_token()
    assert str(caught.value) == "model_auth_not_ready"
    assert caught.value.__suppress_context__
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400, 0o666])
def test_wrong_key_mode_is_rejected(synthetic_source, mode):
    source, path, _ = synthetic_source
    path.chmod(mode)
    with pytest.raises(ModelAuthError, match="unprotected_model_key"):
        source.read_token()


def test_key_symlink_is_not_followed(synthetic_source):
    source, key, _ = synthetic_source
    target = key.with_name("other")
    key.rename(target)
    key.symlink_to(target)
    with pytest.raises(ModelAuthError, match="model_key_unavailable"):
        source.read_token()


def test_key_hardlink_is_rejected(synthetic_source):
    source, key, _ = synthetic_source
    os.link(key, key.with_name("alias"))
    with pytest.raises(ModelAuthError, match="unprotected_model_key"):
        source.read_token()


def test_fifo_is_rejected_without_blocking(synthetic_source):
    source, key, _ = synthetic_source
    key.unlink()
    os.mkfifo(key, 0o600)
    started = time.monotonic()
    with pytest.raises(ModelAuthError, match="unprotected_model_key"):
        source.read_token()
    assert time.monotonic() - started < 0.5


def test_writable_gateway_directory_is_rejected(synthetic_source):
    source, key, _ = synthetic_source
    key.parent.chmod(0o770)
    with pytest.raises(ModelAuthError, match="unprotected_model_key_directory"):
        source.read_token()


def test_directory_symlink_is_not_followed(synthetic_source):
    source, key, _ = synthetic_source
    original = key.parent
    moved = original.with_name("moved")
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ModelAuthError, match="model_key_unavailable"):
        source.read_token()


def test_wrong_service_uid_rejects_before_authorization(synthetic_source):
    source, _, checks = synthetic_source
    source.gateway_uid += 1
    with pytest.raises(ModelAuthError, match="wrong_model_key_service_uid"):
        source.read_token()
    assert checks == []


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b" leading",
        b"trailing ",
        b"two\nlines",
        b"line\rnext",
        b"bad\0key",
        b"\xff",
        b"x" * 4097,
    ],
)
def test_malformed_or_oversized_key_material_is_rejected(synthetic_source, data):
    source, key, _ = synthetic_source
    key.write_bytes(data)
    with pytest.raises(ModelAuthError):
        source.read_token()


def test_maximal_single_line_token_and_one_terminator_are_supported(synthetic_source):
    source, key, _ = synthetic_source
    key.write_bytes(b"x" * 4096 + b"\r\n")
    assert len(source.read_token()) == 4096


def test_key_changed_during_read_is_rejected(synthetic_source, monkeypatch):
    source, key, _ = synthetic_source
    original = os.read
    changed = False

    def changing(fd, size):
        nonlocal changed
        if not changed:
            changed = True
            key.write_bytes(b"SYNTHETIC-REPLACEMENT-LONGER-THAN-PREVIOUS-KEY")
        return original(fd, size)

    monkeypatch.setattr(model_auth.os, "read", changing)
    with pytest.raises(ModelAuthError, match="model_key_changed_during_read"):
        source.read_token()


class FakeSocket:
    """A protocol-only transport. SO_PEERCRED bytes here are synthetic evidence."""

    def __init__(self, gateway_uid, transform=None):
        self.gateway_uid = gateway_uid
        self.transform = transform
        self.sent = []
        self.incoming = b""
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def settimeout(self, _):
        pass

    def connect(self, path):
        assert path == "/synthetic/business/s"

    def getsockopt(self, *_):
        return struct.pack("3i", 12345, self.gateway_uid, self.gateway_uid)

    def sendall(self, data):
        request = json.loads(data)
        self.sent.append(request)
        reply = {
            "id": request["id"],
            "ok": True,
            "result": {"token": SYNTHETIC.decode()},
        }
        self.incoming = (
            self.transform(reply)
            if self.transform
            else json.dumps(reply).encode() + b"\n"
        )

    def recv(self, size):
        part, self.incoming = self.incoming[:size], self.incoming[size:]
        return part


@pytest.fixture
def auth_socket(monkeypatch):
    uid = max(12001, os.geteuid() + 1)
    sock = FakeSocket(uid)
    monkeypatch.setattr(model_auth.socket, "socket", lambda *_: sock)
    return sock, uid


def test_helper_request_has_fixed_method_and_no_token_path_or_provider_argument(
    auth_socket, capsys
):
    sock, uid = auth_socket
    assert fetch_token("/synthetic/business/s", uid).encode() == SYNTHETIC
    assert len(sock.sent) == 1
    assert sock.sent[0]["method"] == "model.auth" and sock.sent[0]["params"] == {}
    assert sock.closed
    assert capsys.readouterr() == ("", "")


def test_untrusted_peer_receives_no_auth_request(auth_socket):
    sock, uid = auth_socket
    sock.gateway_uid += 1
    with pytest.raises(ModelAuthError, match="untrusted_model_gateway"):
        fetch_token("/synthetic/business/s", uid)
    assert sock.sent == [] and sock.closed


@pytest.mark.parametrize(
    "kind",
    [
        "id",
        "extra",
        "token-field",
        "unicode",
        "newline",
        "duplicate",
        "nan",
        "truncated",
        "oversized",
        "denied",
    ],
)
def test_malformed_auth_response_never_leaks_payload_or_retries(
    auth_socket, kind, capsys
):
    sock, uid = auth_socket

    def transform(reply):
        if kind == "id":
            reply["id"] = "wrong"
        elif kind == "extra":
            reply["extra"] = "SYNTHETIC-PRIVATE"
        elif kind == "token-field":
            reply["result"]["other"] = "SYNTHETIC-PRIVATE"
        elif kind == "unicode":
            reply["result"]["token"] = "非ASCII"
        elif kind == "newline":
            reply["result"]["token"] = "synthetic\nheader"
        elif kind == "duplicate":
            return b'{"id":"wrong","id":"duplicate","ok":true,"result":{}}\n'
        elif kind == "nan":
            return b'{"id":NaN,"ok":true,"result":{}}\n'
        elif kind == "truncated":
            return b'{"result":'
        elif kind == "oversized":
            return b"x" * 8193
        elif kind == "denied":
            return (
                json.dumps(
                    {
                        "id": reply["id"],
                        "ok": False,
                        "error": {"code": "SYNTHETIC-PRIVATE-UPSTREAM"},
                    }
                ).encode()
                + b"\n"
            )
        return json.dumps(reply).encode() + b"\n"

    sock.transform = transform
    with pytest.raises(ModelAuthError) as caught:
        fetch_token("/synthetic/business/s", uid)
    assert "SYNTHETIC" not in str(caught.value)
    assert len(sock.sent) == 1 and sock.closed
    assert capsys.readouterr() == ("", "")


def test_auth_helper_stdout_is_only_private_token_pipe_and_failures_are_silent(
    auth_socket, capsys
):
    _, uid = auth_socket
    target = io.BytesIO()
    assert helper_main("/synthetic/business/s", uid, target) == 0
    assert target.getvalue() == SYNTHETIC + b"\n"
    failed = io.BytesIO()
    assert helper_main("relative-path", uid, failed) == 1
    assert failed.getvalue() == b""
    assert capsys.readouterr() == ("", "")


def test_helper_does_not_copy_exception_to_any_output(monkeypatch, capsys):
    def fail(*_):
        raise RuntimeError("SYNTHETIC-PRIVATE-PAYLOAD")

    monkeypatch.setattr(model_auth, "fetch_token", fail)
    target = io.BytesIO()
    assert helper_main("/synthetic/business/s", 12001, target) == 1
    assert target.getvalue() == b""
    assert capsys.readouterr() == ("", "")


def test_production_ancestor_check_rejects_unprotected_existing_scratch_parent(
    tmp_path,
):
    if os.geteuid() == 0:
        pytest.skip("rootless metadata negative")
    # No key is created or read. The first non-root or writable ancestor must
    # fail even if an operator accidentally points the source at such a tree.
    private = tmp_path / "gateway"
    private.mkdir(mode=0o700)
    source = ModelKeySource(private / "model.key", os.geteuid(), lambda: None)
    with pytest.raises(ModelAuthError, match="unprotected_model_key_ancestor"):
        source.read_token()


@pytest.mark.parametrize(
    "bad", ["relative/model.key", "/somewhere/../model.key", "/somewhere/other.key"]
)
def test_only_fixed_absolute_model_key_path_is_accepted(bad):
    with pytest.raises(ModelAuthError):
        ModelKeySource(bad, 12001, lambda: None)
