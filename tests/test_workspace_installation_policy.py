"""Rendered policy keeps executable extension surfaces explicit and closed."""

import tomllib

import pytest

from deskd.workspace.installation import Installation, RoleInstallation


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
    assert base["project_root_markers"] == [".codex"]
    for role in installation.roles:
        assert base["projects"][role.root]["trust_level"] == "trusted"
    assert features["hooks"] is False
    assert features["image_generation"] is False
    assert features["plugins"] is False and features["apps"] is False
    # Role layers cannot re-enable these surfaces. Image reading remains a
    # separate native capability whose filesystem path is tested by real CI.
    for role in installation.roles:
        assert "features" not in configs[role.root + "/.codex/config.toml"]
    assert features.get("view_image", True) is True
