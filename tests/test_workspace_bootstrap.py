"""Interrupted explicit registration repairs fixed roots without reviving roles."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from deskd.gateway.identity import PrincipalId
from deskd.workspace import deployment as module
from deskd.workspace.exchange import ACTIONS
from deskd.workspace.service import WorkspaceGateway


@pytest.fixture
def environment(tmp_path, monkeypatch):
    (tmp_path / "policy").mkdir()
    gateway = WorkspaceGateway(
        gateway_db=tmp_path / "gateway.db",
        coordination_db=tmp_path / "workspace.db",
        harness_uid=27002,
        business_gid=27003,
        business_path=tmp_path / "b",
        admin_path=tmp_path / "a",
        principals=["desk/analyst", "desk/trader"],
        activation_check=lambda: None,
    )
    generation = gateway.registry.start_service()
    handlers = gateway._handlers()
    calls = []
    faults = {"fail_trader_bind": False}

    class Runtime:
        def start_root(self, config):
            calls.append(("start", config))
            return SimpleNamespace(thread_id="root-" + config)

        def resume_root(self, root_id, config):
            calls.append(("resume", root_id))

        def close(self):
            pass

    class Deployment:
        def __init__(self):
            self.path = tmp_path / "policy/deployment.json"
            self.roots_path = tmp_path / "policy/roots.json"
            self.coordination_db = tmp_path / "workspace.db"
            self.value = {"desk_id": "desk", "fixture": "credential-free"}
            self.installation = SimpleNamespace(
                roles=[SimpleNamespace(seat="analyst"), SimpleNamespace(seat="trader")]
            )

        def attest(self):
            pass

        def runtime(self):
            return Runtime()

        def root_config(self, role):
            return role.seat

        def seats(self):
            roots = json.loads(self.roots_path.read_text())
            return [
                SimpleNamespace(
                    principal=principal, config=principal.split("/")[1], **row
                )
                for principal, row in roots.items()
            ]

        def admin(self, method, params=None):
            params = params or {}
            if method == "fence":
                gateway.registry.fence(expected_service_generation=generation)
                result = {"fenced": True}
            elif method == "bind":
                calls.append(("bind", params["seat_id"]))
                if faults["fail_trader_bind"] and params["seat_id"] == "trader":
                    return {"ok": False, "error": {"code": "synthetic_bind_failure"}}
                gateway.registry.trusted_bind(
                    PrincipalId(params["desk_id"], params["seat_id"]),
                    root_session_id=params["root_session_id"],
                    manifest_hash=params["manifest_hash"],
                    capabilities=frozenset(params["capabilities"]),
                    expected_binding_generation=params["expected_binding_generation"],
                )
                result = {}
            else:
                result = handlers[method](params)
            return {"ok": True, "result": result}

    value = Deployment()
    monkeypatch.setattr(module, "Deployment", lambda _: value)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    return value, gateway, calls, faults


def test_explicit_retry_finishes_partial_registration_using_same_root_record(
    environment,
):
    value, gateway, calls, faults = environment
    faults["fail_trader_bind"] = True
    with pytest.raises(ValueError, match="partial_bootstrap_retry_same_record"):
        module.run_installed("bootstrap", value.path)
    record = value.roots_path.read_bytes()
    assert len(gateway.store.snapshot()["seats"]) == 1
    faults["fail_trader_bind"] = False
    module.run_installed("bootstrap", value.path)
    assert value.roots_path.read_bytes() == record
    assert sum(kind == "start" for kind, _ in calls) == 2
    assert len(gateway.store.snapshot()["seats"]) == 2
    assert gateway.store.snapshot()["service"]["active"] == 0


def test_partial_repair_never_resumes_or_rebinds_already_revoked_root(environment):
    value, gateway, calls, _ = environment
    digest = hashlib.sha256(
        json.dumps(value.value, sort_keys=True).encode()
    ).hexdigest()
    value.roots_path.write_text(
        json.dumps(
            {
                "desk/" + name: {
                    "root_id": "root-" + name,
                    "manifest_hash": digest,
                    "binding_generation": 1,
                }
                for name in ("analyst", "trader")
            }
        )
    )
    principal = PrincipalId("desk", "analyst")
    gateway.registry.trusted_bind(
        principal,
        root_session_id="root-analyst",
        manifest_hash=digest,
        capabilities=frozenset(
            [
                *ACTIONS,
                "state.read",
                "proposal.create",
                "approval.issue",
                "approval.revoke",
            ]
        ),
        expected_binding_generation=0,
    )
    gateway.registry.trusted_revoke(principal, expected_binding_generation=1)
    # Coordination registration was interrupted before this role appeared there.
    module.run_installed("bootstrap", value.path)
    assert ("resume", "root-analyst") not in calls
    assert ("bind", "analyst") not in calls
    assert ("resume", "root-trader") in calls
    analyst = next(
        row
        for row in gateway.store.snapshot()["seats"]
        if row["principal"] == "desk/analyst"
    )
    assert analyst["revoked"] == 1
    binding = next(
        row
        for row in gateway._handlers()["workspace.bindings"]({})
        if row["principal"] == "desk/analyst"
    )
    assert binding["status"] == "revoked"
    assert binding["binding_generation"] == 2


def test_mismatched_existing_authority_is_rejected_before_any_resume(environment):
    value, gateway, calls, faults = environment
    faults["fail_trader_bind"] = True
    with pytest.raises(ValueError):
        module.run_installed("bootstrap", value.path)
    gateway.registry.trusted_bind(
        PrincipalId("desk", "analyst"),
        root_session_id="replacement-root",
        manifest_hash="b" * 64,
        capabilities=frozenset({"state.read"}),
        expected_binding_generation=1,
    )
    calls.clear()
    with pytest.raises(ValueError, match="bootstrap_existing_authority_mismatch"):
        module.run_installed("bootstrap", value.path)
    assert calls == []


def test_missing_root_record_with_existing_state_never_creates_replacement(environment):
    value, gateway, calls, _ = environment
    gateway.store.register_seat("desk/analyst", "root-existing", "a" * 64)
    with pytest.raises(
        ValueError, match="bootstrap_existing_state_without_root_record"
    ):
        module.run_installed("bootstrap", value.path)
    assert calls == []
    assert not value.roots_path.exists()
