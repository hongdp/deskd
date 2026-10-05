"""Offline, facts-only archives and inert operational guidance.

Archives intentionally omit runtime identity, leases, credentials, approval
authority and active dispatches. Restoring creates a detached archive, never a
running deployment. An administrator must explicitly reconcile/import facts
and establish new identities before resuming any work.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from urllib.parse import quote

from .store import WorkspaceError


ARCHIVE_ID = 0x44534F31
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
# These are facts, never service roots, capability leases or executable policy.
WORKSPACE_FACTS = {
    "seats": ("principal", "version", "paused", "revoked", "budget_turns", "turns_used"),
    "tasks": ("id", "creator", "assignee", "title", "detail", "status", "version", "created_at", "updated_at"),
    "dependencies": ("task_id", "depends_on"),
    "messages": ("id", "sender", "recipient", "kind", "body", "priority", "created_at", "state"),
    "operator_messages": ("id", "sender", "body", "created_at", "is_read"),
    "timers": ("id", "owner", "body", "due_at", "interval_seconds", "sequence", "cancelled"),
    "events": ("sequence", "kind", "actor", "ref", "created_at"),
    "attention_notices": ("id", "kind", "source_id", "revision", "principal", "title", "body", "created_at", "acknowledged_at"),
    "workspace_sources": ("name", "url", "max_bytes", "timeout_seconds", "enabled", "version", "configured_at"),
    "source_jobs": ("id", "owner", "source", "source_version", "url", "max_bytes", "timeout_seconds", "state", "shared", "attempts", "content", "digest", "content_type", "error", "created_at", "completed_at"),
    "source_notices": ("job_id", "state"),
    "workspace_memories": ("id", "owner", "version", "shared", "forgotten", "created_at", "updated_at"),
    "memory_revisions": ("memory_id", "version", "title", "body", "sources", "digest", "created_at"),
    "workspace_goals": ("id", "title", "objective", "researcher", "reviewer", "executor", "source_ids", "state", "version", "cycle", "max_cycles", "interval_seconds", "followup_seconds", "max_followups", "next_due", "created_at", "updated_at", "question", "question_actor", "blocked_reason"),
    "workspace_goal_cycles": ("goal_id", "number", "phase", "research_task", "review_task", "delivery_task", "proposal_id", "approval_id", "memo_id", "body_sha256", "evidence_ids", "followups", "next_followup", "started_at", "completed_at"),
    "workspace_goal_notices": ("id", "goal_id", "cycle", "kind", "body", "created_at", "is_read"),
    "workspace_goal_messages": ("message_id", "goal_id", "cycle"),
}
GATEWAY_FACTS = {
    "memo_proposals": ("proposal_id", "desk_id", "author_principal", "executor_principal", "body", "body_sha256", "created_at"),
    "memo_published": ("memo_id", "proposal_id", "approval_id", "author_principal", "issuer_principal", "executor_principal", "body", "body_sha256", "published_at"),
}


def _path(value, *, file=False, fresh=False):
    path = Path(value).absolute()
    if str(value) in ("", ":memory:") or str(value).startswith("file:"):
        raise WorkspaceError("invalid_operations_path")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise WorkspaceError("unsafe_operations_path")
    if fresh and path.exists():
        raise WorkspaceError("destination_exists")
    if file and (not path.is_file() or path.stat().st_nlink != 1):
        raise WorkspaceError("invalid_operations_file")
    return path


@contextmanager
def _readonly(path):
    path = _path(path, file=True)
    conn = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def _hash_file(path):
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                raise WorkspaceError("archive_too_large")
            hasher.update(chunk)
    return hasher.hexdigest()


def _fact_tables():
    return {**{"workspace_" + name: fields for name, fields in WORKSPACE_FACTS.items()}, **{"gateway_" + name: fields for name, fields in GATEWAY_FACTS.items()}}


def export_backup(workspace_db, gateway_db, destination, *, offline_confirmed=False):
    """Export only fixed fact columns while the explicitly selected service is fenced.

    The caller must have stopped this deployment's writers. A read-only SQLite
    snapshot is consistent per database, not an atomic snapshot across services.
    No deployment, provider configuration or arbitrary files are discovered.
    """
    if offline_confirmed is not True:
        raise WorkspaceError("offline_confirmation_required")
    destination = _path(destination, fresh=True)
    if not destination.parent.is_dir():
        raise WorkspaceError("invalid_archive_parent")
    with _readonly(workspace_db) as workspace, _readonly(gateway_db) as gateway:
        if workspace.execute("PRAGMA application_id").fetchone()[0] != 0x44535731:
            raise WorkspaceError("invalid_workspace_database")
        if gateway.execute("PRAGMA application_id").fetchone()[0] != 0x44534731:
            raise WorkspaceError("invalid_gateway_database")
        if workspace.execute("SELECT active FROM service WHERE singleton=1").fetchone()[0] != 0:
            raise WorkspaceError("workspace_must_be_fenced")
        if gateway.execute("SELECT active FROM service WHERE singleton=1").fetchone()[0] != 0:
            raise WorkspaceError("gateway_must_be_fenced")
        for source in (workspace, gateway):
            if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise WorkspaceError("source_database_invalid")
        destination.mkdir(mode=0o700)
        archive_path = destination / "facts.sqlite"
        descriptor = os.open(archive_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        archive = sqlite3.connect(archive_path)
        tables = {}
        measured_bytes = 0
        try:
            archive.execute(f"PRAGMA application_id={ARCHIVE_ID}")
            archive.execute("PRAGMA user_version=1")
            for prefix, source, allowed in (("workspace_", workspace, WORKSPACE_FACTS), ("gateway_", gateway, GATEWAY_FACTS)):
                existing = {row[0]: row[1] for row in source.execute("SELECT name,type FROM sqlite_master WHERE type IN ('table','view')")}
                for table, columns in allowed.items():
                    if table not in existing:
                        continue
                    if existing[table] != "table":
                        raise WorkspaceError("invalid_fact_table")
                    available = {row[1] for row in source.execute(f'PRAGMA table_info("{table}")')}
                    if not set(columns) <= available:
                        raise WorkspaceError("incompatible_fact_schema")
                    target_table = prefix + table
                    names = ",".join('"' + column + '"' for column in columns)
                    archive.execute(f'CREATE TABLE "{target_table}" ({names})')
                    count = 0
                    rows = source.execute(f'SELECT {names} FROM "{table}"')
                    while batch := rows.fetchmany(100):
                        values = [tuple(row) for row in batch]
                        measured_bytes += sum(len(value.encode()) if isinstance(value, str) else len(value) if isinstance(value, bytes) else 16 for row in values for value in row if value is not None)
                        if measured_bytes > MAX_ARCHIVE_BYTES:
                            raise WorkspaceError("archive_too_large")
                        archive.executemany(f'INSERT INTO "{target_table}" VALUES({",".join("?" for _ in columns)})', values)
                        count += len(values)
                    tables[target_table] = {"columns": list(columns), "rows": count}
            archive.commit()
        finally:
            archive.close()
        manifest = {
            "format": "deskd-detached-facts", "version": 1, "created_at": time.time(),
            "facts_sha256": _hash_file(archive_path), "tables": tables,
            "contains_runtime_authority": False, "requires_rebinding": True,
        }
        descriptor = os.open(destination / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, sort_keys=True)
        return {"path": str(destination), "tables": len(tables), "rows": sum(item["rows"] for item in tables.values()), "requires_rebinding": True}


def verify_backup(archive_path):
    archive_path = _path(archive_path)
    if not archive_path.is_dir() or stat.S_IMODE(archive_path.stat().st_mode) & 0o077:
        raise WorkspaceError("unsafe_archive_directory")
    manifest_path = _path(archive_path / "manifest.json", file=True)
    facts_path = _path(archive_path / "facts.sqlite", file=True)
    if set(path.name for path in archive_path.iterdir()) != {"manifest.json", "facts.sqlite"}:
        raise WorkspaceError("unexpected_archive_files")
    if any(stat.S_IMODE(path.stat().st_mode) & 0o077 for path in (manifest_path, facts_path)):
        raise WorkspaceError("unsafe_archive_permissions")
    if manifest_path.stat().st_size > 65536:
        raise WorkspaceError("invalid_archive_manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if type(manifest) is not dict or set(manifest) != {"format", "version", "created_at", "facts_sha256", "tables", "contains_runtime_authority", "requires_rebinding"}:
            raise ValueError
        if manifest["format"] != "deskd-detached-facts" or type(manifest["version"]) is not int or manifest["version"] != 1 or manifest["contains_runtime_authority"] is not False or manifest["requires_rebinding"] is not True:
            raise ValueError
        if type(manifest["tables"]) is not dict or not manifest["tables"]:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, KeyError, RecursionError):
        raise WorkspaceError("invalid_archive_manifest") from None
    if manifest["facts_sha256"] != _hash_file(facts_path):
        raise WorkspaceError("archive_digest_mismatch")
    with _readonly(facts_path) as conn:
        if conn.execute("PRAGMA application_id").fetchone()[0] != ARCHIVE_ID or conn.execute("PRAGMA user_version").fetchone()[0] != 1 or conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise WorkspaceError("invalid_archive_database")
        objects = conn.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if any(row["type"] != "table" for row in objects) or {row["name"] for row in objects} != set(manifest["tables"]):
            raise WorkspaceError("unexpected_archive_schema")
        allowed = _fact_tables()
        for name, spec in manifest["tables"].items():
            if name not in allowed or type(spec) is not dict or set(spec) != {"columns", "rows"} or spec["columns"] != list(allowed[name]) or type(spec["rows"]) is not int or spec["rows"] < 0:
                raise WorkspaceError("invalid_archive_table")
            if tuple(row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')) != allowed[name] or conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] != spec["rows"]:
                raise WorkspaceError("archive_table_mismatch")
    return {"valid": True, "format": manifest["format"], "tables": manifest["tables"], "requires_rebinding": True, "runtime_started": False}


def restore_archive(archive_path, destination):
    """Copy verified facts into a fresh private directory; never activate them.

    Source and destination parents must be administrator-controlled. This is
    not an importer for a concurrently attacker-controlled filesystem tree.
    """
    verified = verify_backup(archive_path)
    source = _path(archive_path)
    destination = _path(destination, fresh=True)
    if not destination.parent.is_dir():
        raise WorkspaceError("invalid_archive_parent")
    destination.mkdir(mode=0o700)
    for name in ("facts.sqlite", "manifest.json"):
        with (source / name).open("rb") as incoming:
            descriptor = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as outgoing:
                copied = 0
                limit = MAX_ARCHIVE_BYTES if name == "facts.sqlite" else 65536
                while chunk := incoming.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > limit:
                        raise WorkspaceError("archive_too_large")
                    outgoing.write(chunk)
    verify_backup(destination)
    return {**verified, "path": str(destination), "recovery_steps": ["Create a fresh protected deployment with new runtime identities.", "Review and explicitly import selected operational facts; keep timers and pending actions paused.", "Reconcile uncertain work and obtain fresh independent authorization before resuming."]}


def diagnostics(snapshot, *, notification_counts=None, source_counts=None, goal_counts=None):
    """Allowlisted metadata only; never inspect processes, environments or files."""
    seats = snapshot.get("seats", [])
    unknown = sum(seat.get("inbox", {}).get("unknown", 0) for seat in seats)
    exhausted = sum(1 for seat in seats if seat.get("turns_used", 0) >= seat.get("budget_turns", 0) and not seat.get("revoked"))
    fenced = snapshot.get("service", {}).get("active") != 1 or snapshot.get("gateway_fenced") is True
    observed = snapshot.get("live_observation") is True
    def counts(value, keys):
        return {key: value[key] for key in keys if type(value) is dict and type(value.get(key)) is int and value[key] >= 0}
    notices = notification_counts if type(notification_counts) is dict else {}
    return {
        "observation": "live" if observed else "recorded", "health": "attention" if fenced or unknown or exhausted else "observed_active" if observed else "unverified",
        "fenced": fenced, "seats": len(seats), "paused_seats": sum(bool(seat.get("paused")) for seat in seats),
        "unknown_deliveries": unknown, "exhausted_turn_budgets": exhausted,
        "automatic_turns_used": sum(seat.get("turns_used", 0) for seat in seats),
        "cost_metering": "turns_only_not_tokens_or_money", "notifications": {
            **counts(notices, {"unread"}),
            "by_kind": counts(notices.get("by_kind"), {"decision", "completion", "stalled", "error", "budget"}),
            "deliveries": counts(notices.get("deliveries"), {"pending", "sending", "cancelled", "delivered", "failed"}),
        },
        "sources": counts(source_counts, {"queued", "fetching", "succeeded", "failed", "available", "disabled", "done", "completed"}),
        "goals": counts(goal_counts, {"active", "scheduled", "paused", "blocked", "waiting_human", "completed", "cancelled"}),
        "restore_policy": "detached_facts_requires_explicit_import_and_rebinding",
    }


def render_service_unit(*, python_executable, deployment):
    """Return an inert systemd template. Never write, install or enable a unit."""
    for value in (python_executable, deployment):
        if type(value) is not str or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or ".." in Path(value).parts or str(Path(value)) != value:
            raise WorkspaceError("invalid_service_path")
    return (
        "# Review before installation. This file does not install or enable anything.\n"
        "# The manager spawns distinct service UIDs and refuses unverified predecessor endpoints.\n"
        "[Unit]\nDescription=deskd isolated workspace\nAfter=network-online.target\n\n"
        "[Service]\nType=simple\nUser=root\nGroup=root\nUMask=0077\nWorkingDirectory=/\n"
        f"ExecStart={python_executable} -m deskd.workspace up --deployment {deployment}\n"
        "Restart=no\nKillMode=control-group\nTimeoutStopSec=45\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
