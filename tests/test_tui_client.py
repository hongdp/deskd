from __future__ import annotations

import io
import json
import os
import threading
import urllib.error
import urllib.parse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from deskd.tui.client import (APIError, ClientError, ConnectionNotice,
                              ControlPlaneClient, CursorExpired, SSEEvent,
                              load_api_token, parse_sse)


CURSOR_1 = "evt_0123456789abcdef_0000000000000001"
CURSOR_2 = "evt_0123456789abcdef_0000000000000002"
TOKEN = "s" * 32


@contextmanager
def _http_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class FakeResponse:
    def __init__(self, payload=b"", *, content_type="application/json", status=200,
                 lines=None):
        self._payload = payload
        self._lines = list(lines or [])
        self.headers = {"content-type": content_type}
        self.status = status
        self.closed = False

    def read(self, amt=-1):
        return self._payload if amt == -1 else self._payload[:amt]

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, replies):
        self.replies = replies
        self.requests = []
        self._lock = threading.Lock()

    def open(self, request, *, timeout):
        with self._lock:
            self.requests.append((request, timeout))
            parsed = urllib.parse.urlsplit(request.full_url)
            key = (request.method, parsed.path)
            value = self.replies[key]
            if isinstance(value, list):
                value = value.pop(0)
            if isinstance(value, Exception):
                raise value
            return value


def response(value):
    return FakeResponse(json.dumps(value).encode())


def http_error(status, detail):
    return urllib.error.HTTPError(
        "http://control/api/snapshot", status, "failure", {},
        io.BytesIO(json.dumps({"detail": detail}).encode()))


def test_sse_parser_preserves_opaque_cursor_multiline_data_and_heartbeat():
    events = list(parse_sse([
        b": keepalive\n",
        b"id: evt_0123456789abcdef_000000000000000a\n",
        b"event: resource.changed\n",
        b"retry: 2500\n",
        b'data: {"resource":"task",\n',
        b'data: "id":7}\n',
        b"\n",
    ]))
    assert events[0] == SSEEvent("heartbeat", {"comment": "keepalive"})
    assert events[1].id == "evt_0123456789abcdef_000000000000000a"
    assert events[1].event == "resource.changed"
    assert events[1].data == {"resource": "task", "id": 7}
    assert events[1].retry_ms == 2500


def test_sse_parser_does_not_crash_on_unknown_non_json_event():
    [event] = list(parse_sse(["event: future\n", "data: not-json\n", "\n"]))
    assert event.event == "future"
    assert event.data["raw"] == "not-json"
    assert "protocol_error" in event.data


def test_token_file_must_be_private_regular_file(tmp_path: Path):
    token_file = tmp_path / "token"
    token_file.write_text(" secret \n")
    token_file.chmod(0o600)
    token_file.write_text("s" * 32 + "\n")
    assert load_api_token(token_file) == "s" * 32

    token_file.chmod(0o640)
    with pytest.raises(ClientError, match="chmod 600"):
        load_api_token(token_file)

    token_file.chmod(0o600)
    link = tmp_path / "token-link"
    link.symlink_to(token_file)
    with pytest.raises(ClientError, match="non-symlink"):
        load_api_token(link)


def test_auth_choice_is_explicit_and_environment_tokens_are_unsupported(monkeypatch):
    monkeypatch.setenv("DESKD_API_TOKEN", "x" * 32)
    assert load_api_token(None, no_auth=True) is None
    with pytest.raises(ClientError, match="--token-file"):
        load_api_token(None)


def test_remote_http_refuses_to_leak_bearer_token():
    with pytest.raises(ClientError, match="remote HTTP"):
        ControlPlaneClient("http://deskd.internal:8000", token=TOKEN)
    # Loopback and an explicit trusted-network override remain available.
    ControlPlaneClient("http://127.0.0.1:8000", token=TOKEN)
    ControlPlaneClient("http://127.0.0.2:8000", token=TOKEN)
    ControlPlaneClient("http://deskd.internal:8000", token=TOKEN,
                       allow_insecure_http=True)


