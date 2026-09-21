import json
import os
import sys
from pathlib import Path

import pytest

_LEGACY_DESKTOP = Path(__file__).resolve().parent.parent / "archive" / "desktop-gtk-legacy"
if _LEGACY_DESKTOP.is_dir() and str(_LEGACY_DESKTOP) not in sys.path:
    sys.path.insert(0, str(_LEGACY_DESKTOP))


# Fleet resolves its configuration from ~/.config/serena/fleet.json when nothing
# points it elsewhere, so without this the suite grades the machine it happens
# to run on. That is not hypothetical: on 2026-09-18 that file still pinned the
# research verify phase to a retired model, and forty-five tests failed on a
# policy mismatch in a file none of them had written. The policy in the code is
# the thing under test, so the suite reads that.
@pytest.fixture(scope="session", autouse=True)
def _fleet_config_is_the_builtin_one(tmp_path_factory):
    from fleet.policy import builtin_config

    path = tmp_path_factory.mktemp("fleet-config") / "fleet.json"
    path.write_text(json.dumps(builtin_config()), encoding="utf-8")
    previous = os.environ.get("SERENA_FLEET_CONFIG")
    os.environ["SERENA_FLEET_CONFIG"] = str(path)
    try:
        yield path
    finally:
        if previous is None:
            os.environ.pop("SERENA_FLEET_CONFIG", None)
        else:
            os.environ["SERENA_FLEET_CONFIG"] = previous


@pytest.fixture(autouse=True)
def _no_live_brain(tmp_path, monkeypatch):
    """No test reaches the brain daemon running on this machine.

    Task texts ask her brain for a short label; with the real brain.json in
    reach, a unit test would post to a live daemon and wait on a model.
    Tests that exercise the brain point SERENA_BRAIN_FILE somewhere themselves.
    """
    monkeypatch.setenv("SERENA_BRAIN_FILE", str(tmp_path / "no-brain.json"))


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
