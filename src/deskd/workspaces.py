"""Deterministic, non-LLM Git workspace broker.

Agents name an allowlisted repository, base, task and branch.  Only this broker
touches Git metadata; an agent container receives the lease directory (source
files) but neither the seed repository nor its writable ``.git`` target.  Every
Git invocation is a fixed argv assembled here, never a shell command and never
caller-supplied argv.

The lease ledger is durable and idempotent.  A crash after ``git worktree add``
but before the success update is recovered by the next identical acquire: the
broker validates the exact worktree/branch and promotes the existing lease.
There is deliberately no push, merge, fetch, reset or checkout operation.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import selectors
import shutil
import sqlite3
import stat as statmod
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG, RepositorySpec, __version__
from . import orchestration, transaction

__all__ = [
    "WorkspaceError", "WorkspaceOutcomeUnknown", "WorkspaceMetadata",
    "acquire", "inspect", "renew",
    "release", "status", "diff", "commit", "leases", "launch_path",
    "ensure_schema",
]


class WorkspaceError(ValueError):
    """A workspace request failed validation or its optimistic assertion."""


class WorkspaceOutcomeUnknown(WorkspaceError):
    """A recoverable broker side effect may have completed without a receipt.

    The control dispatcher keeps the request reclaimable.  An identical retry
    must invoke the broker's deterministic proof path; it must never start a
    second unrelated operation or silently report a terminal failure.
    """


@dataclass(frozen=True)
class WorkspaceMetadata:
    """Immutable build provenance captured when a lease is allocated."""

    agent_version: str = __version__
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    image: str | None = None
    image_digest: str | None = None
    build_revision: str | None = None
    config_version: str | None = None


WORKSPACE_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_repo_versions (
    repo                  TEXT PRIMARY KEY,
    version               INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_leases (
    lease_id              TEXT PRIMARY KEY,
    repo                  TEXT NOT NULL,
    owner_role            TEXT NOT NULL,
    task_key              TEXT NOT NULL,
    base_ref              TEXT NOT NULL,
    base_sha              TEXT NOT NULL,
    branch                TEXT NOT NULL,
    path                  TEXT NOT NULL,
    git_dir               TEXT,
    worktree_device       INTEGER,
    worktree_inode        INTEGER,
    state                 TEXT NOT NULL
                          CHECK (state IN ('allocating','active','error',
                                           'released','expired')),
    workspace_version     INTEGER NOT NULL,
    head_sha              TEXT,
    agent_version         TEXT NOT NULL,
    provider              TEXT,
    model                 TEXT,
    prompt_version        TEXT,
    image                 TEXT,
    image_digest          TEXT,
    build_revision        TEXT,
    config_version        TEXT,
    request_fingerprint   TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    expires_at            TEXT NOT NULL,
    released_at           TEXT,
    last_error            TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_live_task
ON workspace_leases(repo, owner_role, task_key)
WHERE state IN ('allocating','active','error','expired');

CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_live_branch
ON workspace_leases(repo, branch)
WHERE state IN ('allocating','active','error','expired');

CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_one_live_role
ON workspace_leases(repo, owner_role)
WHERE state IN ('allocating','active','error','expired');

CREATE INDEX IF NOT EXISTS idx_workspace_owner
ON workspace_leases(owner_role, state, expires_at);
"""

_TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,127}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,190}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_EMAIL_ROLE_RE = re.compile(r"[^a-z0-9._-]+")
_MAX_GIT_OUTPUT = 2 * 1024 * 1024


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _repo(name: str) -> RepositorySpec:
    matches = [r for r in CONFIG.repositories if r.name == name]
    if len(matches) != 1:
        known = ", ".join(sorted(r.name for r in CONFIG.repositories)) or "none"
        raise WorkspaceError(
            f"repository {name!r} is not configured (configured: {known})")
    spec = matches[0]
    if not spec.path.is_dir():
        raise WorkspaceError(f"repository {name!r} path is unavailable")
    return spec


def _clean_task(task_key: str) -> str:
    task_key = str(task_key or "").strip()
    if (not _TASK_RE.fullmatch(task_key) or ".." in task_key
            or "@{" in task_key or "//" in task_key):
        raise WorkspaceError("task_key is not a safe stable identifier")
    return task_key


def _clean_branch(spec: RepositorySpec, branch: str) -> str:
    branch = str(branch or "").strip()
    if (not _BRANCH_RE.fullmatch(branch) or not branch.startswith(spec.branch_prefix)
            or ".." in branch or "@{" in branch or "//" in branch
            or branch.endswith(("/", ".", ".lock"))):
        raise WorkspaceError(
            f"branch must be a safe ref under {spec.branch_prefix!r}")
    return branch


def _clean_sha(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    value = value.strip().lower()
    if not _SHA_RE.fullmatch(value):
        raise WorkspaceError(f"{label} must be a full hexadecimal commit id")
    return value


def _slug(value: str, limit: int = 40) -> str:
    out = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.").lower()
    return (out or "task")[:limit]


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _git_env() -> dict[str, str]:
    # Git-specific inherited variables can silently redirect the repository,
    # work tree, object store or config.  The broker owns all of those inputs.
    child = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    child.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_PAGER": "cat",
    })
    return child


