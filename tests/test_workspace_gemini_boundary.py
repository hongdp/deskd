"""Independent loopback boundary checks with synthetic keys and mock upstream.

No provider request, existing credential, installed workspace, or old process is
used. Test state belongs in the caller's scratchpad pytest base directory.
"""

import http.client
import io
import json
import socket
import threading
import time

import pytest

from deskd.workspace import gemini
from deskd.workspace.gemini import GeminiError, GeminiProxy


MODEL = "gemini-synthetic-test"
KEY = "SYNTHETIC-TEST-ONLY-GOOGLE-KEY"
PROMPT = "SYNTHETIC-PRIVATE-QUESTION"


class Harness:
    def __init__(self):
        self.allowed = True
        self.authorizations = 0
        self.key_reads = 0
        self.requests = []

    def authorize(self):
        self.authorizations += 1
        if not self.allowed:
            raise ValueError("SYNTHETIC-PRIVATE-FENCE-REASON")

    def key(self):
        self.key_reads += 1
        return KEY

    def upstream(self, model, body, key):
        self.requests.append((model, body, key))
        yield {"candidates": [{"content": {"role": "model", "parts": [
            {"text": "SYNTHETIC-PUBLIC-ANSWER"}
        ]}, "finishReason": "STOP"}], "usageMetadata": {
            "promptTokenCount": 3, "candidatesTokenCount": 4, "totalTokenCount": 7
        }}

    def proxy(self, **kwargs):
        return GeminiProxy(port=0, model=MODEL, key_source=self.key,
                           authorize=self.authorize, upstream=self.upstream, **kwargs)


@pytest.fixture
def running():
    harness = Harness()
    proxy = harness.proxy()
    proxy.start()
    try:
        yield harness, proxy, proxy.token()
    finally:
        proxy.close()


def request(proxy, token=None, *, method="POST", path="/v1/responses", body=None,
            headers=None):
    payload = json.dumps(body if body is not None else {
        "model": MODEL, "input": [{"role": "user", "content": PROMPT}], "stream": True,
    }).encode()
    fields = {"Content-Type": "application/json", **(headers or {})}
    if token is not None:
        fields["Authorization"] = "Bearer " + token
    connection = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=3)
    try:
        connection.request(method, path, payload, fields)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_valid_local_cap_uses_only_fixed_model_and_does_not_disclose_key(running, capsys):
    harness, proxy, token = running
    assert token != KEY
    status, response = request(proxy, token)
    assert status == 200
    assert b"SYNTHETIC-PUBLIC-ANSWER" in response
    assert KEY.encode() not in response
    assert token.encode() not in response
    assert harness.key_reads == 1
    assert len(harness.requests) == 1
    assert harness.requests[0][0] == MODEL
    assert harness.requests[0][2] == KEY
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("token", [None, "SYNTHETIC-WRONG-CAP", KEY])
def test_untrusted_local_client_cannot_read_key_or_contact_provider(running, token):
    harness, proxy, _ = running
    status, response = request(proxy, token)
    assert status in (400, 401, 403)
    assert harness.key_reads == 0
    assert harness.requests == []
    assert KEY.encode() not in response
    assert PROMPT.encode() not in response


@pytest.mark.parametrize("headers", [
    {"Origin": "http://127.0.0.1:4242"},
    {"Origin": "null"},
    {"Origin": ""},
    {"Host": "example.invalid"},
    {"Host": "localhost"},
    {"Content-Encoding": "zstd"},
    {"Content-Encoding": "gzip"},
    {"Content-Type": "text/plain"},
    {"Transfer-Encoding": "chunked"},
])
def test_browser_compression_and_framing_variants_never_reach_provider(running, headers):
    harness, proxy, token = running
    status, _ = request(proxy, token, headers=headers)
    assert status in (400, 401, 403, 404, 405, 411, 413, 415, 501)
    assert harness.key_reads == 0
    assert harness.requests == []


