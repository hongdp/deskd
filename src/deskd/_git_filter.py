"""Trusted no-op Git filter-process used by the workspace broker.

This file is executed by absolute path, never imported from a leased worktree.
It implements only Git filter protocol v2 clean/smudge pass-through.  Keeping
the helper self-contained avoids Python's current-directory import path turning
an agent-created ``deskd`` package into code execution.
"""

from __future__ import annotations

import sys
import tempfile
from typing import BinaryIO

_FLUSH = object()
_EOF = object()
_MAX_PACKET_DATA = 65_516


def _exact(stream: BinaryIO, size: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            return None
        chunks.extend(chunk)
    return bytes(chunks)


def _read_packet(stream: BinaryIO):
    header = _exact(stream, 4)
    if header is None:
        return _EOF
    try:
        size = int(header, 16)
    except ValueError as exc:
        raise RuntimeError("invalid pkt-line header") from exc
    if size == 0:
        return _FLUSH
    if size < 4 or size > 65_520:
        raise RuntimeError("invalid pkt-line size")
    data = _exact(stream, size - 4)
    if data is None:
        raise RuntimeError("truncated pkt-line")
    return data


def _read_list(stream: BinaryIO) -> list[bytes] | None:
    out: list[bytes] = []
    while True:
        packet = _read_packet(stream)
        if packet is _EOF:
            if out:
                raise RuntimeError("truncated pkt-line list")
            return None
        if packet is _FLUSH:
            return out
        out.append(packet)


def _write_packet(stream: BinaryIO, data: bytes) -> None:
    if len(data) > _MAX_PACKET_DATA:
        raise RuntimeError("pkt-line data is too large")
    stream.write(f"{len(data) + 4:04x}".encode("ascii"))
    stream.write(data)


def _text(data: bytes) -> bytes:
    if not data.endswith(b"\n"):
        raise RuntimeError("Git filter text packet lacks LF")
    return data[:-1]


def _write_text(stream: BinaryIO, data: bytes) -> None:
    _write_packet(stream, data + b"\n")


def _flush(stream: BinaryIO) -> None:
    stream.write(b"0000")


def _serve(source: BinaryIO, sink: BinaryIO) -> int:
    welcome = _read_packet(source)
    versions = _read_list(source)
    if (welcome in {_EOF, _FLUSH} or versions is None
            or _text(welcome) != b"git-filter-client"
            or b"version=2" not in {_text(item) for item in versions}):
        raise RuntimeError("unsupported Git filter handshake")
    _write_text(sink, b"git-filter-server")
    _write_text(sink, b"version=2")
    _flush(sink)
    sink.flush()

    capabilities = _read_list(source)
    if capabilities is None:
        raise RuntimeError("missing Git filter capabilities")
    offered = {_text(item) for item in capabilities}
    for capability in (b"capability=clean", b"capability=smudge"):
        if capability in offered:
            _write_text(sink, capability)
    _flush(sink)
    sink.flush()

    while True:
        headers = _read_list(source)
        if headers is None:
            return 0
        text_headers = [_text(item) for item in headers]
        command = next((item.split(b"=", 1)[1] for item in text_headers
                        if item.startswith(b"command=") and b"=" in item), None)
        if command not in {b"clean", b"smudge"}:
            raise RuntimeError("unsupported Git filter command")
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as content:
            while True:
                packet = _read_packet(source)
                if packet is _EOF:
                    raise RuntimeError("truncated Git filter content")
                if packet is _FLUSH:
                    break
                content.write(packet)

            _write_text(sink, b"status=success")
            _flush(sink)
            content.seek(0)
            while chunk := content.read(_MAX_PACKET_DATA):
                _write_packet(sink, chunk)
            _flush(sink)
            _flush(sink)
            sink.flush()


def main() -> int:
    try:
        return _serve(sys.stdin.buffer, sys.stdout.buffer)
    except (BrokenPipeError, RuntimeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