@pytest.mark.parametrize("target_scheme", ["http", "https"])
def test_protocol_redirects_fail_without_forwarding_bearer(target_scheme):
    captured: list[str | None] = []

    class Sink(BaseHTTPRequestHandler):
        def do_GET(self):
            captured.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *_args):
            pass

    class Redirect(BaseHTTPRequestHandler):
        target = ""

        def _reply(self):
            self.send_response(302)
            self.send_header("Location", self.target)
            self.end_headers()

        do_GET = _reply
        do_POST = _reply

        def log_message(self, *_args):
            pass

    with _http_server(Sink) as sink:
        sink_port = sink.server_address[1]
        # `localhost` also changes the URL host spelling; the independently
        # allocated sink changes the authority port.  HTTPS proves that a
        # scheme upgrade cannot become an implicit credential transfer either.
        Redirect.target = (
            f"{target_scheme}://localhost:{sink_port}/capture")
        with _http_server(Redirect) as redirect:
            base = f"http://127.0.0.1:{redirect.server_address[1]}"
            client = ControlPlaneClient(base, token=TOKEN)
            calls = [
                lambda: client.request_json("GET", "/api/snapshot"),
                lambda: client.fetch_meeting("meeting-1"),
                lambda: client.fetch_agent_feed("analyst"),
                lambda: client.submit_command("task.list", {}),
                lambda: client._open_stream(None),
            ]
            for call in calls:
                with pytest.raises(APIError) as caught:
                    call()
                assert caught.value.status == 302
    assert captured == []


def test_url_must_not_embed_credentials():
    with pytest.raises(ClientError, match="forbidden"):
        ControlPlaneClient("https://user:secret@deskd.example")
    with pytest.raises(ClientError, match="control characters"):
        ControlPlaneClient("https://deskd.example/\x1b[2J")


def test_atomic_snapshot_is_preferred_and_keeps_cursor():
    transport = FakeTransport({
        ("GET", "/api/snapshot"): response({
            "cursor": "evt_2a", "generated_at": "2026-08-11T01:02:03Z",
            "server_version": "0.4.0", "board": {"agents": [], "health": {}},
            "tasks": {"tasks": [], "stalled_ids": []}, "inbox": [],
            "meetings": [], "hooks": [], "wake": {}, "runtime": {}, "meta": {},
        }),
    })
    client = ControlPlaneClient("https://deskd.example", transport=transport)
    snap = client.fetch_snapshot()
    assert snap.consistent is True
    assert snap.cursor == "evt_2a"
    assert snap.server_version == "0.4.0"
    assert len(transport.requests) == 1


def test_legacy_snapshot_fallback_is_explicitly_non_atomic_and_flattens_inbox():
    board = {"generated_at": "now", "health": {}, "agents": [{
        "role": "operator", "inbox": {
            "queued": [{"id": 1, "title": "new"}],
            "delivered": [{"id": 2, "title": "seen"}],
        },
    }]}
    replies = {
        ("GET", "/api/snapshot"): http_error(404, "not found"),
        ("GET", "/api/board"): response(board),
        ("GET", "/api/tasks"): response({"tasks": [], "stalled_ids": []}),
        ("GET", "/api/meetings"): response([]),
        ("GET", "/api/hooks"): response([]),
        ("GET", "/api/wake"): response({"attempts": []}),
        ("GET", "/api/runtime"): response({"roles": []}),
        ("GET", "/api/meeting-meta"): response({"project": "deskd"}),
    }
    client = ControlPlaneClient("https://deskd.example", transport=FakeTransport(replies))
    snap = client.fetch_snapshot()
    assert snap.consistent is False
    assert [(r["id"], r["target_role"], r["delivery_state"])
            for r in snap.inbox] == [(1, "operator", "queued"),
                                     (2, "operator", "delivered")]


@pytest.mark.parametrize("status", [401, 403])
def test_no_auth_control_rejection_falls_back_to_legacy_projections(status):
    replies = {
        ("GET", "/api/snapshot"): http_error(status, "Bearer token required"),
        ("GET", "/api/board"): response(
            {"generated_at": "now", "health": {}, "agents": []}),
        ("GET", "/api/tasks"): response({"tasks": [], "stalled_ids": []}),
        ("GET", "/api/meetings"): response([]),
        ("GET", "/api/hooks"): response([]),
        ("GET", "/api/wake"): response({"attempts": []}),
        ("GET", "/api/runtime"): response({"roles": []}),
        ("GET", "/api/meeting-meta"): response({"project": "deskd"}),
    }
    transport = FakeTransport(replies)
    snapshot = ControlPlaneClient(
        "https://deskd.example", transport=transport).fetch_snapshot()
    assert snapshot.consistent is False
    assert len(transport.requests) == 8


