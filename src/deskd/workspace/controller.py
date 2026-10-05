"""Fixed privileged lifecycle controller. It never creates or rebinds roots.

The service manager must fence before starting/restarting the managed daemon.
This controller renews only exact already-authorized bindings, after protected
installation validation and runtime resume checks. Short leases bound the window
if this process dies; role tools must still be sandboxed from all control paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Callable

from .runtime import RootConfig
from .scheduler import WorkspaceScheduler
from .store import WorkspaceError


@dataclass(frozen=True)
class Seat:
    principal: str
    root_id: str
    manifest_hash: str
    binding_generation: int
    config: RootConfig


def descendant(pid: int, ancestor: int, *, maximum=32) -> bool:
    """Read only Linux PID ancestry, never cmdline, environment or credentials.

    Ancestry is an additional lifecycle check, not role authentication. The
    role sandbox and fixed harness/bridge TCB establish trustworthy metadata.
    """
    if type(pid) is not int or type(ancestor) is not int or pid <= 1 or ancestor <= 1:
        return False
    seen = set()
    for _ in range(maximum):
        if pid == ancestor:
            return True
        if pid in seen or pid <= 1:
            return False
        seen.add(pid)
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
            tail = raw[raw.rindex(")") + 2 :].split()
            pid = int(tail[1])
        except (OSError, ValueError, IndexError):
            return False
    return False


class WorkspaceController:
    def __init__(
        self,
        store,
        runtime,
        admin: Callable,
        seats: tuple[Seat, ...],
        *,
        attest: Callable,
        daemon_alive: Callable,
        accepts_pid: Callable,
        clock=time.monotonic,
    ):
        if (
            not seats
            or len({s.principal for s in seats}) != len(seats)
            or len({s.root_id for s in seats}) != len(seats)
        ):
            raise ValueError("invalid_fixed_seats")
        self.store, self.runtime, self.admin = store, runtime, admin
        self.seats = seats
        self.attest, self.daemon_alive, self.accepts_pid = (
            attest,
            daemon_alive,
            accepts_pid,
        )
        self.clock = clock
        self.generation = None
        self.scheduler = None
        self.last_check = 0.0

    def _admin(self, method, params=None):
        reply = self.admin(method, params or {})
        if reply.get("ok") is not True:
            raise WorkspaceError("gateway_control_rejected")
        return reply["result"]

    def fence(self):
        try:
            self._admin("fence")
        finally:
            if self.generation is not None:
                self.store.fence(self.generation)

    def _active_seats(self):
        """Preserve revocation across both stores, including an interrupted revoke.

        Root declarations are immutable. Revocation may only remove authority;
        a changed binding never becomes a replacement declaration.
        """
        records = self._admin("workspace.bindings")
        rows = self.store.snapshot()["seats"]
        declared = {seat.principal for seat in self.seats}
        if (
            not isinstance(records, list)
            or len(records) != len(declared)
            or {row.get("principal") for row in records} != declared
            or {row.get("principal") for row in rows} != declared
        ):
            raise WorkspaceError("workspace_binding_mismatch")
        bindings = {row["principal"]: row for row in records}
        stored = {row["principal"]: row for row in rows}
        active = []
        for seat in self.seats:
            bound, row = bindings[seat.principal], stored[seat.principal]
            revoked = bound.get("status") == "revoked"
            if (
                bound.get("status") not in {"bound", "revoked"}
                or bound.get("root_id") != seat.root_id
                or bound.get("manifest_hash") != seat.manifest_hash
                or bound.get("binding_generation")
                != seat.binding_generation + int(revoked)
                or row["root_id"] != seat.root_id
                or row["manifest_hash"] != seat.manifest_hash
                or row["binding_generation"] != seat.binding_generation
            ):
                raise WorkspaceError("workspace_binding_mismatch")
            if revoked or row["revoked"]:
                if not revoked or not row["revoked"]:
                    self._admin(
                        "workspace.revoke",
                        {
                            "principal": seat.principal,
                            "expected_binding_generation": seat.binding_generation,
                            "expected_version": row["version"],
                        },
                    )
                continue
            active.append(seat)
        return tuple(active)

    def start(self):
        self._admin("fence")
        self.generation = self.store.start_service()
        try:
            self.attest()
            if not self.daemon_alive():
                raise WorkspaceError("managed_daemon_unavailable")
            attestations = {}
            for seat in self._active_seats():
                binding = self.runtime.resume_root(seat.root_id, seat.config)
                if (
                    binding.thread_id != seat.root_id
                    or binding.session_id != seat.root_id
                ):
                    raise WorkspaceError("root_identity_changed")
                attestations[seat.principal] = {
                    "root_id": seat.root_id,
                    "manifest_hash": seat.manifest_hash,
                    "binding_generation": seat.binding_generation,
                }
            # Configuration equality is checked again after the runtime roundtrip.
            self.attest()
            self._admin("activate")
            self.store.activate(self.generation, attestations)
            self.scheduler = WorkspaceScheduler(
                self.store, self.runtime, self.generation
            )
            self.last_check = self.clock()
        except BaseException:
            self.fence()
            raise

    def _renew(self, active_seats):
        by_root = {s.root_id: s for s in active_seats}
        for connection in self._admin("connections"):
            seat = by_root.get(connection.get("requested_root"))
            if seat is None or not self.accepts_pid(connection["pid"]):
                continue
            desk, name = seat.principal.split("/")
            reply = self.admin(
                "lease",
                {
                    "desk_id": desk,
                    "seat_id": name,
                    "connection_id": connection["connection_id"],
                    "root_session_id": seat.root_id,
                    "binding_generation": seat.binding_generation,
                    "manifest_hash": seat.manifest_hash,
                    "ttl_seconds": 3,
                },
            )
            if reply.get("ok") is not True:
                # A bridge may disconnect after the connection snapshot. Its
                # authority is already gone; this does not invalidate peers.
                if reply.get("error", {}).get("code") == "unknown_live_connection":
                    continue
                raise WorkspaceError("gateway_control_rejected")

    def tick(self):
        if self.scheduler is None:
            raise WorkspaceError("controller_not_started")
        try:
            if not self.daemon_alive():
                raise WorkspaceError("managed_daemon_unavailable")
            if self.clock() - self.last_check >= 1:
                self.attest()
                self.last_check = self.clock()
            active_seats = self._active_seats()
            self._renew(active_seats)
            # Drain notifications before dispatching the next batch. Native TUI
            # turns are allowed but do not count as handled inbox messages.
            for _ in range(100):
                event = self.runtime.poll_event(timeout=0)
                if event is None:
                    break
                if event.get("method") == "turn/completed":
                    params = event.get("params", {})
                    turn = params.get("turn", {})
                    owned = next(
                        (
                            s
                            for s in self.store.snapshot()["seats"]
                            if s["root_id"] == params.get("threadId")
                            and s.get("active_dispatch")
                        ),
                        None,
                    )
                    if owned is not None:
                        dispatch = self.store.dispatch(owned["active_dispatch"])
                        if (
                            dispatch["state"] == "delivered"
                            and dispatch["generation"] == self.generation
                            and dispatch["turn_id"] == turn.get("id")
                        ):
                            self.scheduler.complete_turn(
                                params["threadId"], turn["id"], status=turn["status"]
                            )
            blocked = set()
            for seat in active_seats:
                thread = self.runtime.read_root(seat.root_id)
                status = thread.get("status", {})
                if status.get("type") != "idle":
                    blocked.add(seat.principal)
            result = self.scheduler.tick(blocked_principals=frozenset(blocked))
            if result and result["state"] == "unknown":
                self.fence()
            return result
        except BaseException:
            self.fence()
            raise

    def close(self):
        try:
            self.fence()
        finally:
            self.runtime.close()