def _run(argv: list[str], *, ok: tuple[int, ...] = (0,), timeout: int = 120,
         output_limit: int = _MAX_GIT_OUTPUT,
         truncate: bool = False,
         pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess[str]:
    """Run one broker-owned argv with bounded, incrementally drained output.

    ``subprocess.run(capture_output=True)`` buffers without a ceiling and lets a
    malicious worktree turn ``git status`` into broker OOM.  Both pipes are
    drained here while the child runs; crossing the combined byte ceiling kills
    it.  ``git diff`` may opt into a clearly marked partial result, while every
    state-changing/validation command fails closed on overflow.
    """
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=_git_env(), shell=False,
            pass_fds=pass_fds,
        )
        selector = selectors.DefaultSelector()
        assert proc.stdout is not None and proc.stderr is not None
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
        chunks = {"stdout": bytearray(), "stderr": bytearray()}
        seen = 0
        overflow = False
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.wait()
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in selector.select(min(remaining, 0.25)):
                data = os.read(key.fileobj.fileno(), 64 * 1024)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                room = max(0, output_limit - seen)
                if room:
                    chunks[key.data].extend(data[:room])
                seen += len(data)
                if seen > output_limit:
                    overflow = True
                    proc.kill()
                    # Continue draining until EOF so wait cannot deadlock.
        returncode = proc.wait()
        stdout = chunks["stdout"].decode("utf-8", errors="replace")
        stderr = chunks["stderr"].decode("utf-8", errors="replace")
        result = subprocess.CompletedProcess(argv, returncode, stdout, stderr)
        setattr(result, "output_truncated", overflow)
        if overflow and not truncate:
            raise WorkspaceError(
                f"git output exceeded the {output_limit}-byte broker limit")
        if overflow and truncate:
            return result
    except (OSError, subprocess.TimeoutExpired) as exc:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        raise WorkspaceError(f"git operation failed to start: {exc}") from exc
    if result.returncode not in ok:
        detail = (result.stderr or result.stdout or "git command failed").strip()
        raise WorkspaceError(detail[:4000])
    return result


