from pathlib import Path


def test_release_asset_commands_name_the_repository_without_a_checkout() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    block = workflow.split("  attach-checksums:", 1)[1].split(
        "  publish-container:", 1
    )[0]

    assert block.count('--repo "${GITHUB_REPOSITORY}"') == 2
    assert "actions/checkout" not in block