@pytest.mark.parametrize("status", [401, 403])
def test_authenticated_control_rejection_never_downgrades_to_legacy(status):
    transport = FakeTransport({
        ("GET", "/api/snapshot"): http_error(status, "invalid bearer token"),
    })
    client = ControlPlaneClient(
        "https://deskd.example", token=TOKEN, transport=transport)
    with pytest.raises(APIError) as caught:
        client.fetch_snapshot()
    assert caught.value.status == status
    assert len(transport.requests) == 1


def test_command_has_idempotency_but_never_client_claimed_actor():
    transport = FakeTransport({
        ("POST", "/api/commands"): response(
            {"request_id": "req-1", "accepted": True, "event_cursor": "evt_2"}),
    })
    client = ControlPlaneClient("https://deskd.example", token=TOKEN,
                                transport=transport)
    answer = client.submit_command(
        "directive.send", {"target_role": "engineer", "body": "work"},
        request_id="req-1")
    assert answer["accepted"] is True
    request, _ = transport.requests[0]
    body = json.loads(request.data)
    assert set(body) == {"request_id", "verb", "params"}
    assert "actor" not in body and "role" not in body
    assert request.get_header("Idempotency-key") == "req-1"
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"


def test_http_errors_are_bounded_and_cursor_conflict_is_typed():
    transport = FakeTransport({
        ("GET", "/api/events"): http_error(409, "cursor evt_old expired"),
    })
    client = ControlPlaneClient("https://deskd.example", transport=transport)
    with pytest.raises(CursorExpired):
        client._open_stream("evt_old")


def test_event_follower_sends_both_cursor_forms_and_stops_cleanly():
    stream = FakeResponse(
        content_type="text/event-stream; charset=utf-8",
        lines=[f"id: {CURSOR_2}\n".encode(), b"event: task.changed\n",
               b"data: {}\n", b"\n"])
    transport = FakeTransport({("GET", "/api/events"): stream})
    client = ControlPlaneClient("https://deskd.example", transport=transport)
    stop = threading.Event()
    tail = client.follow_events(stop, cursor=CURSOR_1, min_backoff=0.01)
    assert next(tail).state == "connecting"
    assert next(tail).state == "connected"
    event = next(tail)
    assert isinstance(event, SSEEvent) and event.id == CURSOR_2
    stop.set()
    assert list(tail) == []
    request, timeout = transport.requests[0]
    assert request.get_header("Last-event-id") == CURSOR_1
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query) == {
        "after": [CURSOR_1]}
    assert timeout >= 30


def test_cursor_expiry_stops_follower_until_caller_obtains_new_snapshot():
    transport = FakeTransport({
        ("GET", "/api/events"): [http_error(409, "cursor evt_old expired")],
    })
    client = ControlPlaneClient("https://deskd.example", transport=transport)
    stop = threading.Event()
    items = list(client.follow_events(stop, cursor="evt_old", min_backoff=0.01))
    assert [item.state for item in items if isinstance(item, ConnectionNotice)] == [
        "connecting", "cursor_expired"]
    assert len(transport.requests) == 1


def test_sse_reset_immediately_requires_snapshot_without_stale_reconnect():
    stream = FakeResponse(
        content_type="text/event-stream",
        lines=[
            f"id: {CURSOR_2}\n".encode(), b"event: change\n",
            b'data: {"resource":"task"}\n',
            b"\n", b"event: reset\n",
            b'data: {"error":"event cursor expired","resnapshot":true}\n', b"\n",
        ])
    transport = FakeTransport({("GET", "/api/events"): [stream]})
    client = ControlPlaneClient("https://deskd.example", transport=transport)
    items = list(client.follow_events(
        threading.Event(), cursor=CURSOR_1, min_backoff=0.01))

    assert [item.event for item in items if isinstance(item, SSEEvent)] == ["change"]
    notices = [item for item in items if isinstance(item, ConnectionNotice)]
    assert [item.state for item in notices] == [
        "connecting", "connected", "cursor_expired"]
    assert notices[-1].cursor is None
    assert notices[-1].retry_in == 0.0
    assert len(transport.requests) == 1
