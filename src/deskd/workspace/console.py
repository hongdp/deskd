"""Explicitly paired human console; never a general administrative proxy.

The existing public board stays read-only. This separate interface displays
operator correspondence and shared work only after an independent terminal
approves the browser. Browser proofs never appear in URLs, cookies or logs.
"""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import threading
import time


class ConsoleError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


class BrowserSessions:
    """In-memory, expiring proof bindings. IDs only correlate terminal approval."""

    def __init__(self, *, clock=time.monotonic, on_pair=None):
        self.clock = clock
        self.on_pair = on_pair or (lambda identifier: None)
        self._sessions = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(proof):
        if not isinstance(proof, str) or not re.fullmatch(r"[0-9a-f]{64}", proof):
            raise ConsoleError("session_required", 401)
        return hashlib.sha256(proof.encode("ascii")).digest()

    def _expire(self):
        now = self.clock()
        self._sessions = {k: v for k, v in self._sessions.items() if v["until"] > now}

    def state(self, proof):
        key = self._key(proof)
        with self._lock:
            self._expire()
            entry = self._sessions.get(key)
            if entry is None:
                return {"state": "unpaired"}
            if entry["paired"]:
                return {"state": "paired"}
            return {"state": "pending", "pairing_id": entry["id"]}

    def request(self, proof):
        key = self._key(proof)
        with self._lock:
            self._expire()
            if key in self._sessions:
                return self.state(proof)
            if len(self._sessions) >= 24:
                raise ConsoleError("pairing_busy", 429)
            identifier = secrets.token_hex(4).upper()
            while any(v["id"] == identifier for v in self._sessions.values()):
                identifier = secrets.token_hex(4).upper()
            self._sessions[key] = {"id": identifier, "paired": False, "until": self.clock() + 120}
            self.on_pair(identifier)  # Public correlation only, never the proof.
            return {"state": "pending", "pairing_id": identifier}

    def approve(self, identifier):
        with self._lock:
            self._expire()
            for entry in self._sessions.values():
                if entry["id"] == identifier and not entry["paired"]:
                    entry.update(paired=True, until=self.clock() + 8 * 3600)
                    return True
            return False

    def require(self, proof):
        if self.state(proof)["state"] != "paired":
            raise ConsoleError("session_required", 401)

    def logout(self, proof):
        key = self._key(proof)
        with self._lock:
            self._sessions.pop(key, None)

    def clear(self):
        with self._lock:
            self._sessions.clear()


# No method or administrative identity is supplied by the browser.
COMMANDS = {
    "task": ("workspace.console.task", {"assignee", "title", "body", "request_id"}),
    "message": ("workspace.console.message", {"recipient", "body", "request_id"}),
    "cancel": ("workspace.console.cancel", {"task_id", "expected_version"}),
    "ack": ("workspace.console.read", {"message_ids"}),
    "review": ("workspace.console.review", {"proposal_id", "body_sha256", "reviewer", "request_id"}),
    "pause": ("workspace.pause", {"principal", "paused", "expected_version"}),
}


class ConsoleBackend:
    def __init__(self, admin, *, attest=lambda: None, mode="installed", static_root=None):
        self.admin, self.attest, self.mode = admin, attest, mode
        self.static_root = static_root

    def _call(self, method, params):
        reply = self.admin(method, params)
        if type(reply) is not dict or type(reply.get("ok")) is not bool:
            raise ConsoleError("outcome_unknown", 503)
        if reply["ok"]:
            if "result" not in reply:
                raise ConsoleError("outcome_unknown", 503)
            return reply["result"]
        error = reply.get("error")
        code = error.get("code") if type(error) is dict else None
        if type(code) is not str or not re.fullmatch(r"[a-z_]{1,64}", code):
            raise ConsoleError("outcome_unknown", 503)
        status = 409 if code in {"version_conflict", "request_id_conflict", "task_terminal", "principal_revoked"} else 400
        raise ConsoleError(code, status)

    def snapshot(self):
        self.attest()
        gateway = self._call("status", {})
        snapshot = self._call("workspace.console.snapshot", {})
        if (type(gateway) is not dict or type(gateway.get("fenced")) is not bool
                or type(snapshot) is not dict or type(snapshot.get("service")) is not dict
                or type(snapshot.get("seats")) is not list):
            raise ConsoleError("status_unavailable", 503)
        return {**snapshot, "mode": self.mode, "live_observation": self.mode == "installed",
                "gateway_fenced": gateway["fenced"],
                "fenced": gateway["fenced"] or snapshot["service"].get("active") != 1}

    def command(self, value):
        if type(value) is not dict or set(value) != {"command", "params"}:
            raise ConsoleError("invalid_command")
        command, params = value["command"], value["params"]
        if type(command) is not str or command not in COMMANDS:
            raise ConsoleError("unknown_command")
        method, fields = COMMANDS[command]
        if type(params) is not dict or set(params) != fields:
            raise ConsoleError("invalid_command_params")
        self.attest()
        try:
            return self._call(method, params)
        except OSError as exc:
            # A connection failure is not proof that a mutation did not commit.
            raise ConsoleError("outcome_unknown", 503) from exc


def _decode(body):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("nonfinite")

    try:
        return json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ConsoleError("invalid_json") from exc