def _repo_git(spec: RepositorySpec, *args: str,
              ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    config_probe = _run([
        "git", "-C", str(spec.path), "config", "--local", "--name-only",
        "--get-regexp", r"^(filter|diff)\.",
    ], ok=(0, 1), output_limit=spec.max_git_output_bytes)
    unsafe: list[str] = []
    for key in config_probe.stdout.splitlines():
        if re.fullmatch(
                r"(?:filter\.[A-Za-z0-9._-]+\.(?:clean|smudge|process)|"
                r"diff\.[A-Za-z0-9._-]+\.(?:command|textconv))", key):
            unsafe.extend(["-c", f"{key}="])
    safe = [
        "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
        "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false",
        "-c", "protocol.allow=never", "-c", "protocol.file.allow=never",
        "-c", "submodule.recurse=false", "-c", "fetch.recurseSubmodules=false",
        "-c", "credential.helper=", *unsafe,
    ]
    return _run(["git", "-C", str(spec.path), *safe, *args], ok=ok,
                output_limit=spec.max_git_output_bytes)


def _open_directory(path: str | Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkspaceError(
            "lease worktree cannot be opened without following links") from exc
    info = os.fstat(descriptor)
    return descriptor, info


def _directory_identity(path: str | Path) -> tuple[int, int]:
    descriptor, info = _open_directory(path)
    os.close(descriptor)
    return int(info.st_dev), int(info.st_ino)


def _open_pinned_worktree(row: sqlite3.Row | dict) -> tuple[int, os.stat_result]:
    """Open the leased top-level inode without following a replacement link."""
    expected_device = row["worktree_device"]
    expected_inode = row["worktree_inode"]
    if expected_device is None or expected_inode is None:
        raise WorkspaceError(
            "workspace lease lacks an inode pin; release and reacquire it")
    descriptor, info = _open_directory(row["path"])
    if ((info.st_dev, info.st_ino)
            != (int(expected_device), int(expected_inode))):
        os.close(descriptor)
        raise WorkspaceError("lease top-level inode was replaced")
    return descriptor, info


def _assert_pinned_fd(row: sqlite3.Row | dict, descriptor: int,
                      before: os.stat_result) -> None:
    after = os.fstat(descriptor)
    expected = (int(row["worktree_device"]), int(row["worktree_inode"]))
    try:
        named = os.stat(row["path"], follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceError(
            "lease path changed during Git operation") from exc
    if ((before.st_dev, before.st_ino) != expected
            or (after.st_dev, after.st_ino) != expected
            or (named.st_dev, named.st_ino) != expected
            or not statmod.S_ISDIR(named.st_mode)
            or after.st_nlink == 0):
        raise WorkspaceError("lease top-level inode changed during Git operation")


def _lease_git(row: sqlite3.Row | dict, *args: str,
               ok: tuple[int, ...] = (0,), truncate: bool = False,
               output_limit: int | None = None) -> subprocess.CompletedProcess[str]:
    git_dir = Path(row["git_dir"] or "")
    if not git_dir.is_dir():
        raise WorkspaceError("lease Git metadata is unavailable")
    spec = _repo(row["repo"])
    descriptor, identity = _open_pinned_worktree(row)
    try:
        fd_root = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
        worktree = f"{fd_root}/{descriptor}"
        # Repo-local filter commands are host configuration, but they must not
        # run in response to an agent-written .gitattributes. Discover keys
        # without evaluating them, then shadow each command.
        config_probe = _run([
            "git", f"--git-dir={git_dir}", f"--work-tree={worktree}",
            "config", "--local", "--name-only", "--get-regexp",
            r"^(filter|diff)\.",
        ], ok=(0, 1), output_limit=spec.max_git_output_bytes,
            pass_fds=(descriptor,))
        _assert_pinned_fd(row, descriptor, identity)
        filter_overrides: list[str] = []
        for key in config_probe.stdout.splitlines():
            if re.fullmatch(
                    r"(?:filter\.[A-Za-z0-9._-]+\.(?:clean|smudge|process)|"
                    r"diff\.[A-Za-z0-9._-]+\.(?:command|textconv))", key):
                filter_overrides.extend(["-c", f"{key}="])
        result = _run([
            "git", f"--git-dir={git_dir}", f"--work-tree={worktree}",
            "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=/dev/null",
            "-c", "commit.gpgSign=false",
            "-c", "tag.gpgSign=false",
            "-c", "protocol.allow=never",
            "-c", "protocol.file.allow=never",
            "-c", "submodule.recurse=false",
            "-c", "fetch.recurseSubmodules=false",
            "-c", "credential.helper=",
            *filter_overrides, *args,
        ], ok=ok, output_limit=output_limit or spec.max_git_output_bytes,
            truncate=truncate, pass_fds=(descriptor,))
        _assert_pinned_fd(row, descriptor, identity)
        return result
    finally:
        os.close(descriptor)


@contextmanager
def _repo_lock(spec: RepositorySpec) -> Iterator[None]:
    # Control-private: the agent receives the worktree mount and must not be
    # able to replace the file whose flock serializes broker Git operations.
    lock_dir = Path(CONFIG.db_path).parent / ".workspace-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{spec.name}.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def ensure_schema(db_path: Path | str | None = None) -> None:
    path = Path(db_path or CONFIG.db_path)
    if transaction.current(path) is not None:
        # The outer control transaction installed every schema before BEGIN;
        # executescript here would implicitly commit it.
        return
    with orchestration.connect(db_path, write=True) as conn:
        conn.executescript(WORKSPACE_SCHEMA)
        columns = {r["name"] for r in conn.execute(
            "PRAGMA table_info(workspace_leases)")}
        if "image_digest" not in columns:
            conn.execute("ALTER TABLE workspace_leases ADD COLUMN image_digest TEXT")
        if "build_revision" not in columns:
            conn.execute("ALTER TABLE workspace_leases ADD COLUMN build_revision TEXT")
        if "worktree_device" not in columns:
            conn.execute("ALTER TABLE workspace_leases ADD COLUMN worktree_device INTEGER")
        if "worktree_inode" not in columns:
            conn.execute("ALTER TABLE workspace_leases ADD COLUMN worktree_inode INTEGER")


@contextmanager
def _connect(db_path: Path | str | None = None, *,
             write: bool = False) -> Iterator[sqlite3.Connection]:
    ensure_schema(db_path)
    with orchestration.connect(db_path, write=write) as conn:
        yield conn


def _next_version(conn: sqlite3.Connection, repo: str) -> int:
    conn.execute(
        "INSERT INTO workspace_repo_versions(repo,version) VALUES (?,0) "
        "ON CONFLICT(repo) DO NOTHING", (repo,))
    conn.execute(
        "UPDATE workspace_repo_versions SET version=version+1 WHERE repo=?",
        (repo,))
    return int(conn.execute(
        "SELECT version FROM workspace_repo_versions WHERE repo=?",
        (repo,)).fetchone()["version"])


def _row_view(row: sqlite3.Row | dict) -> dict:
    d = dict(row)
    # Broker metadata and request hashes never cross into an agent container.
    # The lease path is intentionally retained: it is the source mount the
    # runner must attach.  The seed repo and writable git-dir are not.
    d.pop("git_dir", None)
    d.pop("worktree_device", None)
    d.pop("worktree_inode", None)
    d.pop("request_fingerprint", None)
    d.pop("last_error", None)
    try:
        spec = _repo(d["repo"])
        relative = Path(d["path"]).resolve(strict=False).relative_to(
            spec.worktree_root.resolve(strict=False))
        d["path"] = str(spec.container_worktree_root / relative)
    except (KeyError, ValueError):
        # Never leak an unconfigured host path after config drift.
        d["path"] = None
    d["expired"] = d["state"] in {"active", "error"} and d["expires_at"] <= _iso()
    return d


def _fingerprint(repo: str, role: str, task: str, base: str, branch: str,
                 metadata: WorkspaceMetadata) -> str:
    raw = json.dumps({
        "repo": repo, "role": role, "task": task, "base": base,
        "branch": branch, "metadata": metadata.__dict__,
    }, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _validate_owner(conn: sqlite3.Connection, spec: RepositorySpec,
                    role: str) -> str:
    role = orchestration._agent_role(conn, role)
    if spec.allowed_roles and role not in spec.allowed_roles:
        raise WorkspaceError(
            f"role {role!r} may not lease repository {spec.name!r}")
    return role


def _default_metadata(role: str,
                      db_path: Path | str | None = None) -> WorkspaceMetadata:
    runtime = orchestration.role_runtime(role, db_path=db_path)
    return WorkspaceMetadata(
        provider=runtime.get("provider"), model=runtime.get("model"),
        prompt_version=CONFIG.prompt_version, image=CONFIG.agent_image,
        image_digest=CONFIG.image_digest, build_revision=CONFIG.build_revision,
        config_version=CONFIG.config_version,
    )


def _validate_lease_files(spec: RepositorySpec, row: sqlite3.Row | dict) -> None:
    path = Path(row["path"])
    git_dir = Path(row["git_dir"] or "")
    if not _under(path, spec.worktree_root):
        raise WorkspaceError("lease path escaped its configured worktree root")
    descriptor, identity = _open_pinned_worktree(row)
    try:
        _assert_pinned_fd(row, descriptor, identity)
    finally:
        os.close(descriptor)
    common = Path(_repo_git(
        spec, "rev-parse", "--path-format=absolute", "--git-common-dir",
    ).stdout.strip()).resolve()
    if not _under(git_dir, common / "worktrees"):
        raise WorkspaceError("lease Git directory escaped broker metadata")
    # The normal worktree .git pointer contains the absolute host metadata path
    # and is writable with the source mount.  The broker removes it after
    # allocation and always supplies --git-dir/--work-tree explicitly.  Any
    # later .git entry was created by the agent and is rejected, never parsed.
    if (path / ".git").exists() or (path / ".git").is_symlink():
        raise WorkspaceError("lease contains an unauthorized .git entry")


def _registration(spec: RepositorySpec, path: Path) -> tuple[str, str, str] | None:
    """(git_dir, head, branch) from broker-owned metadata, never path/.git."""
    blocks = _repo_git(spec, "worktree", "list", "--porcelain").stdout.split("\n\n")
    head = branch = None
    for block in blocks:
        lines = block.splitlines()
        if not lines or lines[0] != f"worktree {path}":
            continue
        for line in lines[1:]:
            if line.startswith("HEAD "):
                head = line[5:].strip()
            elif line.startswith("branch refs/heads/"):
                branch = line[len("branch refs/heads/"):].strip()
        break
    if not head or not branch:
        return None
    common = Path(_repo_git(
        spec, "rev-parse", "--path-format=absolute", "--git-common-dir",
    ).stdout.strip()).resolve()
    expected_pointer = str(path / ".git")
    for candidate in (common / "worktrees").iterdir():
        marker = candidate / "gitdir"
        if (candidate.is_dir() and marker.is_file()
                and marker.read_text(encoding="utf-8").strip() == expected_pointer):
            return str(candidate.resolve()), head, branch
    raise WorkspaceError("registered worktree has no matching broker git-dir")


def _recover(spec: RepositorySpec, row: sqlite3.Row,
             db_path: Path | str | None) -> dict | None:
    path = Path(row["path"])
    if not path.exists():
        return None
    registered = _registration(spec, path)
    if registered is None:
        raise WorkspaceError("allocation path exists but is not this repo's worktree")
    absolute_git, head, branch = registered
    if branch != row["branch"]:
        raise WorkspaceError("recovered worktree is on the wrong branch")
    pointer = path / ".git"
    if pointer.exists() and not pointer.is_symlink() and pointer.is_file():
        pointer.unlink()
    elif pointer.exists() or pointer.is_symlink():
        raise WorkspaceError("allocation .git pointer was replaced before recovery")
    device, inode = _directory_identity(path)
    with _connect(db_path, write=True) as conn:
        conn.execute(
            "UPDATE workspace_leases SET state='active',git_dir=?,head_sha=?,"
            " worktree_device=?,worktree_inode=?,updated_at=?,last_error=NULL "
            "WHERE lease_id=?",
            (absolute_git, head, device, inode, _iso(), row["lease_id"]))
        fresh = conn.execute(
            "SELECT * FROM workspace_leases WHERE lease_id=?",
            (row["lease_id"],)).fetchone()
    _validate_lease_files(spec, fresh)
    out = _row_view(fresh)
    out["recovered"] = True
    return out


def acquire(repo: str, *, owner_role: str, task_key: str, base_ref: str,
            branch: str | None = None, expected_base_sha: str | None = None,
            metadata: WorkspaceMetadata | None = None,
            lease_seconds: int | None = None,
            db_path: Path | str | None = None) -> dict:
    """Allocate or idempotently recover one dedicated Git worktree lease."""
    spec = _repo(repo)
    task_key = _clean_task(task_key)
    if base_ref not in spec.allowed_bases:
        raise WorkspaceError(
            f"base_ref must be one of {list(spec.allowed_bases)!r}")
    expected_base_sha = _clean_sha(expected_base_sha, "expected_base_sha")
    duration = int(lease_seconds or spec.lease_seconds)
    if duration < 60 or duration > 7 * 86_400:
        raise WorkspaceError("lease_seconds must be between 60 and 604800")

    with _connect(db_path, write=True) as conn:
        owner_role = _validate_owner(conn, spec, owner_role)
    metadata = metadata or _default_metadata(owner_role, db_path)

    with _repo_lock(spec):
        # Recovery performs Git inspection and then opens its own short update
        # transaction.  Do it only after the lookup transaction has closed;
        # nesting another BEGIN IMMEDIATE here self-locks the process.
        with _connect(db_path, write=True) as conn:
            candidate = conn.execute(
                "SELECT * FROM workspace_leases WHERE repo=? AND owner_role=? "
                "AND task_key=? AND state IN ('allocating','active','error','expired')",
                (repo, owner_role, task_key)).fetchone()
            if candidate is not None:
                chosen = _clean_branch(spec, branch or candidate["branch"])
                candidate_fp = _fingerprint(
                    repo, owner_role, task_key, base_ref, chosen, metadata)
                if candidate["request_fingerprint"] != candidate_fp:
                    raise WorkspaceError(
                        "task already has a lease with different immutable inputs")
                if candidate["expires_at"] <= _iso():
                    conn.execute(
                        "UPDATE workspace_leases SET state='expired',updated_at=? "
                        "WHERE lease_id=?", (_iso(), candidate["lease_id"]))
                    raise WorkspaceError(
                        "task lease expired; release it before allocating again")
                if candidate["state"] == "active":
                    _validate_lease_files(spec, candidate)
                    return _row_view(candidate)
        if candidate is not None:
            recovered = _recover(spec, candidate, db_path)
            if recovered is not None:
                return recovered

        with _connect(db_path, write=True) as conn:
            existing = conn.execute(
                "SELECT * FROM workspace_leases WHERE repo=? AND owner_role=? "
                "AND task_key=? AND state IN ('allocating','active','error','expired')",
                (repo, owner_role, task_key)).fetchone()
            occupied = conn.execute(
                "SELECT lease_id,task_key FROM workspace_leases WHERE repo=? "
                "AND owner_role=? AND task_key!=? "
                "AND state IN ('allocating','active','error','expired')",
                (repo, owner_role, task_key)).fetchone()
            if occupied is not None:
                raise WorkspaceError(
                    f"role already has live lease {occupied['lease_id']} for "
                    f"task {occupied['task_key']!r}; release it first")
            if existing is not None:
                chosen_branch = branch or existing["branch"]
                chosen_branch = _clean_branch(spec, chosen_branch)
                fp = _fingerprint(repo, owner_role, task_key, base_ref,
                                  chosen_branch, metadata)
                if existing["request_fingerprint"] != fp:
                    raise WorkspaceError(
                        "task already has a lease with different immutable inputs")
                lease_id = existing["lease_id"]
                workspace_version = int(existing["workspace_version"])
                chosen_branch = existing["branch"]
                path = Path(existing["path"])
            else:
                workspace_version = _next_version(conn, repo)
                suffix = hashlib.sha256(
                    f"{repo}\0{owner_role}\0{task_key}\0{workspace_version}".encode()
                ).hexdigest()[:10]
                chosen_branch = branch or (
                    f"{spec.branch_prefix}{_slug(owner_role)}/"
                    f"{_slug(task_key)}-v{workspace_version}-{suffix}")
                chosen_branch = _clean_branch(spec, chosen_branch)
                path = (spec.worktree_root /
                        f"{spec.name}-{_slug(owner_role)}-{_slug(task_key)}-{suffix}")
                if not _under(path, spec.worktree_root) or path.exists():
                    raise WorkspaceError("derived worktree path is unavailable")
                fp = _fingerprint(repo, owner_role, task_key, base_ref,
                                  chosen_branch, metadata)
                lease_id = str(uuid.uuid4())

            base_sha = _repo_git(
                spec, "rev-parse", "--verify", f"{base_ref}^{{commit}}",
            ).stdout.strip().lower()
            if expected_base_sha and base_sha != expected_base_sha:
                raise WorkspaceError(
                    f"base moved: expected {expected_base_sha}, found {base_sha}")

            if existing is None:
                # A pre-existing branch may contain unrelated work.  Never
                # silently attach a new task to it.
                found = _repo_git(
                    spec, "show-ref", "--verify", "--quiet",
                    f"refs/heads/{chosen_branch}", ok=(0, 1),
                ).returncode == 0
                if found:
                    raise WorkspaceError(f"branch already exists: {chosen_branch}")
                now = _now()
                conn.execute(
                    """INSERT INTO workspace_leases
                       (lease_id,repo,owner_role,task_key,base_ref,base_sha,branch,
                        path,state,workspace_version,head_sha,agent_version,
                        provider,model,prompt_version,image,image_digest,
                        build_revision,config_version,
                        request_fingerprint,created_at,updated_at,expires_at)
                       VALUES (?,?,?,?,?,?,?,?,'allocating',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (lease_id, repo, owner_role, task_key, base_ref, base_sha,
                     chosen_branch, str(path), workspace_version, base_sha,
                     metadata.agent_version, metadata.provider, metadata.model,
                     metadata.prompt_version, metadata.image,
                     metadata.image_digest, metadata.build_revision,
                     metadata.config_version, fp, _iso(now), _iso(now),
                     _iso(now + dt.timedelta(seconds=duration))),
                )
            else:
                conn.execute(
                    "UPDATE workspace_leases SET state='allocating',base_sha=?,"
                    " head_sha=?,updated_at=?,last_error=NULL WHERE lease_id=?",
                    (base_sha, base_sha, _iso(), lease_id))

        allocation_started = False
        try:
            spec.worktree_root.mkdir(parents=True, exist_ok=True)
            allocation_started = True
            _repo_git(
                spec, "-c", "core.hooksPath=/dev/null", "worktree", "add",
                "-b", chosen_branch, str(path), base_sha,
            )
            absolute_git = _repo_git(
                spec, "-C", str(path), "rev-parse", "--absolute-git-dir",
            ).stdout.strip()
            head = _repo_git(
                spec, "-C", str(path), "rev-parse", "HEAD",
            ).stdout.strip()
            # Hide the absolute broker metadata path before the source tree can
            # be mounted into an agent container.
            pointer = path / ".git"
            if pointer.is_symlink() or not pointer.is_file():
                raise WorkspaceError("git worktree produced an unsafe .git pointer")
            pointer.unlink()
            device, inode = _directory_identity(path)
            with _connect(db_path, write=True) as conn:
                conn.execute(
                    "UPDATE workspace_leases SET state='active',git_dir=?,head_sha=?,"
                    " worktree_device=?,worktree_inode=?,updated_at=?,"
                    " last_error=NULL WHERE lease_id=?",
                    (absolute_git, head, device, inode, _iso(), lease_id))
                row = conn.execute(
                    "SELECT * FROM workspace_leases WHERE lease_id=?",
                    (lease_id,)).fetchone()
            _validate_lease_files(spec, row)
            return _row_view(row)
        except Exception as exc:
            try:
                with _connect(db_path, write=True) as conn:
                    conn.execute(
                        "UPDATE workspace_leases SET state='error',updated_at=?,"
                        " last_error=? WHERE lease_id=?",
                        (_iso(), str(exc)[:4000], lease_id))
            except Exception as receipt_exc:
                if allocation_started:
                    raise WorkspaceOutcomeUnknown(
                        "workspace allocation may exist without a lease receipt; "
                        "identical retry required") from receipt_exc
                raise
            if allocation_started:
                raise WorkspaceOutcomeUnknown(
                    "workspace allocation may have started; identical retry required"
                ) from exc
            if isinstance(exc, WorkspaceError):
                raise
            raise WorkspaceError(str(exc)) from exc


def _owned(lease_id: str, owner_role: str | None,
           db_path: Path | str | None, *, allow_released: bool = False) -> tuple[RepositorySpec, sqlite3.Row]:
    try:
        uuid.UUID(str(lease_id))
    except (ValueError, TypeError) as exc:
        raise WorkspaceError("invalid lease_id") from exc
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,)).fetchone()
        if row is None:
            raise WorkspaceError("workspace lease not found")
        if owner_role is not None:
            role = _validate_owner(conn, _repo(row["repo"]), owner_role)
            if row["owner_role"] != role:
                raise WorkspaceError("workspace lease belongs to another role")
        if not allow_released and row["state"] not in {"active", "error"}:
            raise WorkspaceError(f"workspace lease is {row['state']}")
    return _repo(row["repo"]), row


def inspect(lease_id: str, *, owner_role: str | None = None,
            db_path: Path | str | None = None) -> dict:
    spec, row = _owned(lease_id, owner_role, db_path, allow_released=True)
    out = _row_view(row)
    if row["state"] == "active":
        _validate_lease_files(spec, row)
        out["vcs"] = status(lease_id, owner_role=owner_role, db_path=db_path)
        out["clean"] = out["vcs"]["clean"]
        out["committed"] = out["vcs"]["committed"]
    else:
        out["clean"] = None
        out["committed"] = None
    out["version"] = out["workspace_version"]
    return out


def launch_path(lease_id: str, *, owner_role: str,
                db_path: Path | str | None = None) -> Path:
    """Return the broker-private cwd for one validated active lease."""
    spec, row = _owned(lease_id, owner_role, db_path)
    if row["expires_at"] <= _iso():
        raise WorkspaceError("workspace lease is expired")
    _validate_lease_files(spec, row)
    _enforce_quota(spec, Path(row["path"]))
    return Path(row["path"])


def leases(*, owner_role: str | None = None, include_released: bool = False,
           db_path: Path | str | None = None) -> list[dict]:
    clauses, params = [], []
    if owner_role:
        clauses.append("owner_role=?")
        params.append(owner_role)
    if not include_released:
        clauses.append("state!='released'")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _connect(db_path) as conn:
        return [_row_view(r) for r in conn.execute(
            f"SELECT * FROM workspace_leases{where} ORDER BY created_at DESC",
            params).fetchall()]


def renew(lease_id: str, *, owner_role: str, lease_seconds: int | None = None,
          expected_workspace_version: int | None = None,
          db_path: Path | str | None = None) -> dict:
    spec, row = _owned(lease_id, owner_role, db_path)
    _validate_lease_files(spec, row)
    if row["expires_at"] <= _iso():
        raise WorkspaceError("expired workspace lease cannot be renewed")
    if (expected_workspace_version is not None
            and int(row["workspace_version"]) != int(expected_workspace_version)):
        raise WorkspaceError("workspace version changed")
    duration = int(lease_seconds or spec.lease_seconds)
    if duration < 60 or duration > 7 * 86_400:
        raise WorkspaceError("lease_seconds must be between 60 and 604800")
    expiry = _iso(_now() + dt.timedelta(seconds=duration))
    with _connect(db_path, write=True) as conn:
        conn.execute(
            "UPDATE workspace_leases SET expires_at=?,updated_at=? WHERE lease_id=?",
            (expiry, _iso(), lease_id))
        return _row_view(conn.execute(
            "SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,)).fetchone())


def _changed_files(row: sqlite3.Row | dict) -> list[dict]:
    _enforce_quota(_repo(row["repo"]), Path(row["path"]))
    raw = _lease_git(
        row, "status", "--porcelain=v1", "-z", "--untracked-files=all",
    ).stdout
    parts = raw.split("\0")
    out: list[dict] = []
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        item = {"status": code, "path": path}
        if code[0] in {"R", "C"} and i < len(parts):
            item["from"] = parts[i]
            i += 1
        out.append(item)
    return out


def _enforce_quota(spec: RepositorySpec, root: Path) -> dict:
    """Bound a source tree without following agent-created symlinks.

    The filesystem/overlay quota is the hard outer boundary in production.
    This scan is the broker's semantic boundary: special files are rejected,
    and file count/per-file/aggregate bytes are checked before Git receives the
    tree.  Directories themselves count so millions of empty directories do not
    turn the preflight into unbounded work.
    """
    files = 0
    total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise WorkspaceError(f"cannot inspect workspace quota: {exc}") from exc
        for entry in entries:
            # The broker itself removed .git.  A recreated entry is caught by
            # _validate_lease_files before this scan; never descend into it.
            if entry.name == ".git":
                raise WorkspaceError("lease contains an unauthorized .git entry")
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise WorkspaceError(
                    f"cannot inspect workspace entry {entry.name!r}: {exc}") from exc
            files += 1
            if files > spec.max_files:
                raise WorkspaceError(
                    f"workspace exceeds the {spec.max_files}-entry quota")
            mode = stat.st_mode
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
                continue
            if entry.is_symlink():
                # Git stores the link text and does not follow its target.
                size = stat.st_size
            elif entry.is_file(follow_symlinks=False):
                size = stat.st_size
            else:
                raise WorkspaceError(
                    f"workspace contains unsupported special file {entry.name!r}")
            if size > spec.max_file_bytes:
                raise WorkspaceError(
                    f"workspace file {entry.name!r} exceeds the per-file quota")
            total += size
            if total > spec.max_total_bytes:
                raise WorkspaceError(
                    f"workspace exceeds the {spec.max_total_bytes}-byte quota")
    return {"entries": files, "bytes": total}


def status(lease_id: str, *, owner_role: str,
           db_path: Path | str | None = None) -> dict:
    spec, row = _owned(lease_id, owner_role, db_path)
    _validate_lease_files(spec, row)
    head = _lease_git(row, "rev-parse", "HEAD").stdout.strip().lower()
    branch = _lease_git(
        row, "symbolic-ref", "--short", "HEAD",
    ).stdout.strip()
    if branch != row["branch"]:
        raise WorkspaceError("workspace branch no longer matches its lease")
    quota = _enforce_quota(spec, Path(row["path"]))
    changed = _changed_files(row)
    return {
        "lease_id": lease_id, "repo": row["repo"], "branch": branch,
        "base_ref": row["base_ref"], "base_sha": row["base_sha"],
        "head_sha": head, "workspace_version": row["workspace_version"],
        "expires_at": row["expires_at"], "dirty": bool(changed),
        "changed_files": changed, "quota": quota,
        "clean": not changed,
        "committed": (not changed and head == row["head_sha"]),
    }


def diff(lease_id: str, *, owner_role: str, staged: bool = False,
         db_path: Path | str | None = None) -> dict:
    spec, row = _owned(lease_id, owner_role, db_path)
    _validate_lease_files(spec, row)
    args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
    if staged:
        args.append("--cached")
    args.append("--")
    _enforce_quota(spec, Path(row["path"]))
    proc = _lease_git(row, *args, truncate=True)
    raw = proc.stdout.encode("utf-8", errors="replace")
    truncated = bool(getattr(proc, "output_truncated", False))
    return {
        "lease_id": lease_id,
        "staged": staged,
        "diff": proc.stdout,
        "truncated": truncated,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _clear_index(row: sqlite3.Row | dict) -> None:
    # Broker-owned index only; preserves every worktree file.  This is not a
    # public reset operation and runs solely to roll back a failed staging step.
    _lease_git(row, "read-tree", "HEAD")


def commit(lease_id: str, *, owner_role: str, message: str,
           expected_head: str, expected_workspace_version: int,
           expected_base_sha: str | None = None,
           db_path: Path | str | None = None) -> dict:
    """Stage all lease files and create one commit under a broker identity.

    The caller must assert the head and workspace version it inspected.  The
    broker refuses a pre-staged index, disables hooks/signing/fsmonitor, and
    never exposes an arbitrary pathspec or Git option.
    """
    message = str(message or "").strip()
    if (not message or len(message) > 4000
            or any(ord(c) < 32 and c not in "\n\t" for c in message)):
        raise WorkspaceError("commit message is empty, too long, or unsafe")
    expected_head = _clean_sha(expected_head, "expected_head") or ""
    expected_base_sha = _clean_sha(expected_base_sha, "expected_base_sha")
    spec, row = _owned(lease_id, owner_role, db_path)
    with _repo_lock(spec):
        # Reload inside the repo lock so optimistic assertions cover the exact
        # state the following fixed Git sequence consumes.
        _, row = _owned(lease_id, owner_role, db_path)
        _validate_lease_files(spec, row)
        _enforce_quota(spec, Path(row["path"]))
        if expected_base_sha and row["base_sha"] != expected_base_sha:
            raise WorkspaceError("workspace base SHA changed")
        head = _lease_git(row, "rev-parse", "HEAD").stdout.strip().lower()
        if head != expected_head:
            # Crash recovery: Git may have durably moved HEAD while the process
            # died before the lease/result transaction committed.  Accept only
            # the one exact broker-authored commit this request describes.
            proof = _lease_git(
                row, "show", "-s", "--format=%P%x00%B%x00%ae", head,
            ).stdout.split("\0", 2)
            email_role = _EMAIL_ROLE_RE.sub("-", owner_role.lower()).strip("-")
            expected_email = f"{email_role or 'agent'}@deskd.invalid"
            parents = proof[0].split() if proof else []
            recovered = (len(proof) == 3 and parents == [expected_head]
                         and proof[1].strip() == message
                         and proof[2].strip() == expected_email)
            if not recovered:
                raise WorkspaceError(f"workspace head moved: {head}")
            try:
                with _connect(db_path, write=True) as conn:
                    current = conn.execute(
                        "SELECT head_sha,workspace_version FROM workspace_leases "
                        "WHERE lease_id=?", (lease_id,)).fetchone()
                    if current["head_sha"] == expected_head:
                        version = _next_version(conn, row["repo"])
                        conn.execute(
                            "UPDATE workspace_leases SET head_sha=?,workspace_version=?,"
                            " updated_at=?,last_error=NULL WHERE lease_id=?",
                            (head, version, _iso(), lease_id))
                    elif current["head_sha"] != head:
                        raise WorkspaceError("lease ledger and Git HEAD diverged")
            except WorkspaceError:
                raise
            except Exception as exc:
                raise WorkspaceOutcomeUnknown(
                    "recovered commit proof could not update its lease receipt"
                ) from exc
            try:
                recovered_status = status(
                    lease_id, owner_role=owner_role, db_path=db_path)
            except Exception as exc:
                raise WorkspaceOutcomeUnknown(
                    "recovered commit exists but its result could not be projected"
                ) from exc
            return {
                **recovered_status,
                "committed": True, "commit_sha": head, "recovered": True,
                "author": {"name": f"deskd agent {owner_role}",
                           "email": expected_email},
            }
        if int(row["workspace_version"]) != int(expected_workspace_version):
            raise WorkspaceError("workspace version changed")
        if _lease_git(
                row, "diff", "--cached", "--quiet", "--", ok=(0, 1),
        ).returncode != 0:
            raise WorkspaceError("index is not clean; broker refuses pre-staged data")
        _lease_git(row, "add", "--all", "--")
        if _lease_git(
                row, "diff", "--cached", "--quiet", "--", ok=(0, 1),
        ).returncode == 0:
            return {**status(lease_id, owner_role=owner_role, db_path=db_path),
                    "committed": False, "reason": "no changes"}
        try:
            _lease_git(row, "diff", "--cached", "--check", "--")
            email_role = _EMAIL_ROLE_RE.sub("-", owner_role.lower()).strip("-")
            _lease_git(
                row,
                "-c", "core.hooksPath=/dev/null",
                "-c", "commit.gpgSign=false",
                "-c", f"user.name=deskd agent {owner_role}",
                "-c", f"user.email={email_role or 'agent'}@deskd.invalid",
                "commit", "--no-gpg-sign", "--no-verify", "-m", message,
            )
        except Exception as exc:
            # A killed Git process or lost pipe can report an error after Git
            # atomically moved HEAD.  Inspect the durable fact before deciding
            # this was a normal validation failure.
            try:
                observed = _lease_git(
                    row, "rev-parse", "HEAD").stdout.strip().lower()
            except Exception as inspect_exc:
                raise WorkspaceOutcomeUnknown(
                    "commit outcome cannot be inspected; identical retry required"
                ) from inspect_exc
            if observed != head:
                raise WorkspaceOutcomeUnknown(
                    "commit moved HEAD before its lease receipt committed; "
                    "identical retry required") from exc
            _clear_index(row)
            raise
        try:
            new_head = _lease_git(row, "rev-parse", "HEAD").stdout.strip().lower()
            if new_head == head:
                raise WorkspaceOutcomeUnknown(
                    "commit reported success but HEAD did not move")
            with _connect(db_path, write=True) as conn:
                version = _next_version(conn, row["repo"])
                conn.execute(
                    "UPDATE workspace_leases SET head_sha=?,workspace_version=?,"
                    " updated_at=?,last_error=NULL WHERE lease_id=?",
                    (new_head, version, _iso(), lease_id))
            return {
                **status(lease_id, owner_role=owner_role, db_path=db_path),
                "committed": True, "commit_sha": new_head,
                "author": {"name": f"deskd agent {owner_role}",
                           "email": f"{email_role or 'agent'}@deskd.invalid"},
            }
        except WorkspaceOutcomeUnknown:
            raise
        except Exception as exc:
            raise WorkspaceOutcomeUnknown(
                "commit moved HEAD before its result committed; "
                "identical retry required") from exc


def release(lease_id: str, *, owner_role: str,
            expected_workspace_version: int,
            db_path: Path | str | None = None) -> dict:
    spec, row = _owned(lease_id, owner_role, db_path, allow_released=True)
    if row["state"] == "released":
        if int(row["workspace_version"]) != int(expected_workspace_version):
            raise WorkspaceError("workspace version changed")
        out = _row_view(row)
        out["recovered"] = True
        return out
    with _repo_lock(spec):
        _, row = _owned(lease_id, owner_role, db_path, allow_released=True)
        if int(row["workspace_version"]) != int(expected_workspace_version):
            raise WorkspaceError("workspace version changed")
        marker = f"release-in-progress:{int(expected_workspace_version)}"
        recovering = row["last_error"] == marker
        if (row["last_error"] or "").startswith("release-in-progress:") \
                and not recovering:
            raise WorkspaceError("workspace has a different release intent")
        path = Path(row["path"])
        if path.exists() and not recovering:
            if row["git_dir"]:
                _validate_lease_files(spec, row)
                dirty = _changed_files(row)
                if dirty:
                    raise WorkspaceError(
                        "workspace has uncommitted changes; commit before release")
                head = _lease_git(row, "rev-parse", "HEAD").stdout.strip().lower()
                if head != row["head_sha"]:
                    raise WorkspaceError(
                        "workspace HEAD is not a broker-recorded commit")
        if not recovering:
            # Durable intent separates "validation failed before deletion"
            # from "deletion may be partial".  A retry bearing the same CAS
            # version may finish only this exact broker-derived path.
            with _connect(db_path, write=True) as conn:
                changed = conn.execute(
                    "UPDATE workspace_leases SET last_error=?,updated_at=? "
                    "WHERE lease_id=? AND state!='released' AND workspace_version=?",
                    (marker, _iso(), lease_id, int(expected_workspace_version)),
                ).rowcount
                if changed != 1:
                    raise WorkspaceError("workspace release intent CAS failed")
        try:
            if path.exists():
                # The source-only lease intentionally has no .git pointer, and
                # Git refuses worktree remove without it.  The clean proof was
                # committed with the intent above; recovery completes this one
                # exact derived path even if an earlier rmtree was partial.
                shutil.rmtree(path)
            _repo_git(spec, "worktree", "prune", "--expire", "now")
            now = _iso()
            with _connect(db_path, write=True) as conn:
                changed = conn.execute(
                    "UPDATE workspace_leases SET state='released',released_at=?,"
                    " updated_at=?,last_error=NULL WHERE lease_id=? "
                    "AND last_error=? AND workspace_version=?",
                    (now, now, lease_id, marker,
                     int(expected_workspace_version)),
                ).rowcount
                if changed != 1:
                    raise WorkspaceOutcomeUnknown(
                        "release finished but its lease receipt CAS failed")
                fresh = conn.execute(
                    "SELECT * FROM workspace_leases WHERE lease_id=?",
                    (lease_id,)).fetchone()
            out = _row_view(fresh)
            if recovering:
                out["recovered"] = True
            return out
        except WorkspaceOutcomeUnknown:
            raise
        except Exception as exc:
            raise WorkspaceOutcomeUnknown(
                "workspace release intent committed; identical retry required"
            ) from exc
