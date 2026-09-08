import shlex
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from test_fleet_supervisor import fleet_env  # noqa: F401

from fleet import supervisor
from fleet.isolation import run_test_gate, run_test_gates
from fleet.policy import (
    build_policy,
    builtin_config,
    policy_from_snapshot,
    policy_models_match_contract,
    validate_policy_snapshot,
)
from fleet.retry_policy import difficult_retry_reason
from fleet.store import FleetStore
from fleet.workers import WorkerResult


def _failed_run(tmp_path, *, task="", activity="coding"):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = store.create_run(
        task=task,
        activity=activity,
        cwd=str(tmp_path),
        origin_session_id=None,
        origin_agent=None,
        dry_run=False,
        policy=build_policy(activity, config=builtin_config(), task=task, worker_count=2).to_dict(),
    )
    leg = run["phases"][1]["legs"][0]
    attempt = store.begin_attempt(leg["leg_id"])
    store.finish_attempt(
        attempt["attempt_id"],
        state="failed",
        output_text="previous implementation",
        error="test gate failed after integration",
        exit_code=0,
        actual_model=leg["model"],
        actual_effort=leg["effort"],
    )
    return store, store.get_run(run["run_id"]), leg, attempt


def _gate(output="AssertionError: expected 4, got 3", **kwargs):
    return {"ran": True, "ok": False, "exit_code": 1, "output_tail": output, **kwargs}


def test_aggregate_gate_preserves_actual_failure_for_classifier(tmp_path):
    _store, run, leg, _attempt = _failed_run(tmp_path)
    gate = run_test_gates(
        tmp_path,
        [
            [sys.executable, "-c", "assert True"],
            [sys.executable, "-c", "assert False"],
        ],
    )
    assert [result["exit_code"] for result in gate["results"]] == [0, 1]
    assert difficult_retry_reason(run, leg, gate)


def test_real_assertion_escalates_only_failed_leg_and_preserves_attempt_identity(tmp_path):
    store, run, leg, attempt = _failed_run(tmp_path)
    gate = run_test_gate(tmp_path, [sys.executable, "-c", "assert 2 + 2 == 3"])
    assert gate["exit_code"] == 1
    assert difficult_retry_reason(run, leg, gate)
    changed = store.escalate_difficult_leg(
        run["run_id"], leg["leg_id"], attempt_id=attempt["attempt_id"], gate=gate
    )
    target = changed["phases"][1]["legs"][0]
    assert (target["runtime"], target["model"], target["effort"], target["state"]) == (
        "codex",
        "gpt-6-astra",
        "xhigh",
        "queued",
    )
    assert target["worker_key"] == leg["worker_key"]
    assert target["current_attempt"]["requested_model"] == leg["model"]
    assert target["current_attempt"]["state"] == "failed"
    for phase_index in range(4):
        for ordinal in range(2):
            if (phase_index, ordinal) != (1, 0):
                before = run["phases"][phase_index]["legs"][ordinal]
                after = changed["phases"][phase_index]["legs"][ordinal]
                for key in ("model", "effort", "runtime", "worker_key", "current_attempt"):
                    assert after[key] == before[key]
    validate_policy_snapshot(changed["policy"])
    assert policy_models_match_contract("coding", changed["policy"])
    assert (
        policy_from_snapshot(changed["policy"]).to_dict()["difficult_retries"]
        == changed["policy"]["difficult_retries"]
    )
    next_attempt = store.begin_attempt(leg["leg_id"])
    assert (
        store.get_run(run["run_id"])["phases"][1]["legs"][0]["current_attempt"]["requested_model"]
        == "gpt-6-astra"
    )
    store.finish_attempt(
        next_attempt["attempt_id"],
        state="failed",
        output_text="still broken",
        error="assertion failed",
        exit_code=0,
    )
    again = store.escalate_difficult_leg(
        run["run_id"], leg["leg_id"], attempt_id=next_attempt["attempt_id"], gate=gate
    )
    assert again["phases"][1]["legs"][0]["state"] == "failed"
    assert len(again["policy"]["difficult_retries"]) == 1


@pytest.mark.parametrize(
    "output",
    [
        "rate limit exceeded AssertionError",
        "Permission denied AssertionError",
        "AssertionError: connection refused",
        "ModuleNotFoundError: no module named pytest",
        "invalid evidence envelope",
        "worker stopped honestly",
        "AssertionError: no space left on device",
        "command not found",
        "test gate could not run",
        "1 failed",
    ],
)
def test_ambiguous_or_infrastructure_failure_does_not_escalate(tmp_path, output):
    _store, run, leg, _attempt = _failed_run(tmp_path)
    assert difficult_retry_reason(run, leg, _gate(output)) is None


