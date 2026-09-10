"""Build-time proof of the actual frozen replay executable, not its source shim."""

import os
from pathlib import Path

import pytest

from fleet import integration_recovery as recovery, supervisor
from test_fleet_integration_recovery import _failed


def test_frozen_executable_replays_with_real_completion_and_git_gates(tmp_path, monkeypatch):
    configured = os.environ.get("SERENA_FLEET_TEST_REPLAY_BINARY")
    if not configured:
        pytest.skip("requires the build's actual frozen helper executable")
    binary = Path(configured)
    assert binary.is_file(), "configured frozen helper is missing"
    store, run_id, *_ = _failed(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "helper_command", lambda: [str(binary), "--fleet-integration-replay"])
    monkeypatch.setattr(supervisor, "run_worker", lambda *a, **kw: pytest.fail("native model turn dispatched"))
    assert recovery.resume_saved_integrations(store) == [run_id]
    result = supervisor._execute_leg(store, run_id, store.get_run(run_id)["phases"][3]["legs"][0])
    assert result.ok, result.error
    final = store.get_run(run_id)["phases"][3]["legs"][0]
    assert final["state"] == "completed"
    assert final["current_attempt"]["actual_model"] is None
    assert (Path(store.get_run(run_id)["cwd"]) / "core/alpha.py").read_bytes() == b"alpha = 2\n"
