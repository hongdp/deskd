"""Fixed stdio MCP bridge to the credential-free gateway Unix socket.

The bridge runs in the trusted harness domain, outside role shells. It never
executes commands, discovers tools dynamically, or forwards arbitrary upstream
calls. Top-level metadata must be supplied by the trusted harness; metadata is
not a signature and this bridge does not make an unsandboxed caller trustworthy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import struct
import time
from typing import Any, BinaryIO, Callable
import uuid

from .events import canonical_json
from .identity import identifier, parse_metadata
from .wire import MAX_FRAME_BYTES, MAX_RESPONSE_BYTES

MAX_MCP_RESPONSE_BYTES = 3 * MAX_RESPONSE_BYTES
PROTOCOL_VERSION = "2025-06-18"


class BridgeError(ValueError):
    pass


def _decode(raw: bytes, *, response: bool = False) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise BridgeError("duplicate_json_key")
            result[name] = value
        return result

    value = json.loads(raw, object_pairs_hook=pairs)
    if type(value) is not dict:
        raise BridgeError("invalid_message")
    if response:
        if len(raw) > MAX_RESPONSE_BYTES + 1:
            raise BridgeError("response_too_large")
        # The wire envelope wraps an independently bounded stored receipt.
        for member in value.values():
            canonical_json(member)
    else:
        canonical_json(value)  # finite, bounded JSON; no implicit coercion
    return value


def _serve_stdio(
    source: BinaryIO,
    target: BinaryIO,
    rpc: Callable[[str, dict[str, Any]], dict[str, Any]],
    catalog: list[dict[str, Any]],
    *,
    identify: bool = False,
) -> None:
    """Protocol loop over an already authenticated gateway connection."""
    initialized = ready = False

    def send(message):
        # Tool text contains an encoded receipt. Escaping it again can more
        # than double the gateway frame size. Output limits must be separate
        # from input limits; an output failure must never imply no commit.
        try:
            encoded = (
                json.dumps(
                    message, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode()
                + b"\n"
            )
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise OSError("unencodable_mcp_response") from exc
        if len(encoded) > MAX_MCP_RESPONSE_BYTES:
            raise OSError("response_too_large")
        target.write(encoded)
        target.flush()

    while True:
        raw = source.readline(MAX_FRAME_BYTES + 1)
        if not raw:
            return
        if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
            send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "invalid_frame"},
                }
            )
            return
        mid = None
        try:
            message = _decode(raw)
            mid = message.get("id")
            if (
                message.get("jsonrpc") != "2.0"
                or type(message.get("method")) is not str
                or (
                    "id" in message
                    and (type(mid) not in (str, int) or len(str(mid)) > 256)
                )
            ):
                raise BridgeError("invalid_request")
            method, params = message["method"], message.get("params", {})
            if type(params) is not dict:
                raise BridgeError("invalid_params")
            if "id" not in message:
                if method == "notifications/initialized" and initialized:
                    ready = True
                # Notifications never execute business actions.
                continue
            if method == "ping":
                result = {}
            elif method == "initialize" and not initialized:
                if (
                    type(params.get("protocolVersion")) is not str
                    or type(params.get("capabilities")) is not dict
                    or type(params.get("clientInfo")) is not dict
                ):
                    raise BridgeError("invalid_initialize")
                initialized = True
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "deskd", "version": "0.1-experimental"},
                }
            elif not ready:
                raise BridgeError("not_initialized")
            elif method == "tools/list":
                if set(params) - {"_meta"}:
                    raise BridgeError("unsupported_tool_cursor")
                result = {
                    "tools": [
                        {k: v for k, v in t.items() if k != "_deskd_read"}
                        for t in catalog
                    ]
                }
            elif method == "tools/call":
                if (
                    set(params) != {"name", "arguments", "_meta"}
                    or type(params["name"]) is not str
                    or type(params["arguments"]) is not dict
                ):
                    raise BridgeError("invalid_tool_params")
                parse_metadata(params)
                if params["name"] not in {tool["name"] for tool in catalog}:
                    raise BridgeError("unknown_tool")
                arguments = dict(params["arguments"])
                request_id = identifier(arguments.pop("request_id", None), "request_id")
                if identify:
                    deadline = time.monotonic() + 5
                    while True:
                        observation = rpc("identify", {"_meta": params["_meta"]})
                        if observation.get("ok") is not True:
                            break
                        if observation.get("result", {}).get("ready") is True:
                            break
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.05)
                # No client supplied transport/generation fields are forwarded.
                payload = {
                    "request_id": request_id,
                    "mcp": {
                        "name": params["name"],
                        "arguments": arguments,
                        "_meta": params["_meta"],
                    },
                }
                # This dispatch flag comes from the installed catalog, never
                # from an MCP caller. The gateway still authenticates the read.
                read_only = any(
                    t["name"] == params["name"] and t.get("_deskd_read") is True
                    for t in catalog
                )
                reply = (
                    rpc("read", payload["mcp"])
                    if read_only
                    else rpc("execute", payload)
                )
                if reply.get("ok") is not True and reply.get("error", {}).get(
                    "code"
                ) in {"response_encoding_error", "storage_error", "internal_error"}:
                    raise OSError("gateway_outcome_unknown")
                try:
                    receipt = canonical_json(
                        reply.get("result")
                        if reply.get("ok") is True
                        else {"error": reply.get("error", {"code": "gateway_rejected"})}
                    )
                except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
                    raise OSError("invalid_gateway_result") from exc
                result = {
                    "content": [{"type": "text", "text": receipt}],
                    "isError": reply.get("ok") is not True,
                }
            else:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "error": {"code": -32601, "message": "method_not_found"},
                    }
                )
                continue
            send({"jsonrpc": "2.0", "id": mid, "result": result})
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            safe_id = mid if type(mid) in (str, int) and len(str(mid)) <= 256 else None
            send(
                {
                    "jsonrpc": "2.0",
                    "id": safe_id,
                    "error": {"code": -32602, "message": "invalid_request"},
                }
            )
        except OSError:
            # Outcome may be unknown after the gateway has committed. Never
            # reconnect and repeat a command automatically.
            send(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {
                        "code": -32603,
                        "message": "gateway_unavailable_do_not_resubmit",
                    },
                }
            )
            return


def run_bridge(
    socket_path: Path | str,
    *,
    gateway_uid: int,
    source: BinaryIO,
    target: BinaryIO,
    catalog: list[dict[str, Any]] | None = None,
    identify: bool = False,
) -> None:
    """Connect once; authenticate the server UID before sending any tool data."""
    if (
        not hasattr(socket, "SO_PEERCRED")
        or type(gateway_uid) is not int
        or gateway_uid <= 0
        or gateway_uid == os.geteuid()
    ):
        raise BridgeError("distinct_gateway_uid_required")
    from .actions import tool_catalog

    catalog = (
        json.loads(canonical_json(catalog)) if catalog is not None else tool_catalog()
    )
    for tool in catalog:
        schema = tool["inputSchema"]
        schema["properties"]["request_id"] = {
            "type": "string",
            "maxLength": 256,
            "description": "Stable ID for retries of this exact action.",
        }
        schema["required"] = [*schema.get("required", []), "request_id"]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(30)
        sock.connect(os.fspath(socket_path))
        _, peer_uid, _ = struct.unpack(
            "3i",
            sock.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            ),
        )
        if peer_uid != gateway_uid:
            raise BridgeError("untrusted_gateway")
        with sock.makefile("rb", buffering=0) as stream:

            def rpc(method, params):
                mid = uuid.uuid4().hex
                request = (
                    canonical_json(
                        {"id": mid, "method": method, "params": params}
                    ).encode()
                    + b"\n"
                )
                if len(request) > MAX_FRAME_BYTES:
                    raise BridgeError("request_too_large")
                sock.sendall(request)
                raw = stream.readline(MAX_RESPONSE_BYTES + 2)
                if (
                    not raw
                    or len(raw) > MAX_RESPONSE_BYTES + 1
                    or not raw.endswith(b"\n")
                ):
                    raise OSError("invalid_gateway_frame")
                try:
                    reply = _decode(raw, response=True)
                except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
                    raise OSError("invalid_gateway_response") from exc
                if reply.get("id") != mid or type(reply.get("ok")) is not bool:
                    raise OSError("invalid_gateway_response")
                expected = {"id", "ok", "result" if reply["ok"] else "error"}
                if set(reply) != expected:
                    raise OSError("invalid_gateway_response")
                if not reply["ok"]:
                    error = reply["error"]
                    if (
                        type(error) is not dict
                        or set(error) != {"code"}
                        or type(error["code"]) is not str
                    ):
                        raise OSError("invalid_gateway_response")
                return reply

            if rpc("hello", {}).get("ok") is not True:
                raise BridgeError("gateway_rejected_connection")
            _serve_stdio(source, target, rpc, catalog, identify=identify)
