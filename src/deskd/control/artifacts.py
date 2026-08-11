"""Host-private, content-addressed storage for control-plane review uploads."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..config import CONFIG

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_NAME_BYTES = 255


@dataclass(frozen=True)
class StoredArtifact:
    name: str
    sha256: str
    size_bytes: int
    path: Path

    def public(self) -> dict:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "content_type": "text/plain; charset=utf-8",
        }


def _safe_name(raw: object) -> str:
    name = str(raw or "").strip()
    encoded = name.encode("utf-8")
    if (not name or len(encoded) > _MAX_NAME_BYTES or name in {".", ".."}
            or "/" in name or "\\" in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError("artifact name must be a safe basename (1-255 UTF-8 bytes)")
    return name


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ValueError(f"review artifact directory must be owner-only: {path}")


def _verify_existing(path: Path, digest: str, size: int) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("stored artifact is not a private regular file")
    if info.st_size != size:
        raise ValueError("stored artifact size does not match its digest")
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(128 * 1024), b""):
            hasher.update(block)
    if not hmac.compare_digest(hasher.hexdigest(), digest):
        raise ValueError("stored artifact content does not match its digest")


def store_text(*, name: object, content: object, sha256: object) -> StoredArtifact:
    """Store one bounded UTF-8 upload without trusting a client path.

    The final filename is only the caller-verified content digest.  A crash
    before the DB receipt commits can leave an unreferenced immutable blob, but
    retrying is safe: the same bytes resolve to the same path and are verified
    before reuse.  A temporary file is linked into place atomically, never
    replacing an existing blob.
    """
    safe_name = _safe_name(name)
    if not isinstance(content, str):
        raise ValueError("artifact content must be a UTF-8 string")
    payload = content.encode("utf-8")
    limit = int(CONFIG.review_artifact_max_bytes)
    if limit < 1:
        raise ValueError("review_artifact_max_bytes must be positive")
    if not payload or len(payload) > limit:
        raise ValueError(f"artifact content must be 1-{limit} UTF-8 bytes")
    expected = str(sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(expected):
        raise ValueError("artifact sha256 must be 64 lowercase hex characters")
    actual = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise ValueError("artifact sha256 does not match content")

    configured = CONFIG.review_artifact_root
    if configured is None:
        raise ValueError("review_artifact_root is not configured")
    root = Path(configured)
    if not root.is_absolute():
        raise ValueError("review_artifact_root must be an absolute path")
    _private_directory(root)
    bucket = root / expected[:2]
    _private_directory(bucket)
    target = bucket / expected
    if target.exists() or target.is_symlink():
        _verify_existing(target, expected, len(payload))
        return StoredArtifact(safe_name, expected, len(payload), target)

    temporary = bucket / f".{expected}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            _verify_existing(target, expected, len(payload))
        else:
            # Persist the new directory entry before recording it in SQLite.
            directory_fd = os.open(bucket, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    _verify_existing(target, expected, len(payload))
    return StoredArtifact(safe_name, expected, len(payload), target)
