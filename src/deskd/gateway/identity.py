"""Application identities; these objects do not authenticate an OS process.

The transport adapter must obtain peer credentials and channel registration
outside MCP input. In particular, constructing TransportEvidence is NOT an
authentication mechanism. Same-uid unsandboxed code remains in the trusted base.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType


class IdentityError(ValueError):
    """A rejected identity or authority transition, with a stable reason code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class RegistryConflict(IdentityError):
    """An administrative compare-and-swap or uniqueness conflict."""


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise IdentityError(f"invalid_{label}")
    return value


def generation(value: object) -> int:
    if type(value) is not int or value < 0:
        raise IdentityError("invalid_binding_generation")
    return value


def manifest_digest(value: object) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise IdentityError("invalid_manifest_hash")
    return value


@dataclass(frozen=True)
class PrincipalId:
    desk_id: str
    seat_id: str

    def __post_init__(self) -> None:
        identifier(self.desk_id, "desk_id")
        identifier(self.seat_id, "seat_id")
        if len(self.value) > 256:
            raise IdentityError("invalid_principal_length")

    @property
    def value(self) -> str:
        return f"{self.desk_id}/{self.seat_id}"


@dataclass(frozen=True)
class ActionPolicy:
    capability: str
    root_only: bool = False

    def __post_init__(self) -> None:
        identifier(self.capability, "capability")
        if type(self.root_only) is not bool:
            raise IdentityError("invalid_root_only")


ROOT_ONLY_ACTIONS = frozenset({
    "grant.issue", "order.review", "order.submit", "order.cancel", "order.replace",
    "risk.tighten",
})
DEFAULT_ACTIONS = MappingProxyType({
    name: ActionPolicy(name, name in ROOT_ONLY_ACTIONS)
    for name in (*sorted(ROOT_ONLY_ACTIONS), "state.read", "proposal.create")
})


@dataclass(frozen=True)
class TransportEvidence:
    """Server-obtained peer UID and registered channel, never request fields."""

    peer_uid: int
    connection_id: str
    service_generation: str

    def __post_init__(self) -> None:
        if type(self.peer_uid) is not int or self.peer_uid < 0:
            raise IdentityError("invalid_peer_uid")
        identifier(self.connection_id, "connection_id")
        identifier(self.service_generation, "service_generation")


def parse_metadata(params: object) -> tuple[str, str]:
    """Read only MCP params._meta; arguments, role labels and epochs are inert."""
    if not isinstance(params, dict):
        raise IdentityError("invalid_mcp_params")
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        raise IdentityError("missing_mcp_metadata")
    return (identifier(meta.get("sessionId"), "session_id"),
            identifier(meta.get("threadId"), "thread_id"))


@dataclass(frozen=True)
class Binding:
    principal: PrincipalId
    root_session_id: str
    binding_generation: int
    manifest_hash: str
    capabilities: frozenset[str]
    status: str


@dataclass(frozen=True)
class CallIdentity:
    """An authorization snapshot, not a reusable approval or bearer ticket."""

    principal: PrincipalId
    root_session_id: str
    thread_id: str
    binding_generation: int
    service_generation: str
    manifest_hash: str
    action: str
