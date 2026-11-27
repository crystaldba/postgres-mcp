import json
from pathlib import Path

import tomllib

REPOSITORY_ROOT = Path(__file__).parents[2]


def test_claude_plugin_version_matches_project_version() -> None:
    """Keep the Claude plugin version synchronized with the package version."""
    pyproject = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text())
    plugin = json.loads((REPOSITORY_ROOT / ".claude-plugin" / "plugin.json").read_text())

    project_version = pyproject["project"]["version"]
    plugin_version = plugin["version"]

    assert plugin_version == project_version, (
        f".claude-plugin/plugin.json version {plugin_version!r} must match "
        f"pyproject.toml version {project_version!r}"
    )
