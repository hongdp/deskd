"""Deployment validation with explicit synthetic files, never a local install.

Protected-path ownership is validated by the disposable root acceptance job.
Tests replacing the trusted manifest reader below exercise semantic checks only.
"""

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat
import tomllib
from types import SimpleNamespace

import pytest

from deskd.workspace import deployment
from deskd.workspace.installation import Installation, RoleInstallation


def test_json_manifest_is_exclusive_and_readable_with_restrictive_umask(tmp_path):
    path = tmp_path / "manifest.json"
    previous = os.umask(0o077)
    try:
        deployment._json_write(path, {"synthetic": True})
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert json.loads(path.read_text()) == {"synthetic": True}
    with pytest.raises(FileExistsError):
        deployment._json_write(path, {"replacement": True})
    assert json.loads(path.read_text()) == {"synthetic": True}


def test_json_manifest_never_follows_an_existing_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("unchanged")
    link = tmp_path / "manifest.json"
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        deployment._json_write(link, {"replacement": True})
    assert target.read_text() == "unchanged"


def test_install_rejects_nonadministrator_before_reading_any_binary(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(deployment.os, "geteuid", lambda: 1000)
    with pytest.raises(ValueError, match="administrator_required"):
        deployment.install(
            tmp_path / "never-created",
            binary=tmp_path / "not-read",
            harness_uid=12001,
            gateway_uid=12002,
            business_gid=12003,
            mock_port=12345,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def declared(tmp_path, monkeypatch):
    prefix = tmp_path / "d"
    roles = tuple(
        RoleInstallation(
            name, str(prefix / "roles" / name), str(prefix / "roles" / name / "data")
        )
        for name in ("analyst", "trader", "engineer")
    )
    installation = Installation(str(prefix), 12001, 12002, 12003, roles)
    path = prefix / "policy/deployment.json"
    path.parent.mkdir(parents=True)
    value = {
        "schema_version": 1,
        "desk_id": "desk",
        "installation": asdict(installation),
        "mock_port": 12345,
        "provider": "mock",
        "model": "gpt-5.5",
        "inventory": {},
        "python": {
            "path": str(Path("/usr/bin/python3").resolve()),
            "sha256": hashlib.sha256(
                Path("/usr/bin/python3").resolve().read_bytes()
            ).hexdigest(),
        },
    }
    path.write_text(json.dumps(value))
    accesses = []

    def read_manifest(candidate, *, protected=False):
        accesses.append((Path(candidate), protected))
        assert protected is True
        return json.loads(Path(candidate).read_text())

    monkeypatch.setattr(deployment, "_manifest", read_manifest)
    instance = deployment.Deployment(path)
    digest = hashlib.sha256(
        json.dumps(instance.value, sort_keys=True).encode()
    ).hexdigest()
    roots = {
        "desk/" + role.seat: {
            "root_id": "root-" + role.seat,
            "manifest_hash": digest,
            "binding_generation": 1,
        }
        for role in roles
    }
    instance.roots_path.write_text(json.dumps(roots))
    return instance, roots, accesses


def test_semantic_deployment_reader_requests_protection_and_exact_role_profiles(
    declared,
):
    instance, roots, accesses = declared
    seats = instance.seats()
    assert len(seats) == 3 and {s.principal for s in seats} == set(roots)
    assert accesses and all(protected for _, protected in accesses)
    for role, seat in zip(instance.installation.roles, seats):
        assert seat.config.permissions == role.seat
        assert seat.config.sandbox is None
        assert seat.config.cwd == role.data
        assert seat.config.expected_sandbox["networkAccess"] is False


def test_missing_or_extra_root_is_rejected_without_implicit_creation(declared):
    instance, roots, _ = declared
    del roots["desk/analyst"]
    instance.roots_path.write_text(json.dumps(roots))
    with pytest.raises(ValueError, match="registered_roots_mismatch"):
        instance.seats()


def test_stale_root_manifest_digest_is_not_current_policy_authority(declared):
    instance, roots, _ = declared
    roots["desk/analyst"]["manifest_hash"] = "f" * 64
    instance.roots_path.write_text(json.dumps(roots))
    with pytest.raises(ValueError):
        instance.seats()


def test_duplicate_root_cannot_represent_two_stable_principals(declared):
    instance, roots, _ = declared
    roots["desk/analyst"]["root_id"] = roots["desk/trader"]["root_id"]
    instance.roots_path.write_text(json.dumps(roots))
    with pytest.raises(ValueError):
        instance.seats()


def test_missing_policy_inventory_rejects_before_opening_runtime_artifacts(declared):
    instance, _, _ = declared
    with pytest.raises(ValueError, match="policy_inventory_mismatch"):
        instance.attest()


def test_changed_deployment_manifest_invalidates_attestation(declared):
    instance, _, _ = declared
    value = dict(instance.value)
    value["mock_port"] = 12346
    instance.path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="deployment_changed"):
        instance.attest()


@pytest.fixture
def bootstrap_model(declared, monkeypatch):
    """Exercise bootstrap transactions through the closed admin RPC in process.

    Only OS admission and runtime launch are replaced. The two databases,
    authority registry, command validation and management handlers are real.
    """
    from deskd.gateway.identity import IdentityError
    from deskd.workspace.runtime import RootBinding
    from deskd.workspace.service import WorkspaceGateway

    instance, roots, _ = declared
    instance.roots_path.unlink()
    instance.gateway_db.parent.mkdir()
    gateway = WorkspaceGateway(
        gateway_db=instance.gateway_db,
        coordination_db=instance.coordination_db,
        harness_uid=instance.installation.harness_uid,
        business_gid=instance.installation.business_gid,
        business_path=instance.business_path,
        admin_path=instance.admin_path,
        principals=["desk/" + r.seat for r in instance.installation.roles],
        activation_check=lambda: None,
    )
    gateway.transport.service_generation = gateway.registry.start_service()
    calls = []
    runtime_calls = []
    fail = {"principal": None}

    def admin(method, params=None):
        params = params or {}
        calls.append((method, params))
        if method == "bind" and params["seat_id"] == fail["principal"]:
            fail["principal"] = None
            return {"ok": False, "error": {"code": "synthetic_failure"}}
        try:
            return {"ok": True, "result": gateway.transport._admin_call(method, params)}
        except IdentityError as exc:
            return {"ok": False, "error": {"code": exc.code}}

    class Runtime:
        def start_root(self, config):
            root_id = "root-" + config.permissions
            runtime_calls.append(("start", root_id))
            return RootBinding(root_id, root_id, config.cwd)

        def resume_root(self, root_id, config):
            runtime_calls.append(("resume", root_id))
            return RootBinding(root_id, root_id, config.cwd)

        def close(self):
            runtime_calls.append(("close", None))

    monkeypatch.setattr(instance, "admin", admin)
    monkeypatch.setattr(instance, "runtime", Runtime)
    monkeypatch.setattr(instance, "attest", lambda **_: None)
    monkeypatch.setattr(deployment, "Deployment", lambda _: instance)
    monkeypatch.setattr(deployment.os, "geteuid", lambda: 0)
    yield instance, gateway, calls, runtime_calls, fail
    gateway.close()


def test_bootstrap_registers_fixed_roots_and_separated_capabilities(bootstrap_model):
    instance, gateway, calls, runtime_calls, _ = bootstrap_model
    deployment.run_installed("bootstrap", instance.path)
    assert calls[0][0] == "fence"
    assert not any(method in {"activate", "lease"} for method, _ in calls)
    assert [kind for kind, _ in runtime_calls] == ["start", "start", "start", "close"]
    bindings = gateway._handlers()["workspace.bindings"]({})
    by_seat = {row["principal"].split("/")[1]: row for row in bindings}
    assert "approval.issue" in by_seat["analyst"]["capabilities"]
    assert "action.execute" not in by_seat["analyst"]["capabilities"]
    assert "action.execute" in by_seat["trader"]["capabilities"]
    assert "approval.issue" not in by_seat["trader"]["capabilities"]
    assert not {"approval.issue", "action.execute"} & set(
        by_seat["engineer"]["capabilities"]
    )
    assert len(gateway.store.snapshot()["seats"]) == 3
    assert gateway.store.snapshot()["service"]["active"] == 0


def test_bootstrap_retry_repairs_only_same_record_after_partial_bind(bootstrap_model):
    instance, gateway, calls, runtime_calls, fail = bootstrap_model
    fail["principal"] = "trader"
    with pytest.raises(ValueError, match="partial_bootstrap"):
        deployment.run_installed("bootstrap", instance.path)
    recorded = instance.roots_path.read_bytes()
    assert len(gateway.store.snapshot()["seats"]) == 1
    deployment.run_installed("bootstrap", instance.path)
    assert instance.roots_path.read_bytes() == recorded
    assert sum(kind == "start" for kind, _ in runtime_calls) == 3
    assert sum(kind == "resume" for kind, _ in runtime_calls) == 3
    assert len(gateway.store.snapshot()["seats"]) == 3
    assert all(
        params.get("expected_binding_generation") == 0
        for method, params in calls
        if method == "bind"
    )


def test_completed_bootstrap_retry_does_not_rebind_or_increase_authority(
    bootstrap_model,
):
    instance, gateway, calls, runtime_calls, _ = bootstrap_model
    deployment.run_installed("bootstrap", instance.path)
    before = gateway._handlers()["workspace.bindings"]({})
    count = len(calls)
    deployment.run_installed("bootstrap", instance.path)
    assert gateway._handlers()["workspace.bindings"]({}) == before
    assert not any(method == "bind" for method, _ in calls[count:])
    assert sum(kind == "start" for kind, _ in runtime_calls) == 3


def test_bootstrap_cannot_restore_a_revoked_principal(bootstrap_model):
    from deskd.gateway.identity import PrincipalId

    instance, gateway, _, runtime_calls, _ = bootstrap_model
    deployment.run_installed("bootstrap", instance.path)
    gateway.registry.trusted_revoke(
        PrincipalId("desk", "trader"), expected_binding_generation=1
    )
    runtime_calls.clear()
    deployment.run_installed("bootstrap", instance.path)
    assert ("resume", "root-trader") not in runtime_calls
    assert (
        next(
            row
            for row in gateway.store.snapshot()["seats"]
            if row["principal"] == "desk/trader"
        )["revoked"]
        == 1
    )
    assert not any(kind == "start" for kind, _ in runtime_calls)
    row = next(
        r
        for r in gateway._handlers()["workspace.bindings"]({})
        if r["principal"] == "desk/trader"
    )
    assert row["status"] == "revoked"


def test_api_provider_has_fixed_endpoint_and_private_command_auth(declared):
    instance, _, _ = declared
    config = tomllib.loads(instance.installation.configuration(provider="api"))
    provider = config["model_providers"]["deskd_api"]
    assert config["model_provider"] == "deskd_api"
    assert provider["base_url"] == "https://api.openai.com/v1"
    assert provider["requires_openai_auth"] is False
    assert provider["supports_websockets"] is False
    assert provider["auth"] == {
        "command": str(instance.prefix / "bin/deskd-model-auth"),
        "args": ["--socket", str(instance.business_path), "--gateway-uid", "12002"],
        "cwd": str(instance.prefix / "base"),
        "timeout_ms": 5000,
        "refresh_interval_ms": 300000,
    }
    assert not {"env_key", "experimental_bearer_token"} & set(provider)
    assert config["shell_environment_policy"] == {
        "inherit": "none",
        "set": {"PATH": "/usr/bin:/bin"},
    }
    for role in instance.installation.roles:
        filesystem = config["permissions"][role.seat]["filesystem"]
        assert filesystem[str(instance.prefix / "gateway")] == "deny"
        assert filesystem[str(instance.prefix / "business")] == "deny"
        assert filesystem[str(instance.prefix / "harness")] == "deny"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"provider": "api", "mock_port": 12345},
        {"provider": "custom"},
        {"provider": "api", "model": "model\n[unauthorized]"},
    ],
)
def test_provider_does_not_accept_endpoint_or_config_injection(declared, kwargs):
    instance, _, _ = declared
    with pytest.raises(ValueError):
        instance.installation.configuration(**kwargs)


