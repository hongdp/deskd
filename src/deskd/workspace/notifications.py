"""Durable human attention and opt-in, metadata-only webhook delivery.

Creating a notice never sends it anywhere. A trusted administrator must supply
an approved target and explicitly run its dispatcher. Retries have stable IDs;
receivers must deduplicate them because HTTP cannot promise exactly-once effects.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Callable

from .store import WorkspaceError, _integer, _text


KINDS = frozenset({"decision", "completion", "stalled", "error", "budget"})
_SCHEMA = """
CREATE TABLE IF NOT EXISTS attention_notices(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_id TEXT NOT NULL,
 revision TEXT NOT NULL, principal TEXT, title TEXT NOT NULL, body TEXT NOT NULL,
 created_at REAL NOT NULL, acknowledged_at REAL);
CREATE INDEX IF NOT EXISTS attention_unread ON attention_notices(acknowledged_at,created_at);
CREATE TABLE IF NOT EXISTS attention_deliveries(
 id TEXT PRIMARY KEY, notice_id TEXT NOT NULL REFERENCES attention_notices(id),
 target_id TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at REAL NOT NULL, lease_until REAL, delivered_at REAL,
 last_error TEXT, UNIQUE(notice_id,target_id));
CREATE INDEX IF NOT EXISTS attention_due ON attention_deliveries(target_id,state,next_attempt_at);
"""


def _digest(value):
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class NotificationStore:
    """Gateway-owned extension sharing WorkspaceStore's ambient transaction."""

    def __init__(self, store):
        self.store = store
        with store._connect(write=True) as conn:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)

    def emit(self, kind, source_id, revision, title, body="", *, principal=None):
        if kind not in KINDS:
            raise WorkspaceError("invalid_notification_kind")
        _text(source_id, "notification_source", 256)
        if type(revision) is int:
            revision = str(revision)
        _text(revision, "notification_revision", 256)
        _text(title, "notification_title", 1024)
        if type(body) is not str or len(body.encode()) > 8192 or "\x00" in body:
            raise WorkspaceError("invalid_notification_body")
        if principal is not None:
            _text(principal, "principal", 256)
        identifier = "notice_" + _digest([kind, source_id, revision])
        with self.store._connect(write=True) as conn:
            old = conn.execute("SELECT * FROM attention_notices WHERE id=?", (identifier,)).fetchone()
            if old is not None:
                if any(old[key] != value for key, value in {"principal": principal, "title": title, "body": body}.items()):
                    raise WorkspaceError("notification_conflict")
                return dict(old)
            conn.execute(
                "INSERT INTO attention_notices VALUES(?,?,?,?,?,?,?,?,NULL)",
                (identifier, kind, source_id, revision, principal, title, body, self.store._now()),
            )
            return dict(conn.execute("SELECT * FROM attention_notices WHERE id=?", (identifier,)).fetchone())

    def list_notifications(self, *, unread_only=False, limit=100):
        if type(unread_only) is not bool:
            raise WorkspaceError("invalid_notification_filter")
        _integer(limit, "limit", 1, 100)
        with self.store._connect() as conn:
            condition = "WHERE acknowledged_at IS NULL " if unread_only else ""
            rows = conn.execute(
                "SELECT * FROM attention_notices " + condition
                + "ORDER BY acknowledged_at IS NOT NULL,CASE WHEN acknowledged_at IS NULL THEN created_at ELSE -created_at END,id LIMIT ?", (limit + 1,),
            ).fetchall()
            unread = conn.execute("SELECT count(*) FROM attention_notices WHERE acknowledged_at IS NULL").fetchone()[0]
            notices = []
            for row in rows[:limit]:
                candidate = [*notices, dict(row)]
                if len(json.dumps(candidate, ensure_ascii=False).encode()) > 65536:
                    break
                notices = candidate
            return {"notifications": notices, "unread_count": unread, "has_more": len(rows) > len(notices)}

    def acknowledge(self, notification_ids):
        if type(notification_ids) is not list or not 1 <= len(notification_ids) <= 100:
            raise WorkspaceError("invalid_notification_ids")
        identifiers = sorted(set(_text(value, "notification_id", 80) for value in notification_ids))
        with self.store._connect(write=True) as conn:
            for identifier in identifiers:
                if conn.execute("SELECT 1 FROM attention_notices WHERE id=?", (identifier,)).fetchone() is None:
                    raise WorkspaceError("unknown_notification")
            now = self.store._now()
            for identifier in identifiers:
                conn.execute("UPDATE attention_notices SET acknowledged_at=coalesce(acknowledged_at,?) WHERE id=?", (now, identifier))
                conn.execute("UPDATE attention_deliveries SET state='cancelled' WHERE notice_id=? AND state='pending'", (identifier,))
        return {"acknowledged": identifiers}

    def counters(self):
        with self.store._connect() as conn:
            return {
                "unread": conn.execute("SELECT count(*) FROM attention_notices WHERE acknowledged_at IS NULL").fetchone()[0],
                "by_kind": {row["kind"]: row["n"] for row in conn.execute("SELECT kind,count(*) AS n FROM attention_notices WHERE acknowledged_at IS NULL GROUP BY kind")},
                "deliveries": {row["state"]: row["n"] for row in conn.execute("SELECT state,count(*) AS n FROM attention_deliveries GROUP BY state")},
            }

    def enqueue_target(self, target_id):
        _text(target_id, "notification_target", 80)
        with self.store._connect(write=True) as conn:
            now = self.store._now()
            created = 0
            for row in conn.execute("SELECT id FROM attention_notices WHERE acknowledged_at IS NULL"):
                identifier = "delivery_" + _digest([row["id"], target_id])
                created += conn.execute(
                    "INSERT OR IGNORE INTO attention_deliveries(id,notice_id,target_id,state,next_attempt_at) VALUES(?,?,?,'pending',?)",
                    (identifier, row["id"], target_id, now),
                ).rowcount
            return created

    def claim(self, target_id, *, max_attempts=6):
        _text(target_id, "notification_target", 80)
        _integer(max_attempts, "max_attempts", 1, 20)
        with self.store._connect(write=True) as conn:
            now = self.store._now()
            conn.execute("UPDATE attention_deliveries SET state='pending',lease_until=NULL WHERE target_id=? AND state='sending' AND lease_until<=?", (target_id, now))
            conn.execute("UPDATE attention_deliveries SET state='cancelled' WHERE target_id=? AND state='pending' AND notice_id IN (SELECT id FROM attention_notices WHERE acknowledged_at IS NOT NULL)", (target_id,))
            conn.execute("UPDATE attention_deliveries SET state='failed' WHERE target_id=? AND state='pending' AND attempts>=?", (target_id, max_attempts))
            row = conn.execute(
                "SELECT d.id,d.notice_id,d.attempts,n.kind,n.created_at FROM attention_deliveries d JOIN attention_notices n ON n.id=d.notice_id "
                "WHERE d.target_id=? AND d.state='pending' AND d.next_attempt_at<=? ORDER BY d.next_attempt_at,d.id LIMIT 1",
                (target_id, now),
            ).fetchone()
            if row is None:
                return None
            attempt = row["attempts"] + 1
            conn.execute("UPDATE attention_deliveries SET state='sending',attempts=?,lease_until=? WHERE id=?", (attempt, now + 60, row["id"]))
            # No title, body, source, principal, endpoint, credentials or task IDs leave the desk.
            return {"id": row["id"], "attempt": attempt, "payload": {"version": 1, "event": "deskd.attention", "delivery_id": row["id"], "notification_id": row["notice_id"], "kind": row["kind"], "created_at": row["created_at"], "message": "Open your deskd workspace to review an update."}}

    def finish(self, delivery_id, attempt, *, delivered, max_attempts=6):
        _text(delivery_id, "delivery_id", 80)
        _integer(attempt, "attempt", 1, 20)
        _integer(max_attempts, "max_attempts", 1, 20)
        if type(delivered) is not bool:
            raise WorkspaceError("invalid_delivery_result")
        with self.store._connect(write=True) as conn:
            row = conn.execute("SELECT state,attempts FROM attention_deliveries WHERE id=?", (delivery_id,)).fetchone()
            if row is None or row["state"] != "sending" or row["attempts"] != attempt:
                raise WorkspaceError("stale_notification_delivery")
            now = self.store._now()
            conn.execute(
                "UPDATE attention_deliveries SET state=?,next_attempt_at=?,lease_until=NULL,delivered_at=?,last_error=? WHERE id=?",
                ("delivered" if delivered else "failed" if attempt >= max_attempts else "pending", now + min(3600, 30 * 2 ** (attempt - 1)), now if delivered else None, None if delivered else "delivery_unconfirmed", delivery_id),
            )


