"""A bounded Responses-to-Gemini bridge owned by the credential gateway.

Only the trusted harness receives a process-local bearer capability. Google
credentials stay behind ``key_source``; this module has no credential discovery,
environment configuration, request logging, redirect or proxy support. Custom
providers must disable request compression. Unsupported wire features fail
explicitly instead of silently changing the agent's available tools.

``upstream`` is an in-process test seam, never installation configuration. It
accepts ``(model, translated_body, key)`` and returns a closeable iterator of
Gemini JSON chunks. The production transport always uses the fixed Google host.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import re
import secrets
import select
import socket
from socketserver import ThreadingMixIn
import threading
import time
from typing import Callable
import uuid

MAX_BODY = 2 * 1024 * 1024
MAX_STREAM = 16 * 1024 * 1024
MAX_EVENT = 1024 * 1024
MAX_CHUNKS = 10000
MAX_CONNECTIONS = 4
IO_TIMEOUT = 10.0
TURN_TIMEOUT = 120.0
GOOGLE_HOST = "generativelanguage.googleapis.com"
_MODEL = re.compile(r"gemini-[A-Za-z0-9][A-Za-z0-9._-]{0,100}\Z")
_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")
_KEY = re.compile(r"[A-Za-z0-9._~+/-]{1,4096}={0,8}\Z")
_BLOB = "deskd-gemini-v1:"
_CODES = frozenset(
    {
        "invalid_gemini_configuration",
        "gemini_not_ready",
        "gemini_not_authorized",
        "gemini_port_unavailable",
        "gemini_invalid_request",
        "gemini_wrong_model",
        "gemini_unsupported_encoding",
        "gemini_request_too_large",
        "gemini_unsupported_tool",
        "gemini_unsupported_input",
        "gemini_invalid_replay",
        "gemini_upstream_failed",
        "gemini_invalid_response",
        "gemini_stream_limit",
        "gemini_stream_incomplete",
        "gemini_blocked",
        "gemini_timeout",
    }
)


class GeminiError(ValueError):
    """Stable, nonsecret errors only; foreign exception messages are discarded."""

    def __init__(self, code: str):
        self.code = code if code in _CODES else "gemini_upstream_failed"
        super().__init__(self.code)


def _json(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def _load(data: str | bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise GeminiError("gemini_invalid_request")
            result[key] = value
        return result

    def constant(_value):
        raise GeminiError("gemini_invalid_request")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise GeminiError("gemini_invalid_request") from None


def _name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise GeminiError("gemini_unsupported_tool")
    return value


def _alias(namespace: str | None, name: str) -> str:
    # Encoding the complete identity avoids colliding inner names or separators.
    return "f_" + hashlib.sha256(_json([namespace, name])).hexdigest()[:40]


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise GeminiError("gemini_unsupported_input")
    texts = []
    for item in value:
        if (
            not isinstance(item, dict)
            or item.get("type") not in {"input_text", "output_text", "text"}
            or not isinstance(item.get("text"), str)
        ):
            raise GeminiError("gemini_unsupported_input")
        texts.append(item["text"])
    return "\n".join(texts)


@dataclass(frozen=True)
class _Tool:
    name: str
    namespace: str | None
    custom: bool


@dataclass
class _Translation:
    body: dict
    tools: dict[str, _Tool]


def translate_request(request: dict) -> _Translation:
    """Translate supported text/function history, preserving exact tool identity."""
    if not isinstance(request, dict) or request.get("stream") is not True:
        raise GeminiError("gemini_invalid_request")
    if request.get("previous_response_id") is not None or request.get("background"):
        raise GeminiError("gemini_unsupported_input")
    # Endpoint routing is installation policy, never part of a model request.
    if any(key in request for key in ("endpoint", "base_url", "url", "api_key")):
        raise GeminiError("gemini_invalid_request")
    declarations = []
    tools: dict[str, _Tool] = {}
    listed = request.get("tools", [])
    if not isinstance(listed, list) or len(listed) > 256:
        raise GeminiError("gemini_unsupported_tool")

    def add(tool, namespace=None):
        if not isinstance(tool, dict):
            raise GeminiError("gemini_unsupported_tool")
        kind = tool.get("type")
        if kind == "namespace" and namespace is None:
            ns = _name(tool.get("name"))
            children = tool.get("tools")
            if not isinstance(children, list) or len(children) > 256:
                raise GeminiError("gemini_unsupported_tool")
            for child in children:
                add(child, ns)
            return
        name = _name(tool.get("name"))
        custom = kind == "custom" and name == "apply_patch"
        if kind != "function" and not custom:
            raise GeminiError("gemini_unsupported_tool")
        alias = _alias(namespace, name)
        if alias in tools or len(tools) >= 256:
            raise GeminiError("gemini_unsupported_tool")
        description = tool.get("description", "")
        if not isinstance(description, str):
            raise GeminiError("gemini_unsupported_tool")
        if custom:
            parameters = {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
                "additionalProperties": False,
            }
            description += " Supply the complete raw apply_patch text in input."
        else:
            parameters = tool.get("parameters", {"type": "object", "properties": {}})
            if not isinstance(parameters, dict):
                raise GeminiError("gemini_unsupported_tool")
        declarations.append(
            {
                "name": alias,
                "description": description,
                "parametersJsonSchema": parameters,
            }
        )
        tools[alias] = _Tool(name, namespace, custom)

    for tool in listed:
        add(tool)
    contents: list[dict] = []
    visible: list[tuple[dict, int]] = []
    call_names: dict[str, str] = {}
    system = []
    instructions = request.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, str):
            raise GeminiError("gemini_invalid_request")
        system.append(instructions)

    def push(role, part, *, is_visible=False):
        if contents and contents[-1]["role"] == role:
            entry = contents[-1]
        else:
            entry = {"role": role, "parts": []}
            contents.append(entry)
        entry["parts"].append(part)
        if is_visible:
            visible.append((entry, len(entry["parts"])))

    items = request.get("input", [])
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    if not isinstance(items, list):
        raise GeminiError("gemini_unsupported_input")
    for item in items:
        if not isinstance(item, dict):
            raise GeminiError("gemini_unsupported_input")
        kind = item.get("type", "message")
        if kind == "message":
            role = item.get("role")
            text = _text(item.get("content"))
            if role in {"system", "developer"}:
                system.append(text)
            elif role in {"user", "assistant"}:
                push(
                    "model" if role == "assistant" else "user",
                    {"text": text},
                    is_visible=role == "assistant",
                )
            else:
                raise GeminiError("gemini_unsupported_input")
        elif kind in {"function_call", "custom_tool_call"}:
            namespace = item.get("namespace")
            if namespace is not None:
                _name(namespace)
            alias = _alias(namespace, _name(item.get("name")))
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id or call_id in call_names:
                raise GeminiError("gemini_invalid_replay")
            if kind == "custom_tool_call":
                if item["name"] != "apply_patch" or not isinstance(
                    item.get("input"), str
                ):
                    raise GeminiError("gemini_unsupported_input")
                args = {"input": item["input"]}
            else:
                args = _load(item.get("arguments"))
                if not isinstance(args, dict):
                    raise GeminiError("gemini_invalid_replay")
            call_names[call_id] = alias
            push(
                "model",
                {"functionCall": {"name": alias, "args": args}},
                is_visible=True,
            )
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            alias = call_names.get(item.get("call_id"))
            if alias is None:
                raise GeminiError("gemini_invalid_replay")
            output = item.get("output")
            if isinstance(output, dict) and "content" in output:
                output = output["content"]
            text = _text(output)
            push(
                "user",
                {"functionResponse": {"name": alias, "response": {"output": text}}},
            )
        elif kind == "reasoning":
            blob = item.get("encrypted_content")
            if blob is None:
                continue  # Reasoning summaries are display text, not model history.
            if not isinstance(blob, str) or not blob.startswith(_BLOB):
                raise GeminiError("gemini_invalid_replay")
            try:
                replay = _load(base64.b64decode(blob[len(_BLOB) :], validate=True))
            except (ValueError, TypeError):
                raise GeminiError("gemini_invalid_replay") from None
            if (
                not isinstance(replay, dict)
                or set(replay) != {"v", "covers_prev", "parts"}
                or replay["v"] != 1
                or type(replay["covers_prev"]) is not int
                or replay["covers_prev"] not in (0, 1)
                or not isinstance(replay["parts"], list)
                or not replay["parts"]
            ):
                raise GeminiError("gemini_invalid_replay")
            if replay["covers_prev"]:
                if not visible or not contents or visible[-1][0] is not contents[-1]:
                    raise GeminiError("gemini_invalid_replay")
                entry, length = visible.pop()
                if entry["role"] != "model" or len(entry["parts"]) != length:
                    raise GeminiError("gemini_invalid_replay")
                entry["parts"].pop()
            for part in replay["parts"]:
                if not isinstance(part, dict) or set(part) - {
                    "text",
                    "thoughtSignature",
                    "functionCall",
                }:
                    raise GeminiError("gemini_invalid_replay")
                if "thoughtSignature" in part and not isinstance(
                    part["thoughtSignature"], str
                ):
                    raise GeminiError("gemini_invalid_replay")
                if "functionCall" in part:
                    call = part["functionCall"]
                    if (
                        not isinstance(call, dict)
                        or set(call) - {"name", "args", "id"}
                        or call.get("name") not in call_names.values()
                        or not isinstance(call.get("args"), dict)
                    ):
                        raise GeminiError("gemini_invalid_replay")
                elif not isinstance(part.get("text"), str):
                    raise GeminiError("gemini_invalid_replay")
                push("model", part)
        else:
            raise GeminiError("gemini_unsupported_input")
    contents = [entry for entry in contents if entry["parts"]]
    body = {"contents": contents or [{"role": "user", "parts": [{"text": ""}]}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
    if declarations:
        body["tools"] = [{"functionDeclarations": declarations}]
    choice = request.get("tool_choice", "auto")
    modes = {"auto": "AUTO", "required": "ANY", "none": "NONE"}
    if isinstance(choice, str) and choice in modes:
        if declarations:
            body["toolConfig"] = {"functionCallingConfig": {"mode": modes[choice]}}
        elif choice == "required":
            raise GeminiError("gemini_unsupported_tool")
    else:
        raise GeminiError("gemini_unsupported_tool")
    reasoning = request.get("reasoning") or {}
    if not isinstance(reasoning, dict):
        raise GeminiError("gemini_invalid_request")
    effort = reasoning.get("effort")
    thinking = {"includeThoughts": True}
    if effort is not None:
        # Gemini 3.8 Flash exposes low/medium/high, with no thinking-off mode.
        # Codex's minimal is its lowest available level; none is unsupported.
        levels = {
            "minimal": "low",
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "high",
        }
        if effort not in levels:
            raise GeminiError("gemini_invalid_request")
        thinking["thinkingLevel"] = levels[effort]
    generation = {"thinkingConfig": thinking}
    if "max_output_tokens" in request:
        limit = request["max_output_tokens"]
        if type(limit) is not int or not 1 <= limit <= 65536:
            raise GeminiError("gemini_invalid_request")
        generation["maxOutputTokens"] = limit
    body["generationConfig"] = generation
    if len(_json(body)) > MAX_BODY:
        raise GeminiError("gemini_request_too_large")
    return _Translation(body, tools)


class _Translator:
    def __init__(self, tools, emit):
        self.tools = tools
        self.emit = emit
        self.id = "resp_" + uuid.uuid4().hex
        self.sequence = 0
        self.index = 0
        self.open_kind = None
        self.item_id = None
        self.text = ""
        self.raw_parts = []
        self.output = []
        self.reason = None
        self.usage = {}
        self.terminal = False

    def event(self, kind, **fields):
        self.sequence += 1
        self.emit({"type": kind, "sequence_number": self.sequence, **fields})

    def created(self):
        self.event(
            "response.created",
            response={
                "id": self.id,
                "object": "response",
                "status": "in_progress",
                "output": [],
            },
        )

    def item(self, item):
        self.event("response.output_item.added", output_index=self.index, item=item)
        self.event("response.output_item.done", output_index=self.index, item=item)
        self.output.append(item)
        self.index += 1

    def blob(self, parts, covers):
        blob = base64.b64encode(
            _json({"v": 1, "covers_prev": covers, "parts": parts})
        ).decode()
        self.item(
            {
                "type": "reasoning",
                "id": "rs_" + uuid.uuid4().hex,
                "summary": [],
                "encrypted_content": _BLOB + blob,
            }
        )

    def close_item(self):
        if self.open_kind is None:
            return
        common = {"item_id": self.item_id, "output_index": self.index}
        if self.open_kind == "reasoning":
            self.event(
                "response.reasoning_summary_text.done",
                **common,
                summary_index=0,
                text=self.text,
            )
            self.event(
                "response.reasoning_summary_part.done",
                **common,
                summary_index=0,
                part={"type": "summary_text", "text": self.text},
            )
            item = {
                "type": "reasoning",
                "id": self.item_id,
                "summary": [{"type": "summary_text", "text": self.text}],
            }
        else:
            self.event(
                "response.output_text.done", **common, content_index=0, text=self.text
            )
            part = {"type": "output_text", "text": self.text, "annotations": []}
            self.event(
                "response.content_part.done", **common, content_index=0, part=part
            )
            item = {
                "type": "message",
                "id": self.item_id,
                "role": "assistant",
                "status": "completed",
                "content": [part],
            }
        self.event("response.output_item.done", output_index=self.index, item=item)
        self.output.append(item)
        self.index += 1
        parts = self.raw_parts
        kind = self.open_kind
        self.open_kind = None
        self.raw_parts = []
        if any("thoughtSignature" in part for part in parts):
            if kind == "reasoning":
                parts = [
                    {"text": "", "thoughtSignature": part["thoughtSignature"]}
                    for part in parts
                    if "thoughtSignature" in part
                ]
            self.blob(parts, 0 if kind == "reasoning" else 1)

    def part(self, part):
        if not isinstance(part, dict):
            raise GeminiError("gemini_invalid_response")
        if set(part) - {"text", "thought", "thoughtSignature", "functionCall"}:
            raise GeminiError("gemini_invalid_response")
        signature = part.get("thoughtSignature")
        if signature is not None and not isinstance(signature, str):
            raise GeminiError("gemini_invalid_response")
        if "functionCall" in part:
            self.close_item()
            call = part["functionCall"]
            if not isinstance(call, dict) or set(call) - {"name", "args", "id"}:
                raise GeminiError("gemini_invalid_response")
            tool = self.tools.get(call.get("name"))
            args = call.get("args", {})
            if tool is None or not isinstance(args, dict):
                raise GeminiError("gemini_invalid_response")
            item = {
                "type": "custom_tool_call" if tool.custom else "function_call",
                "id": "fc_" + uuid.uuid4().hex,
                "call_id": "call_" + uuid.uuid4().hex,
                "name": tool.name,
                "status": "completed",
            }
            if tool.namespace is not None:
                item["namespace"] = tool.namespace
            if tool.custom:
                if set(args) != {"input"} or not isinstance(args["input"], str):
                    raise GeminiError("gemini_invalid_response")
                item["input"] = args["input"]
            else:
                item.update(arguments=_json(args).decode(), encrypted_function_args=[])
            self.item(item)
            if signature is not None or "id" in call:
                replay = {"functionCall": call}
                if signature is not None:
                    replay["thoughtSignature"] = signature
                self.blob([replay], 1)
            return
        text = part.get("text", "")
        if not isinstance(text, str) or (
            "thought" in part and type(part["thought"]) is not bool
        ):
            raise GeminiError("gemini_invalid_response")
        if not text:
            if signature is not None:
                self.close_item()
                self.blob([{"text": "", "thoughtSignature": signature}], 0)
            return
        kind = "reasoning" if part.get("thought") else "message"
        if self.open_kind != kind:
            self.close_item()
            self.open_kind = kind
            self.text = ""
            self.item_id = ("rs_" if kind == "reasoning" else "msg_") + uuid.uuid4().hex
            if kind == "reasoning":
                item = {"type": "reasoning", "id": self.item_id, "summary": []}
            else:
                item = {
                    "type": "message",
                    "id": self.item_id,
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                }
            self.event("response.output_item.added", output_index=self.index, item=item)
            if kind == "reasoning":
                self.event(
                    "response.reasoning_summary_part.added",
                    item_id=self.item_id,
                    output_index=self.index,
                    summary_index=0,
                    part={"type": "summary_text", "text": ""},
                )
            else:
                self.event(
                    "response.content_part.added",
                    item_id=self.item_id,
                    output_index=self.index,
                    content_index=0,
                    part={"type": "output_text", "text": "", "annotations": []},
                )
        self.text += text
        replay = {"text": text}
        if signature is not None:
            replay["thoughtSignature"] = signature
        self.raw_parts.append(replay)
        if kind == "reasoning":
            self.event(
                "response.reasoning_summary_text.delta",
                item_id=self.item_id,
                output_index=self.index,
                summary_index=0,
                delta=text,
            )
        else:
            self.event(
                "response.output_text.delta",
                item_id=self.item_id,
                output_index=self.index,
                content_index=0,
                delta=text,
            )

    def chunk(self, chunk):
        if not isinstance(chunk, dict) or "error" in chunk:
            raise GeminiError("gemini_upstream_failed")
        feedback = chunk.get("promptFeedback") or {}
        if not isinstance(feedback, dict):
            raise GeminiError("gemini_invalid_response")
        if feedback.get("blockReason"):
            raise GeminiError("gemini_blocked")
        candidates = chunk.get("candidates", [])
        if not isinstance(candidates, list) or len(candidates) > 1:
            raise GeminiError("gemini_invalid_response")
        for candidate in candidates:
            if not isinstance(candidate, dict) or candidate.get("index", 0) != 0:
                raise GeminiError("gemini_invalid_response")
            content = candidate.get("content") or {}
            if not isinstance(content, dict) or content.get("role", "model") != "model":
                raise GeminiError("gemini_invalid_response")
            parts = content.get("parts", [])
            if not isinstance(parts, list):
                raise GeminiError("gemini_invalid_response")
            if self.reason is not None and parts:
                raise GeminiError("gemini_invalid_response")
            for part in parts:
                self.part(part)
            reason = candidate.get("finishReason")
            if reason is not None:
                if reason not in {"STOP", "MAX_TOKENS"}:
                    raise GeminiError("gemini_blocked")
                if self.reason is not None:
                    raise GeminiError("gemini_invalid_response")
                self.reason = reason
        if "usageMetadata" in chunk:
            usage = chunk["usageMetadata"]
            if not isinstance(usage, dict):
                raise GeminiError("gemini_invalid_response")
            for name in (
                "promptTokenCount",
                "candidatesTokenCount",
                "thoughtsTokenCount",
                "totalTokenCount",
                "cachedContentTokenCount",
            ):
                if name in usage and (
                    type(usage[name]) is not int or not 0 <= usage[name] <= 2**53
                ):
                    raise GeminiError("gemini_invalid_response")
            self.usage = usage

    def finish(self):
        if self.reason is None:
            raise GeminiError("gemini_stream_incomplete")
        self.close_item()
        usage = self.usage
        prompt = usage.get("promptTokenCount", 0)
        output = usage.get("candidatesTokenCount", 0) + usage.get(
            "thoughtsTokenCount", 0
        )
        mapped = {
            "input_tokens": prompt,
            "output_tokens": output,
            "total_tokens": usage.get("totalTokenCount", prompt + output),
            "input_tokens_details": {
                "cached_tokens": usage.get("cachedContentTokenCount", 0)
            },
            "output_tokens_details": {
                "reasoning_tokens": usage.get("thoughtsTokenCount", 0)
            },
        }
        status = "incomplete" if self.reason == "MAX_TOKENS" else "completed"
        response = {
            "id": self.id,
            "object": "response",
            "status": status,
            "output": self.output,
            "usage": mapped,
        }
        if status == "incomplete":
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        self.event("response." + status, response=response)
        self.terminal = True

    def failed(self, code):
        if not self.terminal:
            self.event(
                "response.failed",
                response={
                    "id": self.id,
                    "object": "response",
                    "status": "failed",
                    "error": {
                        "code": GeminiError(code).code,
                        "message": "Gemini request failed.",
                    },
                },
            )
            self.terminal = True


class _GoogleStream:
    """No redirects, environment proxies, alternate hosts, or error-body reads."""

    def __init__(self, model, body, key):
        self.model = model
        self.body = body
        self.key = key
        self.closed = threading.Event()
        self._peer = None
        transport = self
        cancelled = self.closed

        class Connection(http.client.HTTPSConnection):
            # DNS resolution may outlive a socket deadline. If cancellation
            # wins while connect is blocked, the late socket must never send
            # the already-buffered Google authorization header or request.
            def connect(connection):
                if cancelled.is_set():
                    raise GeminiError("gemini_upstream_failed")
                super().connect()
                transport._peer = connection.sock
                if cancelled.is_set():
                    peer = connection.sock
                    if peer is not None:
                        _shutdown(peer)
                    super().close()
                    raise GeminiError("gemini_upstream_failed")

            def send(connection, data):
                if cancelled.is_set():
                    raise GeminiError("gemini_upstream_failed")
                if connection.sock is None:
                    connection.connect()
                if cancelled.is_set():
                    raise GeminiError("gemini_upstream_failed")
                return super().send(data)

        self.connection = Connection(GOOGLE_HOST, 443, timeout=IO_TIMEOUT)

    def close(self):
        self.closed.set()
        connection = self.connection
        # getresponse() may clear connection.sock for Connection: close while
        # HTTPResponse's makefile still owns it. Retain the socket for shutdown.
        peer = self._peer if self._peer is not None else connection.sock
        if peer is not None:
            _shutdown(peer)
        connection.close()
        self.key = None

    def __iter__(self):
        if self.closed.is_set():
            raise GeminiError("gemini_upstream_failed")
        path = f"/v1beta/models/{self.model}:streamGenerateContent?alt=sse"
        try:
            self.connection.request(
                "POST",
                path,
                body=_json(self.body),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    "Accept-Encoding": "identity",
                    "x-goog-api-key": self.key,
                },
            )
            self.key = None
            response = self.connection.getresponse()
            if (
                response.status != 200
                or response.getheader("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
                != "text/event-stream"
                or response.getheader("Content-Encoding", "identity").lower()
                != "identity"
            ):
                raise GeminiError("gemini_upstream_failed")
            total = 0
            fields = []
            size = 0
            while not self.closed.is_set():
                raw = response.readline(MAX_EVENT + 1)
                if not raw:
                    if fields:
                        raise GeminiError("gemini_stream_incomplete")
                    return
                total += len(raw)
                if len(raw) > MAX_EVENT or total > MAX_STREAM:
                    raise GeminiError("gemini_stream_limit")
                line = raw.rstrip(b"\r\n")
                if not line:
                    if fields:
                        yield _load(b"\n".join(fields))
                    fields, size = [], 0
                elif line.startswith(b"data:"):
                    value = line[5:].lstrip(b" ")
                    size += len(value)
                    if size > MAX_EVENT:
                        raise GeminiError("gemini_stream_limit")
                    fields.append(value)
            raise GeminiError("gemini_upstream_failed")
        finally:
            self.close()


def _shutdown(connection):
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _close_stream(stream):
    try:
        stream.close()
    except Exception:
        pass  # Foreign transport exceptions must not escape watcher threads.


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    # Reuse TIME_WAIT after our own close, never share a live listening port.
    # SO_REUSEPORT is deliberately not enabled.
    allow_reuse_address = True
    request_queue_size = MAX_CONNECTIONS

    def __init__(self, owner):
        self.owner = owner
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.active = set()
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", owner.port), _Handler)

    def process_request(self, request, client_address):
        request.settimeout(IO_TIMEOUT)
        if not self.slots.acquire(blocking=False):
            # Do not create an unbounded thread even for unauthenticated peers.
            self.shutdown_request(request)
            return
        with self.lock:
            self.active.add(request)
        try:
            super().process_request(request, client_address)
        except Exception:
            with self.lock:
                self.active.discard(request)
            self.slots.release()
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        timer = threading.Timer(TURN_TIMEOUT + IO_TIMEOUT, _shutdown, args=(request,))
        timer.daemon = True
        timer.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()
            with self.lock:
                self.active.discard(request)
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # No traceback can expose upstream body, credential or user prompt.


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "deskd"
    sys_version = ""

    def log_message(self, *_args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def handle_expect_100(self):
        self.send_error(417)
        return False

    def do_POST(self):
        self.close_connection = True
        owner = self.server.owner
        try:
            if (
                self.path != "/v1/responses"
                or self.headers.get_all("Origin")
                or self.headers.get_all("Transfer-Encoding")
                or self.headers.get_all("Expect")
            ):
                raise GeminiError("gemini_invalid_request")
            for header in ("Host", "Authorization", "Content-Type", "Content-Length"):
                if len(self.headers.get_all(header, [])) != 1:
                    raise GeminiError("gemini_invalid_request")
            if self.headers["Host"] != f"127.0.0.1:{owner.port}" or self.headers[
                "Content-Type"
            ].lower().replace(" ", "") not in {
                "application/json",
                "application/json;charset=utf-8",
            }:
                raise GeminiError("gemini_invalid_request")
            encodings = self.headers.get_all("Content-Encoding", [])
            if len(encodings) > 1 or (encodings and encodings[0].lower() != "identity"):
                raise GeminiError("gemini_unsupported_encoding")
            supplied = self.headers["Authorization"]
            if not hmac.compare_digest(
                supplied.encode("latin-1"), ("Bearer " + owner._capability).encode()
            ):
                raise GeminiError("gemini_not_authorized")
            owner._authorize()
            length = self.headers["Content-Length"]
            if not re.fullmatch(r"[0-9]{1,8}", length):
                raise GeminiError("gemini_invalid_request")
            size = int(length)
            if not 0 < size <= MAX_BODY:
                raise GeminiError("gemini_request_too_large")
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise GeminiError("gemini_invalid_request")
            request = _load(raw)
            if not isinstance(request, dict) or request.get("model") != owner.model:
                raise GeminiError("gemini_wrong_model")
            translation = translate_request(request)
            owner._authorize()
            key = owner._key_source()
            if not isinstance(key, str) or not _KEY.fullmatch(key):
                raise GeminiError("gemini_upstream_failed")
            owner._authorize()  # File validation may have raced with fencing.
            stream = owner._upstream(owner.model, translation.body, key)
            key = None
            if not callable(getattr(stream, "close", None)):
                raise GeminiError("gemini_upstream_failed")
        except Exception as error:
            code = (
                error.code
                if isinstance(error, GeminiError)
                else "gemini_upstream_failed"
            )
            status = {
                "gemini_not_authorized": 401,
                "gemini_not_ready": 503,
                "gemini_request_too_large": 413,
                "gemini_unsupported_encoding": 415,
                "gemini_upstream_failed": 502,
            }.get(code, 400)
            self.send_error(status)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
        except Exception:
            _close_stream(stream)
            return
        done = threading.Event()
        timed_out = threading.Event()
        deadline = time.monotonic() + TURN_TIMEOUT

        def monitor():
            while not done.wait(0.05):
                try:
                    if time.monotonic() >= deadline:
                        timed_out.set()
                        _close_stream(stream)
                        return
                    readable, _, _ = select.select([self.connection], [], [], 0)
                    if (
                        readable
                        and self.connection.recv(
                            1, socket.MSG_PEEK | socket.MSG_DONTWAIT
                        )
                        == b""
                    ):
                        _close_stream(stream)
                        return
                    if owner._closed.is_set():
                        _close_stream(stream)
                        return
                except (OSError, ValueError):
                    _close_stream(stream)
                    return

        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        total = 0

        def emit(event):
            nonlocal total
            payload = _json(event)
            total += len(payload)
            if len(payload) > MAX_STREAM or total > 4 * MAX_STREAM:
                raise GeminiError("gemini_stream_limit")
            self.wfile.write(
                b"event: " + event["type"].encode() + b"\ndata: " + payload + b"\n\n"
            )
            self.wfile.flush()

        translator = _Translator(translation.tools, emit)
        try:
            translator.created()
            count, received = 0, 0
            for chunk in stream:
                owner._authorize()
                if timed_out.is_set() or time.monotonic() >= deadline:
                    raise GeminiError("gemini_timeout")
                count += 1
                received += len(_json(chunk))
                if count > MAX_CHUNKS or received > MAX_STREAM:
                    raise GeminiError("gemini_stream_limit")
                translator.chunk(chunk)
            if timed_out.is_set():
                raise GeminiError("gemini_timeout")
            owner._authorize()
            translator.finish()
        except Exception as error:
            code = (
                "gemini_timeout"
                if timed_out.is_set()
                else (
                    error.code
                    if isinstance(error, GeminiError)
                    else "gemini_upstream_failed"
                )
            )
            try:
                translator.failed(code)
            except Exception:
                pass  # Disconnected clients cannot receive a terminal event.
        finally:
            done.set()
            _close_stream(stream)
            watcher.join(timeout=0.2)


class GeminiProxy:
    """New loopback listener, fenced process capability, fixed model/upstream."""

    def __init__(
        self,
        port: int,
        model: str,
        key_source: Callable[[], str],
        authorize: Callable[[], None],
        upstream=None,
    ):
        if (
            type(port) is not int
            or not 0 <= port <= 65535
            or not isinstance(model, str)
            or not _MODEL.fullmatch(model)
            or not callable(key_source)
            or not callable(authorize)
            or (upstream is not None and not callable(upstream))
        ):
            raise GeminiError("invalid_gemini_configuration")
        self.port = port
        self.model = model
        self._key_source = key_source
        self._check = authorize
        self._upstream = _GoogleStream if upstream is None else upstream
        self._capability = ""
        self._closed = threading.Event()
        self._closed.set()
        self._server = None
        self._thread = None
        self._lock = threading.Lock()

    def _authorize(self):
        if self._closed.is_set():
            raise GeminiError("gemini_not_ready")
        try:
            result = self._check()
        except Exception:
            raise GeminiError("gemini_not_authorized") from None
        if result is not None:
            raise GeminiError("gemini_not_authorized")

    def token(self) -> str:
        self._authorize()
        return self._capability

    def start(self):
        with self._lock:
            if self._server is not None:
                return
            try:
                server = _Server(self)
            except OSError:
                raise GeminiError("gemini_port_unavailable") from None
            self.port = server.server_address[1]
            self._capability = secrets.token_urlsafe(32)
            self._server = server
            self._closed.clear()
            self._thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            self._thread.start()

    def close(self):
        with self._lock:
            server, thread = self._server, self._thread
            if server is None:
                return
            self._closed.set()
            self._capability = ""
            server.shutdown()
            with server.lock:
                for connection in tuple(server.active):
                    _shutdown(connection)
            server.server_close()
            thread.join(timeout=1)
            self._server = self._thread = None
