"""Credential-free identity state machine, isolated from deskd's v1 database.

trusted_* methods are internal administrative operations, not authenticated
endpoints. A future fixed lifecycle controller must authenticate the human or
service manager and independently verify manifest/health before invoking them.
No HTTP, MCP transport, signatures, service lifecycle or OS isolation is supplied.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Mapping

from .identity import (
    DEFAULT_ACTIONS, ROOT_ONLY_ACTIONS, ActionPolicy, Binding, CallIdentity,
    IdentityError, PrincipalId, RegistryConflict, TransportEvidence, generation,
    identifier, manifest_digest, parse_metadata,
)

_APPLICATION_ID = 0x44534731
_SCHEMA = """
CREATE TABLE service (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    generation TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1))
);
CREATE TABLE bindings (
    principal TEXT PRIMARY KEY, desk TEXT NOT NULL, seat TEXT NOT NULL,
    root TEXT NOT NULL UNIQUE, generation INTEGER NOT NULL CHECK(generation>0),
    manifest TEXT NOT NULL, capabilities TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('bound','revoked'))
);
CREATE TABLE roots (root TEXT PRIMARY KEY, principal TEXT NOT NULL);
CREATE TABLE leases (
    connection TEXT PRIMARY KEY, service TEXT NOT NULL, principal TEXT NOT NULL,
    root TEXT NOT NULL, binding_generation INTEGER NOT NULL, manifest TEXT NOT NULL,
    expires REAL NOT NULL, revoked INTEGER NOT NULL CHECK(revoked IN (0,1))
);
"""


class Registry:
    """Explicit new SQLite store; each instance must start a fresh fenced service.

    Opening a second handle never adopts persisted active authority. Monotonic
    lease deadlines are usable only within the process service generation that
    issued them. Opening an existing database performs no activation.
    """

    def __init__(self, db_path: Path | str, *, harness_uid: int,
                 actions: Mapping[str, ActionPolicy] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 max_lease_seconds: float = 60.0):
        if type(harness_uid) is not int or harness_uid < 0:
            raise IdentityError("invalid_harness_uid")
        if (not isinstance(db_path, (str, Path)) or not str(db_path).strip()
                or str(db_path) == ":memory:" or str(db_path).startswith("file:")):
            raise IdentityError("invalid_registry_path")
        self.db_path = Path(db_path).resolve()
        if self.db_path.exists() and not self.db_path.is_file():
            raise IdentityError("invalid_registry_path")
        self.harness_uid = harness_uid
        self._clock = clock
        self._max_lease = self._duration(max_lease_seconds)
        self._generation: str | None = None
        self._actions = dict(DEFAULT_ACTIONS if actions is None else actions)
        for name, policy in self._actions.items():
            identifier(name, "action")
            if not isinstance(policy, ActionPolicy):
                raise IdentityError("invalid_action_policy")
        self._initialize()

    @staticmethod
    def _duration(value: object) -> float:
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise IdentityError("invalid_lease_duration")
        return float(value)

    def _now(self) -> float:
        value = self._clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise IdentityError("invalid_clock")
        return float(value)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._transaction() as conn:
            app_id = conn.execute("PRAGMA application_id").fetchone()[0]
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'").fetchall()
            if app_id == _APPLICATION_ID:
                if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise IdentityError("unsupported_registry_version")
                return
            if app_id or tables:
                raise IdentityError("not_a_gateway_registry")
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"PRAGMA application_id={_APPLICATION_ID}")
            conn.execute("PRAGMA user_version=1")

    def _service(self, conn: sqlite3.Connection, expected: str,
                 *, active: bool = False) -> sqlite3.Row:
        identifier(expected, "service_generation")
        row = conn.execute("SELECT * FROM service WHERE singleton=1").fetchone()
        if self._generation is None:
            raise IdentityError("service_not_started")
        if not row or expected != self._generation or row["generation"] != expected:
            raise IdentityError("stale_service_generation")
        if active and not row["active"]:
            raise IdentityError("service_fenced")
        return row

    def start_service(self) -> str:
        """Trusted startup: rotate the local incarnation and fence all channels."""
        current = uuid.uuid4().hex
        with self._transaction() as conn:
            conn.execute("INSERT INTO service VALUES (1,?,0) ON CONFLICT(singleton) "
                         "DO UPDATE SET generation=excluded.generation,active=0", (current,))
            conn.execute("UPDATE leases SET revoked=1")
        self._generation = current
        return current

    def trusted_activate_service(self, *, expected_service_generation: str) -> None:
        """Controller-only AFTER independent lifecycle/config health verification.

        Matching this generation alone is not health proof or OS authentication.
        This slice deliberately does not expose a network activation endpoint.
        """
        with self._transaction() as conn:
            self._service(conn, expected_service_generation)
            conn.execute("UPDATE service SET active=1 WHERE singleton=1")

    def fence(self, *, expected_service_generation: str) -> None:
        with self._transaction() as conn:
            self._service(conn, expected_service_generation)
            conn.execute("UPDATE service SET active=0 WHERE singleton=1")
            conn.execute("UPDATE leases SET revoked=1")

    @staticmethod
    def _binding(row: sqlite3.Row) -> Binding:
        return Binding(PrincipalId(row["desk"], row["seat"]), row["root"],
                       row["generation"], row["manifest"],
                       frozenset(json.loads(row["capabilities"])), row["status"])

    def trusted_bind(self, principal: PrincipalId, *, root_session_id: str,
                     manifest_hash: str, capabilities: frozenset[str],
                     expected_binding_generation: int) -> Binding:
        """Human-admin-only bind/replace; generation 0 means first binding.

        Root ownership never migrates across stable principals, even after revoke.
        Replacing a binding revokes its old channels in this same transaction.
        """
        if not isinstance(principal, PrincipalId):
            raise IdentityError("invalid_principal")
        root = identifier(root_session_id, "root_session_id")
        digest = manifest_digest(manifest_hash)
        expected = generation(expected_binding_generation)
        if not isinstance(capabilities, (set, frozenset)):
            raise IdentityError("invalid_capabilities")
        caps = frozenset(identifier(c, "capability") for c in capabilities)
        with self._transaction() as conn:
            old = conn.execute("SELECT * FROM bindings WHERE principal=?",
                               (principal.value,)).fetchone()
            if (old["generation"] if old else 0) != expected:
                raise RegistryConflict("binding_generation_conflict")
            owner = conn.execute("SELECT principal FROM roots WHERE root=?", (root,)).fetchone()
            if owner and owner["principal"] != principal.value:
                raise RegistryConflict("root_already_owned")
            conn.execute("INSERT OR IGNORE INTO roots VALUES (?,?)", (root, principal.value))
            conn.execute("INSERT INTO bindings VALUES (?,?,?,?,?,?,?, 'bound') "
                         "ON CONFLICT(principal) DO UPDATE SET root=excluded.root, "
                         "generation=excluded.generation,manifest=excluded.manifest, "
                         "capabilities=excluded.capabilities,status='bound'",
                         (principal.value, principal.desk_id, principal.seat_id, root,
                          expected + 1, digest, json.dumps(sorted(caps))))
            conn.execute("UPDATE leases SET revoked=1 WHERE principal=?", (principal.value,))
            return self._binding(conn.execute("SELECT * FROM bindings WHERE principal=?",
                                              (principal.value,)).fetchone())

    def trusted_revoke(self, principal: PrincipalId, *,
                       expected_binding_generation: int) -> Binding:
        if not isinstance(principal, PrincipalId):
            raise IdentityError("invalid_principal")
        expected = generation(expected_binding_generation)
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM bindings WHERE principal=?",
                               (principal.value,)).fetchone()
            if row is None or row["generation"] != expected:
                raise RegistryConflict("binding_generation_conflict")
            conn.execute("UPDATE bindings SET status='revoked', generation=generation+1 "
                         "WHERE principal=?", (principal.value,))
            conn.execute("UPDATE leases SET revoked=1 WHERE principal=?", (principal.value,))
            return self._binding(conn.execute("SELECT * FROM bindings WHERE principal=?",
                                              (principal.value,)).fetchone())

    def trusted_grant_lease(self, principal: PrincipalId, *, connection_id: str,
                            service_generation: str, root_session_id: str,
                            binding_generation: int, manifest_hash: str,
                            ttl_seconds: float) -> None:
        """Controller registers an independently authenticated live connection.

        Inputs come from trusted channel registration, never MCP arguments. A
        revoked channel ID cannot be recycled, including across service restarts.
        """
        if not isinstance(principal, PrincipalId):
            raise IdentityError("invalid_principal")
        channel = identifier(connection_id, "connection_id")
        root = identifier(root_session_id, "root_session_id")
        digest = manifest_digest(manifest_hash)
        epoch = generation(binding_generation)
        ttl = self._duration(ttl_seconds)
        if ttl > self._max_lease:
            raise IdentityError("lease_too_long")
        with self._transaction() as conn:
            self._service(conn, service_generation, active=True)
            row = conn.execute("SELECT * FROM bindings WHERE principal=?",
                               (principal.value,)).fetchone()
            if (not row or row["status"] != "bound" or row["root"] != root
                    or row["generation"] != epoch or row["manifest"] != digest):
                raise IdentityError("binding_mismatch")
            old = conn.execute("SELECT * FROM leases WHERE connection=?", (channel,)).fetchone()
            expected = (service_generation, principal.value, root, epoch, digest)
            if old and (old["revoked"] or tuple(old[k] for k in
                        ("service", "principal", "root", "binding_generation", "manifest")) != expected):
                raise RegistryConflict("channel_cannot_be_rebound")
            expires = self._now() + ttl
            if not math.isfinite(expires):
                raise IdentityError("invalid_lease_deadline")
            conn.execute("INSERT INTO leases VALUES (?,?,?,?,?,?,?,0) "
                         "ON CONFLICT(connection) DO UPDATE SET expires=excluded.expires",
                         (channel, *expected, expires))

    @contextmanager
    def _authorization_transaction(self, connection: sqlite3.Connection | None
                                   ) -> Iterator[sqlite3.Connection | sqlite3.Cursor]:
        if connection is None:
            with self._transaction() as conn:
                yield conn
            return
        if not isinstance(connection, sqlite3.Connection) or not connection.in_transaction:
            raise IdentityError("authorization_requires_transaction")
        databases = connection.execute("PRAGMA database_list").fetchall()
        main = [r for r in databases if r[1] == "main"]
        if (len(main) != 1 or Path(main[0][2]).resolve() != self.db_path
                or any(r[1] not in ("main", "temp") for r in databases)):
            raise IdentityError("authorization_database_mismatch")
        # Acquires the SQLite writer lock even for a caller's deferred BEGIN.
        # An obsolete read snapshot cannot upgrade after a concurrent revoke;
        # SQLite fails it with BUSY rather than allowing a stale authorization.
        connection.execute("UPDATE service SET active=active WHERE singleton=1")
        cursor = connection.cursor()
        cursor.row_factory = sqlite3.Row
        try:
            yield cursor
        finally:
            cursor.close()

    def authorize(self, action: str, params: object,
                  transport: TransportEvidence, *,
                  connection: sqlite3.Connection | None = None) -> CallIdentity:
        """Check one call; optionally share the domain mutation's write transaction.

        Without connection, returned identity is only a snapshot. With connection,
        the caller owns commit/rollback and must apply effect plus outbox before
        releasing that transaction. Neither path returns a reusable approval.
        """
        if not isinstance(transport, TransportEvidence) or transport.peer_uid != self.harness_uid:
            raise IdentityError("untrusted_peer")
        policy = self._actions.get(action) if isinstance(action, str) else None
        if policy is None:
            raise IdentityError("unknown_action")
        session, thread = parse_metadata(params)
        with self._authorization_transaction(connection) as conn:
            self._service(conn, transport.service_generation, active=True)
            lease = conn.execute("SELECT * FROM leases WHERE connection=?",
                                 (transport.connection_id,)).fetchone()
            if (not lease or lease["revoked"] or lease["service"] != transport.service_generation
                    or lease["expires"] <= self._now()):
                raise IdentityError("inactive_channel")
            row = conn.execute("SELECT * FROM bindings WHERE root=?", (session,)).fetchone()
            if (not row or row["status"] != "bound" or lease["root"] != session
                    or lease["principal"] != row["principal"]
                    or lease["binding_generation"] != row["generation"]
                    or lease["manifest"] != row["manifest"]):
                raise IdentityError("binding_mismatch")
            if policy.capability not in json.loads(row["capabilities"]):
                raise IdentityError("capability_denied")
            if (policy.root_only or action in ROOT_ONLY_ACTIONS) and thread != session:
                raise IdentityError("root_required")
            return CallIdentity(PrincipalId(row["desk"], row["seat"]), session, thread,
                                row["generation"], transport.service_generation,
                                row["manifest"], action)
