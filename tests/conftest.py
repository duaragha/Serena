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
    import core.artifacts
    monkeypatch.setattr(core.artifacts, '_DEFAULT_REGISTRY', None)
