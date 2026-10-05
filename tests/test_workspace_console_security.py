"""Adversarial HTTP checks with fresh, in-memory browser proofs only.

No proof is logged, persisted to fixtures, put in a URL, or returned as evidence.
The fake backend records only fixed synthetic work, never host state.
"""

from contextlib import contextmanager
import http.client
import json
import secrets
import socket
import threading

import pytest

from deskd.workspace.console import (
    BrowserSessions,
    ConsoleBackend,
    ConsoleError,
    make_server,
)


class Backend:
    static_root = None

    def __init__(self):
        self.calls = []

    def attest(self):
        pass

    def snapshot(self):
        self.calls.append("snapshot")
        return {"synthetic_operator_content": "private synthetic result"}

    def command(self, value):
        self.calls.append("command")
        return {"queued": True}


@contextmanager
def server_fixture():
    sessions = BrowserSessions()
    backend = Backend()
    server = make_server(backend, sessions=sessions)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server, backend, sessions
    finally:
        server.shutdown()
        worker.join(timeout=3)
        server.server_close()


def request(server, method, path, headers=(), body=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for key, value in headers:
            connection.putheader(key, value)
        connection.endheaders(body)
        response = connection.getresponse()
        status, response_headers, payload = response.status, response.getheaders(), response.read()
        return status, dict(response_headers), payload
    finally:
        connection.close()


def pair(sessions):
    proof = secrets.token_hex(32)
    pending = sessions.request(proof)
    assert sessions.approve(pending["pairing_id"])
    return proof


def test_public_pairing_identifier_cannot_be_used_by_another_browser():
    sessions = BrowserSessions()
    first, other = secrets.token_hex(32), secrets.token_hex(32)
    pending = sessions.request(first)
    assert sessions.approve(pending["pairing_id"])
    assert sessions.state(first) == {"state": "paired"}
    assert sessions.state(other) == {"state": "unpaired"}
    assert sessions.state(pending["pairing_id"].lower() * 8) == {"state": "unpaired"}
    with pytest.raises(ConsoleError, match="session_required"):
        sessions.require(other)
    assert not sessions.approve(pending["pairing_id"])


def test_expired_pairing_logout_and_restart_remove_authority():
    now = [100.0]
    sessions = BrowserSessions(clock=lambda: now[0])
    proof = secrets.token_hex(32)
    pending = sessions.request(proof)
    now[0] += 121
    assert not sessions.approve(pending["pairing_id"])
    pending = sessions.request(proof)
    assert sessions.approve(pending["pairing_id"])
    now[0] += 8 * 3600
    with pytest.raises(ConsoleError, match="session_required"):
        sessions.require(proof)
    pending = sessions.request(proof)
    assert sessions.approve(pending["pairing_id"])
    sessions.logout(proof)
    assert sessions.state(proof) == {"state": "unpaired"}
    pending = sessions.request(proof)
    assert sessions.approve(pending["pairing_id"])
    sessions.clear()
    assert sessions.state(proof) == {"state": "unpaired"}


def test_private_http_reads_require_proof_even_with_valid_origin_or_public_id():
    with server_fixture() as (server, backend, sessions):
        host = f"127.0.0.1:{server.server_port}"
        proof = secrets.token_hex(32)
        pending = sessions.request(proof)
        assert sessions.approve(pending["pairing_id"])
        for headers in (
            [("Host", host)],
            [("Host", host), ("Origin", "http://" + host)],
            [("Host", host), ("X-Deskd-Session", pending["pairing_id"].lower() * 8)],
            [("Host", host), ("X-Deskd-Session", proof), ("X-Deskd-Session", proof)],
        ):
            status, _, payload = request(server, "GET", "/api/snapshot", headers)
            assert status == 401
            assert b"private synthetic result" not in payload
        assert backend.calls == []
        status, headers, payload = request(
            server, "GET", "/api/snapshot", [("Host", host), ("X-Deskd-Session", proof)]
        )
        assert status == 200 and b"private synthetic result" in payload
        assert "Set-Cookie" not in headers
        assert headers["Cache-Control"] == "no-store"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert backend.calls == ["snapshot"]


def test_duplicate_hosts_and_foreign_origins_never_reach_private_backend():
    with server_fixture() as (server, backend, sessions):
        host = f"127.0.0.1:{server.server_port}"
        proof = pair(sessions)
        for bad in (
            [("Host", host), ("Host", host)],
            [("Host", "localhost:" + str(server.server_port))],
            [("Host", host), ("Origin", "http://127.0.0.1:1")],
            [("Host", host), ("Origin", "null")],
            [("Host", host), ("Sec-Fetch-Site", "same-site")],
            [("Host", host), ("Sec-Fetch-Site", "cross-site")],
        ):
            status, _, _ = request(
                server, "GET", "/api/snapshot", [*bad, ("X-Deskd-Session", proof)]
            )
            assert status == 403
        assert backend.calls == []


def test_browser_writes_reject_missing_origin_and_ambiguous_http_framing():
    with server_fixture() as (server, backend, sessions):
        host = f"127.0.0.1:{server.server_port}"
        proof = pair(sessions)
        base = [("Host", host), ("X-Deskd-Session", proof)]
        origin = [("Origin", "http://" + host), ("X-Deskd-Console", "1")]
        content = [("Content-Type", "application/json"), ("Content-Length", "2")]
        for headers, expected in (
            ([*base, *content], 403),
            ([*base, *origin, *content, ("Content-Length", "2")], 400),
            ([*base, *origin, *content, ("Transfer-Encoding", "chunked")], 400),
            ([*base, *origin, *content, ("Content-Type", "text/plain")], 415),
        ):
            status, _, _ = request(server, "POST", "/api/commands", headers, b"{}")
            assert status == expected
        assert backend.calls == []


def test_json_duplicates_and_nonfinite_payloads_are_rejected_before_mutation():
    with server_fixture() as (server, backend, sessions):
        host = f"127.0.0.1:{server.server_port}"
        proof = pair(sessions)
        for body in (
            b'{"command":"message","command":"pause","params":{}}',
            b'{"command":"message","params":{"body":1,"body":2}}',
            b'{"command":"message","params":{"body":NaN}}',
        ):
            status, _, payload = request(server, "POST", "/api/commands", [
                ("Host", host), ("X-Deskd-Session", proof),
                ("Origin", "http://" + host), ("X-Deskd-Console", "1"),
                ("Content-Type", "application/json"), ("Content-Length", str(len(body))),
            ], body)
            assert status == 400
            assert json.loads(payload)["error"]["code"] == "invalid_json"
        assert backend.calls == []


@pytest.mark.parametrize("command", ["bind", "lease", "model.auth", "fence", "workspace.store.reconcile"])
def test_browser_never_supplies_an_arbitrary_administrative_method(command):
    called = []
    backend = ConsoleBackend(lambda *args: called.append(args))
    with pytest.raises(ConsoleError, match="unknown_command"):
        backend.command({"command": command, "params": {}})
    assert called == []


def test_spoofed_human_or_role_identity_is_not_forwarded():
    called = []
    backend = ConsoleBackend(lambda *args: called.append(args))
    for extra in ({"sender": "desk/trader"}, {"actor": "@supervisor"}, {"method": "bind"}):
        with pytest.raises(ConsoleError, match="invalid_command_params"):
            backend.command({"command": "message", "params": {
                "recipient": "desk/analyst", "body": "synthetic", "request_id": "r1", **extra,
            }})
    assert called == []


def test_concurrent_slow_clients_are_bounded_before_any_backend_access():
    with server_fixture() as (server, backend, _):
        clients = []
        try:
            # Each incomplete header occupies one of the bounded handlers.
            for _ in range(24):
                client = socket.create_connection(("127.0.0.1", server.server_port), timeout=1)
                clients.append(client)
                client.sendall(b"GET /api/snapshot HTTP/1.1\r\n")
            overflow = socket.create_connection(("127.0.0.1", server.server_port), timeout=1)
            clients.append(overflow)
            # A full handler pool closes an excess connection immediately;
            # it cannot accumulate another waiting root server thread.
            assert overflow.recv(1) == b""
            assert backend.calls == []
        finally:
            for client in clients:
                client.close()
