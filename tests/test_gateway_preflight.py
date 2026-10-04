"""Metadata checks with synthetic ownership; not a two-uid installation test."""

import builtins
import json
import os
from pathlib import Path, PurePosixPath
import stat
from types import SimpleNamespace

import pytest

from deskd.gateway import preflight as module


@pytest.fixture
def manifest():
    source = (
        Path(__file__).resolve().parents[1]
        / "docs/fixtures/local-install-manifest.json"
    )
    data = json.loads(source.read_text())
    # Synthetic pin declarations only; no executable is opened or attested.
    data["runtime"].update(version="0.160.0", sha256="a" * 64)
    return data


def metadata(manifest):
    entries = {}

    def add(path, kind, uid, mode, gid=0):
        for ancestor in PurePosixPath(path).parents:
            entries.setdefault(
                str(ancestor),
                SimpleNamespace(
                    st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_gid=0, st_nlink=2
                ),
            )
        entries[path] = SimpleNamespace(
            st_mode=kind | mode,
            st_uid=uid,
            st_gid=gid,
            st_nlink=1 if kind == stat.S_IFREG else 2,
        )

    uid = {
        "admin": 0,
        "harness": manifest["harness_uid"],
        "gateway": manifest["gateway_uid"],
    }
    # Deliberately independent expected contract, not imported implementation rules.
    for key in ("installation", "policy", "runtime"):
        add(manifest["paths"][key], stat.S_IFDIR, 0, 0o755)
    for key in ("controller", "harness_binary", "bridge"):
        add(manifest["paths"][key], stat.S_IFREG, 0, 0o755)
    add(manifest["paths"]["runtime_config"], stat.S_IFREG, 0, 0o644)
    for key in ("daemon", "harness_state"):
        add(manifest["paths"][key], stat.S_IFDIR, uid["harness"], 0o700)
    for key in ("admin", "gateway_state", "secrets"):
        add(manifest["paths"][key], stat.S_IFDIR, uid["gateway"], 0o700)
    add(
        manifest["paths"]["gateway"],
        stat.S_IFDIR,
        uid["gateway"],
        0o750,
        manifest["business_gid"],
    )
    for role in manifest["roles"]:
        add(role["root"], stat.S_IFDIR, 0, 0o755)
        add(role["config"], stat.S_IFREG, 0, 0o644)
        add(role["data"], stat.S_IFDIR, uid["harness"], 0o700)
    return entries


def codes(report):
    return {fault.code for fault in report.faults}


def test_metadata_success_never_admits_credentials_or_discharges_gates(
    manifest, monkeypatch
):
    entries = metadata(manifest)
    seen = []

    def inspect(path):
        seen.append(path)
        return entries[path]

    monkeypatch.setattr(module, "lstat", inspect)

    # Configuration and binary content are never opened by this API.
    def no_open(*args, **kwargs):
        raise AssertionError("preflight attempted to read file content")

    monkeypatch.setattr(builtins, "open", no_open)
    report = module.preflight(manifest)
    assert report.metadata_ok
    assert not report.ready_for_credentials
    assert report.release_gates == module.RELEASE_GATES
    assert set(seen) == set(entries)
    assert report.as_dict()["ready_for_credentials"] is False


@pytest.mark.parametrize(
    "change, expected",
    [
        (lambda m: m.update(harness_uid=m["gateway_uid"]), "service_uids_must_differ"),
        (lambda m: m.update(gateway_uid=0), "invalid_service_id"),
        (lambda m: m.update(harness_uid=True), "invalid_service_id"),
        (lambda m: m.update(unverified=True), "invalid_fields"),
        (lambda m: m["roles"][0].update(network="on"), "network_must_be_off"),
        (
            lambda m: m["roles"][0].update(sandbox="danger-full-access"),
            "restricted_sandbox_required",
        ),
        (lambda m: m["roles"][0].update(deny_read=[]), "missing_required_deny"),
        (
            lambda m: m["roles"][0].update(root="/opt/../roles/one"),
            "invalid_absolute_path",
        ),
        (lambda m: m["runtime"].update(version="latest"), "invalid_runtime_pin"),
        (lambda m: m["paths"].pop("secrets"), "invalid_fields"),
    ],
)
def test_invalid_manifest_rejected_before_any_filesystem_access(
    manifest, monkeypatch, change, expected
):
    def no_stat(*args):
        raise AssertionError("invalid manifest reached filesystem")

    monkeypatch.setattr(module, "lstat", no_stat)
    change(manifest)
    report = module.preflight(manifest)
    assert expected in codes(report)
    assert not report.metadata_ok


