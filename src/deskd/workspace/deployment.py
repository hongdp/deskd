"""Explicit fresh-prefix installation and managed workspace entry points.

No discovery of existing users, daemons, homes or credentials. Installation is
an explicit administrator operation into a new protected prefix. CI exercises
it only on disposable machines, with an isolated /tmp and a local model mock.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import time

from deskd.gateway.__main__ import _manifest, control
from deskd.gateway.identity import PrincipalId, identifier
from .installation import (
    Installation,
    RoleInstallation,
    OFFICIAL_LINUX_X64_SHA256,
    OFFICIAL_BWRAP_SHA256,
    CONSOLE_ASSETS,
)


def _protected_directory(path):
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("unprotected_installation_parent")


def _json_write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    os.fchmod(fd, 0o644)
    with os.fdopen(fd, "w") as target:
        json.dump(value, target, ensure_ascii=False, indent=2)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _copy_package(package, library, inventory):
    """Copy only executable modules and the three fixed console assets.

    Every copied file belongs to the protected installation inventory. A new
    file appearing in a source static directory is not implicitly published.
    """
    relative_paths = (
        Path("__init__.py"),
        Path("config.py"),
        *[
            p.relative_to(package)
            for folder in ("gateway", "workspace")
            for p in sorted((package / folder).glob("*.py"))
        ],
        *[Path("workspace/static") / name for name in CONSOLE_ASSETS],
    )
    for relative in relative_paths:
        source = package / relative
        for part in (source, *source.parents):
            if part == package:
                break
            if part.is_symlink():
                raise ValueError("untrusted_package_symlink")
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("invalid_package_artifact")
            contents = stream.read()
        target = library / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        for parent in (library, target.parent):
            parent.chmod(0o755)
        target.write_bytes(contents)
        target.chmod(0o644)
        inventory[str(target)] = hashlib.sha256(contents).hexdigest()


def install(
    prefix: Path,
    *,
    binary: Path,
    harness_uid: int,
    gateway_uid: int,
    business_gid: int,
    mock_port: int | None = None,
    provider: str = "mock",
    model: str = "gpt-5.5",
    desk_id="desk",
    python: Path | None = None,
):
    """Install a fixed provider profile into a fresh root-owned prefix.

    This operation never imports or reads a key. API installations require
    separate administrator provisioning of the gateway's private key file.
    """
    if os.geteuid() != 0:
        raise ValueError("administrator_required")
    if provider == "mock" and mock_port is None:
        raise ValueError("mock_endpoint_required")
    python = (python or Path(sys.executable)).resolve()
    _protected_directory(python.parent)
    info = python.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("unprotected_python_interpreter")
    version = subprocess.run(
        [
            str(python),
            "-I",
            "-S",
            "-c",
            "import sys;print(int(sys.version_info >= (3,11)))",
        ],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    if version.stdout.strip() != "1":
        raise ValueError("python_3_11_required")
    python_pin = {
        "path": str(python),
        "sha256": hashlib.sha256(python.read_bytes()).hexdigest(),
    }
    prefix = prefix.absolute()
    _protected_directory(prefix.parent)
    PrincipalId(desk_id, "analyst")
    fd = os.open(binary, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        binary_info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(binary_info.st_mode)
            or binary_info.st_size > 300 * 1024 * 1024
        ):
            raise ValueError("invalid_runtime_artifact")
        binary_bytes = source.read(300 * 1024 * 1024 + 1)
    if hashlib.sha256(binary_bytes).hexdigest() != OFFICIAL_LINUX_X64_SHA256:
        raise ValueError("official_runtime_pin_mismatch")
    helper_path = binary.parent / "codex-resources/bwrap"
    fd = os.open(helper_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
            raise ValueError("invalid_sandbox_helper")
        helper_bytes = source.read(2 * 1024 * 1024 + 1)
    if hashlib.sha256(helper_bytes).hexdigest() != OFFICIAL_BWRAP_SHA256:
        raise ValueError("sandbox_helper_pin_mismatch")
    roles = tuple(
        RoleInstallation(
            name, str(prefix / "roles" / name), str(prefix / "roles" / name / "data")
        )
        for name in ("analyst", "trader", "engineer")
    )
    installation = Installation(
        str(prefix), harness_uid, gateway_uid, business_gid, roles
    )
    plan = installation.plan(
        mock_port=mock_port, with_gateway_bridge=True, provider=provider, model=model
    )
    prefix.mkdir(mode=0o755)  # Never replace or repair an existing installation.
    for item in plan["directories"]:
        path = Path(item["path"])
        if path != prefix:
            path.mkdir(mode=int(item["mode"], 8))
        os.chown(path, item["uid"], item["gid"])
        path.chmod(int(item["mode"], 8))
    for name, uid, gid, mode in (
        ("business", gateway_uid, business_gid, 0o750),
        ("admin", gateway_uid, gateway_uid, 0o700),
        ("lib", 0, 0, 0o755),
    ):
        path = prefix / name
        path.mkdir(mode=mode)
        os.chown(path, uid, gid)
        path.chmod(mode)
    inventory = {}
    for item in plan["files"]:
        path = Path(item["path"])
        with path.open("x") as target:
            target.write(item["content"])
        path.chmod(int(item["mode"], 8))
        inventory[str(path)] = hashlib.sha256(item["content"].encode()).hexdigest()
    Path(installation.binary).write_bytes(binary_bytes)
    Path(installation.binary).chmod(0o755)
    inventory[installation.binary] = OFFICIAL_LINUX_X64_SHA256
    helpers = prefix / "bin/codex-resources"
    helpers.mkdir(mode=0o755)
    helpers.chmod(0o755)
    bwrap = helpers / "bwrap"
    bwrap.write_bytes(helper_bytes)
    bwrap.chmod(0o755)
    inventory[str(bwrap)] = OFFICIAL_BWRAP_SHA256
    package = Path(__file__).resolve().parents[1]
    _copy_package(package, prefix / "lib/deskd", inventory)
    wrapper = (
        f"#!{python} -I\nimport sys\nsys.dont_write_bytecode = True\nsys.path.insert(0, "
        + repr(str(prefix / "lib"))
        + ')\nfrom deskd.workspace.__main__ import main\nraise SystemExit(main(["bridge", *sys.argv[1:]]))\n'
    )
    bridge = prefix / "bin/deskd-bridge"
    bridge.write_text(wrapper)
    bridge.chmod(0o755)
    inventory[str(bridge)] = hashlib.sha256(wrapper.encode()).hexdigest()
    if provider == "api":
        auth_wrapper = wrapper.replace('main(["bridge",', 'main(["model-auth",')
        helper = prefix / "bin/deskd-model-auth"
        helper.write_text(auth_wrapper)
        helper.chmod(0o755)
        inventory[str(helper)] = hashlib.sha256(auth_wrapper.encode()).hexdigest()
    deployment = {
        "schema_version": 1,
        "desk_id": desk_id,
        "installation": asdict(installation),
        "mock_port": mock_port,
        "provider": provider,
        "model": model,
        "inventory": inventory,
        "python": python_pin,
    }
    _json_write(prefix / "policy/deployment.json", deployment)
    return prefix / "policy/deployment.json"


class Deployment:
    def __init__(self, path):
        self.path = Path(path)
        self.value = _manifest(self.path, protected=True)
        if (
            set(self.value)
            != {
                "schema_version",
                "desk_id",
                "installation",
                "mock_port",
                "inventory",
                "python",
                "provider",
                "model",
            }
            or self.value["schema_version"] != 1
        ):
            raise ValueError("invalid_deployment")
        declaration = dict(self.value["installation"])
        declaration["roles"] = tuple(
            RoleInstallation(**r) for r in declaration["roles"]
        )
        self.installation = Installation(**declaration)
        self.prefix = Path(self.installation.prefix)
        if self.path != self.prefix / "policy/deployment.json":
            raise ValueError("unexpected_deployment_path")
        self.plan = self.installation.plan(
            mock_port=self.value["mock_port"],
            with_gateway_bridge=True,
            provider=self.value["provider"],
            model=self.value["model"],
        )
        self.gateway_db = self.prefix / "gateway/authority.db"
        self.coordination_db = self.prefix / "gateway/workspace.db"
        self.admin_path = self.prefix / "admin/s"
        self.business_path = self.prefix / "business/s"
        self.roots_path = self.prefix / "policy/roots.json"
        self._verified = {}

    def attest_console(self):
        """Require the pinned UI only when starting its private control surface.

        Older installations can still use their existing observer and runtime;
        they cannot silently start a new console from untracked local assets.
        """
        assets = self.prefix / "lib/deskd/workspace/static"
        if any(
            str(assets / name) not in self.value["inventory"]
            for name in CONSOLE_ASSETS
        ):
            raise ValueError("console_assets_not_installed")
        self.attest()

    def attest(self, *, gateway_only=False):
        python_pin = self.value["python"]
        python = Path(python_pin["path"])
        _protected_directory(python.parent)
        info = python.lstat()
        stamp = (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("unprotected_python_interpreter")
        if self._verified.get("python") != stamp:
            if hashlib.sha256(python.read_bytes()).hexdigest() != python_pin["sha256"]:
                raise ValueError("python_interpreter_changed")
            self._verified["python"] = stamp
        if _manifest(self.path, protected=True) != self.value:
            raise ValueError("deployment_changed")
        for item in self.plan["files"]:
            if (
                self.value["inventory"].get(item["path"])
                != hashlib.sha256(item["content"].encode()).hexdigest()
            ):
                raise ValueError("policy_inventory_mismatch")
        inventory = self.value["inventory"]
        required = {
            self.installation.binary: OFFICIAL_LINUX_X64_SHA256,
            str(self.prefix / "bin/codex-resources/bwrap"): OFFICIAL_BWRAP_SHA256,
        }
        if any(inventory.get(path) != digest for path, digest in required.items()):
            raise ValueError("missing_official_runtime_pin")
        if (
            self.value["provider"] == "api"
            and str(self.prefix / "bin/deskd-model-auth") not in inventory
        ):
            raise ValueError("missing_model_auth_pin")
        if str(self.prefix / "bin/deskd-bridge") not in inventory:
            raise ValueError("missing_bridge_pin")
        library = self.prefix / "lib"
        for path in library.rglob("*"):
            info = path.lstat()
            if (
                info.st_uid != 0
                or info.st_mode & 0o022
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
            ):
                raise ValueError("unprotected_library_entry")
            if stat.S_ISREG(info.st_mode) and str(path) not in inventory:
                raise ValueError("untracked_library_entry")
        for name, expected in inventory.items():
            path = Path(name)
            if self.prefix not in path.parents:
                raise ValueError("inventory_outside_installation")
            if gateway_only and self.prefix / "harness" in path.parents:
                continue  # Root controller independently validates private harness policy.
            for parent in reversed(path.parents):
                info = parent.lstat()
                allowed_owner = (
                    self.installation.harness_uid
                    if parent == self.prefix / "harness"
                    else 0
                )
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != allowed_owner
                    or info.st_mode & 0o022
                ):
                    raise ValueError("unprotected_artifact_parent")
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o022
                or info.st_nlink != 1
            ):
                raise ValueError("unprotected_runtime_artifact")
            stamp = (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
            if self._verified.get(name) != stamp:
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise ValueError("runtime_artifact_changed")
                self._verified[name] = stamp
        for item in self.plan["directories"]:
            if gateway_only and (
                Path(item["path"]) == self.prefix / "harness"
                or self.prefix / "harness" in Path(item["path"]).parents
            ):
                continue
            info = Path(item["path"]).lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != item["uid"]
                or info.st_gid != item["gid"]
                or stat.S_IMODE(info.st_mode) != int(item["mode"], 8)
            ):
                raise ValueError("installation_directory_changed")

    def admin(self, method, params=None):
        return control(
            self.admin_path, self.installation.gateway_uid, method, params or {}
        )

    def runtime(self):
        from .runtime import CodexRuntime

        return CodexRuntime(
            str(self.prefix / "harness/app-server-control/app-server-control.sock"),
            self.installation.harness_uid,
            str(self.prefix / "harness"),
            timeout=5,
        ).connect()

    def root_config(self, role):
        from .runtime import RootConfig

        return RootConfig(
            cwd=role.data,
            model=self.value["model"],
            model_provider="deskd_" + self.value["provider"],
            sandbox=None,
            permissions=role.seat,
            expected_sandbox={
                "type": "workspaceWrite",
                "writableRoots": [],
                "networkAccess": False,
                "excludeTmpdirEnvVar": True,
                "excludeSlashTmp": True,
            },
        )

    def seats(self):
        from .controller import Seat

        roots = _manifest(self.roots_path, protected=True)
        result = []
        expected = {
            self.value["desk_id"] + "/" + r.seat: r for r in self.installation.roles
        }
        if set(roots) != set(expected):
            raise ValueError("registered_roots_mismatch")
        for principal, role in expected.items():
            row = roots[principal]
            digest = hashlib.sha256(
                json.dumps(self.value, sort_keys=True).encode()
            ).hexdigest()
            if (
                type(row) is not dict
                or set(row) != {"root_id", "manifest_hash", "binding_generation"}
                or row["manifest_hash"] != digest
                or type(row["binding_generation"]) is not int
                or row["binding_generation"] < 1
            ):
                raise ValueError("registered_policy_mismatch")
            identifier(row["root_id"], "root_id")
            if row["root_id"] in {s.root_id for s in result}:
                raise ValueError("duplicate_registered_root")
            result.append(
                Seat(
                    principal,
                    row["root_id"],
                    row["manifest_hash"],
                    row["binding_generation"],
                    self.root_config(role),
                )
            )
        return tuple(result)


def run_installed(command, path, *, daemon_pid=None):
    from .service import RemoteWorkspaceStore

    deployment = Deployment(path)
    if command == "gateway":
        from .service import WorkspaceGateway

        if os.geteuid() != deployment.installation.gateway_uid:
            raise ValueError("wrong_gateway_uid")
        deployment.attest(gateway_only=True)
        os.umask(0o077)
        auth_provider = None
        if deployment.value["provider"] == "api":
            from .model_auth import ModelKeySource

            def authorize_model_key():
                deployment.attest(gateway_only=True)
                with sqlite3.connect(deployment.gateway_db) as conn:
                    row = conn.execute(
                        "SELECT generation,active FROM service WHERE singleton=1"
                    ).fetchone()
                if (
                    not row
                    or row[0] != gateway.transport.service_generation
                    or row[1] != 1
                ):
                    raise ValueError("model_auth_service_fenced")

            key_source = ModelKeySource(
                deployment.prefix / "gateway/model.key",
                deployment.installation.gateway_uid,
                authorize=authorize_model_key,
            )
            auth_provider = key_source.read_token
        gateway = WorkspaceGateway(
            gateway_db=deployment.gateway_db,
            coordination_db=deployment.coordination_db,
            harness_uid=deployment.installation.harness_uid,
            business_gid=deployment.installation.business_gid,
            business_path=deployment.business_path,
            admin_path=deployment.admin_path,
            principals=[
                deployment.value["desk_id"] + "/" + r.seat
                for r in deployment.installation.roles
            ],
            activation_check=lambda: deployment.attest(gateway_only=True),
            auth_provider=auth_provider,
        )
        gateway.start()

        def stop(_sig, _frame):
            gateway.close()

        signal.signal(signal.SIGTERM, stop)
        try:
            gateway.serve_forever()
        finally:
            gateway.close()
        return
    if os.geteuid() != 0:
        raise ValueError("independent_administrator_required")
    deployment.attest()
    if not deployment.coordination_db.is_file():
        raise ValueError("start_fenced_gateway_first")
    store = RemoteWorkspaceStore(deployment.admin)
    if command == "bootstrap":
        from .exchange import ACTIONS

        if deployment.admin("fence").get("ok") is not True:
            raise ValueError("gateway_fence_failed")
        store.start_service()  # Fence coordination before completing any registration.
        roots = {}
        digest = hashlib.sha256(
            json.dumps(deployment.value, sort_keys=True).encode()
        ).hexdigest()
        observed = deployment.admin("workspace.bindings")
        if observed.get("ok") is not True:
            raise ValueError("bootstrap_registry_unavailable")
        existing = {b["principal"]: b for b in observed["result"]}
        stored = {row["principal"]: row for row in store.snapshot()["seats"]}
        expected = {
            deployment.value["desk_id"] + "/" + role.seat
            for role in deployment.installation.roles
        }
        if set(existing) - expected or set(stored) - expected:
            raise ValueError("bootstrap_unexpected_existing_principal")

        def capabilities(principal):
            seat = principal.split("/")[1]
            caps = [*ACTIONS, "state.read", "proposal.create"]
            if seat == "analyst":
                caps += ["approval.issue", "approval.revoke"]
            if seat == "trader":
                caps += ["action.execute"]
            return caps

        def validate_record(principal, row):
            old = existing.get(principal)
            previous = stored.get(principal)
            if old is not None and (
                old["root_id"] != row["root_id"]
                or old["manifest_hash"] != digest
                or old["status"] not in {"bound", "revoked"}
                or old["binding_generation"] != 1 + int(old["status"] == "revoked")
                or set(old["capabilities"]) != set(capabilities(principal))
            ):
                raise ValueError("bootstrap_existing_authority_mismatch")
            if previous is not None and (
                previous["root_id"] != row["root_id"]
                or previous["manifest_hash"] != digest
                or previous["binding_generation"] != 1
            ):
                raise ValueError("bootstrap_existing_workspace_mismatch")
            if old is None and previous is not None and previous["revoked"]:
                raise ValueError("bootstrap_revoked_authority_missing")
            return (old is not None and old["status"] == "revoked") or (
                previous is not None and bool(previous["revoked"])
            )

        if not deployment.roots_path.exists() and (existing or stored):
            raise ValueError("bootstrap_existing_state_without_root_record")
        runtime = deployment.runtime()
        try:
            if deployment.roots_path.exists():
                for seat in deployment.seats():
                    if seat.manifest_hash != digest or seat.binding_generation != 1:
                        raise ValueError("bootstrap_record_changed")
                    roots[seat.principal] = {
                        "root_id": seat.root_id,
                        "manifest_hash": digest,
                        "binding_generation": 1,
                    }
                # Validate every existing identity before resuming any root.
                revoked = {
                    principal: validate_record(principal, row)
                    for principal, row in roots.items()
                }
                for seat in deployment.seats():
                    if not revoked[seat.principal]:
                        runtime.resume_root(seat.root_id, seat.config)
            else:
                for role in deployment.installation.roles:
                    binding = runtime.start_root(deployment.root_config(role))
                    principal = deployment.value["desk_id"] + "/" + role.seat
                    roots[principal] = {
                        "root_id": binding.thread_id,
                        "manifest_hash": digest,
                        "binding_generation": 1,
                    }
                _json_write(deployment.roots_path, roots)
        finally:
            runtime.close()
        for principal, row in roots.items():
            desk, seat = principal.split("/")
            caps = capabilities(principal)
            old = existing.get(principal)
            revoked = validate_record(principal, row)
            if old is None:
                reply = deployment.admin(
                    "bind",
                    {
                        "desk_id": desk,
                        "seat_id": seat,
                        "root_session_id": row["root_id"],
                        "manifest_hash": digest,
                        "capabilities": caps,
                        "expected_binding_generation": 0,
                    },
                )
                if reply.get("ok") is not True:
                    raise ValueError("partial_bootstrap_retry_same_record")
            registered = store.register_seat(principal, row["root_id"], digest)
            if revoked:
                reply = deployment.admin(
                    "workspace.revoke",
                    {
                        "principal": principal,
                        "expected_binding_generation": 1,
                        "expected_version": registered["version"],
                    },
                )
                if reply.get("ok") is not True:
                    raise ValueError("partial_bootstrap_revocation_repair_required")
        print(json.dumps({"registered": len(roots), "fenced": True}))
        return
    from .controller import WorkspaceController, descendant

    if type(daemon_pid) is not int or daemon_pid <= 1:
        raise ValueError("managed_daemon_pid_required")

    def process_identity():
        path = Path(f"/proc/{daemon_pid}")
        raw = (path / "stat").read_text()
        tail = raw[raw.rindex(")") + 2 :].split()
        executable = (path / "exe").stat()
        expected_executable = Path(deployment.installation.binary).stat()
        if (executable.st_dev, executable.st_ino) != (
            expected_executable.st_dev,
            expected_executable.st_ino,
        ):
            raise ValueError("managed_daemon_executable_mismatch")
        return (path.stat().st_uid, tail[19])

    pinned_process = process_identity()
    if pinned_process[0] != deployment.installation.harness_uid:
        raise ValueError("wrong_daemon_owner")

    def alive():
        try:
            return process_identity() == pinned_process
        except (OSError, ValueError, IndexError):
            return False

    seats = deployment.seats()
    roots_snapshot = _manifest(deployment.roots_path, protected=True)

    def attest():
        deployment.attest()
        if _manifest(deployment.roots_path, protected=True) != roots_snapshot:
            raise ValueError("registered_roots_changed")

    runtime = deployment.runtime()
    if runtime.peer_pid != daemon_pid:
        runtime.close()
        raise ValueError("managed_daemon_peer_mismatch")
    controller = WorkspaceController(
        store,
        runtime,
        deployment.admin,
        seats,
        attest=attest,
        daemon_alive=alive,
        accepts_pid=lambda pid: descendant(pid, daemon_pid),
    )
    stopping = False

    def stop(_sig, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    try:
        controller.start()
        while not stopping:
            controller.tick()
            time.sleep(0.05)
    finally:
        controller.close()


def attach_seat(path, seat_name):
    """Explicit human entry, using the original runtime and registered root."""
    if os.geteuid() != 0:
        raise ValueError("independent_administrator_required")
    deployment = Deployment(path)
    deployment.attest()
    seats = deployment.seats()
    seat = next((s for s in seats if s.principal.split("/")[1] == seat_name), None)
    if seat is None:
        raise ValueError("unknown_seat")
    gateway = deployment.admin("status")
    workspace = deployment.admin("workspace.status")
    bindings = deployment.admin("workspace.bindings")
    if (
        gateway.get("ok") is not True
        or gateway.get("result", {}).get("fenced") is not False
        or workspace.get("ok") is not True
        or workspace.get("result", {}).get("service", {}).get("active") != 1
        or bindings.get("ok") is not True
    ):
        raise ValueError("managed_workspace_not_ready")
    row = next(
        (r for r in workspace["result"]["seats"] if r["principal"] == seat.principal),
        None,
    )
    binding = next(
        (r for r in bindings["result"] if r["principal"] == seat.principal), None
    )
    if (
        row is None
        or row["revoked"]
        or binding is None
        or binding["status"] != "bound"
        or any(
            record[key] != expected
            for record in (row, binding)
            for key, expected in (
                ("root_id", seat.root_id),
                ("manifest_hash", seat.manifest_hash),
                ("binding_generation", seat.binding_generation),
            )
        )
    ):
        raise ValueError("seat_not_authorized")
    runtime = deployment.runtime()
    try:
        runtime.resume_root(seat.root_id, seat.config)
        runtime.read_root(seat.root_id)
    finally:
        runtime.close()
    environment = dict(deployment.plan["launch"]["environment"])
    environment["TERM"] = "xterm-256color"
    os.chdir(seat.config.cwd)
    os.setgroups([deployment.installation.business_gid])
    os.setgid(deployment.installation.harness_uid)
    os.setuid(deployment.installation.harness_uid)
    os.execve(
        deployment.installation.binary,
        [
            deployment.installation.binary,
            "--remote",
            "unix://"
            + str(
                deployment.prefix / "harness/app-server-control/app-server-control.sock"
            ),
            "--cd",
            seat.config.cwd,
            "resume",
            seat.root_id,
        ],
        environment,
    )
