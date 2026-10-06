"""Protocol/transport acceptance using only synthetic data and mock upstreams."""

import base64
import http.client
import io
import json
import socket
import threading

import pytest

from deskd.workspace import gemini
from deskd.workspace.gemini import GeminiError, GeminiProxy, translate_request

MODEL = "gemini-synthetic-test"
KEY = "SYNTHETIC-GOOGLE-KEY-ONLY"


def payload(**changes):
    return {
        "model": MODEL,
        "stream": True,
        "input": [{"role": "user", "content": "synthetic prompt"}],
        **changes,
    }


def events(data):
    return [
        json.loads(line[6:]) for line in data.splitlines() if line.startswith(b"data: ")
    ]


def exchange(proxy, request):
    connection = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=3)
    try:
        connection.request(
            "POST",
            "/v1/responses",
            json.dumps(request),
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + proxy.token(),
            },
        )
        response = connection.getresponse()
        return response.status, events(response.read())
    finally:
        connection.close()


def candidate(parts=(), reason=None, **extra):
    item = {"content": {"role": "model", "parts": list(parts)}}
    if reason:
        item["finishReason"] = reason
    return {"candidates": [item], **extra}


@pytest.fixture
def start_proxy():
    proxies = []

    def start(upstream, authorize=lambda: None):
        proxy = GeminiProxy(0, MODEL, lambda: KEY, authorize, upstream)
        proxy.start()
        proxies.append(proxy)
        return proxy

    yield start
    for proxy in reversed(proxies):
        proxy.close()


def test_stream_text_reasoning_usage_and_exact_terminal(start_proxy):
    def upstream(model, body, key):
        assert (model, key) == (MODEL, KEY)
        assert body["contents"][0]["parts"][0]["text"] == "synthetic prompt"
        yield candidate([{"text": "consider", "thought": True}])
        yield candidate([{"text": "hel"}])
        yield candidate(
            [{"text": "lo"}],
            "STOP",
            usageMetadata={
                "promptTokenCount": 5,
                "candidatesTokenCount": 2,
                "thoughtsTokenCount": 3,
                "cachedContentTokenCount": 1,
                "totalTokenCount": 10,
            },
        )

    status, output = exchange(start_proxy(upstream), payload())
    assert status == 200
    assert output[0]["type"] == "response.created"
    assert output[-1]["type"] == "response.completed"
    assert [event["sequence_number"] for event in output] == list(
        range(1, len(output) + 1)
    )
    terminal = output[-1]["response"]
    assert terminal["output"][-1]["content"][0]["text"] == "hello"
    assert terminal["usage"]["output_tokens"] == 5
    assert terminal["usage"]["output_tokens_details"]["reasoning_tokens"] == 3
    assert (
        sum(
            event["type"]
            in {"response.completed", "response.incomplete", "response.failed"}
            for event in output
        )
        == 1
    )


@pytest.mark.parametrize(
    "reason,kind",
    [
        ("MAX_TOKENS", "response.incomplete"),
        ("SAFETY", "response.failed"),
        ("MALFORMED_FUNCTION_CALL", "response.failed"),
        (None, "response.failed"),
    ],
)
def test_non_success_finish_is_not_misreported_as_completed(start_proxy, reason, kind):
    def upstream(*_args):
        yield candidate([{"text": "partial"}], reason)

    _, output = exchange(start_proxy(upstream), payload())
    assert output[-1]["type"] == kind
    if reason == "MAX_TOKENS":
        assert output[-1]["response"]["incomplete_details"] == {
            "reason": "max_output_tokens"
        }
    assert not any(event["type"] == "response.completed" for event in output)


