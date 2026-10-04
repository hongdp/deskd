"""Bounded JSON-lines framing shared by the local gateway and its fixed bridge."""

from __future__ import annotations

import json
import socket
import time
from typing import Any

from .events import EventValidationError, canonical_json

MAX_FRAME_BYTES = 262_144
MAX_RESPONSE_BYTES = 1_052_672


class WireError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WireError("duplicate_json_key")
        result[key] = value
    return result


def decode_frame(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_FRAME_BYTES:
        raise WireError("frame_too_large")

    def constant(_: str) -> None:
        raise WireError("invalid_json_number")

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=constant
        )
        canonical_json(value)
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        EventValidationError,
    ) as exc:
        raise WireError("invalid_json") from exc
    except ValueError as exc:
        if isinstance(exc, WireError):
            raise
        raise WireError("invalid_json") from exc
    if type(value) is not dict:
        raise WireError("request_must_be_object")
    return value


def encode_frame(value: dict[str, Any], *, response: bool = False) -> bytes:
    if response:
        # A persisted receipt can itself occupy the storage JSON size/depth
        # budget. The transport envelope adds a small, separately bounded layer.
        if type(value) is not dict or any(type(key) is not str for key in value):
            raise WireError("invalid_response")
        encoded = (
            "{"
            + ",".join(
                canonical_json(key) + ":" + canonical_json(value[key])
                for key in sorted(value)
            )
            + "}"
        ).encode("utf-8")
    else:
        encoded = canonical_json(value).encode("utf-8")
    if len(encoded) > (MAX_RESPONSE_BYTES if response else MAX_FRAME_BYTES):
        raise WireError("response_too_large" if response else "frame_too_large")
    return encoded + b"\n"


class JsonLineReader:
    """One bounded buffer; partial-frame timeout cannot be reset by trickling."""

    def __init__(
        self,
        connection: socket.socket,
        *,
        idle_timeout: float = 300,
        frame_timeout: float = 5,
    ):
        self.connection = connection
        self.idle_timeout = idle_timeout
        self.frame_timeout = frame_timeout
        self.buffer = bytearray()

    def read(self) -> dict[str, Any] | None:
        deadline = time.monotonic() + self.frame_timeout if self.buffer else None
        while True:
            end = self.buffer.find(b"\n")
            if end >= 0:
                if end > MAX_FRAME_BYTES:
                    raise WireError("frame_too_large")
                raw = bytes(self.buffer[:end])
                del self.buffer[: end + 1]
                return decode_frame(raw)
            if len(self.buffer) > MAX_FRAME_BYTES:
                raise WireError("frame_too_large")
            remaining = (
                self.idle_timeout if deadline is None else deadline - time.monotonic()
            )
            if remaining <= 0:
                raise WireError("frame_timeout")
            self.connection.settimeout(remaining)
            try:
                chunk = self.connection.recv(
                    min(4096, MAX_FRAME_BYTES + 1 - len(self.buffer))
                )
            except TimeoutError as exc:
                raise WireError(
                    "idle_timeout" if deadline is None else "frame_timeout"
                ) from exc
            if not chunk:
                if self.buffer:
                    raise WireError("incomplete_frame")
                return None
            if deadline is None:
                deadline = time.monotonic() + self.frame_timeout
            self.buffer.extend(chunk)
