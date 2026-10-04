"""Read-only installation metadata checks, never an isolation attestation.

The caller supplies a non-secret manifest. Only explicit paths and their ancestors
are lstat'ed; no file content, process state, home discovery or network is read.
A successful metadata check does not certify isolation or admit credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from os import lstat
from pathlib import PurePosixPath
import re
import stat
from typing import Mapping


RELEASE_GATES = (
    "supported_linux_kernel_and_sandbox_prerequisites",
    "official_runtime_artifact_and_effective_configuration",
    "two_service_uids_and_protected_administration",
    "root_binding_and_authenticated_transport",
    "sandbox_shell_edit_children_and_code_mode",
    "network_off_and_socket_alias_denial",
    "credential_environment_proc_and_hardlink_isolation",
    "complete_trusted_executable_and_dependency_inventory",
    "acl_mount_alias_and_directory_replacement_probes",
    "restart_fence_and_same_root_manifest_revalidation",
)

# Kind, owner selector and exact permissions. Group membership is a release gate.
_PATH_RULES = {
    "installation": ("directory", "admin", 0o755),
    "policy": ("directory", "admin", 0o755),
    "runtime": ("directory", "admin", 0o755),
    "daemon": ("directory", "harness", 0o700),
    "gateway": ("directory", "gateway", 0o750),
    "admin": ("directory", "gateway", 0o700),
    "harness_state": ("directory", "harness", 0o700),
    "gateway_state": ("directory", "gateway", 0o700),
    "secrets": ("directory", "gateway", 0o700),
    "controller": ("file", "admin", 0o755),
    "harness_binary": ("file", "admin", 0o755),
    "bridge": ("file", "admin", 0o755),
    "runtime_config": ("file", "admin", 0o644),
}
_PRIVATE = {"daemon", "gateway", "admin", "harness_state", "gateway_state", "secrets"}
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class PreflightFault:
    code: str
    field: str


@dataclass(frozen=True)
class PreflightReport:
    faults: tuple[PreflightFault, ...]
    release_gates: tuple[str, ...] = RELEASE_GATES

    @property
    def metadata_ok(self) -> bool:
        return not self.faults

    @property
    def ready_for_credentials(self) -> bool:
        # No manifest flag or successful stat can discharge a release gate.
        return False

    def as_dict(self) -> dict[str, object]:
        return {
            "metadata_ok": self.metadata_ok,
            "ready_for_credentials": False,
            "faults": [{"code": f.code, "field": f.field} for f in self.faults],
            "release_gates": list(self.release_gates),
        }


def _canonical_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 < len(value) <= 4096
        and "\x00" not in value
        and value.startswith("/")
        and not value.startswith("//")
        and str(PurePosixPath(value)) == value
        and ".." not in PurePosixPath(value).parts
        and len(PurePosixPath(value).parts) <= 64
    )


def _inside(child: str, parent: str) -> bool:
    return PurePosixPath(parent) in PurePosixPath(child).parents


def preflight(manifest: Mapping[str, object]) -> PreflightReport:
    """Reject malformed declarations and unsafe metadata; never change the host.

    File contents and effective runtime configuration are deliberately unverified.
    Results are a point-in-time observation with TOCTOU limitations, not a lease.
    """
    faults: list[PreflightFault] = []

    def fault(code: str, field: str) -> None:
        faults.append(PreflightFault(code, field))

    def keys(value: object, expected: set[str], field: str) -> bool:
        if not isinstance(value, dict) or set(value) != expected:
            fault("invalid_fields", field)
            return False
        return True

    if not keys(
        manifest,
        {
            "schema_version",
            "harness_uid",
            "gateway_uid",
            "business_gid",
            "paths",
            "roles",
            "runtime",
        },
        "manifest",
    ):
        return PreflightReport(tuple(faults))
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        fault("unsupported_schema", "schema_version")
    for field in ("harness_uid", "gateway_uid", "business_gid"):
        value = manifest[field]
        if type(value) is not int or not 0 < value < 2**32 - 1:
            fault("invalid_service_id", field)
    if manifest["harness_uid"] == manifest["gateway_uid"]:
        fault("service_uids_must_differ", "gateway_uid")
    runtime = manifest["runtime"]
    if keys(runtime, {"kind", "version", "sha256"}, "runtime"):
        if runtime["kind"] != "official-codex":
            fault("unsupported_runtime", "runtime.kind")
        for key, pattern in (("version", _VERSION), ("sha256", _DIGEST)):
            if not isinstance(runtime[key], str) or not pattern.fullmatch(runtime[key]):
                fault("invalid_runtime_pin", f"runtime.{key}")
        if runtime["version"] == "0.0.0" or runtime["sha256"] == "0" * 64:
            fault("placeholder_runtime_pin", "runtime")
    paths = manifest["paths"]
    if not keys(paths, set(_PATH_RULES), "paths"):
        return PreflightReport(tuple(faults))
    for name, path in paths.items():
        if not _canonical_path(path):
            fault("invalid_absolute_path", f"paths.{name}")
    roles = manifest["roles"]
    if not isinstance(roles, list) or not 2 <= len(roles) <= 8:
        fault("invalid_roles", "roles")
        return PreflightReport(tuple(faults))
    role_fields = {"seat", "root", "config", "data", "network", "sandbox", "deny_read"}
    for i, role in enumerate(roles):
        prefix = f"roles.{i}"
        if not keys(role, role_fields, prefix):
            continue
        if not isinstance(role["seat"], str) or not _IDENTIFIER.fullmatch(role["seat"]):
            fault("invalid_seat", f"{prefix}.seat")
        for key in ("root", "config", "data"):
            if not _canonical_path(role[key]):
                fault("invalid_absolute_path", f"{prefix}.{key}")
        if role["network"] != "off":
            fault("network_must_be_off", f"{prefix}.network")
        if role["sandbox"] != "local-restricted":
            fault("restricted_sandbox_required", f"{prefix}.sandbox")
        deny = role["deny_read"]
        if (
            not isinstance(deny, list)
            or len(deny) > 32
            or not all(_canonical_path(p) for p in deny)
        ):
            fault("invalid_deny_paths", f"{prefix}.deny_read")
    if faults:
        return PreflightReport(tuple(faults))

    if len({role["seat"] for role in roles}) != len(roles):
        fault("duplicate_seat", "roles")
    all_paths = list(paths.values()) + [
        r[k] for r in roles for k in ("root", "config", "data")
    ]
    if len(set(all_paths)) != len(all_paths):
        fault("duplicate_path", "paths")
    for name in ("controller", "harness_binary", "bridge"):
        if not _inside(paths[name], paths["installation"]):
            fault("outside_installation", f"paths.{name}")
    if not _inside(paths["runtime_config"], paths["policy"]):
        fault("outside_policy", "paths.runtime_config")
    for name in ("daemon", "gateway", "admin"):
        if not _inside(paths[name], paths["runtime"]):
            fault("outside_runtime", f"paths.{name}")
    protected = [paths[n] for n in _PRIVATE] + [paths["installation"], paths["policy"]]
    for i, role in enumerate(roles):
        prefix = f"roles.{i}"
        required_deny = {paths[name] for name in _PRIVATE}
        required_deny.update(r["root"] for r in roles if r is not role)
        if not required_deny.issubset(set(role["deny_read"])):
            fault("missing_required_deny", f"{prefix}.deny_read")
        if not _inside(role["config"], role["root"]) or not _inside(
            role["data"], role["root"]
        ):
            fault("outside_role_root", prefix)
        # Writable data may never be an ancestor of policy/configuration.
        if _inside(role["config"], role["data"]):
            fault("configuration_in_writable_data", f"{prefix}.config")
        others = protected + [r["root"] for r in roles if r is not role]
        if any(_inside(role["root"], p) or _inside(p, role["root"]) for p in others):
            fault("overlapping_role_boundary", f"{prefix}.root")
    if faults:
        return PreflightReport(tuple(faults))

    def inspect(path: str, field: str, kind: str, uid: int, mode: int) -> None:
        target = PurePosixPath(path)
        chain = [*reversed(target.parents), target]
        for part in chain:
            leaf = part == target
            try:
                info = lstat(str(part))
            except OSError:
                fault("metadata_unavailable", field)
                return
            if stat.S_ISLNK(info.st_mode):
                fault("symlink_forbidden", field)
                return
            expected_kind = kind if leaf else "directory"
            if not (
                stat.S_ISDIR(info.st_mode)
                if expected_kind == "directory"
                else stat.S_ISREG(info.st_mode)
            ):
                fault("wrong_path_type", field)
                return
            if info.st_uid != (uid if leaf else 0):
                fault("wrong_owner" if leaf else "untrusted_ancestor_owner", field)
                return
            actual = stat.S_IMODE(info.st_mode)
            if leaf:
                # Exact modes make executable/readability requirements reviewable.
                if actual != mode:
                    fault("wrong_mode", field)
                if field == "paths.gateway" and info.st_gid != manifest["business_gid"]:
                    fault("wrong_business_group", field)
                if kind == "file" and info.st_nlink != 1:
                    fault("hardlink_forbidden", field)
            elif actual & 0o7022:
                # Reject writable/sticky/setid ancestors, including shared /tmp.
                fault("replaceable_ancestor", field)
                return
            elif not actual & 0o001:
                fault("inaccessible_ancestor", field)
                return

    owners = {
        "admin": 0,
        "harness": manifest["harness_uid"],
        "gateway": manifest["gateway_uid"],
    }
    for name, (kind, owner, mode) in _PATH_RULES.items():
        inspect(paths[name], f"paths.{name}", kind, owners[owner], mode)
    for i, role in enumerate(roles):
        for key, kind, uid, mode in (
            ("root", "directory", 0, 0o755),
            ("config", "file", 0, 0o644),
            ("data", "directory", owners["harness"], 0o700),
        ):
            inspect(role[key], f"roles.{i}.{key}", kind, uid, mode)
    return PreflightReport(tuple(faults))
