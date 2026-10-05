"""Detached facts backup, integrity and metadata diagnostics on fresh mock files."""

import hashlib
import json
import os
import sqlite3
import stat

import pytest

from deskd.workspace.goals import GoalEngine
from deskd.workspace.notifications import NotificationStore
from deskd.workspace.operations import diagnostics, export_backup, render_service_unit, restore_archive, verify_backup
from deskd.workspace.store import WorkspaceError, WorkspaceStore
from deskd.workspace import operations


@pytest.fixture
def databases(tmp_path):
    store = WorkspaceStore(tmp_path / "workspace.sqlite", clock=lambda: 100.0)
    store.register_seat("demo/analyst", "synthetic-runtime-binding", "a" * 64)
    store.trusted_enqueue("demo/analyst", "A synthetic user request", request_id="request-one")
    notices = NotificationStore(store)
    notices.emit("decision", "goal-1", 1, "请确认下一步")
    GoalEngine(store)
    gateway = tmp_path / "gateway.sqlite"
    with sqlite3.connect(gateway) as conn:
        conn.execute("PRAGMA application_id=0x44534731")
        conn.execute("CREATE TABLE service(singleton INTEGER PRIMARY KEY,active INTEGER)")
        conn.execute("INSERT INTO service VALUES(1,0)")
        conn.execute("CREATE TABLE synthetic_forbidden_auth(value TEXT)")
        conn.execute("INSERT INTO synthetic_forbidden_auth VALUES('SYNTHETIC_NEVER_EXPORT')")
        conn.execute("CREATE TABLE memo_proposals(proposal_id,desk_id,author_principal,executor_principal,body,body_sha256,created_at)")
        conn.execute("INSERT INTO memo_proposals VALUES('p1','demo','demo/analyst','demo/trader','Synthetic memo',?,100)", (hashlib.sha256(b"Synthetic memo").hexdigest(),))
    return store, gateway


def test_offline_export_verify_restore_omits_authority(databases, tmp_path):
    store, gateway = databases
    output = tmp_path / "archive"
    report = export_backup(store.db_path, gateway, output, offline_confirmed=True)
    assert report["requires_rebinding"] is True
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in output.iterdir())
    verified = verify_backup(output)
    assert verified["valid"] is True
    restored = restore_archive(output, tmp_path / "recovered")
    assert restored["runtime_started"] is False
    with sqlite3.connect(tmp_path / "recovered" / "facts.sqlite") as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "workspace_workspace_goals" in tables
        assert not any("auth" in name or "lease" in name or "dispatch" in name or "receipt" in name for name in tables)
        assert "root_id" not in {row[1] for row in conn.execute("PRAGMA table_info(workspace_seats)")}
        assert conn.execute("SELECT body FROM workspace_messages").fetchone()[0] == "A synthetic user request"
        assert conn.execute("SELECT body FROM gateway_memo_proposals").fetchone()[0] == "Synthetic memo"
    assert b"SYNTHETIC_NEVER_EXPORT" not in (output / "facts.sqlite").read_bytes()
    assert b"synthetic-runtime-binding" not in (output / "facts.sqlite").read_bytes()
    with pytest.raises(WorkspaceError, match="destination_exists"):
        restore_archive(output, tmp_path / "recovered")


def test_backup_requires_offline_fenced_state(databases, tmp_path):
    store, gateway = databases
    with pytest.raises(WorkspaceError, match="offline_confirmation_required"):
        export_backup(store.db_path, gateway, tmp_path / "archive")
    with store._connect(write=True) as conn:
        conn.execute("UPDATE service SET active=1")
    with pytest.raises(WorkspaceError, match="workspace_must_be_fenced"):
        export_backup(store.db_path, gateway, tmp_path / "archive", offline_confirmed=True)
    assert not (tmp_path / "archive").exists()
    with store._connect(write=True) as conn:
        conn.execute("UPDATE service SET active=0")
    with sqlite3.connect(gateway) as conn:
        conn.execute("UPDATE service SET active=1")
    with pytest.raises(WorkspaceError, match="gateway_must_be_fenced"):
        export_backup(store.db_path, gateway, tmp_path / "archive", offline_confirmed=True)
    assert not (tmp_path / "archive").exists()


