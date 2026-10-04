"""Local transactional events and independently acknowledged projections.

This module is storage, not an authenticated API or an external-effect runner.
The caller supplies an explicit database path and an already authenticated
principal. SQL callbacks are trusted installation code: they may change domain
or projection tables on the supplied connection only. They must not do network
I/O, open another connection, replace the authorizer, or retain the connection.
A SQLite authorizer catches accidental transaction control and internal-ledger
writes; it does not sandbox hostile Python callbacks.

Producer state, its event and its request receipt commit together. At a consumer,
projection changes and that consumer's receipt commit together. Receipts are
returned only after commit, so a lost ACK can be retried without applying the
projection twice. There is no distributed transaction, automatic pruning,
network delivery, broker integration, or projection-rebuild mutation API.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator
import uuid

SCHEMA_VERSION = 1
MAX_JSON_DEPTH = 32
MAX_JSON_BYTES = 1_048_576


class EventValidationError(ValueError):
    """An event or request is outside the supported JSON/schema contract."""


class EventConflict(ValueError):
    """An existing identity was reused with different immutable content."""


@dataclass(frozen=True)
class PublishReceipt:
    sequence: int
    event: dict[str, Any]
    result: Any


@dataclass(frozen=True)
class ConsumerReceipt:
    consumer_id: str
    event_id: str
    fingerprint: str
    committed_at: str
    result: Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or value != value.strip():
        raise EventValidationError(f"{label} must be a nonempty, trimmed string of at most 256 characters")
    return value


def canonical_json(value: Any) -> str:
    """Strict finite JSON, with stable object order and bounded depth/size.

    This is a protocol encoding, not a numeric equivalence scheme: 1 and 1.0
    remain distinct. No stringification/default serializer is permitted.
    """
    def validate(item: Any, depth: int) -> None:
        if depth > MAX_JSON_DEPTH:
            raise EventValidationError("JSON nesting exceeds the supported depth")
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise EventValidationError("non-finite JSON numbers are forbidden")
            return
        if type(item) is list:
            for member in item:
                validate(member, depth + 1)
            return
        if type(item) is dict:
            for key, member in item.items():
                if type(key) is not str:
                    raise EventValidationError("JSON object keys must be strings")
                validate(member, depth + 1)
            return
        raise EventValidationError("value is not a supported JSON type")

    validate(value, 0)
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        size = len(encoded.encode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise EventValidationError("value cannot be encoded as JSON") from exc
    if size > MAX_JSON_BYTES:
        raise EventValidationError("JSON value exceeds the supported size")
    return encoded


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


_EVENT_FIELDS = frozenset({
    "event_id", "source_id", "principal_id", "request_id", "event_type",
    "schema_version", "occurred_at", "causation_id", "correlation_id", "payload",
    "fingerprint", "provenance",
})


def _validate_event(event: dict[str, Any]) -> dict[str, Any]:
    # Snapshot mutable caller input before using it inside a transaction.
    clean = json.loads(canonical_json(event))
    if type(clean) is not dict or set(clean) != _EVENT_FIELDS:
        raise EventValidationError("event envelope has missing or unknown fields")
    if type(clean["schema_version"]) is not int or clean["schema_version"] != SCHEMA_VERSION:
        raise EventValidationError("unsupported event schema version")
    for field in ("event_id", "source_id", "principal_id", "request_id", "event_type"):
        _identifier(clean[field], field)
    for field in ("causation_id", "correlation_id"):
        if clean[field] is not None:
            _identifier(clean[field], field)
    if type(clean["payload"]) is not dict:
        raise EventValidationError("payload must be a JSON object")
    if type(clean["provenance"]) is not dict:
        raise EventValidationError("provenance must be a JSON object")
    try:
        timestamp = datetime.fromisoformat(clean["occurred_at"])
    except (TypeError, ValueError) as exc:
        raise EventValidationError("occurred_at must be an aware ISO timestamp") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise EventValidationError("occurred_at must include a timezone")
    supplied = clean.pop("fingerprint")
    actual = _fingerprint(clean)
    if supplied != actual:
        raise EventValidationError("event fingerprint does not match its content")
    clean["fingerprint"] = supplied
    return clean


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS events_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS events_outbox (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        fingerprint TEXT NOT NULL,
        envelope_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS events_requests (
        principal_id TEXT NOT NULL, request_id TEXT NOT NULL,
        request_fingerprint TEXT NOT NULL, receipt_json TEXT NOT NULL,
        PRIMARY KEY (principal_id, request_id)
    )""",
    """CREATE TABLE IF NOT EXISTS events_received (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
        envelope_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS events_consumers (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        consumer_id TEXT NOT NULL, event_id TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        UNIQUE (consumer_id, event_id),
        FOREIGN KEY (event_id) REFERENCES events_received(event_id)
    )""",
)


