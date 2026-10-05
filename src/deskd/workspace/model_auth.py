"""Fixed provider-auth bridge; model keys never enter MCP, events or arguments.

Only an explicitly configured gateway service may read its operator-installed
model.key. The caller-supplied authorization callback must establish active
service generation, the protected API-provider configuration, and installation
attestation *before* the file is opened. This module never discovers credentials
or creates/imports a key. The official runtime captures helper stdout privately.

The unsandboxed official harness necessarily receives and caches its model token
in memory. It is part of the trusted computing base; role commands must be
sandboxed from its sockets, files, process memory and network. This helper is
not a replacement for the kernel acceptance tests or OS administrator trust.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import socket
import stat
import struct
import time
from typing import BinaryIO, Callable
import uuid

from deskd.gateway.wire import decode_frame, encode_frame

MAX_TOKEN_BYTES = 4096
MAX_AUTH_RESPONSE = 8192
_TOKEN = re.compile(rb"[A-Za-z0-9._~+/-]+={0,8}\Z")


class ModelAuthError(ValueError):
    """Stable nonsecret error. Never attach upstream errors or token material."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _uid(value: int) -> int:
    if type(value) is not int or not 0 < value < 2**32 - 1:
        raise ModelAuthError("invalid_model_service_uid")
    return value


def _path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
        raise ModelAuthError("invalid_model_auth_path")
    return path


def _token(data: bytes) -> str:
    # One optional line terminator is useful for a manually provisioned file;
    # whitespace, interior newlines and arbitrary header bytes are not tokens.
    if data.endswith(b"\r\n"):
        data = data[:-2]
    elif data.endswith(b"\n"):
        data = data[:-1]
    if not 1 <= len(data) <= MAX_TOKEN_BYTES or _TOKEN.fullmatch(data) is None:
        raise ModelAuthError("invalid_model_key")
    return data.decode("ascii")


def _directory(info, path: Path, gateway_uid: int, *, final: bool) -> None:
    """Metadata-only policy check, kept separate for rootless unit modeling."""
    if not stat.S_ISDIR(info.st_mode):
        raise ModelAuthError("unprotected_model_key_directory")
    if final:
        if info.st_uid != gateway_uid or stat.S_IMODE(info.st_mode) != 0o700:
            raise ModelAuthError("unprotected_model_key_directory")
    elif info.st_uid != 0 or info.st_mode & 0o022:
        raise ModelAuthError("unprotected_model_key_ancestor")


