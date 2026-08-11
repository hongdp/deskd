"""Role/service bearer authentication for the container control API.

This identity is intentionally disjoint from supervisor authentication.  A
role token can act only as its one registered role; a service token carries an
explicit non-supervisor scope.  Neither token form can mint, inherit or invoke
supervisor authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from ..config import CONFIG, env

__all__ = ["ControlAuthError", "Principal", "TokenStore"]


class ControlAuthError(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str | None
    scopes: frozenset[str]

    @property
    def actor(self) -> str:
        return self.role or f"service:{self.subject}"

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise ControlAuthError(f"principal lacks required scope: {scope}")


_SUBJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SERVICE_SCOPES = frozenset({
    "read", "directive", "orchestrator", "scheduler", "operator"})


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _read_private_text(path: Path, *, label: str, max_bytes: int) -> str:
    """Open, validate and read one private regular file through the same fd."""
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ControlAuthError(f"{label} is not a readable non-symlink file") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ControlAuthError(f"{label} is not a regular file")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ControlAuthError(
                f"{label} must not be group/world accessible")
        chunks: list[bytes] = []
        seen = 0
        while True:
            block = os.read(descriptor, min(64 * 1024, max_bytes + 1 - seen))
            if not block:
                break
            chunks.append(block)
            seen += len(block)
            if seen > max_bytes:
                raise ControlAuthError(f"{label} exceeds {max_bytes} bytes")
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ControlAuthError(f"{label} is not valid UTF-8") from exc
    finally:
        os.close(descriptor)


def _read_secret(path: Path) -> str:
    value = _read_private_text(
        path, label=f"token secret {path.name!r}", max_bytes=8192).strip()
    if len(value) < 24 or len(value) > 4096 or any(c.isspace() for c in value):
        raise ControlAuthError(
            f"token secret {path.name!r} must be 24-4096 non-space characters")
    return value


class TokenStore:
    """In-memory SHA-256 token index loaded once at service startup.

    The control process may read raw Docker/Kubernetes secret files, but raw
    values are immediately hashed and never retained or written to SQLite.
    """

    def __init__(self, principals_by_hash: dict[str, Principal] | None = None):
        self._principals = dict(principals_by_hash or {})

    @classmethod
    def from_tokens(cls, entries: dict[str, Principal]) -> "TokenStore":
        """Test/embedding seam: raw token -> principal, retained only as hash."""
        return cls({_token_hash(token): principal for token, principal in entries.items()})

    @classmethod
    def from_environment(cls) -> "TokenStore":
        principals: dict[str, Principal] = {}
        known_roles = {r.name for r in CONFIG.roles}
        role_dir_raw = env("ROLE_TOKENS_DIR")
        if role_dir_raw:
            role_dir = Path(role_dir_raw)
            if role_dir.is_symlink() or not role_dir.is_dir():
                raise ControlAuthError("DESKD_ROLE_TOKENS_DIR must be a directory")
            for path in sorted(role_dir.iterdir()):
                if path.name.startswith("."):
                    continue
                role = path.name[:-6] if path.name.endswith(".token") else path.name
                if role == CONFIG.supervisor_role:
                    raise ControlAuthError("supervisor cannot have an agent role token")
                if role not in known_roles:
                    raise ControlAuthError(
                        f"role token {path.name!r} does not name a configured role")
                token = _read_secret(path)
                digest = _token_hash(token)
                if digest in principals:
                    raise ControlAuthError("duplicate role/service token")
                principals[digest] = Principal(
                    subject=f"role:{role}", role=role,
                    scopes=frozenset({"agent"}),
                )

        manifest_raw = env("SERVICE_TOKENS_FILE")
        if manifest_raw:
            manifest_path = Path(manifest_raw)
            try:
                payload = json.loads(_read_private_text(
                    manifest_path, label="DESKD_SERVICE_TOKENS_FILE",
                    max_bytes=1024 * 1024))
            except (ControlAuthError, ValueError) as exc:
                if isinstance(exc, ControlAuthError):
                    raise
                raise ControlAuthError("invalid service token manifest") from exc
            if payload.get("version") != 1 or not isinstance(
                    payload.get("principals", []), list):
                raise ControlAuthError("service token manifest must have version=1")
            for item in payload["principals"]:
                if not isinstance(item, dict):
                    raise ControlAuthError("service token principal must be an object")
                subject = str(item.get("subject") or "")
                scopes = frozenset(item.get("scopes") or [])
                if not _SUBJECT_RE.fullmatch(subject):
                    raise ControlAuthError("invalid service token subject")
                if not scopes or not scopes <= _SERVICE_SCOPES:
                    raise ControlAuthError(
                        f"invalid service scopes for {subject!r}: {sorted(scopes)}")
                if item.get("role") is not None or "supervisor" in scopes:
                    raise ControlAuthError(
                        "service tokens cannot carry a role or supervisor scope")
                digest = item.get("token_sha256")
                secret_file = item.get("token_file")
                if bool(digest) == bool(secret_file):
                    raise ControlAuthError(
                        "service principal needs exactly one token_sha256/token_file")
                if secret_file:
                    digest = _token_hash(_read_secret(Path(secret_file)))
                digest = str(digest).lower()
                if not _HASH_RE.fullmatch(digest):
                    raise ControlAuthError("service token hash must be SHA-256 hex")
                if digest in principals:
                    raise ControlAuthError("duplicate role/service token")
                principals[digest] = Principal(subject, None, scopes)
        return cls(principals)

    @property
    def configured(self) -> bool:
        return bool(self._principals)

    def authenticate(self, authorization: str | None) -> Principal:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token or " " in token:
            raise ControlAuthError("Bearer token required")
        digest = _token_hash(token)
        # Walk and compare rather than dict-indexing only: a timing distinction
        # between known/unknown digests is cheap to avoid at this boundary.
        matched: Principal | None = None
        for expected, principal in self._principals.items():
            if hmac.compare_digest(digest, expected):
                matched = principal
        if matched is None:
            raise ControlAuthError("invalid bearer token")
        if matched.role == CONFIG.supervisor_role:
            # Defence in depth for an embedded TokenStore supplied by a host.
            raise ControlAuthError("supervisor is not an agent API principal")
        return matched

    def status(self) -> dict:
        return {
            "configured": self.configured,
            "role_principals": sum(p.role is not None for p in self._principals.values()),
            "service_principals": sum(p.role is None for p in self._principals.values()),
            "raw_tokens_persisted": False,
            "supervisor_inheritance": False,
        }