def _callback_authorizer(action: int, first: str | None, second: str | None,
                         database: str | None, trigger: str | None) -> int:
    if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
                  sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                  sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE,
                  sqlite3.SQLITE_CREATE_TABLE):
        # ALTER_TABLE names the database first and the table second.
        table = second if action == sqlite3.SQLITE_ALTER_TABLE else first
        if table and table.startswith("events_"):
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _trusted_call(conn: sqlite3.Connection, callback: Callable[..., Any], *args: Any) -> Any:
    conn.set_authorizer(_callback_authorizer)
    try:
        result = callback(conn, *args)
        if not conn.in_transaction:
            raise RuntimeError("callback escaped the event transaction")
        # Return values are part of the atomic receipt; invalid JSON rolls back
        # the domain/projection mutation as well.
        return json.loads(canonical_json(result))
    finally:
        conn.set_authorizer(None)


class GatewayEventStore:
    """An additive, explicit-path ledger; does not initialize other schemas."""

    def __init__(self, db_path: Path | str):
        if (not isinstance(db_path, (str, Path)) or not str(db_path).strip()
                or str(db_path) == ":memory:" or str(db_path).startswith("file:")):
            raise EventValidationError("a persistent, explicit database path is required")
        self.db_path = Path(db_path).resolve()
        if self.db_path.exists() and not self.db_path.is_file():
            raise EventValidationError("database path must name a file")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            conn.execute(_SCHEMA[0])
            version = conn.execute("SELECT value FROM events_meta WHERE key='schema_version'").fetchone()
            if version is not None and version["value"] != str(SCHEMA_VERSION):
                raise EventValidationError("unsupported events database schema version")
            for statement in _SCHEMA[1:]:
                conn.execute(statement)
            conn.execute("INSERT OR IGNORE INTO events_meta VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
            conn.execute("INSERT OR IGNORE INTO events_meta VALUES ('source_id', ?)",
                         (uuid.uuid4().hex,))

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def publish(self, principal_id: str, request_id: str, event_type: str,
                payload: dict[str, Any], *,
                effect: Callable[[sqlite3.Connection], Any] | None = None,
                validate: Callable[[sqlite3.Connection], Any] | None = None,
                provenance: dict[str, Any] | None = None,
                causation_id: str | None = None, correlation_id: str | None = None,
                schema_version: int = SCHEMA_VERSION) -> PublishReceipt:
        """Commit a trusted SQL effect, event and stable request receipt together.

        The principal must already be authenticated by the caller. The effect
        implementation is trusted and is never taken from a network request.
        Its code identity is not fingerprinted: change the command/event type
        or schema version when changing the meaning of existing requests.
        ``validate`` runs inside the write transaction before receipt lookup,
        including on replay, so callers can recheck current authorization.
        It must have no domain effect; its return value is ignored.
        ``provenance`` is trusted runtime identity context, bound into the event
        but excluded from semantic request identity. Authorized retry from a
        replacement root returns the original receipt and original provenance.
        """
        for label, value in (("principal_id", principal_id), ("request_id", request_id),
                             ("event_type", event_type)):
            _identifier(value, label)
        for label, value in (("causation_id", causation_id), ("correlation_id", correlation_id)):
            if value is not None:
                _identifier(value, label)
        if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
            raise EventValidationError("unsupported event schema version")
        if type(payload) is not dict:
            raise EventValidationError("payload must be a JSON object")
        if provenance is not None and type(provenance) is not dict:
            raise EventValidationError("provenance must be a JSON object")
        provenance = json.loads(canonical_json(provenance if provenance is not None else {}))
        request = json.loads(canonical_json(dict(
            principal_id=principal_id, request_id=request_id, event_type=event_type,
            payload=payload, schema_version=schema_version,
            causation_id=causation_id, correlation_id=correlation_id)))
        request_fingerprint = _fingerprint(request)
        with self._transaction() as conn:
            if validate is not None:
                # Authorization may return a non-JSON principal object. Only
                # its success matters; do not persist or expose that object.
                def check(connection: sqlite3.Connection) -> None:
                    validate(connection)
                _trusted_call(conn, check)
            old = conn.execute(
                "SELECT request_fingerprint, receipt_json FROM events_requests "
                "WHERE principal_id=? AND request_id=?", (principal_id, request_id)).fetchone()
            if old is not None:
                if old["request_fingerprint"] != request_fingerprint:
                    raise EventConflict("request_id was reused with different content")
                receipt = PublishReceipt(**json.loads(old["receipt_json"]))
            else:
                source_id = conn.execute("SELECT value FROM events_meta WHERE key='source_id'").fetchone()[0]
                event = {**request, "event_id": uuid.uuid4().hex, "source_id": source_id,
                         "occurred_at": _now(), "provenance": provenance}
                event["fingerprint"] = _fingerprint(event)
                # Validate the final envelope too: framing may exceed size limits.
                event = _validate_event(event)
                result = _trusted_call(conn, effect) if effect is not None else None
                inserted = conn.execute(
                    "INSERT INTO events_outbox(event_id,fingerprint,envelope_json) VALUES (?,?,?)",
                    (event["event_id"], event["fingerprint"], canonical_json(event)))
                receipt = PublishReceipt(int(inserted.lastrowid), event, result)
                serialized = canonical_json(dict(sequence=receipt.sequence, event=event, result=result))
                conn.execute("INSERT INTO events_requests VALUES (?,?,?,?)",
                             (principal_id, request_id, request_fingerprint, serialized))
        return receipt

    def events(self, *, after_sequence: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Read retained outbox records; there is no global delivered flag."""
        if type(after_sequence) is not int or after_sequence < 0:
            raise EventValidationError("after_sequence must be a nonnegative integer")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise EventValidationError("limit must be between 1 and 1000")
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT sequence,envelope_json FROM events_outbox WHERE sequence>? "
                "ORDER BY sequence LIMIT ?", (after_sequence, limit)).fetchall()
        return [dict(sequence=row["sequence"], event=json.loads(row["envelope_json"])) for row in rows]

    def consume(self, consumer_id: str, event: dict[str, Any], *,
                project: Callable[[sqlite3.Connection, dict[str, Any]], Any]) -> ConsumerReceipt:
        """Apply one trusted SQL projection and return its committed ACK.

        Deduplication is per consumer, while event identity/content is pinned
        across all consumers in this database. Fingerprints detect content
        conflicts, not forgery: the event's source requires authenticated
        transport outside this storage module.
        """
        _identifier(consumer_id, "consumer_id")
        clean = _validate_event(event)
        event_id, fingerprint = clean["event_id"], clean["fingerprint"]
        with self._transaction() as conn:
            received = conn.execute("SELECT fingerprint FROM events_received WHERE event_id=?",
                                    (event_id,)).fetchone()
            if received is not None and received["fingerprint"] != fingerprint:
                raise EventConflict("event_id was reused with different content")
            old = conn.execute(
                "SELECT receipt_json FROM events_consumers WHERE consumer_id=? AND event_id=?",
                (consumer_id, event_id)).fetchone()
            if old is not None:
                receipt = ConsumerReceipt(**json.loads(old["receipt_json"]))
            else:
                if received is None:
                    conn.execute(
                        "INSERT INTO events_received(event_id,fingerprint,envelope_json) VALUES (?,?,?)",
                        (event_id, fingerprint, canonical_json(clean)))
                # A separate snapshot prevents callback mutation of the envelope
                # from changing the persisted identity or receipt binding.
                result = _trusted_call(conn, project, json.loads(canonical_json(clean)))
                receipt = ConsumerReceipt(consumer_id, event_id, fingerprint, _now(), result)
                serialized = canonical_json(dict(
                    consumer_id=consumer_id, event_id=event_id, fingerprint=fingerprint,
                    committed_at=receipt.committed_at, result=result))
                conn.execute("INSERT INTO events_consumers(consumer_id,event_id,receipt_json) VALUES (?,?,?)",
                             (consumer_id, event_id, serialized))
        return receipt

    def projection_events(self, consumer_id: str) -> list[dict[str, Any]]:
        """Read immutable inputs in this consumer's own application order.

        This performs no replay and creates no command, receipt or wake. An
        eventual rebuild API must reset and reconstruct its dedicated tables
        in one transaction with a projection-only callback; ordinary business
        callbacks must never be used for reconstruction.
        """
        _identifier(consumer_id, "consumer_id")
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT r.envelope_json FROM events_received r JOIN events_consumers c "
                "ON c.event_id=r.event_id WHERE c.consumer_id=? ORDER BY c.sequence",
                (consumer_id,)).fetchall()
        return [json.loads(row["envelope_json"]) for row in rows]
