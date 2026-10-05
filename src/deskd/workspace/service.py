"""Compose the fixed collaboration surface with the existing gateway.

Configuration and OS directory preparation belong to the privileged installer.
This module opens only explicitly supplied databases and socket paths. An API
installation may supply its separately guarded model-auth callback. This module
never discovers credentials, auto-binds a role or starts another runtime instance.
"""

from __future__ import annotations

import json
import sqlite3
import threading

from deskd.gateway.actions import MemoWorkflow, WORKFLOW_ACTIONS
from deskd.gateway.commands import GatewayCommands
from deskd.gateway.events import GatewayEventStore
from deskd.gateway.identity import ActionPolicy, IdentityError, PrincipalId, identifier
from deskd.gateway.registry import Registry
from deskd.gateway.transport import GatewayTransport
from .exchange import ACTIONS, WorkspaceExchange
from .store import WorkspaceError, WorkspaceStore


# Explicit, closed administrative protocol. It does not accept a method name,
# SQL statement, callback or arbitrary object from a role/business connection.
_STORE_METHODS = {
    "register_seat": (
        ("principal", "root_id", "manifest_hash"),
        (),
        ("binding_generation", "budget_turns"),
    ),
    "start_service": ((), (), ()),
    "activate": (("generation", "attestations"), (), ()),
    "fence": (("generation",), (), ()),
    "snapshot": ((), (), ()),
    "dispatch": (("dispatch_id",), (), ()),
    "set_paused": (("principal", "paused"), ("expected_version",), ()),
    "revoke": (("principal",), ("expected_version",), ()),
    "set_budget": (("principal", "budget_turns"), ("expected_version",), ()),
    "trusted_enqueue": (("recipient", "body"), ("request_id",), ("priority",)),
    "claim_next": (("generation",), (), ("batch_limit", "blocked_principals")),
    "mark_delivered": (("dispatch_id", "turn_id", "generation"), (), ()),
    "mark_unknown": (("dispatch_id", "generation"), (), ("reason",)),
    "complete_turn": (("root_id", "turn_id", "generation"), (), ("status",)),
    "reconcile": (
        ("dispatch_id",),
        ("generation", "root_id", "turn_id", "outcome"),
        ("human_confirmed",),
    ),
    "fire_timers": (("generation",), (), ()),
    "schedule_timer": (
        ("actor",),
        ("due_at", "body", "request_id"),
        ("interval_seconds",),
    ),
    "cancel_timer": (("actor", "timer_id"), (), ()),
}


class RemoteWorkspaceStore:
    """Controller proxy: every SQLite writer stays in the gateway service UID.

    `admin` must use the protected management socket and authenticate its peer.
    No mutation is retried: a lost admin reply requires explicit reconciliation.
    The method allowlist is installer code, never supplied by a peer or model.
    """

    def __init__(self, admin):
        self._admin = admin

    def __getattr__(self, name):
        specification = _STORE_METHODS.get(name)
        if specification is None:
            raise AttributeError(name)
        positional, required, optional = specification

        def call(*args, **kwargs):
            if len(args) > len(positional):
                raise WorkspaceError("invalid_management_params")
            params = dict(zip(positional, args))
            if set(params) & set(kwargs):
                raise WorkspaceError("invalid_management_params")
            params.update(kwargs)
            needed = set(positional) | set(required)
            if not needed <= set(params) or set(params) - needed - set(optional):
                raise WorkspaceError("invalid_management_params")
            if "blocked_principals" in params:
                if not isinstance(params["blocked_principals"], frozenset):
                    raise WorkspaceError("invalid_blocked_principals")
                params["blocked_principals"] = sorted(params["blocked_principals"])
            reply = self._admin("workspace.store." + name, params)
            if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
                raise WorkspaceError("unknown_management_outcome")
            if reply["ok"]:
                if "result" not in reply:
                    raise WorkspaceError("unknown_management_outcome")
                return reply["result"]
            error = reply.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str) or len(code) > 64 or not code.isidentifier():
                raise WorkspaceError("unknown_management_outcome")
            raise WorkspaceError(code)

        return call


