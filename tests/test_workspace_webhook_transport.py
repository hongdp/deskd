"""Transport security with deterministic sockets; never send a real notification."""

import json
import threading

import pytest

from deskd.workspace import sources
from deskd.workspace.notifications import WebhookTarget, send_webhook
from deskd.workspace.store import WorkspaceError


def fake_transport(monkeypatch, *, status=204, stall=False):
    events = []
    interrupted = threading.Event()

    class Socket:
        def shutdown(self, direction):
            interrupted.set()
            events.append("shutdown")

    class Response:
        def __init__(self):
            self.status = status

        def read(self, *args):
            pytest.fail("webhook must not read arbitrary remote response bodies")

        def close(self):
            events.append("response_closed")

    class Connection:
        def __init__(self, host, ip, **kwargs):
            events.append(("connect_target", host, ip, kwargs))
            self.sock = Socket()

        def connect(self):
            events.append("connected")

        def request(self, method, path, *, body, headers):
            events.append((method, path, json.loads(body), headers))

        def getresponse(self):
            if stall:
                assert interrupted.wait(1), "total deadline did not interrupt header read"
                raise OSError("do not expose the remote endpoint or response")
            return Response()

        def close(self):
            events.append("connection_closed")

    monkeypatch.setattr(sources, "resolve_source_addresses", lambda parsed, **kwargs: ("8.8.8.8",))
    monkeypatch.setattr(sources, "PinnedHTTPSConnection", Connection)
    return events


def test_webhook_acknowledges_status_without_reading_body(monkeypatch):
    events = fake_transport(monkeypatch)
    target = WebhookTarget("https://notifications.example.com/inbox", approved=True)
    payload = {"delivery_id": "delivery_synthetic", "kind": "decision"}
    assert send_webhook(target, payload) is True
    request = next(event for event in events if isinstance(event, tuple) and event[0] == "POST")
    assert request[1] == "/inbox"
    assert request[3]["Idempotency-Key"] == payload["delivery_id"]
    assert request[3]["Connection"] == "close"
    assert "response_closed" in events and "connection_closed" in events


@pytest.mark.parametrize("status", [301, 302, 307, 401, 429, 500])
def test_webhook_never_follows_redirect_or_accepts_error(monkeypatch, status):
    events = fake_transport(monkeypatch, status=status)
    target = WebhookTarget("https://notifications.example.com/inbox", approved=True)
    assert send_webhook(target, {"delivery_id": "synthetic"}) is False
    assert sum(isinstance(event, tuple) and event[0] == "POST" for event in events) == 1


def test_webhook_deadline_interrupts_slow_headers_and_sanitizes_error(monkeypatch):
    events = fake_transport(monkeypatch, stall=True)
    real_timer = threading.Timer
    # Accelerate only the timer; assertions require the actual shutdown callback.
    monkeypatch.setattr(threading, "Timer", lambda interval, callback: real_timer(0.03, callback))
    target = WebhookTarget("https://notifications.example.com/inbox", approved=True)
    with pytest.raises(WorkspaceError, match="^webhook_transport_failed$"):
        send_webhook(target, {"delivery_id": "synthetic"})
    assert "shutdown" in events and "connection_closed" in events


def test_webhook_no_network_without_real_target_approval_or_for_plain_http(monkeypatch):
    monkeypatch.setattr(sources, "resolve_source_addresses", lambda *a, **kw: pytest.fail("must reject before DNS"))
    with pytest.raises(WorkspaceError, match="webhook_approval_required"):
        send_webhook(object(), {})
    target = WebhookTarget("http://127.0.0.1:9999/inbox", approved=True, allow_loopback_test_mode=True)
    with pytest.raises(WorkspaceError, match="webhook_https_required"):
        send_webhook(target, {"delivery_id": "synthetic"})
