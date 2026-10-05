"""Loopback, read-only workspace status. No inbox bodies or control operations."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote

HTML = b"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>deskd workspace</title><link rel="stylesheet" href="/board.css"><main><header><span>deskd</span><h1>One desk. Separate responsibilities.</h1><p id="health">Connecting to the workspace...</p></header><section id="seats" aria-label="Seats"></section><section><h2>Delivery</h2><p>Queued, delivered and handled are different states. Unknown outcomes require reconciliation.</p><pre id="delivery"></pre></section><footer>Read-only local status. Use the independent management channel for pause, recovery and identity changes. Private messages are omitted.</footer></main><script src="/board.js"></script></html>"""
CSS = b"""*{box-sizing:border-box}body{margin:0;background:#101820;color:#edf0eb;font:16px system-ui}main{max-width:1120px;margin:auto;padding:56px 24px}header span{color:#b5e88b;letter-spacing:.2em;font-size:18px}h1{font-size:clamp(28px,5vw,48px);font-weight:500;max-width:800px;margin:20px 0}p,footer{color:#aebdbd;line-height:1.6}#seats{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px;margin:36px 0}.seat{border:1px solid #425250;border-radius:12px;padding:24px;background:#18232b}.seat h2{margin-top:0;font-size:22px}.seat dl{line-height:1.7}.seat dt{color:#aebdbd}.seat dd{margin:0 0 10px;overflow-wrap:anywhere}pre{white-space:pre-wrap;font:15px system-ui}footer{margin-top:50px;font-size:13px}"""
JS = b""""use strict";async function refresh(){try{const r=await fetch("/status",{cache:"no-store"});if(!r.ok)throw Error();const s=await r.json();document.querySelector("#health").textContent=!s.live_observation?"Recorded ledger state. Live health is unverified.":s.fenced?"Observed fenced: new work is paused until verified recovery.":"Observed active: gateway and workspace responded. Event-driven work; no model heartbeat.";const root=document.querySelector("#seats");root.replaceChildren();for(const seat of s.seats){const card=document.createElement("article");card.className="seat";const title=document.createElement("h2");title.textContent=seat.principal;card.append(title);const dl=document.createElement("dl");for(const [key,value]of Object.entries(seat)){if(key==="principal")continue;const dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=key.replaceAll("_"," ");dd.textContent=String(value);dl.append(dt,dd)}card.append(dl);root.append(card)}document.querySelector("#delivery").textContent=JSON.stringify(s.delivery,null,2)}catch(e){document.querySelector("#health").textContent="Disconnected. Last status may be stale."}finally{setTimeout(refresh,1500)}}refresh();"""


def public_snapshot(snapshot):
    """Explicit allowlist; never serialize arbitrary store rows or message text."""
    seats = []
    for seat in snapshot.get("seats", []):
        seat = dict(seat)
        for state, count in seat.get("inbox", {}).items():
            if (
                state in {"queued", "delivered", "handled", "unknown"}
                and type(count) is int
            ):
                seat[state] = count
        dispatch = next(
            (
                d
                for d in snapshot.get("dispatches", [])
                if d.get("id") == seat.get("active_dispatch")
            ),
            {},
        )
        seat["status"] = (
            "revoked"
            if seat.get("revoked")
            else "paused"
            if seat.get("paused")
            else dispatch.get("state", "busy")
            if seat.get("active_dispatch")
            else "idle"
        )
        seats.append(
            {
                key: seat[key]
                for key in (
                    "principal",
                    "status",
                    "paused",
                    "version",
                    "budget_turns",
                    "turns_used",
                    "next_trigger_at",
                    "oldest_unhandled_at",
                    "queued",
                    "delivered",
                    "handled",
                    "unknown",
                )
                if key in seat
            }
        )
    delivery = {}
    counts = snapshot.get("counts", snapshot.get("delivery", {}))
    if type(counts) is dict:
        delivery = {
            k: v
            for k, v in counts.items()
            if k in {"queued", "delivering", "delivered", "handled", "unknown"}
            and type(v) is int
        }
    if not delivery:
        for seat in snapshot.get("seats", []):
            for state, count in seat.get("inbox", {}).items():
                if (
                    state in {"queued", "delivering", "delivered", "handled", "unknown"}
                    and type(count) is int
                ):
                    delivery[state] = delivery.get(state, 0) + count
    live_observation = snapshot.get("live_observation") is True
    fenced = snapshot.get("service", {}).get("active") != 1
    if live_observation:
        fenced = fenced or snapshot.get("gateway_fenced") is not False
    return {
        "live_observation": live_observation,
        "fenced": fenced,
        "seats": seats,
        "delivery": delivery,
    }