def test_namespaces_with_same_inner_tool_name_round_trip_without_collision(start_proxy):
    tools = [
        {
            "type": "namespace",
            "name": ns,
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                    },
                }
            ],
        }
        for ns in ("analyst", "reviewer")
    ]
    translated = translate_request(payload(tools=tools))
    aliases = [
        item["name"] for item in translated.body["tools"][0]["functionDeclarations"]
    ]
    assert len(set(aliases)) == 2
    requests = []

    def upstream(_model, body, _key):
        requests.append(body)
        if len(requests) == 1:
            yield candidate(
                [
                    {
                        "functionCall": {
                            "name": aliases[1],
                            "args": {"q": "synthetic query"},
                        },
                        "thoughtSignature": "SYNTHETIC-SIGNATURE",
                    }
                ],
                "STOP",
            )
        else:
            yield candidate([{"text": "reviewed"}], "STOP")

    proxy = start_proxy(upstream)
    _, first = exchange(proxy, payload(tools=tools))
    history = first[-1]["response"]["output"]
    call = next(item for item in history if item["type"] == "function_call")
    assert call["namespace"] == "reviewer"
    assert call["name"] == "lookup"
    assert call["encrypted_function_args"] == []
    inputs = (
        payload()["input"]
        + history
        + [
            {
                "type": "function_call_output",
                "call_id": call["call_id"],
                "output": "synthetic result",
            }
        ]
    )
    status, second = exchange(proxy, payload(tools=tools, input=inputs))
    assert status == 200 and second[-1]["type"] == "response.completed"
    parts = requests[1]["contents"][1]["parts"]
    assert len(parts) == 1
    assert parts[0]["functionCall"]["name"] == aliases[1]
    assert parts[0]["thoughtSignature"] == "SYNTHETIC-SIGNATURE"
    assert (
        requests[1]["contents"][2]["parts"][0]["functionResponse"]["name"] == aliases[1]
    )


def test_custom_apply_patch_is_freeform_on_return_and_replay(start_proxy):
    patch = "*** Begin Patch\n*** Add File: synthetic.txt\n+mock\n*** End Patch"
    tools = [
        {
            "type": "custom",
            "name": "apply_patch",
            "description": "Edit files",
            "format": {"type": "grammar", "syntax": "lark", "definition": "synthetic"},
        }
    ]
    calls = []

    def upstream(_model, body, _key):
        calls.append(body)
        alias = body["tools"][0]["functionDeclarations"][0]["name"]
        if len(calls) == 1:
            yield candidate(
                [
                    {
                        "functionCall": {"name": alias, "args": {"input": patch}},
                        "thoughtSignature": "PATCH-SIGNATURE",
                    }
                ],
                "STOP",
            )
        else:
            yield candidate([{"text": "done"}], "STOP")

    proxy = start_proxy(upstream)
    _, output = exchange(proxy, payload(tools=tools))
    history = output[-1]["response"]["output"]
    call = history[0]
    assert call["type"] == "custom_tool_call"
    assert call["input"] == patch
    assert "arguments" not in call
    inputs = (
        payload()["input"]
        + history
        + [
            {
                "type": "custom_tool_call_output",
                "call_id": call["call_id"],
                "output": [{"type": "input_text", "text": "Applied"}],
            }
        ]
    )
    status, follow = exchange(proxy, payload(tools=tools, input=inputs))
    assert status == 200 and follow[-1]["type"] == "response.completed"
    assert calls[1]["contents"][1]["parts"][0]["functionCall"]["args"] == {
        "input": patch
    }


def test_message_thought_signature_preserves_exact_chunk_parts(start_proxy):
    def upstream(*_args):
        yield candidate([{"text": "first", "thoughtSignature": "TEXT-SIGNATURE"}])
        yield candidate([{"text": " second"}], "STOP")

    _, output = exchange(start_proxy(upstream), payload())
    replay = translate_request(
        payload(input=payload()["input"] + output[-1]["response"]["output"])
    )
    assert replay.body["contents"][1]["parts"] == [
        {"text": "first", "thoughtSignature": "TEXT-SIGNATURE"},
        {"text": " second"},
    ]


@pytest.mark.parametrize(
    "tool",
    [
        {"type": "web_search"},
        {"type": "custom", "name": "other"},
        {"type": "image_generation"},
        {
            "type": "namespace",
            "name": "a",
            "tools": [{"type": "namespace", "name": "b", "tools": []}],
        },
    ],
)
def test_unknown_tools_are_rejected_instead_of_dropped(tool):
    with pytest.raises(GeminiError, match="gemini_unsupported_tool"):
        translate_request(payload(tools=[tool]))


