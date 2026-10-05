"""Rendered policy keeps executable extension surfaces explicit and closed."""

import tomllib

import pytest

from deskd.workspace.installation import Installation, RoleInstallation
from deskd.workspace.exchange import tool_catalog


@pytest.mark.parametrize("provider", ["api", "mock"])
def test_fixed_profiles_disable_hook_discovery_and_hosted_image_generation(provider):
    installation = Installation(
        "/synthetic/deskd",
        12001,
        12002,
        12003,
        tuple(
            RoleInstallation(
                seat,
                "/synthetic/deskd/roles/" + seat,
                "/synthetic/deskd/roles/" + seat + "/data",
            )
            for seat in ("analyst", "trader", "engineer")
        ),
    )
    plan = installation.plan(
        provider=provider,
        mock_port=12345 if provider == "mock" else None,
        with_gateway_bridge=True,
    )
    configs = {
        entry["path"]: tomllib.loads(entry["content"]) for entry in plan["files"]
    }
    features = configs["/synthetic/deskd/harness/config.toml"]["features"]
    base = configs["/synthetic/deskd/harness/config.toml"]
    assert base["project_root_markers"] == [".codex/config.toml"]
    assert base["memories"] == {"generate_memories": False, "use_memories": False}
    for role in installation.roles:
        assert base["projects"][role.root]["trust_level"] == "trusted"
        assert base["projects"][role.data]["trust_level"] == "trusted"
    assert features["hooks"] is False
    assert features["image_generation"] is False
    assert features["goals"] is False
    assert features["memories"] is False
    assert features["external_agent_memory_import"] is False
    assert features["plugins"] is False and features["apps"] is False
    # Role layers cannot re-enable these surfaces. Image reading remains a
    # separate native capability whose filesystem path is tested by real CI.
    for role in installation.roles:
        assert "features" not in configs[role.root + "/.codex/config.toml"]
    assert features.get("view_image", True) is True
    expected_tools = {tool["name"] for tool in tool_catalog()}
    expected_reads = {"inbox.read", "tasks.read", "workspace.receipt"}
    assert base["approval_policy"] == "never"
    assert base["model"] == "gpt-5.5"
    assert base["notice"]["model_migrations"] == {
        "gpt-5.6-sol": "gpt-6-sol",
        "gpt-5.6-terra": "gpt-6-sol",
        "gpt-5.6-luna": "gpt-6-luna",
        "gpt-5.5": "gpt-6-sol",
    }
    for config in configs.values():
        bridge = config["mcp_servers"]["deskd"]
        assert set(bridge["enabled_tools"]) == expected_tools
        assert set(bridge["tools"]) == expected_tools - expected_reads
        assert all(
            value == {"approval_mode": "approve"} for value in bridge["tools"].values()
        )
