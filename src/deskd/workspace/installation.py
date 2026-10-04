"""Deterministic, inert installation plans for a pinned official runtime.

Rendering never installs software, creates accounts, starts services, reads an
existing config, or admits credentials. Apply and attest the resulting plan in
an administrator-controlled deployment; role data never contains policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re

OFFICIAL_VERSION = "0.160.0"
OFFICIAL_LINUX_X64_SHA256 = (
    "12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad"
)
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_PATH = re.compile(r"/[A-Za-z0-9_./-]+\Z")


def _path(value: str) -> str:
    # Also excludes systemd specifiers, shell metacharacters and TOML controls.
    if (
        not isinstance(value, str)
        or not _PATH.fullmatch(value)
        or str(PurePosixPath(value)) != value
        or ".." in PurePosixPath(value).parts
        or len(value) > 180
        or value == "/"
    ):
        raise ValueError("canonical restricted absolute path required")
    return value


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


@dataclass(frozen=True)
class RoleInstallation:
    seat: str
    root: str
    data: str

    def __post_init__(self):
        if not isinstance(self.seat, str) or not _IDENTIFIER.fullmatch(self.seat):
            raise ValueError("invalid seat")
        _path(self.root)
        _path(self.data)
        if PurePosixPath(self.root) not in PurePosixPath(self.data).parents:
            raise ValueError("role data must be below its protected root")
        if PurePosixPath(self.data).name == ".codex":
            raise ValueError("role policy cannot be writable data")


@dataclass(frozen=True)
class Installation:
    prefix: str
    harness_uid: int
    gateway_uid: int
    business_gid: int
    roles: tuple[RoleInstallation, ...]

    def __post_init__(self):
        _path(self.prefix)
        for ident in (self.harness_uid, self.gateway_uid, self.business_gid):
            if type(ident) is not int or not 0 < ident < 2**32 - 1:
                raise ValueError("non-root service IDs required")
        if self.harness_uid == self.gateway_uid:
            raise ValueError("service UIDs must differ")
        if not isinstance(self.roles, tuple) or not 2 <= len(self.roles) <= 8:
            raise ValueError("two to eight fixed roles required")
        if len({r.seat for r in self.roles}) != len(self.roles):
            raise ValueError("duplicate role seat")
        roots = [PurePosixPath(r.root) for r in self.roles]
        reserved = [
            PurePosixPath(self.path(name))
            for name in ("bin", "policy", "harness", "gateway", "home", "tmp", "base")
        ]
        for i, root in enumerate(roots):
            if PurePosixPath(self.prefix) not in root.parents:
                raise ValueError("role root outside installation")
            for other in roots[:i] + reserved:
                if root == other or root in other.parents or other in root.parents:
                    raise ValueError("overlapping role or management boundary")

    def path(self, suffix: str) -> str:
        return f"{self.prefix}/{suffix}"

    @property
    def binary(self) -> str:
        return self.path("bin/codex")

    def configuration(self, *, mock_port: int | None = None) -> str:
        """Render fixed profiles; an optional loopback mock never needs a key.

        Production provider credentials are deliberately absent. The privileged
        manager must select an authorized provider through its own installation
        mechanism; no role can change this profile catalogue.
        """
        if mock_port is not None and (
            type(mock_port) is not int or not 0 < mock_port < 65536
        ):
            raise ValueError("invalid mock port")
        lines = [
            'approval_policy = "never"',
            'default_permissions = "locked"',
            'web_search = "disabled"',
            'model = "gpt-5.5"',
        ]
        if mock_port is not None:
            lines.append('model_provider = "deskd_mock"')
        lines += [
            "[analytics]",
            "enabled = false",
            "[features]",
            "plugins = false",
            "apps = false",
            "browser_use = false",
            "computer_use = false",
            "remote_models = false",
            "api_key_model_discovery = false",
            "multi_agent = false",
            "shell_snapshot = false",
            "js_repl = false",
            "code_mode = false",
            "[mcp_servers.codex_tui]",
            'command = "/bin/false"',
            "enabled = false",
        ]
        if mock_port is not None:
            lines += [
                "[model_providers.deskd_mock]",
                'name = "deskd local mock"',
                f'base_url = "http://127.0.0.1:{mock_port}/v1"',
                'wire_api = "responses"',
                "request_max_retries = 0",
                "stream_max_retries = 0",
            ]
        private = [self.path(x) for x in ("harness", "gateway", "home", "tmp")]
        private += [f"/tmp/codex-daemon-{self.harness_uid}"]
        for seat, role in [("locked", None)] + [(r.seat, r) for r in self.roles]:
            lines += [
                f"[permissions.{seat}.filesystem]",
                '":minimal" = "read"',
                f'{_quote(self.binary)} = "read"',
            ]
            # Runtime-internal shell launch artifacts live in the shared temp
            # tree; the role only needs its own data and protected cwd.
            if role is not None:
                lines += [
                    f'{_quote(role.root)} = "read"',
                    f'{_quote(role.data)} = "write"',
                ]
            for denied in private + [r.root for r in self.roles if r != role]:
                lines.append(f'{_quote(denied)} = "deny"')
            lines += [f"[permissions.{seat}.network]", "enabled = false"]
        for root in [self.path("base")] + [r.root for r in self.roles]:
            lines += [f"[projects.{_quote(root)}]", 'trust_level = "trusted"']
        return "\n".join(lines) + "\n"

    def role_configuration(self, role: RoleInstallation) -> str:
        if role not in self.roles:
            raise ValueError("unknown role")
        return (
            f"default_permissions = {_quote(role.seat)}\n"
            f"developer_instructions = {_quote('You are the ' + role.seat + ' role. Work only in your designated data directory.')}\n"
        )

    def plan(self, *, mock_port: int | None = None) -> dict:
        """Return reviewable metadata/content. Every emitted path is explicit."""
        config = self.configuration(mock_port=mock_port)
        files = [
            {
                "path": self.path("harness/config.toml"),
                "uid": 0,
                "gid": 0,
                "mode": "0644",
                "content": config,
            }
        ]
        for role in self.roles:
            files.append(
                {
                    "path": f"{role.root}/.codex/config.toml",
                    "uid": 0,
                    "gid": 0,
                    "mode": "0644",
                    "content": self.role_configuration(role),
                }
            )
        directories = [{"path": self.prefix, "uid": 0, "gid": 0, "mode": "0755"}]
        for name in ("bin", "policy", "base", "roles"):
            directories.append(
                {"path": self.path(name), "uid": 0, "gid": 0, "mode": "0755"}
            )
        for name, uid in (
            ("harness", self.harness_uid),
            ("home", self.harness_uid),
            ("tmp", self.harness_uid),
            ("gateway", self.gateway_uid),
        ):
            directories.append(
                {"path": self.path(name), "uid": uid, "gid": uid, "mode": "0700"}
            )
        for role in self.roles:
            directories += [
                {"path": role.root, "uid": 0, "gid": 0, "mode": "0755"},
                {"path": f"{role.root}/.codex", "uid": 0, "gid": 0, "mode": "0755"},
                {
                    "path": role.data,
                    "uid": self.harness_uid,
                    "gid": self.harness_uid,
                    "mode": "0700",
                },
            ]
        return {
            "schema_version": 1,
            "runtime_version": OFFICIAL_VERSION,
            "runtime_sha256": OFFICIAL_LINUX_X64_SHA256,
            "binary": self.binary,
            "directories": directories,
            "files": files,
            "config_sha256": hashlib.sha256(config.encode()).hexdigest(),
            "ready_for_credentials": False,
            "launch": {
                "uid": self.harness_uid,
                "gid": self.harness_uid,
                "supplementary_groups": [self.business_gid],
                "cwd": self.path("base"),
                "argv": [
                    self.binary,
                    "app-server",
                    "--listen",
                    "unix://",
                    "--managed-daemon",
                    "--strict-config",
                ],
                "environment": {
                    "PATH": "/usr/bin:/bin",
                    "HOME": self.path("home"),
                    "CODEX_HOME": self.path("harness"),
                    "TMPDIR": self.path("tmp"),
                    "LANG": "C.UTF-8",
                },
            },
        }