@pytest.mark.parametrize(
    "effort,level",
    [
        ("minimal", "low"),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("xhigh", "high"),
    ],
)
def test_thinking_levels_match_supported_flash_values(effort, level):
    translated = translate_request(payload(reasoning={"effort": effort}))
    assert (
        translated.body["generationConfig"]["thinkingConfig"]["thinkingLevel"] == level
    )


def test_thinking_off_is_explicitly_unsupported():
    with pytest.raises(GeminiError, match="gemini_invalid_request"):
        translate_request(payload(reasoning={"effort": "none"}))


@pytest.mark.parametrize(
    "item",
    [
        {"type": "unknown"},
        {
            "role": "user",
            "content": [
                {"type": "input_image", "image_url": "https://example.invalid/image"}
            ],
        },
        {"type": "function_call_output", "call_id": "absent", "output": "x"},
        {"type": "reasoning", "encrypted_content": "foreign"},
    ],
)
def test_unsupported_or_broken_history_is_explicit(item):
    with pytest.raises(GeminiError):
        translate_request(payload(input=[item]))


def test_replay_cannot_remove_a_part_from_a_different_turn():
    blob = (
        gemini._BLOB
        + base64.b64encode(
            json.dumps(
                {"v": 1, "covers_prev": 1, "parts": [{"text": "replacement"}]}
            ).encode()
        ).decode()
    )
    with pytest.raises(GeminiError, match="gemini_invalid_replay"):
        translate_request(
            payload(
                input=[
                    {"role": "assistant", "content": "previous"},
                    {"role": "user", "content": "boundary"},
                    {"type": "reasoning", "encrypted_content": blob},
                ]
            )
        )


@pytest.mark.parametrize(
    "chunk",
    [
        {"error": {"message": "SYNTHETIC-PRIVATE-PROVIDER-BODY"}},
        {"promptFeedback": {"blockReason": "SYNTHETIC-PRIVATE-REASON"}},
        {
            "candidates": [
                {
                    "content": {
                        "parts": [{"functionCall": {"name": "unknown", "args": {}}}]
                    },
                    "finishReason": "STOP",
                }
            ]
        },
        {"candidates": [], "usageMetadata": {"promptTokenCount": -1}},
    ],
)
def test_upstream_errors_and_invalid_tool_calls_have_nonsecret_failed_terminal(
    start_proxy, chunk, capsys
):
    def upstream(*_args):
        yield chunk

    _, output = exchange(start_proxy(upstream), payload())
    assert output[-1]["type"] == "response.failed"
    assert "SYNTHETIC-PRIVATE" not in json.dumps(output)
    assert capsys.readouterr() == ("", "")


def test_factory_exception_cannot_expose_key_or_prompt(start_proxy, capsys):
    def upstream(*_args):
        raise RuntimeError(KEY + " private synthetic prompt")

    status, output = exchange(start_proxy(upstream), payload())
    assert status >= 400 and output == []
    assert capsys.readouterr() == ("", "")


class BlockingStream:
    def __init__(self):
        self.started = threading.Event()
        self.closed = threading.Event()

    def __iter__(self):
        self.started.set()
        if not self.closed.wait(3):
            raise AssertionError("mock stream was not cancelled")
        raise RuntimeError("synthetic private cancelled transport")
        yield  # an iterator without performing any network request

    def close(self):
        self.closed.set()


def test_client_disconnect_closes_blocked_upstream_promptly(start_proxy):
    stream = BlockingStream()
    proxy = start_proxy(lambda *_args: stream)
    connection = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=2)
    connection.request(
        "POST",
        "/v1/responses",
        json.dumps(payload()),
        {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + proxy.token(),
        },
    )
    response = connection.getresponse()
    assert response.status == 200
    assert stream.started.wait(1)
    response.close()
    connection.close()
    assert stream.closed.wait(1)


def test_total_deadline_cancels_blocked_upstream_and_reports_timeout(
    start_proxy, monkeypatch
):
    monkeypatch.setattr(gemini, "TURN_TIMEOUT", 0.15)
    stream = BlockingStream()
    proxy = start_proxy(lambda *_args: stream)
    _, output = exchange(proxy, payload())
    assert stream.closed.is_set()
    assert output[-1]["type"] == "response.failed"
    assert output[-1]["response"]["error"]["code"] == "gemini_timeout"


