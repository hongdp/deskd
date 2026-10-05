"""Offline end-to-end coordination and independent authorization rehearsal."""

import json
import stat

import pytest

from deskd.workspace.demo import render_board, run_demo
from deskd.workspace.store import WorkspaceStore


def test_demo_runs_real_store_outbox_authorization_and_recovery(tmp_path):
    output = tmp_path / "rehearsal"
    report = run_demo(output)
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert report["kind"] == "offline-mock-workspace"
    assert report["runtime_calls"] == 4
    assert report["tool_output_only"] is True
    assert len(report["checks"]) >= 15
    assert report["memo"]["issuer_principal"] == "demo/reviewer"
    assert report["memo"]["executor_principal"] == "demo/operator"
    assert report["snapshot"]["service"]["active"] == 0
    assert json.loads((output / "report.json").read_text()) == report
    restored = WorkspaceStore(output / "workspace.sqlite")
    assert all(
        restored.inbox(seat["principal"]) == [] for seat in report["snapshot"]["seats"]
    )
    assert all(
        item["state"] == "completed" for item in report["snapshot"]["dispatches"]
    )
    assert restored.tasks("demo/reviewer")[0]["status"] == "done"


def test_demo_refuses_existing_output_without_changing_it(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "preserve.txt"
    marker.write_text("existing user data")
    with pytest.raises(FileExistsError):
        run_demo(output)
    assert marker.read_text() == "existing user data"
    assert list(output.iterdir()) == [marker]


def test_offline_board_escapes_untrusted_values_and_omits_message_bodies(tmp_path):
    report = run_demo(tmp_path / "demo")
    report["checks"].append(
        {"scenario": "<script>alert(1)</script>", "outcome": "<img src=x>"}
    )
    html = render_board(report)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img" not in html
    assert "default-src 'none'" in html
    assert "Review the synthetic release memo." not in html
    assert "No credentials" in html
