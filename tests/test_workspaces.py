"""Workspace broker security, lifecycle and crash-recovery contracts."""

from __future__ import annotations

import os
import subprocess
import dataclasses
from pathlib import Path

import pytest

from deskd import RepositorySpec, workspaces
from deskd.config import CONFIG
from deskd.control import commands
from deskd.control.auth import Principal


def git(repo: Path, *args: str) -> str:
    run = subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        capture_output=True, text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    return run.stdout.strip()


@pytest.fixture
def repo(desk, tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-b", "main")
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    git(seed, "add", "README.md")
    git(seed, "-c", "user.name=Seed", "-c", "user.email=seed@example.invalid",
        "commit", "-m", "seed")
    worktree_root = tmp_path / "workspaces"
    desk.repositories = (RepositorySpec(
        "demo", seed, worktree_root, container_worktree_root=worktree_root,
        allowed_bases=("main",),
        branch_prefix="codex/", allowed_roles=("alpha",), lease_seconds=3600,
    ),)
    return seed, worktree_root


def acquire(**overrides):
    params = {
        "repo": "demo", "owner_role": "alpha", "task_key": "task-219",
        "base_ref": "main",
    }
    params.update(overrides)
    return workspaces.acquire(**params)


def alpha_principal() -> Principal:
    return Principal("role:alpha", "alpha", frozenset({"agent"}))


def test_allocate_is_idempotent_and_records_provenance(repo):
    first = acquire()
    second = acquire()
    assert second["lease_id"] == first["lease_id"]
    assert second["base_sha"] == git(repo[0], "rev-parse", "main")
    assert second["head_sha"] == second["base_sha"]
    assert second["workspace_version"] == 1
    assert second["agent_version"]
    assert second["provider"] == "claude"
    assert "git_dir" not in second and "request_fingerprint" not in second
    assert Path(second["path"]).is_dir()
    assert not (Path(second["path"]) / ".git").exists()
    assert second["branch"].startswith("codex/alpha/")


def test_only_allowlisted_role_repo_base_and_branch_are_accepted(repo):
    with pytest.raises(workspaces.WorkspaceError, match="may not lease"):
        acquire(owner_role="beta")
    with pytest.raises(workspaces.WorkspaceError, match="not configured"):
        acquire(repo="elsewhere")
    with pytest.raises(workspaces.WorkspaceError, match="base_ref"):
        acquire(base_ref="HEAD")
    with pytest.raises(workspaces.WorkspaceError, match="task_key"):
        acquire(task_key="../../escape")
    with pytest.raises(workspaces.WorkspaceError, match="branch"):
        acquire(branch="-c core.hooksPath=/tmp/owned")
    with pytest.raises(workspaces.WorkspaceError, match="branch"):
        acquire(branch="codex/x;touch-pwned")


def test_expected_base_sha_fails_closed_if_base_moved(repo):
    with pytest.raises(workspaces.WorkspaceError, match="base moved"):
        acquire(expected_base_sha="0" * 40)
    assert workspaces.leases() == []


def test_broker_status_diff_commit_and_release(repo):
    lease = acquire()
    path = Path(lease["path"])
    (path / "feature.txt").write_text("implemented\n", encoding="utf-8")

    state = workspaces.status(lease["lease_id"], owner_role="alpha")
    assert state["dirty"] is True
    assert any(f["path"] == "feature.txt" for f in state["changed_files"])
    patch = workspaces.diff(lease["lease_id"], owner_role="alpha")
    # Untracked files are reported by status; Git's ordinary diff deliberately
    # does not invent a patch for content not yet staged by the broker.
    assert patch["truncated"] is False

    result = workspaces.commit(
        lease["lease_id"], owner_role="alpha", message="Implement feature",
        expected_head=state["head_sha"],
        expected_workspace_version=state["workspace_version"],
        expected_base_sha=state["base_sha"],
    )
    assert result["committed"] is True
    assert result["dirty"] is False
    assert result["workspace_version"] > state["workspace_version"]
    assert git(repo[0], "show", "-s", "--format=%an <%ae>",
               result["commit_sha"]) == \
        "deskd agent alpha <alpha@deskd.invalid>"

    released = workspaces.release(
        lease["lease_id"], owner_role="alpha",
        expected_workspace_version=result["workspace_version"])
    assert released["state"] == "released"
    assert not path.exists()
    # Release is itself idempotent.
    assert workspaces.release(
        lease["lease_id"], owner_role="alpha",
        expected_workspace_version=result["workspace_version"])["state"] == "released"


def test_control_commit_recovers_same_request_after_head_moved_before_ledger(
        repo, monkeypatch):
    lease = acquire()
    path = Path(lease["path"])
    (path / "recovery.txt").write_text("durable\n", encoding="utf-8")
    state = workspaces.status(lease["lease_id"], owner_role="alpha")
    original_next_version = workspaces._next_version
    failed = False

    def lose_ledger_after_git(conn, repo_name):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("simulated DB loss after Git moved HEAD")
        return original_next_version(conn, repo_name)

    monkeypatch.setattr(workspaces, "_next_version", lose_ledger_after_git)
    params = {
        "lease_id": lease["lease_id"], "message": "Recover exact commit",
        "expected_head": state["head_sha"],
        "workspace_version": state["workspace_version"],
        "expected_base_sha": state["base_sha"],
    }
    with pytest.raises(workspaces.WorkspaceOutcomeUnknown):
        commands.execute(alpha_principal(), "workspace-commit-recovery",
                         "workspace.commit", params)
    moved_head = git(repo[0], "rev-parse", lease["branch"])
    assert moved_head != state["head_sha"]

    recovered = commands.execute(
        alpha_principal(), "workspace-commit-recovery",
        "workspace.commit", params)
    assert recovered["result"]["recovered"] is True
    assert recovered["result"]["commit_sha"] == moved_head
    with workspaces._connect() as conn:
        receipt = conn.execute(
            "SELECT status FROM control_commands WHERE principal_id=? "
            "AND request_id=?", ("role:alpha", "workspace-commit-recovery")
        ).fetchone()
    assert receipt["status"] == "completed"


def test_control_release_recovers_same_request_after_tree_deletion(
        repo, monkeypatch):
    lease = acquire(task_key="release-recovery")
    path = Path(lease["path"])
    original_repo_git = workspaces._repo_git
    failed = False

    def lose_release_receipt(spec, *args, **kwargs):
        nonlocal failed
        if args == ("worktree", "prune", "--expire", "now") and not failed:
            failed = True
            raise RuntimeError("simulated loss after rmtree")
        return original_repo_git(spec, *args, **kwargs)

    monkeypatch.setattr(workspaces, "_repo_git", lose_release_receipt)
    params = {"lease_id": lease["lease_id"],
              "expected_version": lease["workspace_version"]}
    with pytest.raises(workspaces.WorkspaceOutcomeUnknown):
        commands.execute(alpha_principal(), "workspace-release-recovery",
                         "workspace.release", params)
    assert not path.exists()
    released = commands.execute(
        alpha_principal(), "workspace-release-recovery",
        "workspace.release", params)
    assert released["result"]["state"] == "released"
    assert released["result"]["recovered"] is True


def test_workspace_lease_session_start_requires_and_accepts_all_build_pins(repo):
    lease = acquire(task_key="session-pins")
    principal = alpha_principal()
    params = {
        "session_id": "workspace-session", "mode": "spawn",
        "provider": "claude", "workspace_lease_id": lease["lease_id"],
        "image_digest": CONFIG.image_digest,
        "build_revision": CONFIG.build_revision,
        "config_version": CONFIG.config_version,
        "prompt_version": CONFIG.prompt_version,
    }
    for index, key in enumerate((
            "image_digest", "build_revision", "config_version", "prompt_version")):
        missing = dict(params)
        missing.pop(key)
        with pytest.raises(commands.CommandError, match=key):
            commands.execute(
                principal, f"session-pin-missing-{index}",
                "agent.session.start", missing)
        mismatch = {**params, key: f"wrong-{key}"}
        with pytest.raises(commands.CommandConflict, match=key):
            commands.execute(
                principal, f"session-pin-mismatch-{index}",
                "agent.session.start", mismatch)
    started = commands.execute(
        principal, "session-pins-success", "agent.session.start", params)
    provenance = started["result"]["provenance"]
    assert {key: provenance[key] for key in (
        "image_digest", "build_revision", "config_version", "prompt_version"
    )} == {key: params[key] for key in (
        "image_digest", "build_revision", "config_version", "prompt_version")}


def test_commit_requires_clean_index_and_optimistic_head(repo):
    lease = acquire()
    path = Path(lease["path"])
    (path / "x.txt").write_text("x\n", encoding="utf-8")
    state = workspaces.status(lease["lease_id"], owner_role="alpha")
    with pytest.raises(workspaces.WorkspaceError, match="head moved"):
        workspaces.commit(
            lease["lease_id"], owner_role="alpha", message="x",
            expected_head="f" * 40,
            expected_workspace_version=state["workspace_version"],
        )
    from deskd import orchestration
    with orchestration.connect() as conn:
        raw = conn.execute(
            "SELECT git_dir,path FROM workspace_leases WHERE lease_id=?",
            (lease["lease_id"],)).fetchone()
    subprocess.run([
        "git", f"--git-dir={raw['git_dir']}", f"--work-tree={raw['path']}",
        "add", "--all", "--",
    ], check=True, env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"})
    with pytest.raises(workspaces.WorkspaceError, match="pre-staged"):
        workspaces.commit(
            lease["lease_id"], owner_role="alpha", message="x",
            expected_head=state["head_sha"],
            expected_workspace_version=state["workspace_version"],
        )


def test_agent_cannot_redirect_git_pointer(repo):
    lease = acquire()
    pointer = Path(lease["path"]) / ".git"
    pointer.write_text("gitdir: /tmp/attacker\n", encoding="utf-8")
    with pytest.raises(workspaces.WorkspaceError, match="unauthorized .git"):
        workspaces.status(lease["lease_id"], owner_role="alpha")


def test_git_uses_pinned_directory_fd_across_top_level_rename(repo, monkeypatch):
    lease = acquire(task_key="inode-race")
    path = Path(lease["path"])
    moved = path.with_name(path.name + "-moved")
    sensitive = path.parent / "control-secrets"
    sensitive.mkdir()
    secret = sensitive / "token"
    secret.write_text("must-not-become-a-worktree\n", encoding="utf-8")
    with workspaces._connect() as conn:
        row = dict(conn.execute(
            "SELECT * FROM workspace_leases WHERE lease_id=?",
            (lease["lease_id"],)).fetchone())
    original_run = workspaces._run
    raced = False

    def swap_during_git(argv, **kwargs):
        nonlocal raced
        if not raced and "status" in argv and any(
                arg.startswith("--work-tree=/proc/self/fd/") for arg in argv):
            raced = True
            path.rename(moved)
            path.symlink_to(sensitive, target_is_directory=True)
        return original_run(argv, **kwargs)

    monkeypatch.setattr(workspaces, "_run", swap_during_git)
    try:
        with pytest.raises(workspaces.WorkspaceError, match="inode changed"):
            workspaces._lease_git(row, "status", "--porcelain=v1")
        assert secret.read_text(encoding="utf-8") == \
            "must-not-become-a-worktree\n"
        assert not (sensitive / ".git").exists()
    finally:
        if path.is_symlink():
            path.unlink()
        if moved.exists():
            moved.rename(path)


def test_nested_git_metadata_is_rejected(repo):
    lease = acquire()
    nested = Path(lease["path"]) / "src" / ".git"
    nested.mkdir(parents=True)
    with pytest.raises(workspaces.WorkspaceError, match="unauthorized .git"):
        workspaces.status(lease["lease_id"], owner_role="alpha")


def test_git_output_is_killed_at_broker_limit(repo):
    spec = CONFIG.repositories[0]
    CONFIG.repositories = (dataclasses.replace(
        spec, max_files=1000, max_git_output_bytes=4096),)
    lease = acquire()
    path = Path(lease["path"])
    for i in range(250):
        (path / f"untracked-{i:04d}-{'x' * 30}.txt").write_text("x")
    with pytest.raises(workspaces.WorkspaceError, match="output exceeded"):
        workspaces.status(lease["lease_id"], owner_role="alpha")


def test_repo_filter_and_textconv_commands_never_execute(repo):
    seed = repo[0]
    marker = seed.parent / "unsafe-driver-ran"
    driver = seed.parent / "unsafe-driver"
    driver.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8")
    driver.chmod(0o755)
    git(seed, "config", "filter.evil.clean", str(driver))
    git(seed, "config", "filter.evil.smudge", str(driver))
    git(seed, "config", "diff.evil.textconv", str(driver))
    (seed / ".gitattributes").write_text(
        "*.evil filter=evil diff=evil\n", encoding="utf-8")
    (seed / "tracked.evil").write_text("base\n", encoding="utf-8")
    git(seed, "add", ".gitattributes", "tracked.evil")
    # The setup's ordinary git add invokes the malicious clean driver; reset
    # the marker so the assertion covers broker calls only.
    marker.unlink(missing_ok=True)
    git(seed, "-c", "user.name=Seed", "-c", "user.email=seed@example.invalid",
        "commit", "-m", "attributes")
    marker.unlink(missing_ok=True)

    lease = acquire()
    path = Path(lease["path"])
    (path / "tracked.evil").write_text("changed\n", encoding="utf-8")
    state = workspaces.status(lease["lease_id"], owner_role="alpha")
    workspaces.diff(lease["lease_id"], owner_role="alpha")
    result = workspaces.commit(
        lease["lease_id"], owner_role="alpha", message="safe drivers",
        expected_head=state["head_sha"],
        expected_workspace_version=state["workspace_version"])
    assert result["committed"] is True
    assert not marker.exists()


def test_acquire_recovers_crash_after_worktree_creation(repo, desk):
    lease = acquire()
    # Exact durable state left by a crash after `git worktree add` and before
    # the final active update.  The next identical request must inspect and
    # promote it, not create a second branch/worktree.
    from deskd import orchestration
    with orchestration.connect(write=True) as conn:
        conn.execute(
            "UPDATE workspace_leases SET state='allocating',git_dir=NULL "
            "WHERE lease_id=?", (lease["lease_id"],))
    recovered = acquire()
    assert recovered["lease_id"] == lease["lease_id"]
    assert recovered["state"] == "active"
    porcelain = git(repo[0], "worktree", "list", "--porcelain")
    assert porcelain.count("worktree ") == 2  # seed + exactly one lease


def test_worktree_add_disables_repository_hooks(repo):
    hooks = repo[0] / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    marker = repo[0].parent / "post-checkout-ran"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    acquire()
    assert not marker.exists()