@pytest.fixture
def install_model(tmp_path, monkeypatch):
    """Test installation assembly with synthetic artifacts; no OS trust claim."""
    source = tmp_path / "source"
    source.mkdir()
    binary = source / "codex"
    binary.write_bytes(b"synthetic-runtime-artifact-never-executed")
    helper = source / "codex-resources/bwrap"
    helper.parent.mkdir()
    helper.write_bytes(b"synthetic-sandbox-artifact-never-executed")
    monkeypatch.setattr(deployment.os, "geteuid", lambda: 0)
    monkeypatch.setattr(deployment.os, "chown", lambda *_: None)
    monkeypatch.setattr(deployment, "_protected_directory", lambda _: None)
    monkeypatch.setattr(
        deployment,
        "OFFICIAL_LINUX_X64_SHA256",
        hashlib.sha256(binary.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(
        deployment,
        "OFFICIAL_BWRAP_SHA256",
        hashlib.sha256(helper.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(
        deployment.subprocess, "run", lambda *_, **__: SimpleNamespace(stdout="1\n")
    )
    return tmp_path / "i", binary


@pytest.mark.parametrize("provider", ["api", "mock"])
def test_install_writes_pinned_helpers_without_creating_a_key(install_model, provider):
    prefix, binary = install_model
    path = deployment.install(
        prefix,
        binary=binary,
        harness_uid=12001,
        gateway_uid=12002,
        business_gid=12003,
        provider=provider,
        mock_port=12345 if provider == "mock" else None,
        model="synthetic-model",
        python=Path("/usr/bin/python3"),
    )
    manifest = json.loads(path.read_text())
    assert manifest["provider"] == provider and manifest["model"] == "synthetic-model"
    assert not (prefix / "gateway/model.key").exists()
    for name, command in [
        ("deskd-bridge", "bridge"),
        ("deskd-model-auth", "model-auth"),
    ]:
        wrapper = prefix / "bin" / name
        if provider == "mock" and command == "model-auth":
            assert not wrapper.exists()
            continue
        text = wrapper.read_text()
        assert text.startswith(f"#!{Path('/usr/bin/python3').resolve()} -I\n")
        assert text.index("sys.dont_write_bytecode = True") < text.index("from deskd.")
        assert f'main(["{command}", *sys.argv[1:]])' in text
        assert (
            manifest["inventory"][str(wrapper)]
            == hashlib.sha256(wrapper.read_bytes()).hexdigest()
        )
        assert stat.S_IMODE(wrapper.stat().st_mode) == 0o755
    assert not list((prefix / "lib").rglob("__pycache__"))


@pytest.mark.parametrize("provider", ["api", "mock"])
def test_install_cli_explicitly_selects_provider_without_key_arguments(
    monkeypatch, capsys, provider
):
    from deskd.workspace.__main__ import main

    calls = []
    monkeypatch.setattr(
        deployment,
        "install",
        lambda *args, **kwargs: (
            calls.append((args, kwargs)) or Path("/synthetic/deployment.json")
        ),
    )
    argv = [
        "install" if provider == "api" else "install-mock",
        "--prefix",
        "/synthetic",
        "--binary",
        "/synthetic/codex",
        "--harness-uid",
        "12001",
        "--gateway-uid",
        "12002",
        "--business-gid",
        "12003",
        "--model",
        "synthetic-model",
    ]
    if provider == "mock":
        argv += ["--mock-port", "12345"]
    assert main(argv) == 0
    assert calls[0][1]["provider"] == provider
    assert calls[0][1]["mock_port"] == (12345 if provider == "mock" else None)
    assert not {"key", "token", "endpoint"} & set(calls[0][1])
    assert capsys.readouterr().err == ""


def test_api_install_cli_requires_explicit_model_before_install(monkeypatch):
    from deskd.workspace.__main__ import main

    monkeypatch.setattr(
        deployment, "install", lambda *_, **__: pytest.fail("must not install")
    )
    with pytest.raises(SystemExit) as error:
        main(
            [
                "install",
                "--prefix",
                "/synthetic",
                "--binary",
                "/synthetic/codex",
                "--harness-uid",
                "12001",
                "--gateway-uid",
                "12002",
                "--business-gid",
                "12003",
            ]
        )
    assert error.value.code == 2


@pytest.fixture
def gateway_auth_model(declared, monkeypatch):
    """Exercise deployment's authorization callback with a new synthetic key.

    Filesystem admission is modeled by a private test source; the production
    source's descriptor/owner/mode checks have separate adversarial unit tests.
    No gateway socket, process, real key or external provider is used here.
    """
    import sqlite3
    from deskd.workspace import model_auth, service

    instance, _, _ = declared
    instance.value["provider"] = "api"
    instance.value["mock_port"] = None
    instance.gateway_db.parent.mkdir()
    (instance.prefix / "gateway/model.key").write_text(
        "SYNTHETIC-ONLY-NOT-A-REAL-MODEL-KEY"
    )
    with sqlite3.connect(instance.gateway_db) as conn:
        conn.execute(
            "CREATE TABLE service(singleton INTEGER PRIMARY KEY, generation INTEGER, active INTEGER)"
        )
        conn.execute("INSERT INTO service VALUES(1,7,0)")
    state = {"reads": 0, "attested": 0, "tampered": False}

    def attest(**kwargs):
        assert kwargs == {"gateway_only": True}
        state["attested"] += 1
        if state["tampered"]:
            raise ValueError("synthetic_policy_changed")

    class SyntheticSource:
        def __init__(self, path, gateway_uid, *, authorize):
            assert path == instance.prefix / "gateway/model.key"
            assert gateway_uid == 12002
            self.authorize = authorize
            self.path = path

        def read_token(self):
            self.authorize()
            state["reads"] += 1
            return self.path.read_text()

    class Gateway:
        def __init__(self, **kwargs):
            state["provider"] = kwargs["auth_provider"]
            self.transport = SimpleNamespace(service_generation=7)

        def start(self):
            pass

        def serve_forever(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(instance, "attest", attest)
    monkeypatch.setattr(deployment, "Deployment", lambda _: instance)
    monkeypatch.setattr(deployment.os, "geteuid", lambda: 12002)
    monkeypatch.setattr(deployment.os, "umask", lambda _: 0o077)
    monkeypatch.setattr(deployment.signal, "signal", lambda *_: None)
    monkeypatch.setattr(model_auth, "ModelKeySource", SyntheticSource)
    monkeypatch.setattr(service, "WorkspaceGateway", Gateway)

    def status(generation, active):
        with sqlite3.connect(instance.gateway_db) as conn:
            conn.execute(
                "UPDATE service SET generation=?,active=?", (generation, active)
            )

    return instance, state, status


def test_model_key_read_waits_for_attested_current_active_generation(
    gateway_auth_model,
):
    instance, state, status = gateway_auth_model
    deployment.run_installed("gateway", instance.path)
    assert state["reads"] == 0
    auth = state["provider"]
    with pytest.raises(ValueError, match="model_auth_service_fenced"):
        auth()
    assert state["reads"] == 0
    status(6, 1)
    with pytest.raises(ValueError, match="model_auth_service_fenced"):
        auth()
    assert state["reads"] == 0
    status(7, 1)
    assert auth() == "SYNTHETIC-ONLY-NOT-A-REAL-MODEL-KEY"
    assert state["reads"] == 1
    state["tampered"] = True
    with pytest.raises(ValueError, match="synthetic_policy_changed"):
        auth()
    assert state["reads"] == 1
    state["tampered"] = False
    status(7, 0)
    with pytest.raises(ValueError, match="model_auth_service_fenced"):
        auth()
    assert state["reads"] == 1


def test_mock_gateway_never_configures_model_key_source(gateway_auth_model):
    instance, state, _ = gateway_auth_model
    instance.value["provider"] = "mock"
    deployment.run_installed("gateway", instance.path)
    assert state["provider"] is None and state["reads"] == 0


@pytest.fixture
def attach_model(declared, monkeypatch):
    """Observe the explicit human attach boundary without switching any UID."""
    instance, _, _ = declared
    seats = instance.seats()
    rows = [
        {
            "principal": seat.principal,
            "root_id": seat.root_id,
            "manifest_hash": seat.manifest_hash,
            "binding_generation": seat.binding_generation,
            "revoked": False,
        }
        for seat in seats
    ]
    bindings = [{**row, "status": "bound"} for row in rows]
    replies = {
        "status": {"ok": True, "result": {"fenced": False}},
        "workspace.status": {
            "ok": True,
            "result": {"service": {"active": 1}, "seats": rows},
        },
        "workspace.bindings": {"ok": True, "result": bindings},
    }
    calls = []
    runtime_failure = {"enabled": False}

    class Runtime:
        def resume_root(self, root_id, config):
            calls.append(("resume", root_id, config))
            if runtime_failure["enabled"]:
                raise ValueError("synthetic_runtime_mismatch")

        def read_root(self, root_id):
            calls.append(("read", root_id))

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(deployment, "Deployment", lambda _: instance)
    monkeypatch.setattr(instance, "attest", lambda: calls.append(("attest",)))
    monkeypatch.setattr(instance, "admin", lambda method: replies[method])
    monkeypatch.setattr(instance, "runtime", Runtime)
    monkeypatch.setattr(deployment.os, "geteuid", lambda: 0)
    for name in ("chdir", "setgroups", "setgid", "setuid", "execve"):
        monkeypatch.setattr(
            deployment.os, name, lambda *args, name=name: calls.append((name, *args))
        )
    monkeypatch.setenv("DESKD_SYNTHETIC_INHERITED_SECRET", "SYNTHETIC-NOT-A-REAL-KEY")
    return instance, replies, calls, runtime_failure


def test_attach_checks_existing_root_then_drops_groups_gid_uid_before_exec(
    attach_model,
):
    instance, _, calls, _ = attach_model
    deployment.attach_seat(instance.path, "trader")
    assert [call[0] for call in calls] == [
        "attest",
        "resume",
        "read",
        "close",
        "chdir",
        "setgroups",
        "setgid",
        "setuid",
        "execve",
    ]
    role = next(r for r in instance.installation.roles if r.seat == "trader")
    assert calls[1][1] == calls[2][1] == "root-trader"
    assert calls[1][2].permissions == "trader"
    assert calls[4:8] == [
        ("chdir", role.data),
        ("setgroups", [12003]),
        ("setgid", 12001),
        ("setuid", 12001),
    ]
    _, binary, argv, environment = calls[-1]
    assert binary == instance.installation.binary
    assert argv == [
        binary,
        "--remote",
        "unix://"
        + str(instance.prefix / "harness/app-server-control/app-server-control.sock"),
        "--cd",
        role.data,
        "resume",
        "root-trader",
    ]
    assert environment == {
        **instance.plan["launch"]["environment"],
        "TERM": "xterm-256color",
    }
    assert "DESKD_SYNTHETIC_INHERITED_SECRET" not in environment


@pytest.mark.parametrize(
    "failure",
    [
        "fenced",
        "inactive",
        "status_error",
        "revoked",
        "binding_revoked",
        "missing_binding",
        "workspace_root",
        "workspace_hash",
        "workspace_generation",
        "binding_root",
        "binding_hash",
        "binding_generation",
    ],
)
def test_attach_denies_unready_or_changed_authority_before_runtime_or_privilege_drop(
    attach_model, failure
):
    instance, replies, calls, _ = attach_model
    row = replies["workspace.status"]["result"]["seats"][1]
    binding = replies["workspace.bindings"]["result"][1]
    assert row["principal"] == "desk/trader"
    if failure == "fenced":
        replies["status"]["result"]["fenced"] = True
    elif failure == "inactive":
        replies["workspace.status"]["result"]["service"]["active"] = 0
    elif failure == "status_error":
        replies["status"] = {"ok": False}
    elif failure == "revoked":
        row["revoked"] = True
    elif failure == "binding_revoked":
        binding["status"] = "revoked"
    elif failure == "missing_binding":
        replies["workspace.bindings"]["result"].remove(binding)
    else:
        target = row if failure.startswith("workspace") else binding
        key = {
            "root": "root_id",
            "hash": "manifest_hash",
            "generation": "binding_generation",
        }[failure.split("_")[1]]
        target[key] = 2 if key == "binding_generation" else "changed"
    with pytest.raises(
        ValueError, match="managed_workspace_not_ready|seat_not_authorized"
    ):
        deployment.attach_seat(instance.path, "trader")
    assert calls == [("attest",)]


def test_attach_runtime_config_mismatch_closes_before_privilege_drop(attach_model):
    instance, _, calls, runtime_failure = attach_model
    runtime_failure["enabled"] = True
    with pytest.raises(ValueError, match="synthetic_runtime_mismatch"):
        deployment.attach_seat(instance.path, "trader")
    assert [call[0] for call in calls] == ["attest", "resume", "close"]


def test_attach_unknown_seat_never_spawns_a_new_root(attach_model):
    instance, _, calls, _ = attach_model
    with pytest.raises(ValueError, match="unknown_seat"):
        deployment.attach_seat(instance.path, "invented")
    assert calls == [("attest",)]


def test_attach_requires_independent_administrator_before_manifest_read(monkeypatch):
    monkeypatch.setattr(deployment.os, "geteuid", lambda: 12001)
    monkeypatch.setattr(
        deployment, "Deployment", lambda _: pytest.fail("must not read manifest")
    )
    with pytest.raises(ValueError, match="independent_administrator_required"):
        deployment.attach_seat(Path("/synthetic"), "trader")