def read_installed_snapshot(deployment):
    """Observe two fixed read-only admin methods using the pinned gateway UID.

    A reachable database cannot prove that its writer or gateway is alive.
    The HTTP handler projects this observed snapshot through public_snapshot;
    private administrative fields never become board response fields. Failure
    propagates to its generic 503 response, rather than retaining a live flag.
    These are point-in-time observations, not a lease or safety guarantee.
    """
    deployment.attest()
    gateway = deployment.admin("status")
    workspace = deployment.admin("workspace.status")
    if (
        type(gateway) is not dict
        or gateway.get("ok") is not True
        or type(gateway.get("result")) is not dict
        or type(gateway["result"].get("fenced")) is not bool
        or type(workspace) is not dict
        or workspace.get("ok") is not True
        or type(workspace.get("result")) is not dict
        or type(workspace["result"].get("service")) is not dict
        or type(workspace["result"]["service"].get("active")) is not int
        or workspace["result"]["service"]["active"] not in (0, 1)
        or type(workspace["result"].get("seats")) is not list
    ):
        raise ValueError("workspace_observation_unavailable")
    return {
        **workspace["result"],
        "live_observation": True,
        "gateway_fenced": gateway["result"]["fenced"],
    }


def make_server(snapshot, *, port=0):
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid_board_port")

    class Handler(BaseHTTPRequestHandler):
        server_version = "deskd"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(2)

        def log_message(self, *args):
            pass

        def _reply(self, code, body, content_type="text/plain; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            allowed_host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != allowed_host or self.headers.get(
                "Origin"
            ) not in (None, "http://" + allowed_host):
                return self._reply(403, b"Forbidden")
            assets = {
                "/": (HTML, "text/html; charset=utf-8"),
                "/board.js": (JS, "text/javascript; charset=utf-8"),
                "/board.css": (CSS, "text/css; charset=utf-8"),
            }
            if self.path in assets:
                body, kind = assets[self.path]
                return self._reply(200, body, kind)
            if self.path == "/status":
                try:
                    body = json.dumps(
                        public_snapshot(snapshot()), allow_nan=False
                    ).encode()
                except Exception:
                    return self._reply(503, b"Status unavailable")
                return self._reply(200, body, "application/json")
            self._reply(404, b"Not found")

        def do_POST(self):
            self._reply(405, b"Read-only board")

        do_PUT = do_DELETE = do_PATCH = do_POST

    return HTTPServer(("127.0.0.1", port), Handler)


def read_snapshot(path):
    """A human observer never opens the gateway database for writing."""
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError("existing_workspace_required")
    conn = sqlite3.connect(
        "file:" + quote(str(path), safe="/") + "?mode=ro", uri=True, timeout=2
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        if conn.execute("PRAGMA application_id").fetchone()[0] != 0x44535731:
            raise ValueError("invalid_workspace_database")
        seats = [
            dict(row)
            for row in conn.execute(
                "SELECT principal,paused,revoked,version,budget_turns,turns_used,active_dispatch FROM seats ORDER BY principal"
            )
        ]
        for seat in seats:
            seat["inbox"] = {
                row["state"]: row["n"]
                for row in conn.execute(
                    "SELECT state,count(*) AS n FROM messages WHERE recipient=? GROUP BY state",
                    (seat["principal"],),
                )
            }
        for seat in seats:
            seat["oldest_unhandled_at"] = conn.execute(
                "SELECT min(created_at) FROM messages WHERE recipient=? AND state!='handled'",
                (seat["principal"],),
            ).fetchone()[0]
            seat["next_trigger_at"] = conn.execute(
                "SELECT min(due_at) FROM timers WHERE owner=? AND cancelled=0",
                (seat["principal"],),
            ).fetchone()[0]
        return {
            "service": dict(
                conn.execute("SELECT generation,active FROM service").fetchone()
            ),
            "seats": seats,
            "dispatches": [
                dict(row)
                for row in conn.execute(
                    "SELECT id,state FROM dispatches WHERE state IN ('delivering','delivered','unknown')"
                )
            ],
        }
    finally:
        conn.close()