@pytest.mark.parametrize("code", [0, 124, 126, 127, 137, -9, True, "1"])
def test_non_test_exit_does_not_escalate(tmp_path, code):
    _store, run, leg, _attempt = _failed_run(tmp_path)
    assert difficult_retry_reason(run, leg, _gate(exit_code=code)) is None


def test_claude_only_research_review_and_cancelled_runs_do_not_escalate(tmp_path):
    store, run, leg, attempt = _failed_run(tmp_path, task="no-codex: repair the code")
    assert difficult_retry_reason(run, leg, _gate()) is None
    with pytest.raises(ValueError, match="not eligible"):
        store.escalate_difficult_leg(
            run["run_id"], leg["leg_id"], attempt_id=attempt["attempt_id"], gate=_gate()
        )
    for value in ("discover", "verify"):
        other = {**leg, "phase": value}
        assert difficult_retry_reason(run, other, _gate()) is None
    other_run = {**run, "activity": "research"}
    assert difficult_retry_reason(other_run, leg, _gate()) is None
    store.cancel_run(run["run_id"])
    with pytest.raises(RuntimeError, match="uncancelled"):
        store.escalate_difficult_leg(
            run["run_id"], leg["leg_id"], attempt_id=attempt["attempt_id"], gate=_gate()
        )


def test_astra_xhigh_requires_exact_leg_receipt(tmp_path):
    store, run, leg, attempt = _failed_run(tmp_path)
    changed = store.escalate_difficult_leg(
        run["run_id"], leg["leg_id"], attempt_id=attempt["attempt_id"], gate=_gate()
    )
    policy = deepcopy(changed["policy"])
    policy["difficult_retries"] = []
    assert not policy_models_match_contract("coding", policy)
    policy = deepcopy(changed["policy"])
    policy["difficult_retries"][0]["ordinal"] = 1
    assert not policy_models_match_contract("coding", policy)
    policy = deepcopy(changed["policy"])
    policy["difficult_retries"].append(dict(policy["difficult_retries"][0]))
    with pytest.raises(ValueError, match="bounded coding"):
        validate_policy_snapshot(policy)


@pytest.mark.parametrize(
    "repair,codex_available", [(True, True), (False, True), (True, False), (True, None)]
)
def test_supervisor_real_integration_failure_retries_once(
    fleet_env, monkeypatch, repair, codex_available  # noqa: F811
):
    root = fleet_env / "retry-repo"
    root.mkdir()
    for command in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", "-C", str(root), *command], check=True)
    (root / "value.txt").write_text("good\n")
    (root / "test_value.py").write_text(
        "from pathlib import Path\nassert Path('value.txt').read_text() == 'good\\n'\n"
    )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)
    monkeypatch.delenv("SERENA_FLEET_ISOLATION", raising=False)
    monkeypatch.setenv("SERENA_FLEET_WORKSPACE_ROOT", str(fleet_env / "worktrees"))
    monkeypatch.setenv(
        "SERENA_FLEET_INTEGRATION_TEST_COMMAND", shlex.join([sys.executable, "test_value.py"])
    )
    calls = []

    def fake(request, *, cancel_requested, on_event):
        calls.append((request.phase, request.model, request.effort))
        if request.phase == "execute":
            if codex_available is not True:
                monkeypatch.setattr(
                    supervisor,
                    "_read_start_capacity",
                    lambda: {
                        "codex": {
                            "usable": codex_available,
                            "status": "unknown" if codex_available is None else "exhausted",
                        }
                    },
                )
            improved = request.model == "gpt-6-astra" and request.effort == "xhigh"
            if improved:
                assert "single escalated difficult retry" in request.prompt
                assert "AssertionError" in request.prompt
                assert (root / "value.txt").read_text() == "good\n"  # failed patch rolled back
            (Path(request.cwd) / "value.txt").write_text(
                "good\n" if improved and repair else "bad\n"
            )
        return WorkerResult(True, "implementation result", None, request.model, request.effort, 0)

    monkeypatch.setattr(supervisor, "run_worker", fake)
    run = supervisor.start_run(
        "repair one isolated value", activity="coding", worker_count=1, cwd=str(root)
    )
    result = supervisor.run_supervisor(run["run_id"])
    assert result["state"] == ("completed" if repair and codex_available else "failed"), result.get(
        "error"
    )
    expected = [("execute", "gpt-6-astra", "medium")]
    if codex_available:
        expected.append(("execute", "gpt-6-astra", "xhigh"))
    assert [call for call in calls if call[0] == "execute"] == expected
    assert (root / "value.txt").read_text() == "good\n"
    assert len(result["policy"]["difficult_retries"]) == (1 if codex_available else 0)
