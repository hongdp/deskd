import io
import json
import os

import pytest

from deskd.gateway.bridge import (
    BridgeError,
    MAX_FRAME_BYTES,
    _decode,
    _serve_stdio,
    run_bridge,
)


CATALOG = [
    {
        "name": "proposal.create",
        "description": "Mock proposal",
        "inputSchema": {"type": "object", "properties": {}},
    }
]


def conversation(messages, rpc):
    source = io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in messages))
    target = io.BytesIO()
    _serve_stdio(source, target, rpc, CATALOG)
    return [json.loads(line) for line in target.getvalue().splitlines()]


def handshake():
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "synthetic-harness", "version": "test"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]


def call(*, meta=None, arguments=None, method="tools/call"):
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "method": method,
        "params": {
            "name": "proposal.create",
            "arguments": arguments or {"request_id": "stable-1", "body": "Example"},
            "_meta": meta
            if meta is not None
            else {"sessionId": "root-a", "threadId": "root-a"},
        },
    }


def test_preserves_harness_metadata_and_extracts_only_request_id():
    observed = []

    def rpc(method, params):
        observed.append((method, params))
        return {"ok": True, "result": {"accepted": True}}

    forged = {
        "request_id": "stable-1",
        "body": "Example",
        "_meta": {"sessionId": "root-b"},
        "role": "reviewer",
    }
    replies = conversation([*handshake(), call(arguments=forged)], rpc)
    assert observed == [
        (
            "execute",
            {
                "request_id": "stable-1",
                "mcp": {
                    "name": "proposal.create",
                    "arguments": {k: v for k, v in forged.items() if k != "request_id"},
                    "_meta": {"sessionId": "root-a", "threadId": "root-a"},
                },
            },
        )
    ]
    assert replies[-1]["result"]["isError"] is False


def test_tools_are_public_descriptions_and_listing_does_not_execute():
    def rpc(*args):
        pytest.fail("listing must not execute")

    replies = conversation(
        [
            *handshake(),
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ],
        rpc,
    )
    assert replies[-1]["result"] == {"tools": CATALOG}


@pytest.mark.parametrize(
    "messages",
    [
        [call()],
        [handshake()[0], call()],
        [*handshake(), call(meta={})],
        [*handshake(), call(arguments={"body": "no request id"})],
        [*handshake(), call(method="admin.activate")],
    ],
)
def test_invalid_calls_never_reach_gateway(messages):
    def rpc(*args):
        pytest.fail("invalid request reached the gateway")

    assert "error" in conversation(messages, rpc)[-1]


def test_gateway_denial_is_a_tool_error():
    replies = conversation(
        [*handshake(), call()],
        lambda *args: {"ok": False, "error": {"code": "root_required"}},
    )
    assert replies[-1]["result"] == {
        "content": [{"type": "text", "text": '{"error":{"code":"root_required"}}'}],
        "isError": True,
    }


def test_no_automatic_resubmit_after_gateway_disconnect():
    attempted = []

    def rpc(*args):
        attempted.append(args)
        raise OSError("synthetic disconnect after commit")

    replies = conversation([*handshake(), call(), call()], rpc)
    assert len(attempted) == 1
    assert replies[-1]["error"]["message"] == "gateway_unavailable_do_not_resubmit"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n',
        b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":NaN}}\n',
        b"[1,2]\n",
        b"x" * (MAX_FRAME_BYTES + 1),
    ],
)
def test_bad_frames_are_rejected_without_forwarding(raw):
    target = io.BytesIO()
    _serve_stdio(
        io.BytesIO(raw), target, lambda *args: pytest.fail("forwarded"), CATALOG
    )
    assert "error" in json.loads(target.getvalue().splitlines()[0])


def test_notification_cannot_execute_a_tool():
    notification = call()
    del notification["id"]
    replies = conversation(
        [*handshake(), notification], lambda *args: pytest.fail("forwarded")
    )
    assert len(replies) == 1


def test_public_bridge_cannot_use_same_uid_as_gateway():
    with pytest.raises(BridgeError, match="distinct_gateway_uid_required"):
        run_bridge(
            "/nonexistent",
            gateway_uid=os.geteuid(),
            source=io.BytesIO(),
            target=io.BytesIO(),
        )


@pytest.mark.parametrize(
    "code", ["storage_error", "internal_error", "response_encoding_error"]
)
def test_gateway_post_commit_errors_are_unknown_and_stop(code):
    attempts = []

    def rpc(*args):
        attempts.append(args)
        return {"ok": False, "error": {"code": code}}

    replies = conversation([*handshake(), call(), call()], rpc)
    assert len(attempts) == 1
    assert replies[-1]["error"]["message"] == "gateway_unavailable_do_not_resubmit"


def test_oversize_request_id_is_not_reflected():
    message = call()
    message["id"] = "x" * 10000
    replies = conversation(
        [*handshake(), message], lambda *args: pytest.fail("forwarded")
    )
    assert replies[-1]["id"] is None
    assert len(json.dumps(replies[-1])) < 200


def test_maximum_stored_receipt_fits_response_envelope():
    from deskd.gateway.events import MAX_JSON_BYTES, canonical_json
    from deskd.gateway.wire import encode_frame

    result = {"body": "a" * (MAX_JSON_BYTES - len(canonical_json({"body": ""})))}
    assert len(canonical_json(result)) == MAX_JSON_BYTES
    raw = encode_frame({"id": "request-1", "ok": True, "result": result}, response=True)
    assert len(raw) > MAX_JSON_BYTES
    assert _decode(raw, response=True)["result"] == result


@pytest.mark.parametrize("reply_kind", ["invalid_json", "wrong_id", "missing_result"])
def test_public_bridge_stops_on_malformed_reply_after_send(monkeypatch, reply_kind):
    import socket
    import struct

    sent = []

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def connect(self, path):
            pass

        def getsockopt(self, *args):
            return struct.pack("3i", 42, os.geteuid() + 1, 123)

        def sendall(self, raw):
            sent.append(json.loads(raw))

        def makefile(self, *args, **kwargs):
            return self

        def readline(self, limit):
            request = sent[-1]
            if request["method"] == "hello":
                return (
                    json.dumps({"id": request["id"], "ok": True, "result": {}}).encode()
                    + b"\n"
                )
            if reply_kind == "invalid_json":
                return b'{"ok": broken}\n'
            if reply_kind == "wrong_id":
                return b'{"id":"wrong","ok":true,"result":{}}\n'
            return json.dumps({"id": request["id"], "ok": True}).encode() + b"\n"

    monkeypatch.setattr(socket, "socket", lambda *args: FakeSocket())
    source = io.BytesIO(
        b"".join(json.dumps(x).encode() + b"\n" for x in [*handshake(), call(), call()])
    )
    target = io.BytesIO()
    run_bridge("/synthetic", gateway_uid=os.geteuid() + 1, source=source, target=target)
    assert [r["method"] for r in sent] == ["hello", "execute"]
    assert (
        json.loads(target.getvalue().splitlines()[-1])["error"]["message"]
        == "gateway_unavailable_do_not_resubmit"
    )
