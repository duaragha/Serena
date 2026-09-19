import sys
from pathlib import Path

_LEGACY_DESKTOP = Path(__file__).resolve().parent.parent / "archive" / "desktop-gtk-legacy"
if _LEGACY_DESKTOP.is_dir() and str(_LEGACY_DESKTOP) not in sys.path:
    sys.path.insert(0, str(_LEGACY_DESKTOP))


import pytest


@pytest.fixture(autouse=True)
def fleet_private_proof_state(request, tmp_path, monkeypatch):
    """Fleet fixtures never acquire operator locks or write operator artifacts."""
    if not request.node.path.name.startswith('test_fleet'):
        return
    monkeypatch.delenv('SERENA_FLEET_WORKER', raising=False)
    monkeypatch.setenv('SERENA_FLEET_INTEGRATION_LOCK_ROOT', str(tmp_path / 'integration-locks'))
    monkeypatch.setenv('SERENA_FLEET_WORKSPACE_ROOT', str(tmp_path / 'worktrees'))
    monkeypatch.setenv('SERENA_ARTIFACT_ROOT', str(tmp_path / 'artifacts'))
    monkeypatch.setenv('SERENA_ARTIFACT_DB', str(tmp_path / 'artifacts.db'))
    monkeypatch.setenv('SERENA_ARTIFACT_KEY', str(tmp_path / 'artifact.key'))
    monkeypatch.setenv('SERENA_COMMITMENTS_DB_PATH', str(tmp_path / 'commitments.sqlite3'))
    import core.artifacts
    monkeypatch.setattr(core.artifacts, '_DEFAULT_REGISTRY', None)

@pytest.fixture(autouse=True)
def _brain_surface_is_not_machine_state(monkeypatch):
    """Keep the resident brain's mounted surface out of his real MCP config.

    The daemon now mounts every server from ~/.config/serena/mcp.json plus the
    macOS VM tools. Both are properties of the machine, not of the code under
    test, so they are sealed off unless a test asks for them explicitly.
    """

    monkeypatch.setenv("SERENA_BRAIN_MCP", "0")
    monkeypatch.setenv("SERENA_BRAIN_VM", "0")