def test_stream_limit_cancels_and_does_not_claim_success(start_proxy, monkeypatch):
    monkeypatch.setattr(gemini, "MAX_CHUNKS", 2)
    closed = []

    def upstream(*_args):
        try:
            for _ in range(5):
                yield candidate([{"text": "x"}])
            yield candidate(reason="STOP")
        finally:
            closed.append(True)

    _, output = exchange(start_proxy(upstream), payload())
    assert output[-1]["type"] == "response.failed"
    assert output[-1]["response"]["error"]["code"] == "gemini_stream_limit"
    assert closed == [True]


class FakeResponse:
    def __init__(self, content, status=200, content_type="text/event-stream"):
        self.input = io.BytesIO(content)
        self.status = status
        self.content_type = content_type

    def getheader(self, name, default=None):
        return self.content_type if name == "Content-Type" else default

    def readline(self, limit):
        return self.input.readline(limit)


def fake_https(monkeypatch, response):
    calls = []

    class Connection:
        sock = None

        def __init__(self, host, port, timeout):
            calls.append((host, port, timeout))

        def request(self, *args, **kwargs):
            calls.append((args, kwargs))

        def getresponse(self):
            return response

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(gemini.http.client, "HTTPSConnection", Connection)
    return calls


def test_production_transport_pins_google_and_ignores_proxy_environment(monkeypatch):
    chunk = candidate([{"text": "mock"}], "STOP")
    response = FakeResponse(b"data: " + json.dumps(chunk).encode() + b"\n\n")
    calls = fake_https(monkeypatch, response)
    monkeypatch.setenv("HTTPS_PROXY", "http://must-not-contact.invalid:1")
    monkeypatch.setenv("GEMINI_API_KEY", "MUST-NOT-READ-ENV-KEY")
    stream = gemini._GoogleStream(MODEL, {"contents": []}, KEY)
    assert list(stream) == [chunk]
    assert calls[0] == ("generativelanguage.googleapis.com", 443, gemini.IO_TIMEOUT)
    args, kwargs = calls[1]
    assert args == ("POST", f"/v1beta/models/{MODEL}:streamGenerateContent?alt=sse")
    assert kwargs["headers"]["x-goog-api-key"] == KEY
    assert calls[-1] == "closed"


@pytest.mark.parametrize(
    "status,content_type",
    [(302, "text/event-stream"), (401, "application/json"), (200, "application/json")],
)
def test_production_transport_never_reads_redirect_or_error_body(
    monkeypatch, status, content_type
):
    response = FakeResponse(b"PRIVATE-ERROR-BODY", status, content_type)
    fake_https(monkeypatch, response)
    with pytest.raises(GeminiError, match="gemini_upstream_failed"):
        list(gemini._GoogleStream(MODEL, {}, KEY))
    assert response.input.tell() == 0


def test_truncated_sse_frame_is_not_accepted_as_completed(monkeypatch):
    response = FakeResponse(b"data: " + json.dumps(candidate(reason="STOP")).encode())
    fake_https(monkeypatch, response)
    with pytest.raises(GeminiError, match="gemini_stream_incomplete"):
        list(gemini._GoogleStream(MODEL, {}, KEY))


def test_unauthenticated_open_connections_are_bounded(start_proxy, monkeypatch):
    monkeypatch.setattr(gemini, "MAX_CONNECTIONS", 2)
    proxy = start_proxy(lambda *_args: iter(()))
    sockets = []
    try:
        for _ in range(2):
            connection = socket.create_connection(("127.0.0.1", proxy.port), timeout=1)
            sockets.append(connection)
            connection.sendall(b"POST /v1/responses HTTP/1.1\r\n")
        deadline = threading.Event()
        for _ in range(100):
            with proxy._server.lock:
                if len(proxy._server.active) == 2:
                    break
            deadline.wait(0.01)
        excess = socket.create_connection(("127.0.0.1", proxy.port), timeout=1)
        sockets.append(excess)
        assert excess.recv(1) == b""
        with proxy._server.lock:
            assert len(proxy._server.active) == 2
    finally:
        for connection in sockets:
            connection.close()
