"""Compose the fixed collaboration surface with the existing gateway.

Configuration and OS directory preparation belong to the privileged installer.
This module opens only explicitly supplied databases and socket paths. It never
reads a credential, auto-binds a role or starts another runtime instance.
"""

from __future__ import annotations

import json
import sqlite3
import threading

from deskd.gateway.actions import MemoWorkflow, WORKFLOW_ACTIONS
from deskd.gateway.commands import GatewayCommands
from deskd.gateway.events import GatewayEventStore
from deskd.gateway.identity import ActionPolicy, IdentityError, PrincipalId
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
