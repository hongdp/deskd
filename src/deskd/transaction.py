"""Ambient SQLite transaction seam used by the control-plane adapter.

The public engine API historically opens one connection per call.  A network
command additionally needs its idempotency receipt, domain mutation and event
cursor to commit atomically.  The control plane opens the outer transaction and
binds it here; nested public API calls reuse that exact connection and leave
commit/rollback to the outer owner.

Normal library and CLI calls never bind an ambient connection and retain their
existing connection lifecycle.  A context variable makes the seam safe across
concurrent async request tasks and nested calls.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class AmbientTransaction:
    connection: sqlite3.Connection
    path: Path


_CURRENT: ContextVar[AmbientTransaction | None] = ContextVar(
    "deskd_ambient_transaction", default=None)


def current(db_path: Path | str) -> sqlite3.Connection | None:
    ambient = _CURRENT.get()
    if ambient is None:
        return None
    requested = Path(db_path).resolve(strict=False)
    if requested != ambient.path:
        raise RuntimeError(
            "an engine command tried to cross databases inside one transaction")
    return ambient.connection


def owns(connection: sqlite3.Connection) -> bool:
    """Whether ``connection`` is the control plane's current outer txn."""
    ambient = _CURRENT.get()
    return ambient is not None and ambient.connection is connection


def begin_immediate(connection: sqlite3.Connection) -> None:
    """Take the legacy per-operation lock unless an outer txn already did.

    Mailbox public operations predate the control API and explicitly issue
    ``BEGIN IMMEDIATE`` inside their connection context.  Under an ambient
    command transaction that lock is already held; a nested BEGIN is an error,
    not added safety.  Normal CLI/library calls retain the original statement.
    """
    if not owns(connection):
        connection.execute("BEGIN IMMEDIATE")


@contextmanager
def bind(connection: sqlite3.Connection,
         db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Make ``connection`` the nested engine connection for this context."""
    if _CURRENT.get() is not None:
        raise RuntimeError("ambient deskd transactions cannot be nested")
    token = _CURRENT.set(AmbientTransaction(
        connection=connection, path=Path(db_path).resolve(strict=False)))
    try:
        yield connection
    finally:
        _CURRENT.reset(token)