@pytest.mark.parametrize("path", [
    "/v1/responses?url=https://example.invalid/", "/v1/models", "/responses",
    "http://127.0.0.1/v1/responses", "/v1/responses#fragment",
])
def test_capability_is_not_a_generic_network_or_path_proxy(running, path):
    harness, proxy, token = running
    status, _ = request(proxy, token, path=path)
    assert status in (400, 401, 403, 404, 405)
    assert harness.key_reads == 0
    assert harness.requests == []


def test_model_in_body_cannot_change_configured_model(running):
    harness, proxy, token = running
    status, _ = request(proxy, token, body={
        "model": "other-expensive-model", "input": PROMPT, "stream": True,
    })
    assert status in (400, 403)
    assert harness.requests == []
    assert harness.key_reads == 0


@pytest.mark.parametrize("field", ["endpoint", "base_url", "url", "api_key"])
def test_request_cannot_override_upstream_routing_or_credentials(running, field):
    harness, proxy, token = running
    status, _ = request(proxy, token, body={
        "model": MODEL, "input": PROMPT, "stream": True,
        field: "http://127.0.0.1:1/SYNTHETIC-UNAPPROVED-ENDPOINT",
    })
    assert status in (400, 403)
    assert harness.key_reads == 0
    assert harness.requests == []


def test_fencing_invalidates_an_already_issued_capability(running):
    harness, proxy, token = running
    harness.allowed = False
    with pytest.raises(ValueError):
        proxy.token()
    status, response = request(proxy, token)
    assert status in (401, 403, 503)
    assert harness.requests == []
    assert harness.key_reads == 0
    assert b"SYNTHETIC-PRIVATE-FENCE-REASON" not in response


def test_new_proxy_generation_rejects_capability_from_previous_generation():
    harness = Harness()
    first = harness.proxy()
    first.start()
    old_cap = first.token()
    assert request(first, old_cap)[0] == 200
    fixed_port = first.port
    first.close()
    harness.key_reads = 0
    harness.requests.clear()
    second = GeminiProxy(port=fixed_port, model=MODEL, key_source=harness.key,
                         authorize=harness.authorize, upstream=harness.upstream)
    second.start()
    try:
        assert second.token() != old_cap
        status, _ = request(second, old_cap)
        assert status in (401, 403)
        assert harness.key_reads == 0
        assert harness.requests == []
    finally:
        second.close()


def test_capability_cannot_be_obtained_before_start_or_after_close():
    harness = Harness()
    proxy = harness.proxy()
    with pytest.raises(ValueError):
        proxy.token()
    proxy.start()
    try:
        assert proxy.token()
    finally:
        proxy.close()
    proxy.close()
    with pytest.raises(ValueError):
        proxy.token()
    assert harness.key_reads == 0


@pytest.mark.parametrize("verdict", [False, True, {}, "SYNTHETIC-AMBIGUOUS"])
def test_ambiguous_authorization_callback_never_grants_local_cap(verdict):
    harness = Harness()
    proxy = GeminiProxy(port=0, model=MODEL, key_source=harness.key,
                        authorize=lambda: verdict, upstream=harness.upstream)
    proxy.start()
    try:
        with pytest.raises(ValueError):
            proxy.token()
        assert harness.key_reads == 0
    finally:
        proxy.close()


def test_occupied_port_fails_without_adopting_or_stopping_owner():
    harness = Harness()
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        proxy = GeminiProxy(port=occupied.getsockname()[1], model=MODEL,
                            key_source=harness.key, authorize=harness.authorize,
                            upstream=harness.upstream)
        try:
            with pytest.raises((OSError, ValueError)):
                proxy.start()
            occupied.settimeout(1)
            with socket.create_connection(occupied.getsockname(), timeout=1):
                accepted, _ = occupied.accept()
                accepted.close()
            assert harness.key_reads == 0
            assert harness.requests == []
        finally:
            proxy.close()