def make_server(backend, *, port=0, sessions=None):
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid_console_port")
    sessions = sessions or BrowserSessions()
    static = backend.static_root or Path(__file__).with_name("static")
    assets = {
        "/": (static / "console.html", "text/html; charset=utf-8"),
        "/console.css": (static / "console.css", "text/css; charset=utf-8"),
        "/console.js": (static / "console.js", "text/javascript; charset=utf-8"),
    }

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = False
        request_queue_size = 32

        def __init__(self, *args, **kwargs):
            self._slots = threading.BoundedSemaphore(24)
            super().__init__(*args, **kwargs)

        def process_request(self, request, address):
            if not self._slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, address)
            except BaseException:
                self._slots.release()
                raise

        def process_request_thread(self, request, address):
            try:
                super().process_request_thread(request, address)
            finally:
                self._slots.release()

        def server_close(self):
            sessions.clear()
            super().server_close()

    class Handler(BaseHTTPRequestHandler):
        server_version = "deskd"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def log_message(self, *args):
            pass

        def _reply(self, status, value, kind="application/json; charset=utf-8"):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _origin(self, *, write=False):
            host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get_all("Host") != [host]:
                raise ConsoleError("forbidden_origin", 403)
            origins = self.headers.get_all("Origin")
            if origins != ["http://" + host] and (write or origins is not None):
                raise ConsoleError("forbidden_origin", 403)
            if self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
                raise ConsoleError("forbidden_origin", 403)
            if write and self.headers.get_all("X-Deskd-Console") != ["1"]:
                raise ConsoleError("forbidden_origin", 403)

        def _proof(self):
            values = self.headers.get_all("X-Deskd-Session")
            if not values or len(values) != 1:
                raise ConsoleError("session_required", 401)
            return values[0]

        def _body(self):
            if self.headers.get("Transfer-Encoding") is not None:
                raise ConsoleError("invalid_body")
            if self.headers.get_all("Content-Type") != ["application/json"]:
                raise ConsoleError("invalid_content_type", 415)
            lengths = self.headers.get_all("Content-Length")
            if not lengths or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,7}", lengths[0]):
                raise ConsoleError("invalid_body")
            length = int(lengths[0])
            if length > 512 * 1024:
                raise ConsoleError("body_too_large", 413)
            data = self.rfile.read(length)
            if len(data) != length:
                raise ConsoleError("invalid_body")
            return _decode(data)

        def _run(self, callback):
            try:
                try:
                    callback()
                except ConsoleError as exc:
                    self._reply(exc.status, {"ok": False, "error": {"code": exc.code}})
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    raise
                except Exception:
                    self._reply(503, {"ok": False, "error": {"code": "console_unavailable"}})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

        def do_GET(self):
            def get():
                self._origin()
                if self.path in assets:
                    backend.attest()
                    path, kind = assets[self.path]
                    return self._reply(200, path.read_bytes(), kind)
                if self.path == "/api/session":
                    return self._reply(200, sessions.state(self._proof()))
                if self.path == "/api/snapshot":
                    sessions.require(self._proof())
                    return self._reply(200, {"ok": True, "result": backend.snapshot()})
                raise ConsoleError("not_found", 404)
            self._run(get)

        def do_POST(self):
            def post():
                self._origin(write=True)
                proof = self._proof()
                if self.path == "/api/pair":
                    if self._body() != {}:
                        raise ConsoleError("invalid_command_params")
                    return self._reply(200, sessions.request(proof))
                sessions.require(proof)
                value = self._body()
                if self.path == "/api/logout":
                    if value != {}:
                        raise ConsoleError("invalid_command_params")
                    sessions.logout(proof)
                    return self._reply(200, {"ok": True, "result": {"state": "unpaired"}})
                if self.path == "/api/commands":
                    result = backend.command(value)
                    return self._reply(200, {"ok": True, "result": result})
                raise ConsoleError("not_found", 404)
            self._run(post)

        def do_OPTIONS(self):
            self._reply(405, {"ok": False, "error": {"code": "method_not_allowed"}})

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS

    server = Server(("127.0.0.1", port), Handler)
    server.sessions = sessions
    return server


def serve_console(backend, *, port=0, source=None, target=None):
    """Pair in the launching administrator's terminal, without bearer URLs."""
    import sys
    source = source or sys.stdin
    target = target or sys.stdout

    def notice(identifier):
        print(f"浏览器请求配对：{identifier}。核对浏览器中的编号后，输入 pair {identifier}", file=target, flush=True)

    sessions = BrowserSessions(on_pair=notice)
    server = make_server(backend, port=port, sessions=sessions)
    print(f"http://127.0.0.1:{server.server_port}", file=target, flush=True)
    print("在浏览器中选择连接，随后在此终端确认编号。Ctrl-C 关闭工作台。", file=target, flush=True)

    def approve():
        for line in source:
            match = re.fullmatch(r"pair ([0-9A-Fa-f]{8})\s*", line)
            success = bool(match and sessions.approve(match[1].upper()))
            print("浏览器已连接。" if success else "未确认：编号无效、已过期或已使用。", file=target, flush=True)

    threading.Thread(target=approve, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
