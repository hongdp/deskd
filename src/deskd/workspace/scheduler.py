"""Single-loop durable event dispatcher. Runtime transport remains replaceable."""

from __future__ import annotations

from typing import Protocol

from .store import WorkspaceError, WorkspaceStore


class Runtime(Protocol):
    def start_turn(self, root_id: str, events: list[dict]) -> str: ...

    def read_turn(self, root_id: str, turn_id: str) -> str: ...


class WorkspaceScheduler:
    """Commit before calling runtime; never retry an ambiguous external call.

    The adapter must encode these events as untrusted tool output, never as a
    privileged prompt. The controller owns activation and configuration checks.
    No background threads, automatic root creation, LLM heartbeat, or authority
    are supplied by this class.
    """

    def __init__(self, store: WorkspaceStore, runtime: Runtime, generation: str):
        self.store = store
        self.runtime = runtime
        self.generation = generation

    def tick(self, *, blocked_principals: frozenset[str] = frozenset()) -> dict | None:
        self.store.fire_timers(self.generation)
        dispatch = self.store.claim_next(
            self.generation, blocked_principals=blocked_principals
        )
        if dispatch is None:
            return None
        try:
            turn_id = self.runtime.start_turn(dispatch["root_id"], dispatch["events"])
            self.store.mark_delivered(dispatch["id"], turn_id, self.generation)
        except Exception:
            # No exception text enters persistent or public output: upstream
            # exceptions may embed prompt fragments or private configuration.
            self.store.mark_unknown(dispatch["id"], self.generation)
            return {
                "id": dispatch["id"],
                "principal": dispatch["principal"],
                "state": "unknown",
            }
        return {
            "id": dispatch["id"],
            "principal": dispatch["principal"],
            "state": "delivered",
            "turn_id": turn_id,
        }

    def complete_turn(
        self, root_id: str, turn_id: str, *, status: str = "completed"
    ) -> dict:
        return self.store.complete_turn(
            root_id, turn_id, self.generation, status=status
        )

    def reconcile_turn(self, dispatch_id: str, root_id: str, turn_id: str) -> dict:
        """Explicit caller action using a known turn ID on the registered root.

        Missing history is NOT proof that no turn was started. Only a known
        runtime outcome may leave UNKNOWN; a failed read leaves it unchanged.
        """
        dispatch = self.store.dispatch(dispatch_id)
        if dispatch["root_id"] != root_id or dispatch["state"] != "unknown":
            raise WorkspaceError("reconciliation_conflict")
        if dispatch["turn_id"] is None:
            raise WorkspaceError("reconciliation_unproven")
        if dispatch["turn_id"] != turn_id:
            raise WorkspaceError("turn_conflict")
        status = self.runtime.read_turn(root_id, turn_id)
        mapping = {
            "inProgress": "delivered",
            "in_progress": "delivered",
            "running": "delivered",
            "completed": "completed",
            "failed": "failed",
            "interrupted": "interrupted",
        }
        if status not in mapping:
            raise WorkspaceError("reconciliation_unproven")
        return self.store.reconcile(
            dispatch_id,
            generation=self.generation,
            root_id=root_id,
            turn_id=turn_id,
            outcome=mapping[status],
        )