@dataclass(frozen=True)
class WebhookTarget:
    url: str
    approved: bool = False
    allow_loopback_test_mode: bool = False

    def __post_init__(self):
        if self.approved is not True or type(self.allow_loopback_test_mode) is not bool:
            raise WorkspaceError("webhook_approval_required")
        from .sources import validate_source_url

        validate_source_url(self.url, allow_loopback_test_mode=self.allow_loopback_test_mode)

    @property
    def identifier(self):
        return "target_" + _digest(self.url)


def send_webhook(target, payload):
    """Pinned HTTPS with one total deadline, no redirects or response-body reads."""
    import http.client
    import socket
    import threading
    import time
    from .sources import PinnedHTTPSConnection, resolve_source_addresses, validate_source_url

    if not isinstance(target, WebhookTarget) or target.approved is not True:
        raise WorkspaceError("webhook_approval_required")
    deadline = time.monotonic() + 5
    parsed = validate_source_url(target.url, allow_loopback_test_mode=target.allow_loopback_test_mode)
    if parsed.scheme != "https":
        # Plain HTTP is useful only with an injected test sender; never a production delivery path.
        raise WorkspaceError("webhook_https_required")
    addresses = resolve_source_addresses(parsed, allow_loopback_test_mode=target.allow_loopback_test_mode,
                                         timeout_seconds=max(0, deadline - time.monotonic()))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WorkspaceError("webhook_transport_failed")
    encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > 4096:
        raise WorkspaceError("webhook_payload_too_large")
    conn = PinnedHTTPSConnection(parsed.hostname, addresses[0], port=parsed.port or 443, timeout=remaining)
    response = abort = None
    timed_out = threading.Event()
    try:
        conn.connect()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkspaceError("webhook_transport_failed")
        active_socket = conn.sock

        def abort_request():
            timed_out.set()
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        # Socket timeouts alone reset on each header read. A fixed deadline also
        # stops a peer that sends bytes just often enough to evade idle timeouts.
        abort = threading.Timer(remaining, abort_request)
        abort.daemon = True
        abort.start()
        conn.request("POST", parsed.path or "/", body=encoded, headers={
            "Content-Type": "application/json", "Idempotency-Key": payload["delivery_id"],
            "User-Agent": "deskd-notifications/1", "Connection": "close",
        })
        response = conn.getresponse()
        if timed_out.is_set() or time.monotonic() > deadline:
            raise WorkspaceError("webhook_transport_failed")
        # HTTP acknowledgement needs only the status. Closing immediately avoids
        # reading remote prose, compression, large bodies or slow-drip streams.
        return 200 <= response.status < 300
    except (OSError, http.client.HTTPException, ValueError):
        raise WorkspaceError("webhook_transport_failed") from None
    finally:
        if abort is not None:
            abort.cancel()
        if response is not None:
            response.close()
        conn.close()


class NotificationDispatcher:
    def __init__(self, notifications, target, *, sender: Callable = send_webhook, max_attempts=6):
        if not isinstance(target, WebhookTarget) or target.approved is not True:
            raise WorkspaceError("webhook_approval_required")
        _integer(max_attempts, "max_attempts", 1, 20)
        self.notifications, self.target, self.sender, self.max_attempts = notifications, target, sender, max_attempts

    def dispatch(self, *, limit=10):
        _integer(limit, "limit", 1, 100)
        self.notifications.enqueue_target(self.target.identifier)
        result = {"attempted": 0, "delivered": 0, "unconfirmed": 0}
        for _ in range(limit):
            item = self.notifications.claim(self.target.identifier, max_attempts=self.max_attempts)
            if item is None:
                break
            try:
                delivered = self.sender(self.target, item["payload"]) is True
            except Exception:
                # Exception strings can contain destination secrets or remote bodies.
                delivered = False
            self.notifications.finish(item["id"], item["attempt"], delivered=delivered, max_attempts=self.max_attempts)
            result["attempted"] += 1
            result["delivered" if delivered else "unconfirmed"] += 1
        return result