@pytest.mark.parametrize(
    "target, changes, expected",
    [
        ("role_root", {"st_uid": 21001}, "wrong_owner"),
        ("role_ancestor", {"st_uid": 21001}, "untrusted_ancestor_owner"),
        ("role_ancestor", {"st_mode": stat.S_IFDIR | 0o777}, "replaceable_ancestor"),
        ("role_ancestor", {"st_mode": stat.S_IFLNK | 0o777}, "symlink_forbidden"),
        ("role_config", {"st_nlink": 2}, "hardlink_forbidden"),
        ("role_config", {"st_mode": stat.S_IFREG | 0o666}, "wrong_mode"),
        ("business", {"st_gid": 999}, "wrong_business_group"),
        ("admin", {"st_mode": stat.S_IFDIR | 0o750}, "wrong_mode"),
    ],
)
def test_unsafe_ownership_modes_symlinks_and_aliases(
    manifest, monkeypatch, target, changes, expected
):
    entries = metadata(manifest)
    paths = {
        "role_root": manifest["roles"][0]["root"],
        "role_ancestor": "/srv/deskd-local/roles",
        "role_config": manifest["roles"][0]["config"],
        "business": manifest["paths"]["gateway"],
        "admin": manifest["paths"]["admin"],
    }
    for key, value in changes.items():
        setattr(entries[paths[target]], key, value)
    monkeypatch.setattr(module, "lstat", entries.__getitem__)
    assert expected in codes(module.preflight(manifest))


def test_symlink_ancestor_stops_before_descending(manifest, monkeypatch):
    entries = metadata(manifest)
    prefix = "/srv/deskd-local/roles"
    entries[prefix].st_mode = stat.S_IFLNK | 0o777
    seen = []

    def inspect(path):
        seen.append(path)
        return entries[path]

    monkeypatch.setattr(module, "lstat", inspect)
    assert "symlink_forbidden" in codes(module.preflight(manifest))
    assert not any(path.startswith(prefix + "/") for path in seen)


def test_configuration_below_writable_data_rejected_without_stat(manifest, monkeypatch):
    manifest["roles"][0]["config"] = manifest["roles"][0]["data"] + "/config.toml"
    monkeypatch.setattr(
        module, "lstat", lambda path: pytest.fail("must reject schema first")
    )
    assert "configuration_in_writable_data" in codes(module.preflight(manifest))


def test_real_private_fixture_cannot_pass_as_admin_install(manifest, tmp_path):
    # Only create our own empty fixtures. No chown, setuid, service or model use.
    work = tmp_path / "installation"
    work.mkdir(mode=0o700)
    manifest["paths"]["installation"] = str(work)
    for name in ("controller", "harness_binary", "bridge"):
        manifest["paths"][name] = str(work / name)
    report = module.preflight(manifest)
    assert not report.metadata_ok
    assert codes(report) & {
        "untrusted_ancestor_owner",
        "replaceable_ancestor",
        "wrong_owner",
        "metadata_unavailable",
    }
    assert not report.ready_for_credentials


def test_real_hardlink_is_rejected_with_only_ancestor_owner_metadata_synthetic(
    manifest, tmp_path, monkeypatch
):
    fixture = tmp_path / "synthetic-config"
    fixture.write_text("fixture = true\n")
    os.link(fixture, tmp_path / "alias")
    info = fixture.lstat()
    assert info.st_nlink == 2
    entries = metadata(manifest)
    entries[manifest["roles"][0]["config"]].st_nlink = info.st_nlink
    monkeypatch.setattr(module, "lstat", entries.__getitem__)
    assert "hardlink_forbidden" in codes(module.preflight(manifest))


def test_example_pin_is_intentionally_not_installable(monkeypatch):
    source = (
        Path(__file__).resolve().parents[1]
        / "docs/fixtures/local-install-manifest.json"
    )
    example = json.loads(source.read_text())
    monkeypatch.setattr(
        module, "lstat", lambda path: pytest.fail("placeholder reached filesystem")
    )
    assert "placeholder_runtime_pin" in codes(module.preflight(example))


def test_missing_metadata_is_structured_and_contains_no_os_error_text(
    manifest, monkeypatch
):
    def inaccessible(path):
        raise PermissionError("sensitive path must not appear in result")

    monkeypatch.setattr(module, "lstat", inaccessible)
    report = module.preflight(manifest)
    assert codes(report) == {"metadata_unavailable"}
    assert "sensitive path" not in json.dumps(report.as_dict())


def test_root_owned_binary_with_untraversable_ancestor_is_not_usable(
    manifest, monkeypatch
):
    entries = metadata(manifest)
    entries["/opt"].st_mode = stat.S_IFDIR | 0o700
    monkeypatch.setattr(module, "lstat", entries.__getitem__)
    report = module.preflight(manifest)
    assert "inaccessible_ancestor" in codes(report)
    assert not report.metadata_ok