class ModelKeySource:
    """Fixed gateway-owned file; authenticating the harness is transport's job.

    The explicit gateway UID must also be the current effective UID. File
    descriptors bind validation and reading to the same inode. No symlink,
    hardlink, FIFO, writable ancestor, alternate key path or file content is
    accepted through a model request.
    """

    def __init__(
        self, path: str | Path, gateway_uid: int, authorize: Callable[[], None]
    ):
        self.path = _path(path)
        self.gateway_uid = _uid(gateway_uid)
        if self.path.name != "model.key" or not callable(authorize):
            raise ModelAuthError("invalid_model_auth_configuration")
        self._authorize = authorize

    def read_token(self) -> str:
        if os.geteuid() != self.gateway_uid:
            raise ModelAuthError("wrong_model_key_service_uid")
        try:
            authorized = self._authorize()
        except Exception:
            raise ModelAuthError("model_auth_not_ready") from None
        if authorized is not None:
            raise ModelAuthError("model_auth_not_ready")
        directory_fd = None
        key_fd = None
        try:
            directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            _directory(os.fstat(directory_fd), Path("/"), self.gateway_uid, final=False)
            current = Path("/")
            parts = self.path.parent.parts[1:]
            if not parts:
                raise ModelAuthError("unprotected_model_key_directory")
            for index, component in enumerate(parts):
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = next_fd
                current /= component
                _directory(
                    os.fstat(directory_fd),
                    current,
                    self.gateway_uid,
                    final=index == len(parts) - 1,
                )
            key_fd = os.open(
                self.path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
            before = os.fstat(key_fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != self.gateway_uid
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_nlink != 1
                or not 1 <= before.st_size <= MAX_TOKEN_BYTES + 2
            ):
                raise ModelAuthError("unprotected_model_key")
            chunks = bytearray()
            while len(chunks) <= MAX_TOKEN_BYTES + 2:
                chunk = os.read(key_fd, MAX_TOKEN_BYTES + 3 - len(chunks))
                if not chunk:
                    break
                chunks.extend(chunk)
            after = os.fstat(key_fd)
            attributes = (
                "st_dev",
                "st_ino",
                "st_uid",
                "st_mode",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if any(
                getattr(before, name) != getattr(after, name) for name in attributes
            ):
                raise ModelAuthError("model_key_changed_during_read")
            return _token(bytes(chunks))
        except ModelAuthError:
            raise
        except (OSError, ValueError, OverflowError):
            raise ModelAuthError("model_key_unavailable") from None
        finally:
            if key_fd is not None:
                os.close(key_fd)
            if directory_fd is not None:
                os.close(directory_fd)


def fetch_token(
    socket_path: str | Path, gateway_uid: int, *, timeout: float = 3
) -> str:
    """Single bounded request from the trusted provider helper to the gateway.

    The key and model endpoint are never request parameters. The UID and socket
    path are fixed by a protected auth-command configuration. Responses are not
    persisted and an error never copies any gateway-provided text.
    """
    path = _path(socket_path)
    uid = _uid(gateway_uid)
    if uid == os.geteuid() or not hasattr(socket, "SO_PEERCRED"):
        raise ModelAuthError("distinct_model_gateway_required")
    if type(timeout) not in (int, float) or not 0 < timeout <= 5:
        raise ModelAuthError("invalid_model_auth_timeout")
    deadline = time.monotonic() + timeout
    request_id = uuid.uuid4().hex
    request = encode_frame({"id": request_id, "method": "model.auth", "params": {}})
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(path))
            pid, peer_uid, _ = struct.unpack(
                "3i",
                connection.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
                ),
            )
            if peer_uid != uid or pid <= 0:
                raise ModelAuthError("untrusted_model_gateway")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModelAuthError("model_auth_unavailable")
            connection.settimeout(remaining)
            connection.sendall(request)
            data = bytearray()
            while b"\n" not in data:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or len(data) > MAX_AUTH_RESPONSE:
                    raise ModelAuthError("model_auth_unavailable")
                connection.settimeout(remaining)
                chunk = connection.recv(min(4096, MAX_AUTH_RESPONSE + 1 - len(data)))
                if not chunk:
                    raise ModelAuthError("model_auth_unavailable")
                data.extend(chunk)
            if (
                len(data) > MAX_AUTH_RESPONSE
                or data.count(b"\n") != 1
                or not data.endswith(b"\n")
            ):
                raise ModelAuthError("invalid_model_auth_response")
            reply = decode_frame(bytes(data[:-1]))
            if reply.get("id") != request_id or type(reply.get("ok")) is not bool:
                raise ModelAuthError("invalid_model_auth_response")
            if set(reply) != {"id", "ok", "result" if reply["ok"] else "error"}:
                raise ModelAuthError("invalid_model_auth_response")
            if not reply["ok"]:
                raise ModelAuthError("model_auth_not_ready")
            result = reply["result"]
            if (
                type(result) is not dict
                or set(result) != {"token"}
                or type(result["token"]) is not str
            ):
                raise ModelAuthError("invalid_model_auth_response")
            # JSON permits Unicode and escapes; the provider bearer header does
            # not. Revalidate the exact token after the transport envelope.
            return _token(result["token"].encode("ascii"))
    except ModelAuthError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise ModelAuthError("model_auth_unavailable") from None


def helper_main(socket_path: str | Path, gateway_uid: int, output: BinaryIO) -> int:
    """Entry point for a fixed executable launched only by official provider auth.

    This stdout is the official runtime's private child-process pipe. On failure
    emit nothing to either output stream: official Codex includes stderr in its
    error diagnostics, so even upstream exception text must not be echoed.
    """
    try:
        token = fetch_token(socket_path, gateway_uid)
        payload = token.encode("ascii") + b"\n"
        if output.write(payload) != len(payload):
            return 1
        output.flush()
        return 0
    except Exception:
        return 1
