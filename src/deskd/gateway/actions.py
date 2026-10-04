"""A local multi-principal workflow whose only effect is publishing a DB memo.

Install these fixed handlers in GatewayCommands with WORKFLOW_ACTIONS (or
policies at least as restrictive). The command layer must authenticate callers
and hold the same database write transaction across authorization and effects.
Arguments never supply the author, approver, revoker or executing identity.

Approval is for an immutable proposal body and stable executor principal. It
survives an authorized root replacement, but not its own expiry or revocation.
Revoking an issuer's current login does not retrospectively revoke approvals;
use approval.revoke/revoke_any for that explicit transition. The supplied UTC
clock is trusted and must not move backwards. No network, files chosen by an
agent, trading rules, or external-effect retry mechanism is included.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import math
from pathlib import Path
import re
import sqlite3
import time
from types import MappingProxyType
from typing import Callable, Iterator
import uuid

from .commands import CommandHandler
from .identity import ActionPolicy, CallIdentity, IdentityError, PrincipalId, identifier

MAX_MEMO_BYTES = 65_536
WORKFLOW_ACTIONS = MappingProxyType(
    {
        "proposal.create": ActionPolicy("proposal.create"),
        "approval.issue": ActionPolicy("approval.issue", root_only=True),
        "approval.revoke": ActionPolicy("approval.revoke", root_only=True),
        "approval.revoke_any": ActionPolicy("approval.revoke_any", root_only=True),
        "action.execute": ActionPolicy("action.execute", root_only=True),
    }
)


def tool_catalog() -> list[dict]:
    """Fresh static MCP schemas; listing a tool does not grant its capability.

    The bridge adds/extracts its request_id separately. The administrator-only
    revoke_any verb is intentionally absent from the default tool catalog.
    """
    fields = {
        "proposal.create": {
            "executor_principal": {
                "type": "string",
                "description": "Stable desk/seat principal, not a root thread ID.",
            },
            "body": {"type": "string", "minLength": 1, "maxLength": MAX_MEMO_BYTES},
        },
        "approval.issue": {
            "proposal_id": {"type": "string"},
            "body_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "ttl_seconds": {"type": "number", "exclusiveMinimum": 0},
        },
        "action.execute": {"approval_id": {"type": "string"}},
        "approval.revoke": {"approval_id": {"type": "string"}},
    }
    descriptions = {
        "proposal.create": "Propose an exact shared memo for a designated stable executor.",
        "approval.issue": "Independently approve the immutable memo digest; the issuer must differ from its executor.",
        "action.execute": "Publish the approved memo once as its designated root executor.",
        "approval.revoke": "Revoke an unconsumed approval issued by your stable principal.",
    }
    return [
        dict(
            name=name,
            description=descriptions[name],
            inputSchema={
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        )
        for name, properties in fields.items()
    ]


class ActionError(ValueError):
    """A rejected memo workflow operation, with a non-sensitive reason code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ActionConflict(ActionError):
    """The immutable object or one-shot state does not permit this transition."""


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS memo_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS memo_proposals (
        proposal_id TEXT PRIMARY KEY,
        desk_id TEXT NOT NULL,
        author_principal TEXT NOT NULL,
        executor_principal TEXT NOT NULL,
        body TEXT NOT NULL,
        body_sha256 TEXT NOT NULL,
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS memo_approvals (
        approval_id TEXT PRIMARY KEY,
        proposal_id TEXT NOT NULL REFERENCES memo_proposals(proposal_id),
        issuer_principal TEXT NOT NULL,
        executor_principal TEXT NOT NULL,
        body_sha256 TEXT NOT NULL,
        issued_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','revoked','consumed')),
        revoked_by TEXT,
        revoked_at REAL,
        consumed_at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS memo_published (
        memo_id TEXT PRIMARY KEY,
        proposal_id TEXT NOT NULL UNIQUE REFERENCES memo_proposals(proposal_id),
        approval_id TEXT NOT NULL UNIQUE REFERENCES memo_approvals(approval_id),
        author_principal TEXT NOT NULL,
        issuer_principal TEXT NOT NULL,
        executor_principal TEXT NOT NULL,
        body TEXT NOT NULL,
        body_sha256 TEXT NOT NULL,
        published_at REAL NOT NULL
    )""",
)


def _principal(value: object) -> PrincipalId:
    if not isinstance(value, str) or value.count("/") != 1:
        raise ActionError("invalid_executor_principal")
    try:
        return PrincipalId(*value.split("/"))
    except IdentityError as exc:
        raise ActionError("invalid_executor_principal") from exc


def _args(arguments: dict, required: set[str]) -> None:
    if type(arguments) is not dict or set(arguments) != required:
        raise ActionError("invalid_action_arguments")


def _object_id(value: object, kind: str) -> str:
    try:
        return identifier(value, kind)
    except IdentityError as exc:
        raise ActionError(f"invalid_{kind}") from exc


def _body_digest(body: object) -> str:
    if not isinstance(body, str) or not body.strip():
        raise ActionError("invalid_memo_body")
    try:
        encoded = body.encode("utf-8")
    except UnicodeError as exc:
        raise ActionError("invalid_memo_body") from exc
    if len(encoded) > MAX_MEMO_BYTES:
        raise ActionError("memo_body_too_large")
    return hashlib.sha256(encoded).hexdigest()


def _finite_number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class MemoWorkflow:
    """Additive local schema and fixed handlers; initialize after Registry.

    summary() is a trusted local board read, not an unprotected agent endpoint.
    Exposing it remotely requires the surrounding service's read authorization.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        max_approval_ttl_seconds: float = 3600,
    ):
        if (
            not isinstance(db_path, (str, Path))
            or not str(db_path).strip()
            or str(db_path) == ":memory:"
        ):
            raise ActionError("invalid_workflow_path")
        self.db_path = Path(db_path).resolve()
        self._clock = clock
        if (
            not _finite_number(max_approval_ttl_seconds)
            or max_approval_ttl_seconds <= 0
        ):
            raise ActionError("invalid_max_approval_ttl")
        self._max_ttl = float(max_approval_ttl_seconds)
        # Never create an accidental second authority database through a typo.
        if not self.db_path.is_file():
            raise ActionError("workflow_requires_existing_registry_database")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if not {"bindings", "service", "leases", "roots"}.issubset(tables):
                    raise ActionError("workflow_requires_existing_registry_database")
                conn.execute(_SCHEMA[0])
                version = conn.execute(
                    "SELECT value FROM memo_meta WHERE key='schema_version'"
                ).fetchone()
                if version is not None and version[0] != "1":
                    raise ActionError("unsupported_workflow_schema")
                for statement in _SCHEMA[1:]:
                    conn.execute(statement)
                conn.execute(
                    "INSERT OR IGNORE INTO memo_meta VALUES ('schema_version','1')"
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @contextmanager
    def _connection(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        uri = self.db_path.as_uri() + ("?mode=ro" if readonly else "?mode=rw")
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def _now(self) -> float:
        value = self._clock()
        if not _finite_number(value) or value < 0:
            raise ActionError("invalid_workflow_clock")
        return float(value)

    def _guard(
        self,
        conn: sqlite3.Connection,
        identity: CallIdentity,
        action: str,
        *,
        root_only: bool = True,
    ) -> None:
        if not conn.in_transaction:
            raise ActionError("workflow_requires_command_transaction")
        main = [row for row in conn.execute("PRAGMA database_list") if row[1] == "main"]
        if len(main) != 1 or Path(main[0][2]).resolve() != self.db_path:
            raise ActionError("workflow_database_mismatch")
        if not isinstance(identity, CallIdentity) or identity.action != action:
            raise ActionError("wrong_action_identity")
        if root_only and identity.thread_id != identity.root_session_id:
            raise ActionError("root_required")

    def handlers(self) -> dict[str, CommandHandler]:
        return {
            "proposal.create": CommandHandler("memo.proposed", self._propose),
            "approval.issue": CommandHandler("memo.approved", self._approve),
            "approval.revoke": CommandHandler("memo.approval_revoked", self._revoke),
            "approval.revoke_any": CommandHandler(
                "memo.approval_revoked", self._revoke_any
            ),
            "action.execute": CommandHandler("memo.published", self._execute),
        }

    def _propose(
        self, conn: sqlite3.Connection, args: dict, identity: CallIdentity
    ) -> dict:
        self._guard(conn, identity, "proposal.create", root_only=False)
        _args(args, {"executor_principal", "body"})
        executor = _principal(args["executor_principal"])
        if executor.desk_id != identity.principal.desk_id:
            raise ActionError("cross_desk_executor")
        bound = conn.execute(
            "SELECT status FROM bindings WHERE principal=?", (executor.value,)
        ).fetchone()
        if bound is None or bound["status"] != "bound":
            raise ActionError("executor_not_bound")
        body_sha256 = _body_digest(args["body"])
        proposal_id = "proposal_" + uuid.uuid4().hex
        now = self._now()
        conn.execute(
            "INSERT INTO memo_proposals VALUES (?,?,?,?,?,?,?)",
            (
                proposal_id,
                executor.desk_id,
                identity.principal.value,
                executor.value,
                args["body"],
                body_sha256,
                now,
            ),
        )
        return dict(
            proposal_id=proposal_id,
            author_principal=identity.principal.value,
            executor_principal=executor.value,
            body=args["body"],
            body_sha256=body_sha256,
            created_at=now,
            status="pending",
        )

    @staticmethod
    def _proposal(
        conn: sqlite3.Connection, proposal_id: object, desk: str
    ) -> sqlite3.Row:
        proposal_id = _object_id(proposal_id, "proposal_id")
        row = conn.execute(
            "SELECT * FROM memo_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if row is None or row["desk_id"] != desk:
            raise ActionError("proposal_not_found")
        return row

    @staticmethod
    def _already_published(conn: sqlite3.Connection, proposal_id: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM memo_published WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            is not None
        )

    def _approve(
        self, conn: sqlite3.Connection, args: dict, identity: CallIdentity
    ) -> dict:
        self._guard(conn, identity, "approval.issue")
        _args(args, {"proposal_id", "body_sha256", "ttl_seconds"})
        proposal = self._proposal(conn, args["proposal_id"], identity.principal.desk_id)
        executor = conn.execute(
            "SELECT status FROM bindings WHERE principal=?",
            (proposal["executor_principal"],),
        ).fetchone()
        if executor is None or executor["status"] != "bound":
            raise ActionError("executor_not_bound")
        if identity.principal.value == proposal["executor_principal"]:
            raise ActionError("independent_approver_required")
        if (
            not isinstance(args["body_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", args["body_sha256"])
            or args["body_sha256"] != proposal["body_sha256"]
        ):
            raise ActionError("approval_content_mismatch")
        ttl = args["ttl_seconds"]
        if not _finite_number(ttl) or not 0 < ttl <= self._max_ttl:
            raise ActionError("invalid_approval_ttl")
        if self._already_published(conn, proposal["proposal_id"]):
            raise ActionConflict("proposal_already_executed")
        now = self._now()
        expires = now + float(ttl)
        if not math.isfinite(expires) or expires <= now:
            raise ActionError("invalid_approval_expiry")
        approval_id = "approval_" + uuid.uuid4().hex
        conn.execute(
            "INSERT INTO memo_approvals VALUES (?,?,?,?,?,?,?,'active',NULL,NULL,NULL)",
            (
                approval_id,
                proposal["proposal_id"],
                identity.principal.value,
                proposal["executor_principal"],
                proposal["body_sha256"],
                now,
                expires,
            ),
        )
        return dict(
            approval_id=approval_id,
            proposal_id=proposal["proposal_id"],
            issuer_principal=identity.principal.value,
            executor_principal=proposal["executor_principal"],
            body_sha256=proposal["body_sha256"],
            issued_at=now,
            expires_at=expires,
            status="active",
        )

    def _approval(
        self, conn: sqlite3.Connection, approval_id: object, desk: str
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        approval_id = _object_id(approval_id, "approval_id")
        row = conn.execute(
            "SELECT * FROM memo_approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if row is None:
            raise ActionError("approval_not_found")
        proposal = self._proposal(conn, row["proposal_id"], desk)
        return row, proposal

    def _revoke(
        self, conn: sqlite3.Connection, args: dict, identity: CallIdentity
    ) -> dict:
        return self._revocation(conn, args, identity, control=False)

    def _revoke_any(
        self, conn: sqlite3.Connection, args: dict, identity: CallIdentity
    ) -> dict:
        return self._revocation(conn, args, identity, control=True)

    def _revocation(
        self,
        conn: sqlite3.Connection,
        args: dict,
        identity: CallIdentity,
        *,
        control: bool,
    ) -> dict:
        self._guard(
            conn, identity, "approval.revoke_any" if control else "approval.revoke"
        )
        _args(args, {"approval_id"})
        approval, proposal = self._approval(
            conn, args["approval_id"], identity.principal.desk_id
        )
        if not control and approval["issuer_principal"] != identity.principal.value:
            raise ActionError("approval_issuer_required")
        if approval["status"] != "active":
            raise ActionConflict("approval_not_active")
        now = self._now()
        conn.execute(
            "UPDATE memo_approvals SET status='revoked',revoked_by=?,revoked_at=? "
            "WHERE approval_id=? AND status='active'",
            (identity.principal.value, now, approval["approval_id"]),
        )
        return dict(
            approval_id=approval["approval_id"],
            proposal_id=proposal["proposal_id"],
            status="revoked",
            revoked_by=identity.principal.value,
            revoked_at=now,
        )

    def _execute(
        self, conn: sqlite3.Connection, args: dict, identity: CallIdentity
    ) -> dict:
        self._guard(conn, identity, "action.execute")
        _args(args, {"approval_id"})
        approval, proposal = self._approval(
            conn, args["approval_id"], identity.principal.desk_id
        )
        if identity.principal.value != approval["executor_principal"]:
            raise ActionError("designated_executor_required")
        if approval["issuer_principal"] == identity.principal.value:
            raise ActionError("independent_approver_required")
        if approval["status"] != "active":
            raise ActionConflict("approval_not_active")
        now = self._now()
        if now < approval["issued_at"] or now >= approval["expires_at"]:
            raise ActionError("approval_expired_or_clock_invalid")
        if (
            approval["executor_principal"] != proposal["executor_principal"]
            or approval["body_sha256"] != proposal["body_sha256"]
            or _body_digest(proposal["body"]) != proposal["body_sha256"]
        ):
            raise ActionError("approval_content_mismatch")
        if self._already_published(conn, proposal["proposal_id"]):
            raise ActionConflict("proposal_already_executed")
        memo_id = "memo_" + uuid.uuid4().hex
        conn.execute(
            "UPDATE memo_approvals SET status='consumed',consumed_at=? "
            "WHERE approval_id=? AND status='active'",
            (now, approval["approval_id"]),
        )
        conn.execute(
            "INSERT INTO memo_published VALUES (?,?,?,?,?,?,?,?,?)",
            (
                memo_id,
                proposal["proposal_id"],
                approval["approval_id"],
                proposal["author_principal"],
                approval["issuer_principal"],
                identity.principal.value,
                proposal["body"],
                proposal["body_sha256"],
                now,
            ),
        )
        return dict(
            memo_id=memo_id,
            proposal_id=proposal["proposal_id"],
            approval_id=approval["approval_id"],
            author_principal=proposal["author_principal"],
            issuer_principal=approval["issuer_principal"],
            executor_principal=identity.principal.value,
            body=proposal["body"],
            body_sha256=proposal["body_sha256"],
            published_at=now,
            status="published",
        )

    def summary(self, *, limit: int = 100) -> dict:
        """Read a consistent local board snapshot without changing business state."""
        return self._summary(limit=limit, principal=None)

    def summary_for(self, principal: PrincipalId, *, limit: int = 100) -> dict:
        """Participant drafts/approvals and desk-wide published memos.

        This is a filter, not authentication. A transport must obtain principal
        from its authorized server identity, never an agent-supplied argument.
        """
        if not isinstance(principal, PrincipalId):
            raise ActionError("invalid_summary_principal")
        return self._summary(limit=limit, principal=principal)

    def _summary(self, *, limit: int, principal: PrincipalId | None) -> dict:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ActionError("invalid_summary_limit")
        now = self._now()
        proposal_where, approval_where, memo_where = "", "", ""
        proposal_params = approval_params = memo_params = ()
        if principal is not None:
            proposal_where = (
                "WHERE p.desk_id=? AND (p.author_principal=? OR p.executor_principal=? "
                "OR EXISTS(SELECT 1 FROM memo_approvals visible WHERE visible.proposal_id=p.proposal_id "
                "AND visible.issuer_principal=?)) "
            )
            proposal_params = (
                principal.desk_id,
                principal.value,
                principal.value,
                principal.value,
            )
            approval_where = (
                "WHERE EXISTS(SELECT 1 FROM memo_proposals p WHERE p.proposal_id=a.proposal_id "
                "AND p.desk_id=? AND (p.author_principal=? OR p.executor_principal=? OR a.issuer_principal=?)) "
            )
            approval_params = proposal_params
            memo_where = "WHERE EXISTS(SELECT 1 FROM memo_proposals p WHERE p.proposal_id=m.proposal_id AND p.desk_id=?) "
            memo_params = (principal.desk_id,)
        with self._connection(readonly=True) as conn:
            conn.execute("BEGIN")
            proposals = [
                dict(row)
                for row in conn.execute(
                    "SELECT p.*, CASE WHEN EXISTS(SELECT 1 FROM memo_published m WHERE m.proposal_id=p.proposal_id) "
                    "THEN 'executed' WHEN EXISTS(SELECT 1 FROM memo_approvals a WHERE a.proposal_id=p.proposal_id "
                    "AND a.status='active' AND a.expires_at>?) THEN 'approved' ELSE 'pending' END AS status "
                    "FROM memo_proposals p "
                    + proposal_where
                    + "ORDER BY p.created_at DESC,p.proposal_id LIMIT ?",
                    (now, *proposal_params, limit),
                )
            ]
            approvals = [
                dict(row)
                for row in conn.execute(
                    "SELECT a.*, EXISTS(SELECT 1 FROM memo_published m WHERE m.proposal_id=a.proposal_id) "
                    "AS proposal_executed FROM memo_approvals a "
                    + approval_where
                    + "ORDER BY issued_at DESC,approval_id LIMIT ?",
                    (*approval_params, limit),
                )
            ]
            memos = [
                dict(row)
                for row in conn.execute(
                    "SELECT m.* FROM memo_published m "
                    + memo_where
                    + "ORDER BY published_at DESC,memo_id LIMIT ?",
                    (*memo_params, limit),
                )
            ]
        for approval in approvals:
            executed = approval.pop("proposal_executed")
            if approval["status"] == "active":
                if executed:
                    approval["status"] = "superseded"
                elif approval["expires_at"] <= now:
                    approval["status"] = "expired"
        return dict(proposals=proposals, approvals=approvals, memos=memos, as_of=now)