def test_archive_digest_schema_permissions_and_symlink_rejections(databases, tmp_path):
    store, gateway = databases
    output = tmp_path / "archive"
    export_backup(store.db_path, gateway, output, offline_confirmed=True)
    facts = output / "facts.sqlite"
    with sqlite3.connect(facts) as conn:
        conn.execute("UPDATE workspace_messages SET body='modified'")
    with pytest.raises(WorkspaceError, match="archive_digest_mismatch"):
        verify_backup(output)
    manifest = json.loads((output / "manifest.json").read_text())
    manifest["facts_sha256"] = hashlib.sha256(facts.read_bytes()).hexdigest()
    (output / "manifest.json").write_text(json.dumps(manifest))
    assert verify_backup(output)["valid"]
    with sqlite3.connect(facts) as conn:
        conn.execute("CREATE TABLE forbidden_authority(value)")
    manifest["facts_sha256"] = hashlib.sha256(facts.read_bytes()).hexdigest()
    (output / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(WorkspaceError, match="unexpected_archive_schema"):
        verify_backup(output)
    alias = tmp_path / "alias"
    alias.symlink_to(store.db_path)
    with pytest.raises(WorkspaceError, match="unsafe_operations_path"):
        export_backup(alias, gateway, tmp_path / "other", offline_confirmed=True)


def test_archive_cannot_include_added_files_or_be_world_readable(databases, tmp_path):
    store, gateway = databases
    output = tmp_path / "archive"
    export_backup(store.db_path, gateway, output, offline_confirmed=True)
    (output / "unexpected.txt").write_text("synthetic extra file")
    with pytest.raises(WorkspaceError, match="unexpected_archive_files"):
        verify_backup(output)
    (output / "unexpected.txt").unlink()
    os.chmod(output / "facts.sqlite", 0o644)
    with pytest.raises(WorkspaceError, match="unsafe_archive_permissions"):
        verify_backup(output)


def test_diagnostics_and_unit_are_inert_metadata_only():
    snapshot = {"service": {"active": 1}, "live_observation": True, "seats": [{"principal": "demo/analyst", "budget_turns": 5, "turns_used": 5, "inbox": {"unknown": 2}, "root_id": "MUST_NOT_DISPLAY"}]}
    result = diagnostics(snapshot)
    assert result["health"] == "attention"
    assert result["unknown_deliveries"] == 2
    assert result["exhausted_turn_budgets"] == 1
    assert "MUST_NOT_DISPLAY" not in json.dumps(result)
    unit = render_service_unit(python_executable="/opt/deskd/bin/python", deployment="/opt/deskd/policy/deployment.json")
    assert "Restart=no" in unit
    assert "Environment=" not in unit
    assert "--deployment /opt/deskd/policy/deployment.json" in unit
    with pytest.raises(WorkspaceError, match="invalid_service_path"):
        render_service_unit(python_executable="/bin/python\nExecStart=/bad", deployment="/opt/deskd/deployment.json")


def test_restore_bounds_copy_if_archive_grows_after_verification(databases, tmp_path, monkeypatch):
    store, gateway = databases
    output = tmp_path / "archive"
    export_backup(store.db_path, gateway, output, offline_confirmed=True)
    facts = output / "facts.sqlite"
    monkeypatch.setattr(operations, "MAX_ARCHIVE_BYTES", facts.stat().st_size + 16)
    verify = operations.verify_backup
    def change_after_verification(path):
        result = verify(path)
        with facts.open("ab") as stream:
            stream.write(b"synthetic growth" * 8)
        return result
    monkeypatch.setattr(operations, "verify_backup", change_after_verification)
    with pytest.raises(WorkspaceError, match="archive_too_large"):
        operations.restore_archive(output, tmp_path / "recovered")
    assert (tmp_path / "recovered" / "facts.sqlite").stat().st_size <= operations.MAX_ARCHIVE_BYTES