def test_duplicate_content_length_is_rejected_before_any_key_read(running):
    harness, proxy, token = running
    raw = (f"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1:{proxy.port}\r\n"
           f"Authorization: Bearer {token}\r\nContent-Type: application/json\r\n"
           "Content-Length: 2\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}").encode()
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=3) as client:
        client.sendall(raw)
        response = client.recv(4096)
    assert response.startswith((b"HTTP/1.0 400", b"HTTP/1.1 400"))
    assert harness.key_reads == 0
    assert harness.requests == []


def test_upstream_failure_never_returns_key_or_private_provider_error(capsys):
    harness = Harness()

    def fail(_model, _body, _key):
        raise RuntimeError(KEY + ": " + PROMPT)

    proxy = GeminiProxy(port=0, model=MODEL, key_source=harness.key,
                        authorize=harness.authorize, upstream=fail)
    proxy.start()
    try:
        status, response = request(proxy, proxy.token())
        assert status in (200, 502, 503)
        assert KEY.encode() not in response
        assert PROMPT.encode() not in response
        assert capsys.readouterr() == ("", "")
    finally:
        proxy.close()


def test_cancellation_during_connect_cannot_send_a_late_provider_request(monkeypatch):
    """Model a delayed DNS/TLS connection without resolving or connecting anywhere."""
    entered = threading.Event()
    release = threading.Event()
    sent = threading.Event()
    errors = []

    class FakeSocket:
        def sendall(self, _data):
            sent.set()

        def makefile(self, _mode):
            return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                              b"Content-Length: 0\r\n\r\n")

        def shutdown(self, _how):
            pass

        def close(self):
            pass

    def delayed_connect(connection):
        entered.set()
        if not release.wait(2):
            raise TimeoutError("synthetic connection delay")
        connection.sock = FakeSocket()

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", delayed_connect)
    stream = gemini._GoogleStream(MODEL, {"contents": []}, KEY)

    def consume():
        try:
            list(stream)
        except GeminiError:
            pass
        except Exception as error:
            errors.append(type(error).__name__)

    worker = threading.Thread(target=consume)
    worker.start()
    try:
        assert entered.wait(2)
        stream.close()
    finally:
        release.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert not sent.is_set(), "cancelled connect must not later transmit a provider request"
    assert not errors


@pytest.mark.parametrize("stop", ["client_disconnect", "service_close"])
def test_disconnect_or_service_close_cancels_owned_provider_stream(stop):
    harness = Harness()

    class BlockingStream:
        def __init__(self):
            self.entered = threading.Event()
            self.closed = threading.Event()

        def __iter__(self):
            return self

        def __next__(self):
            self.entered.set()
            if not self.closed.wait(2):
                raise RuntimeError("synthetic stream was not cancelled")
            raise StopIteration

        def close(self):
            self.closed.set()

    stream = BlockingStream()
    proxy = GeminiProxy(port=0, model=MODEL, key_source=harness.key,
                        authorize=harness.authorize, upstream=lambda *_: stream)
    proxy.start()
    client = socket.create_connection(("127.0.0.1", proxy.port), timeout=2)
    try:
        payload = json.dumps({"model": MODEL, "input": PROMPT, "stream": True}).encode()
        headers = (f"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1:{proxy.port}\r\n"
                   f"Authorization: Bearer {proxy.token()}\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {len(payload)}\r\n\r\n").encode()
        client.sendall(headers + payload)
        assert stream.entered.wait(2)
        if stop == "client_disconnect":
            client.shutdown(socket.SHUT_RDWR)
            client.close()
        else:
            proxy.close()
        assert stream.closed.wait(1), "owned stream must be closed when its consumer disappears"
        if proxy._server is not None:
            deadline = time.monotonic() + 1
            while proxy._server.active and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not proxy._server.active
    finally:
        stream.close()
        client.close()
        proxy.close()