class WorkspaceGateway:
    def __init__(
        self,
        *,
        gateway_db,
        coordination_db,
        harness_uid,
        business_gid,
        business_path,
        admin_path,
        principals,
        activation_check,
        auth_provider=None,
    ):
        self.registry = Registry(
            gateway_db,
            harness_uid=harness_uid,
            actions={
                **WORKFLOW_ACTIONS,
                **ACTIONS,
                "state.read": ActionPolicy("state.read"),
            },
        )
        self.events = GatewayEventStore(gateway_db)
        self.memos = MemoWorkflow(gateway_db)
        self.store = WorkspaceStore(coordination_db)
        self.exchange = WorkspaceExchange(
            self.events, self.store, principals=principals
        )
        self.commands = GatewayCommands(
            self.registry,
            self.events,
            handlers={**self.memos.handlers(), **self.exchange.handlers()},
        )
        self.transport = GatewayTransport(
            self.registry,
            self.commands,
            business_path=business_path,
            admin_path=admin_path,
            business_gid=business_gid,
            activation_check=activation_check,
            readers=self.exchange.readers(),
            admin_handlers=self._handlers(),
            allow_identify=True,
            auth_provider=auth_provider,
        )
        self.stopping = threading.Event()
        self._management_lock = threading.RLock()

    @staticmethod
    def _fields(params, names):
        if type(params) is not dict or set(params) != set(names):
            raise IdentityError("invalid_management_params")

    def _handlers(self):
        def guarded(callback):
            def invoke(params):
                try:
                    with self._management_lock:
                        return callback(params)
                except WorkspaceError as exc:
                    raise IdentityError(exc.code) from exc

            return invoke

        def store_handler(name, specification):
            positional, required, optional = specification

            def invoke(params):
                needed = set(positional) | set(required)
                if (
                    type(params) is not dict
                    or not needed <= set(params)
                    or set(params) - needed - set(optional)
                ):
                    raise IdentityError("invalid_management_params")
                values = dict(params)
                if "blocked_principals" in values:
                    blocked = values["blocked_principals"]
                    if (
                        type(blocked) is not list
                        or len(blocked) > 1000
                        or any(type(principal) is not str for principal in blocked)
                    ):
                        raise IdentityError("invalid_blocked_principals")
                    values["blocked_principals"] = frozenset(blocked)
                return getattr(self.store, name)(**values)

            return guarded(invoke)

        def bindings(params):
            self._fields(params, ())
            # Fixed query over our own authority database. No lease secrets,
            # peer channels, credentials or caller-supplied paths are returned.
            with sqlite3.connect(
                self.registry.db_path.as_uri() + "?mode=ro", uri=True
            ) as conn:
                conn.row_factory = sqlite3.Row
                return [
                    {
                        "principal": row["principal"],
                        "root_id": row["root"],
                        "binding_generation": row["generation"],
                        "manifest_hash": row["manifest"],
                        "status": row["status"],
                        "capabilities": json.loads(row["capabilities"]),
                    }
                    for row in conn.execute(
                        "SELECT principal,root,generation,manifest,status,capabilities FROM bindings ORDER BY principal"
                    )
                ]

        def status(params):
            self._fields(params, ())
            return self.store.snapshot()

        def console_snapshot(params):
            self._fields(params, ())
            result = self.store.console_snapshot()
            observed = {row["principal"]: row for row in bindings({})}
            for seat in result["seats"]:
                binding = observed.get(seat["principal"], {})
                seat["capabilities"] = binding.get("capabilities", [])
                seat["binding_status"] = binding.get("status", "unknown")
            workflow = self.memos.summary(limit=101)
            fields = {
                "proposals": (
                    "proposal_id", "desk_id", "author_principal", "executor_principal",
                    "body", "body_sha256", "created_at", "status",
                ),
                "approvals": (
                    "approval_id", "proposal_id", "issuer_principal", "executor_principal",
                    "body_sha256", "issued_at", "expires_at", "status", "revoked_by",
                    "revoked_at", "consumed_at",
                ),
                "memos": (
                    "memo_id", "proposal_id", "approval_id", "author_principal",
                    "issuer_principal", "executor_principal", "body", "body_sha256",
                    "published_at",
                ),
            }
            for name, allowed in fields.items():
                result[name] = [
                    {key: row[key] for key in allowed if key in row}
                    for row in workflow[name][:100]
                ]
                result["truncated"][name] = len(workflow[name]) > 100
            # A bounded management response containing whole records. Long
            # prose is never silently cut mid-result. Flags explicitly report
            # both row-count and byte-limit omissions to the operator UI.
            sections = ("tasks", "messages", "events", "proposals", "approvals", "memos")

            def size(value):
                return len(json.dumps(
                    value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                ).encode("utf-8"))

            sizes = {name: [size(row) for row in result[name]] for name in sections}
            total = size(result)
            while total > 768 * 1024:
                candidates = [
                    name for name in sections if result[name] and not (
                        name == "messages" and len(result[name]) == 1
                        and result[name][0].get("state") == "unread"
                    )
                ]
                if not candidates:
                    raise WorkspaceError("console_snapshot_too_large")
                name = max(candidates, key=lambda key: sum(sizes[key]))
                result[name].pop()
                total -= sizes[name].pop() + bool(result[name])
                result["truncated"][name] = True
            return result

        def console_task(params):
            self._fields(params, ("assignee", "title", "body", "request_id"))
            return self.store.trusted_add_task(
                params["assignee"], params["title"], params["body"],
                request_id=params["request_id"],
            )

        def console_cancel(params):
            self._fields(params, ("task_id", "expected_version"))
            return self.store.trusted_cancel_task(
                params["task_id"], expected_version=params["expected_version"]
            )

        def console_read(params):
            self._fields(params, ("message_ids",))
            return self.store.trusted_read_messages(params["message_ids"])

        def console_review(params):
            self._fields(params, ("proposal_id", "body_sha256", "reviewer", "request_id"))
            proposal_id = identifier(params["proposal_id"], "proposal_id")
            reviewer = params["reviewer"]
            if type(reviewer) is not str or reviewer.count("/") != 1:
                raise IdentityError("invalid_reviewer")
            identity = PrincipalId(*reviewer.split("/"))
            old = self.store.trusted_review_receipt(
                reviewer, proposal_id, params["body_sha256"], request_id=params["request_id"]
            )
            if old is not None:
                # Observing a receipt grants nothing. A later execution or
                # revocation must not turn a lost response into duplicate work.
                return old
            with sqlite3.connect(
                self.registry.db_path.as_uri() + "?mode=ro", uri=True
            ) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN")
                proposal = conn.execute(
                    "SELECT p.desk_id,p.executor_principal,p.body,p.body_sha256, "
                    "EXISTS(SELECT 1 FROM memo_published m WHERE m.proposal_id=p.proposal_id) AS executed "
                    "FROM memo_proposals p WHERE p.proposal_id=?", (proposal_id,),
                ).fetchone()
                candidate = conn.execute(
                    "SELECT status,capabilities FROM bindings WHERE principal=?", (reviewer,),
                ).fetchone()
            if proposal is None:
                raise IdentityError("proposal_not_found")
            if params["body_sha256"] != proposal["body_sha256"]:
                raise IdentityError("proposal_content_mismatch")
            if proposal["executed"]:
                raise IdentityError("proposal_already_executed")
            if reviewer == proposal["executor_principal"]:
                raise IdentityError("independent_reviewer_required")
            if (
                identity.desk_id != proposal["desk_id"]
                or candidate is None or candidate["status"] != "bound"
                or "approval.issue" not in json.loads(candidate["capabilities"])
            ):
                raise IdentityError("reviewer_not_authorized")
            return self.store.trusted_review_request(
                reviewer, proposal_id, proposal["body_sha256"], proposal["body"],
                request_id=params["request_id"],
            )

        def pause(params):
            self._fields(params, ("principal", "paused", "expected_version"))
            return self.store.set_paused(
                params["principal"],
                params["paused"],
                expected_version=params["expected_version"],
            )

        def enqueue(params):
            self._fields(params, ("recipient", "body", "request_id"))
            return self.store.trusted_enqueue(
                params["recipient"], params["body"], request_id=params["request_id"]
            )

        def budget(params):
            self._fields(params, ("principal", "budget_turns", "expected_version"))
            return self.store.set_budget(
                params["principal"],
                params["budget_turns"],
                expected_version=params["expected_version"],
            )

        def revoke(params):
            self._fields(
                params, ("principal", "expected_binding_generation", "expected_version")
            )
            principal = params["principal"]
            if type(principal) is not str or principal.count("/") != 1:
                raise IdentityError("invalid_principal")
            identity = PrincipalId(*principal.split("/"))
            generation, version = (
                params["expected_binding_generation"],
                params["expected_version"],
            )
            if (
                type(generation) is not int
                or generation < 1
                or type(version) is not int
                or version < 1
            ):
                raise IdentityError("invalid_expected_version")
            observed = next(
                (row for row in bindings({}) if row["principal"] == principal), None
            )
            seat = next(
                (
                    row
                    for row in self.store.snapshot()["seats"]
                    if row["principal"] == principal
                ),
                None,
            )
            if observed is None or seat is None:
                raise IdentityError("unknown_principal")
            generation_options = {observed["binding_generation"]}
            if observed["status"] == "revoked":
                generation_options.add(observed["binding_generation"] - 1)
            if generation not in generation_options:
                raise IdentityError("binding_generation_conflict")
            version_options = {seat["version"]}
            if seat["revoked"]:
                version_options.add(seat["version"] - 1)
            if version not in version_options:
                raise IdentityError("version_conflict")
            # Two databases cannot form one transaction. Remove authority first;
            # a partial failure can only leave scheduling still pending removal.
            if observed["status"] != "revoked":
                revoked = self.registry.trusted_revoke(
                    identity, expected_binding_generation=generation
                )
                result_generation = revoked.binding_generation
            else:
                result_generation = observed["binding_generation"]
            if not seat["revoked"]:
                seat = self.store.revoke(principal, expected_version=version)
            return {
                "principal": principal,
                "revoked": True,
                "binding_generation": result_generation,
                "version": seat["version"],
            }

        return {
            "workspace.status": guarded(status),
            "workspace.console.snapshot": guarded(console_snapshot),
            "workspace.console.task": guarded(console_task),
            "workspace.console.message": guarded(enqueue),
            "workspace.console.cancel": guarded(console_cancel),
            "workspace.console.read": guarded(console_read),
            "workspace.console.review": guarded(console_review),
            "workspace.pause": guarded(pause),
            "workspace.enqueue": guarded(enqueue),
            "workspace.budget": guarded(budget),
            "workspace.revoke": guarded(revoke),
            "workspace.bindings": bindings,
            **{
                "workspace.store." + name: store_handler(name, specification)
                for name, specification in _STORE_METHODS.items()
            },
        }

    def start(self):
        self.transport.start()
        return self

    def serve_forever(self):
        try:
            while not self.stopping.wait(0.05):
                self.exchange.pump()
        finally:
            self.close()

    def close(self):
        self.stopping.set()
        self.transport.close()
