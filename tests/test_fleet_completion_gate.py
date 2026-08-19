"""Live supervisor coverage for the completion-contract gate.

tests/test_fleet_completion.py proves the validator's rules in isolation. This
file proves the gate is actually wired into the real Fleet supervisor: that a
worker which exits zero while overstating its work does not get a completed
leg, that the rejection is durably visible, and that the leg stays retryable.

The completion gate is deliberately NOT stubbed here.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from core import fleet_supervisor as supervisor
from core.fleet_completion import EVIDENCE_CLOSE, EVIDENCE_OPEN
from core.fleet_completion_gate import (
    _active_claims,
    _dependency_states,
    _event_log_research_activity,
    _event_log_test_results,
    _git_no_index_check_paths,
    _leg_phase,
    _observed_test_results,
    _run_git_diff_check,
    _test_argv,
    _test_environment,
    _test_spec,
)
from core.fleet_isolation import FleetIsolationStore
from core.fleet_workers import WorkerResult

PROSE = "A real readable answer for the next Fleet phase, long enough to count."
RESEARCH_CRITERIA = (
    "claims stay within scope and distinguish observation from inference",
    "material findings are backed by repository evidence or attributable sources",
    "conflicts, uncertainty, and unresolved questions are explicit",
)


def _online_research_evidence():
    source_types = (
        "official",
        "primary",
        "research",
        "reputable_secondary",
        "community",
    )
    return {
        "search_queries": [
            "current official guidance",
            "2026 best practices",
            "recent technology",
        ],
        "sources": [
            {
                "url": f"https://source{index}.example/direct",
                "title": f"Source {index}",
                "publisher": f"Publisher {index}",
                "accessed_at": date.today().isoformat(),
                "source_type": source_types[index - 1],
                "currency": "current as of the access date",
                "finding": f"Finding {index}",
                "relevance": f"Relevance {index}",
            }
            for index in range(1, 6)
        ],
        "best_practices": ["practice one", "practice two"],
        "recent_developments": ["recent ecosystem change"],
        "recommendation_impact": "the web evidence changes the plan",
    }


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    database = tmp_path / "fleet.sqlite3"
    monkeypatch.setenv("SERENA_FLEET_DB_PATH", str(database))
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SERENA_FLEET_NO_AUTOSTART", "1")
    monkeypatch.delenv("SERENA_FLEET_INLINE", raising=False)
    monkeypatch.delenv("SERENA_FLEET_WORKER", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.setattr(
        supervisor,
        "read_fleet_capacity",
        lambda: {
            "codex": {"usable": True, "status": "available", "reason": "test"},
            "claude": {"usable": True, "status": "available", "reason": "test"},
        },
    )
    monkeypatch.setattr(supervisor, "_surface_session", lambda *_a, **_k: True)
    monkeypatch.setattr(supervisor, "_reconcile_run_sessions", lambda *_a, **_k: False)
    monkeypatch.setattr(supervisor, "_release_session", lambda *_a, **_k: None)
    monkeypatch.setattr(supervisor, "_refresh_index", lambda: None)
    monkeypatch.setattr(supervisor, "_send_spoken_notice", lambda _run, _token: False)
    monkeypatch.setattr(supervisor, "_send_raghav_text", lambda _message: True)
    monkeypatch.setattr(
        "core.fleet_completion_gate._event_log_research_activity",
        lambda _path: {"searches": 3, "fetches": 5},
    )
    return tmp_path


def _evidence_text(request, *, status="completed", stop_condition="", prose=PROSE):
    """A compliant envelope for whatever units this leg actually owns."""

    units = []
    for unit_id in request.assignment_ids:
        entry = {
            "id": unit_id,
            "status": status,
            "constraints_respected": True,
            "changed_paths": [],
            "tests": [],
            "stop_condition": stop_condition,
        }
        if status == "completed":
            entry["acceptance"] = [
                {"criterion": criterion, "met": True, "evidence": "observed"}
                for criterion in RESEARCH_CRITERIA
            ]
            if request.phase == "discover":
                entry["online_research"] = _online_research_evidence()
            if request.phase == "verify":
                # A structured review reports an explicit empty list when it
                # found nothing, so "clean" is distinguishable from "silent".
                entry["findings"] = []
        else:
            entry["acceptance"] = []
        units.append(entry)
    body = json.dumps({"schema_version": 1, "units": units})
    return f"{prose}\n\n{EVIDENCE_OPEN}\n{body}\n{EVIDENCE_CLOSE}"


def _worker(output_for):
    def run(request, *, cancel_requested, on_event):
        del cancel_requested
        sid = request.resume_session_id or f"session-{request.worker_key}"
        on_event("process.started", {"pid": 1, "event_log_path": "/x.jsonl"})
        on_event("session.started", {"session_id": sid})
        return WorkerResult(
            ok=True,
            output_text=output_for(request),
            session_id=sid,
            actual_model=request.model,
            actual_effort=request.effort,
            exit_code=0,
        )

    return run


def _start(env, **kwargs):
    return supervisor.start_run(
        kwargs.pop("task", "research the completion gate end to end"),
        activity=kwargs.pop("activity", "research"),
        provider_mode=kwargs.pop("provider_mode", "codex"),
        worker_count=kwargs.pop("worker_count", 1),
        cwd=str(env),
        **kwargs,
    )


def _legs(run):
    return [leg for phase in run["phases"] for leg in phase["legs"]]


def _events(run_id, event_type):
    store = supervisor.FleetStore()
    return [
        event
        for event in store.events(run_id, limit=500)
        if str(event.get("type") or "") == event_type
    ]


def test_dependency_states_follow_the_current_phase_barrier():
    snapshot = {
        "work_units": [
            {
                "id": "ws-1",
                "state": "queued",
                "phase_executions": [
                    {"phase_index": 0, "state": "completed"},
                    {"phase_index": 1, "state": "queued"},
                ],
            },
            {
                "id": "ws-2",
                "state": "running",
                "phase_executions": [
                    {"phase_index": 0, "state": "completed"},
                    {"phase_index": 1, "state": "running"},
                ],
            },
        ]
    }

    assert _dependency_states(snapshot, 0) == {
        "ws-1": "completed",
        "ws-2": "completed",
    }
    assert _dependency_states(snapshot, 1) == {
        "ws-1": "queued",
        "ws-2": "running",
    }


def test_leg_phase_is_resolved_from_the_projected_phase_container():
    leg = {"leg_id": "leg-integration", "worker_key": "claude:b"}
    snapshot = {
        "phases": [
            {"index": 0, "name": "discover", "legs": [leg]},
            {"index": 1, "name": "execute", "legs": []},
        ]
    }

    assert _leg_phase(snapshot, leg) == (0, "discover")


def test_leg_phase_is_resolved_from_index_when_projected_leg_omits_name():
    leg = {
        "leg_id": "leg-projected",
        "worker_key": "codex:a",
        "phase_index": 0,
    }
    snapshot = {
        "phases": [
            {"index": 0, "name": "discover", "legs": [leg]},
            {"index": 1, "name": "execute", "legs": []},
        ]
    }

    assert _leg_phase(snapshot, leg) == (0, "discover")


def test_an_engaged_workspace_with_zero_claims_is_not_treated_as_unknown(gate_env):
    store = FleetIsolationStore()
    store.record_workspace(
        run_id="run-empty-claims",
        worker_key="codex:a",
        path=str(gate_env / "worker"),
        branch="serena/fleet/run-empty-claims/codex-a",
        base_head="a" * 40,
    )
    assert _active_claims("run-empty-claims", "codex:a") == []


def test_test_receipts_are_reexecuted_in_the_recorded_workspace(gate_env, monkeypatch):
    workspace = gate_env / "worker-test-observation"
    workspace.mkdir()
    observed_calls: list[dict] = []

    class Completed:
        returncode = 7

    def observe(_argv, **kwargs):
        observed_calls.append(kwargs)
        return Completed()

    monkeypatch.setattr("core.fleet_completion_gate.subprocess.run", observe)
    store = FleetIsolationStore()
    store.record_workspace(
        run_id="run-test-observation",
        worker_key="codex:a",
        path=str(workspace),
        branch="serena/fleet/run-test-observation/codex-a",
        base_head="a" * 40,
    )
    output = (
        f"{PROSE}{EVIDENCE_OPEN}"
        + json.dumps(
            {
                "units": [
                    {
                        "id": "ws-1",
                        "tests": [
                            {"command": "python3 -m pytest tests/test_x.py", "exit_code": 0}
                        ],
                    }
                ]
            }
        )
        + EVIDENCE_CLOSE
    )
    assert _observed_test_results(
        "run-test-observation", "codex:a", output
    ) == {"python3 -m pytest tests/test_x.py": 7}
    assert observed_calls[0]["cwd"] == str(workspace)


def test_provider_command_receipt_wins_over_a_wrong_cwd_rerun(gate_env, monkeypatch):
    workspace = gate_env / "worker-recorded-receipt"
    workspace.mkdir()
    FleetIsolationStore().record_workspace(
        run_id="run-recorded-receipt",
        worker_key="codex:a",
        path=str(workspace),
        branch="serena/fleet/run-recorded-receipt/codex-a",
        base_head="a" * 40,
    )
    command = "python3 -m unittest discover -s tests -v"
    event_log = gate_env / "attempt.jsonl"
    provider_event = {
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": f"/bin/bash -lc {command!r}",
            "exit_code": 0,
            "status": "completed",
        },
    }
    event_log.write_text(
        json.dumps({"stream": "stdout", "line": json.dumps(provider_event)}) + "\n",
        encoding="utf-8",
    )
    output = (
        f"{PROSE}{EVIDENCE_OPEN}"
        + json.dumps(
            {
                "units": [
                    {
                        "id": "ws-1",
                        "tests": [{"command": command, "exit_code": 0}],
                    }
                ]
            }
        )
        + EVIDENCE_CLOSE
    )
    monkeypatch.setattr(
        "core.fleet_completion_gate.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a recorded command must not be rerun from another cwd")
        ),
    )

    assert _observed_test_results(
        "run-recorded-receipt",
        "codex:a",
        output,
        event_log_path=str(event_log),
    ) == {command: 0}


def test_final_command_in_setup_script_uses_the_native_exit(gate_env):
    command = "./node_modules/.bin/tsc --project /tmp/health.json --pretty false"
    event_log = gate_env / "setup-attempt.jsonl"
    setup = (
        "if [ -e node_modules ]; then exit 97; fi\n"
        "ln -s /trusted/node_modules node_modules\n"
        "trap 'unlink node_modules' EXIT\n"
        f"{command}"
    )
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": shlex.join(["/bin/bash", "-lc", setup]),
                "exit_code": code,
            },
        }
        for code in (2, 0)
    ]
    event_log.write_text(
        "".join(
            json.dumps({"stream": "stdout", "line": json.dumps(event)}) + "\n"
            for event in events
        ),
        encoding="utf-8",
    )

    assert _event_log_test_results(str(event_log))[command] == 0


def test_read_only_receipt_is_collected_without_a_workspace(gate_env):
    command = "git diff --check"
    event_log = gate_env / "read-attempt.jsonl"
    provider_event = {
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": command,
            "exit_code": 0,
        },
    }
    event_log.write_text(
        json.dumps({"stream": "stdout", "line": json.dumps(provider_event)}) + "\n",
        encoding="utf-8",
    )
    output = (
        f"{PROSE}{EVIDENCE_OPEN}"
        + json.dumps({"units": [{"id": "ws-1", "tests": [{"command": command}]}]})
        + EVIDENCE_CLOSE
    )

    assert _observed_test_results(
        "run-read-receipt",
        "codex:a",
        output,
        event_log_path=str(event_log),
        allow_rerun=False,
    ) == {command: 0}


def test_claude_tool_success_is_not_invented_as_a_numeric_exit(gate_env):
    event_log = gate_env / "claude-attempt.jsonl"
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                        "input": {"command": "./node_modules/.bin/jest tests"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool-1", "is_error": False}
                ]
            },
        },
    ]
    event_log.write_text(
        "".join(
            json.dumps({"stream": "stdout", "line": json.dumps(event)}) + "\n"
            for event in events
        ),
        encoding="utf-8",
    )

    assert _event_log_test_results(str(event_log)) == {}


def test_provider_event_logs_prove_distinct_web_search_activity(gate_env):
    event_log = gate_env / "web-research.jsonl"
    events = [
        {
            "type": "item.completed",
            "item": {
                "id": "search-1",
                "type": "web_search",
                "action": {"type": "search", "queries": ["query one", "query two"]},
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "search-2",
                        "name": "WebSearch",
                        "input": {"query": "query three"},
                    },
                    {
                        "type": "tool_use",
                        "id": "fetch-1",
                        "name": "WebFetch",
                        "input": {"url": "https://example.com/direct"},
                    },
                ]
            },
        },
    ]
    event_log.write_text(
        "".join(
            json.dumps({"stream": "stdout", "line": json.dumps(event)}) + "\n"
            for event in events
        ),
        encoding="utf-8",
    )

    assert _event_log_research_activity(str(event_log)) == {
        "searches": 3,
        "fetches": 1,
    }


def test_base_node_modules_runner_is_used_for_an_isolated_workspace(gate_env):
    workspace = gate_env / "worker-node-runner"
    workspace.mkdir()
    tool_root = gate_env / "base-repo"
    runner = tool_root / "node_modules" / ".bin" / "jest"
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runner.chmod(0o700)

    spec = _test_spec(
        "./node_modules/.bin/jest __tests__/features/health --coverage=false",
        str(workspace),
        tool_root=str(tool_root),
    )

    assert spec is not None
    assert spec[0][0] == str(runner.absolute())
    assert spec[0][1:] == ["__tests__/features/health", "--coverage=false"]


def test_node_rerun_temporarily_links_trusted_base_dependencies(gate_env):
    workspace = gate_env / "worker-node-dependencies"
    workspace.mkdir()
    tool_root = gate_env / "base-repo"
    modules = tool_root / "node_modules"
    runner = modules / ".bin" / "jest"
    runner.parent.mkdir(parents=True)
    (modules / "probe.txt").write_text("trusted\n", encoding="utf-8")
    runner.write_text(
        "#!/bin/sh\ntest -L node_modules && test -f node_modules/probe.txt\n",
        encoding="utf-8",
    )
    runner.chmod(0o700)
    FleetIsolationStore().record_workspace(
        run_id="run-node-dependencies",
        worker_key="claude:a",
        path=str(workspace),
        branch="serena/fleet/run-node-dependencies/claude-a",
        base_head="a" * 40,
    )
    command = "./node_modules/.bin/jest tests"
    output = (
        f"{PROSE}{EVIDENCE_OPEN}"
        + json.dumps(
            {
                "units": [
                    {
                        "id": "ws-1",
                        "tests": [{"command": command, "exit_code": 0}],
                    }
                ]
            }
        )
        + EVIDENCE_CLOSE
    )

    assert _observed_test_results(
        "run-node-dependencies",
        "claude:a",
        output,
        tool_root=str(tool_root),
    ) == {command: 0}
    assert not (workspace / "node_modules").exists()


def test_new_file_whitespace_checks_are_parsed_per_path(gate_env):
    workspace = gate_env / "worker-no-index"
    workspace.mkdir()
    command = (
        "git diff --no-index --check /dev/null src/one.ts && "
        "git diff --no-index --check /dev/null tests/two.test.ts"
    )

    assert _git_no_index_check_paths(command, str(workspace)) == [
        "src/one.ts",
        "tests/two.test.ts",
    ]
    assert _git_no_index_check_paths(
        "git diff --no-index --check /dev/null ../outside", str(workspace)
    ) is None


def test_git_diff_check_has_a_narrow_safe_rerun(gate_env):
    workspace = gate_env / "worker-git-check"
    workspace.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(workspace)], check=True
    )

    assert _run_git_diff_check(
        "git diff --check", str(workspace), timeout=10
    ) == 0
    assert _run_git_diff_check(
        "git status --short", str(workspace), timeout=10
    ) is None


def test_verification_reruns_do_not_inherit_provider_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    environment = _test_environment({"PYTHONPATH": "."}, [])

    assert environment["PATH"] == "/usr/bin"
    assert environment["PYTHONPATH"] == "."
    assert environment["CI"] == "1"
    assert "OPENAI_API_KEY" not in environment
    assert "ANTHROPIC_API_KEY" not in environment


def test_test_runner_preserves_a_validated_virtualenv_symlink(gate_env):
    workspace = gate_env / "worker-venv-test"
    workspace.mkdir()
    interpreter = gate_env / "trusted-venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(Path(sys.executable))

    argv = _test_argv(
        f"{interpreter} -m pytest tests/test_x.py",
        str(workspace),
    )

    assert argv is not None
    assert argv[0] == str(interpreter.absolute())
    assert Path(argv[0]).resolve() == Path(sys.executable).resolve()


def test_test_runner_accepts_only_narrow_test_environment(gate_env, monkeypatch):
    workspace = gate_env / "worker-safe-env"
    workspace.mkdir()
    interpreter = gate_env / "trusted-venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(Path(sys.executable))
    observed_calls: list[dict] = []

    class Completed:
        returncode = 0

    def observe(argv, **kwargs):
        observed_calls.append({"argv": argv, **kwargs})
        return Completed()

    monkeypatch.setattr("core.fleet_completion_gate.subprocess.run", observe)
    FleetIsolationStore().record_workspace(
        run_id="run-safe-env",
        worker_key="codex:a",
        path=str(workspace),
        branch="serena/fleet/run-safe-env/codex-a",
        base_head="a" * 40,
    )
    command = (
        "PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. "
        f"{interpreter} -m pytest tests/test_x.py"
    )
    output = (
        f"{PROSE}{EVIDENCE_OPEN}"
        + json.dumps(
            {
                "units": [
                    {
                        "id": "ws-1",
                        "tests": [{"command": command, "exit_code": 0}],
                    }
                ]
            }
        )
        + EVIDENCE_CLOSE
    )

    assert _observed_test_results("run-safe-env", "codex:a", output) == {command: 0}
    assert observed_calls[0]["argv"][0] == str(interpreter.absolute())
    assert observed_calls[0]["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert observed_calls[0]["env"]["PYTHONPATH"] == "."


def test_test_runner_honors_env_unset_prefix(gate_env, monkeypatch):
    """`env -u NAME` only removes state, so the gate must re-run it instead of
    scoring the command unverifiable (the 126-vs-0 mismatch that failed real
    runs whose workers unset CLAUDE_CODE_SESSION_ID)."""
    workspace = gate_env / "worker-env-unset"
    workspace.mkdir()
    interpreter = gate_env / "trusted-venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(Path(sys.executable))
    observed_calls: list[dict] = []

    class Completed:
        returncode = 0

    def observe(argv, **kwargs):
        observed_calls.append({"argv": argv, **kwargs})
        return Completed()

    monkeypatch.setattr("core.fleet_completion_gate.subprocess.run", observe)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    FleetIsolationStore().record_workspace(
        run_id="run-env-unset",
        worker_key="claude:a",
        path=str(workspace),
        branch="serena/fleet/run-env-unset/claude-a",
        base_head="a" * 40,
    )
    command = (
        "env -u CLAUDE_CODE_SESSION_ID "
        f"{interpreter} -m pytest tests/test_x.py -q -p no:randomly"
    )
    output = (
        f"{PROSE}{EVIDENCE_OPEN}"
        + json.dumps(
            {
                "units": [
                    {
                        "id": "ws-1",
                        "tests": [{"command": command, "exit_code": 0}],
                    }
                ]
            }
        )
        + EVIDENCE_CLOSE
    )

    assert _observed_test_results("run-env-unset", "claude:a", output) == {command: 0}
    assert observed_calls[0]["argv"][0] == str(interpreter.absolute())
    assert "CLAUDE_CODE_SESSION_ID" not in observed_calls[0]["env"]


def test_test_runner_rejects_other_env_options(gate_env):
    workspace = gate_env / "worker-env-opts"
    workspace.mkdir()
    interpreter = gate_env / "trusted-venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(Path(sys.executable))

    assert (
        _test_argv(
            f"env -i {interpreter} -m pytest tests/test_x.py",
            str(workspace),
        )
        is None
    )
    assert (
        _test_argv(
            f"env -S 'x y' {interpreter} -m pytest tests/test_x.py",
            str(workspace),
        )
        is None
    )
    assert (
        _test_argv(
            f"env --chdir=/tmp {interpreter} -m pytest tests/test_x.py",
            str(workspace),
        )
        is None
    )


def test_test_runner_rejects_selector_and_loader_environment(gate_env):
    workspace = gate_env / "worker-unsafe-env"
    workspace.mkdir()
    interpreter = gate_env / "trusted-venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(Path(sys.executable))

    assert (
        _test_argv(
            f"PYTEST_ADDOPTS=-x {interpreter} -m pytest tests/test_x.py",
            str(workspace),
        )
        is None
    )
    assert (
        _test_argv(
            f"LD_PRELOAD=/tmp/worker.so {interpreter} -m pytest tests/test_x.py",
            str(workspace),
        )
        is None
    )


def test_test_runner_accepts_ruff_through_trusted_python(gate_env):
    workspace = gate_env / "worker-python-ruff"
    workspace.mkdir()
    interpreter = gate_env / "trusted-venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(Path(sys.executable))

    argv = _test_argv(
        f"{interpreter} -m ruff check core tests",
        str(workspace),
    )

    assert argv is not None
    assert argv[:3] == [str(interpreter.absolute()), "-m", "ruff"]


def test_a_worker_authored_executable_cannot_pose_as_the_test_runner(gate_env):
    workspace = gate_env / "worker-fake-test"
    workspace.mkdir()
    executable = workspace / "pytest"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    FleetIsolationStore().record_workspace(
        run_id="run-fake-test",
        worker_key="codex:a",
        path=str(workspace),
        branch="serena/fleet/run-fake-test/codex-a",
        base_head="a" * 40,
    )
    output = (
        f"{PROSE}{EVIDENCE_OPEN}"
        + json.dumps(
            {"units": [{"id": "ws-1", "tests": [{"command": "./pytest"}]}]}
        )
        + EVIDENCE_CLOSE
    )
    assert _observed_test_results("run-fake-test", "codex:a", output) == {
        "./pytest": 126
    }


def test_a_compliant_worker_completes_the_run(gate_env, monkeypatch):
    monkeypatch.setattr(supervisor, "run_worker", _worker(_evidence_text))
    run = _start(gate_env)

    completed = supervisor.run_supervisor(run["run_id"])

    assert completed["state"] == "completed", completed.get("error")
    assert all(leg["state"] == "completed" for leg in _legs(completed))
    assert _events(run["run_id"], "leg.completion_evidence_accepted")


def test_claimed_online_research_without_observed_searches_is_rejected(
    gate_env, monkeypatch
):
    monkeypatch.setattr(supervisor, "run_worker", _worker(_evidence_text))
    monkeypatch.setattr(
        "core.fleet_completion_gate._event_log_research_activity",
        lambda _path: {"searches": 0, "fetches": 0},
    )
    run = _start(gate_env)

    finished = supervisor.run_supervisor(run["run_id"])

    assert finished["state"] != "completed"
    error = str(_legs(finished)[0].get("current_attempt", {}).get("error") or "")
    assert "recorded provider web searches" in error


def test_a_worker_that_exits_zero_with_no_evidence_does_not_complete(gate_env, monkeypatch):
    """The core regression: process success is not contract success."""

    monkeypatch.setattr(
        supervisor,
        "run_worker",
        _worker(lambda request: "I did all of it. Everything passed."),
    )
    run = _start(gate_env)

    finished = supervisor.run_supervisor(run["run_id"])

    assert finished["state"] != "completed"
    first = _legs(finished)[0]
    assert first["state"] == "failed"
    assert "completion evidence" in str(first.get("current_attempt", {}).get("error") or "")
    rejected = _events(run["run_id"], "leg.completion_evidence_rejected")
    assert rejected, "the rejection must be durably visible, not silent"
    payload = rejected[0]["payload"]
    assert payload["accepted"] is False
    assert payload["retryable"] is True
    assert any("serena-evidence" in item for item in payload["failures"])
    repairs = _events(run["run_id"], "leg.completion_repair_requested")
    assert len(repairs) == 1
    assert first["attempt_count"] == 2


def test_missing_envelope_repairs_once_and_resumes_the_native_session(gate_env, monkeypatch):
    requests = []

    def run(request, *, cancel_requested, on_event):
        del cancel_requested
        requests.append(request)
        sid = request.resume_session_id or "repair-session"
        on_event("process.started", {"pid": 1, "event_log_path": "/x.jsonl"})
        on_event("session.started", {"session_id": sid})
        return WorkerResult(
            ok=True,
            output_text=(
                "verification was still running when the first turn ended"
                if len(requests) == 1
                else _evidence_text(request)
            ),
            session_id=sid,
            actual_model=request.model,
            actual_effort=request.effort,
            exit_code=0,
        )

    monkeypatch.setattr(supervisor, "run_worker", run)
    run_snapshot = _start(gate_env)

    finished = supervisor.run_supervisor(run_snapshot["run_id"])

    assert finished["state"] == "completed", finished.get("error")
    assert len(requests) >= 2
    assert requests[1].resume_session_id == "repair-session"
    assert len(_events(run_snapshot["run_id"], "leg.completion_repair_requested")) == 1


def test_a_worker_claiming_completed_while_stopping_is_rejected(gate_env, monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "run_worker",
        _worker(
            lambda request: _evidence_text(
                request,
                status="completed",
                stop_condition="a required dependency conflicted with another owner",
            )
        ),
    )
    run = _start(gate_env)

    finished = supervisor.run_supervisor(run["run_id"])

    assert finished["state"] != "completed"
    rejected = _events(run["run_id"], "leg.completion_evidence_rejected")
    assert rejected
    assert any("contradictory" in item for item in rejected[0]["payload"]["failures"])


def test_an_honest_stop_is_not_forced_to_fake_completion(gate_env, monkeypatch):
    """Stopping truthfully must remain a first-class, accepted outcome."""

    monkeypatch.setattr(
        supervisor,
        "run_worker",
        _worker(
            lambda request: _evidence_text(
                request,
                status="blocked",
                stop_condition="the requested result cannot be verified from available evidence",
            )
        ),
    )
    run = _start(gate_env)

    finished = supervisor.run_supervisor(run["run_id"])

    assert _legs(finished)[0]["state"] == "completed"
    assert _events(run["run_id"], "leg.completion_evidence_accepted")


def test_a_rejected_leg_is_retryable_and_recovers_on_compliant_evidence(gate_env, monkeypatch):
    """Blocked completion must be recoverable, not a dead run."""

    state = {"honest": False}

    def output_for(request):
        if not state["honest"]:
            return "trust me, it is done"
        return _evidence_text(request)

    monkeypatch.setattr(supervisor, "run_worker", _worker(output_for))
    run = _start(gate_env)
    finished = supervisor.run_supervisor(run["run_id"])
    assert finished["state"] != "completed"

    state["honest"] = True
    supervisor.retry_run(run["run_id"])
    recovered = supervisor.run_supervisor(run["run_id"])

    assert recovered["state"] == "completed", recovered.get("error")
    assert _events(run["run_id"], "leg.completion_evidence_accepted")


def test_a_read_only_worker_claiming_file_changes_is_rejected(gate_env, monkeypatch):
    def output_for(request):
        units = [
            {
                "id": unit_id,
                "status": "completed",
                "acceptance": [
                    {"criterion": criterion, "met": True, "evidence": "observed"}
                    for criterion in RESEARCH_CRITERIA
                ],
                "constraints_respected": True,
                "changed_paths": ["core/fleet_store.py"],
                "tests": [],
                "stop_condition": "",
            }
            for unit_id in request.assignment_ids
        ]
        body = json.dumps({"schema_version": 1, "units": units})
        return f"{PROSE}\n\n{EVIDENCE_OPEN}\n{body}\n{EVIDENCE_CLOSE}"

    monkeypatch.setattr(supervisor, "run_worker", _worker(output_for))
    run = _start(gate_env)

    finished = supervisor.run_supervisor(run["run_id"])

    assert finished["state"] != "completed"
    rejected = _events(run["run_id"], "leg.completion_evidence_rejected")
    assert any("read-only leg" in item for item in rejected[0]["payload"]["failures"])


def test_the_enforced_envelope_spec_reaches_the_worker_prompt(gate_env, monkeypatch):
    seen: list = []

    def capture(request, *, cancel_requested, on_event):
        del cancel_requested, on_event
        seen.append(request)
        return WorkerResult(
            ok=True,
            output_text=_evidence_text(request),
            session_id=f"session-{request.worker_key}",
            actual_model=request.model,
            actual_effort=request.effort,
            exit_code=0,
        )

    monkeypatch.setattr(supervisor, "run_worker", capture)
    run = _start(gate_env)
    supervisor.run_supervisor(run["run_id"])

    assert seen
    prompt = seen[0].prompt
    assert "COMPLETION CONTRACT ENFORCEMENT" in prompt
    assert EVIDENCE_OPEN in prompt and EVIDENCE_CLOSE in prompt
    assert "read-only" in prompt


def test_a_gate_crash_fails_closed_and_stays_retryable(gate_env, monkeypatch):
    """Infrastructure uncertainty must not be laundered into completion."""

    def explode(*_args, **_kwargs):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(supervisor, "evaluate_leg_completion", explode)
    monkeypatch.setattr(supervisor, "run_worker", _worker(_evidence_text))
    run = _start(gate_env)

    finished = supervisor.run_supervisor(run["run_id"])

    assert finished["state"] != "completed"
    assert _legs(finished)[0]["state"] == "failed"
    failed = _events(run["run_id"], "leg.completion_gate_failed")
    assert failed and failed[0]["payload"]["retryable"] is True


def test_commands_through_fleets_venv_symlink_are_trusted(gate_env):
    """Fleet symlinks the base checkout's .venv into each worktree; commands
    through that symlink must be re-runnable, not scored as worker-authored."""
    workspace = gate_env / "worker-venv-link"
    workspace.mkdir()
    base_venv = gate_env / "base-checkout" / ".venv"
    (base_venv / "bin").mkdir(parents=True)
    interpreter = base_venv / "bin" / "python"
    interpreter.symlink_to(Path(sys.executable))
    (workspace / ".venv").symlink_to(base_venv, target_is_directory=True)

    argv = _test_argv(".venv/bin/python -m pytest tests/test_x.py -q", str(workspace))

    assert argv is not None
    assert argv[0] == str(workspace / ".venv" / "bin" / "python")


def test_a_worker_authored_venv_directory_is_still_rejected(gate_env):
    """A real .venv directory inside the worktree is worker-controlled: a fake
    interpreter there must not become the trusted test runner."""
    workspace = gate_env / "worker-fake-venv"
    (workspace / ".venv" / "bin").mkdir(parents=True)
    fake = workspace / ".venv" / "bin" / "python"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o700)

    assert _test_argv(".venv/bin/python -m pytest tests/test_x.py", str(workspace)) is None


def test_read_only_legs_may_record_informational_tests_without_exit_codes(gate_env, monkeypatch):
    """Regression: a research worker recording outcome-style check notes was
    rejected on an exit_code rule its read-only instructions never stated."""

    def output(request):
        units = []
        for unit_id in request.assignment_ids:
            entry = {
                "id": unit_id,
                "status": "completed",
                "constraints_respected": True,
                "changed_paths": [],
                "tests": [
                    {
                        "command": "python3 -c 'compile checks'",
                        "outcome": "passed: syntax ok",
                    }
                ],
                "stop_condition": "",
                "acceptance": [
                    {"criterion": criterion, "met": True, "evidence": "observed"}
                    for criterion in RESEARCH_CRITERIA
                ],
            }
            if request.phase == "discover":
                entry["online_research"] = _online_research_evidence()
            if request.phase == "verify":
                entry["findings"] = []
            units.append(entry)
        body = json.dumps({"schema_version": 1, "units": units})
        return f"{PROSE}\n\n{EVIDENCE_OPEN}\n{body}\n{EVIDENCE_CLOSE}"

    monkeypatch.setattr(supervisor, "run_worker", _worker(output))
    run = _start(gate_env)

    finished = supervisor.run_supervisor(run["run_id"])

    assert finished["state"] == "completed", finished.get("error")


def _coding_evidence_text(request, *, findings=None):
    """A compliant envelope answering this run's real coding contract."""

    store = supervisor.FleetStore()
    run = store.get_run(request.run_id)
    contracts = {
        str(unit.get("id")): unit
        for unit in (run["policy"].get("work_units") or [])
        if isinstance(unit, dict)
    }
    units = []
    for unit_id in request.assignment_ids:
        criteria = (
            contracts.get(unit_id, {}).get("completion_contract", {}).get(
                "acceptance_criteria"
            )
            or []
        )
        entry = {
            "id": unit_id,
            "status": "completed",
            "constraints_respected": True,
            "changed_paths": [],
            "tests": [],
            "stop_condition": "",
            "acceptance": [
                {"criterion": criterion, "met": True, "evidence": "observed"}
                for criterion in criteria
            ],
        }
        if request.phase == "discover":
            entry["online_research"] = _online_research_evidence()
        if request.phase == "verify":
            entry["findings"] = list(findings or [])
        units.append(entry)
    body = json.dumps({"schema_version": 1, "units": units})
    return f"{PROSE}\n\n{EVIDENCE_OPEN}\n{body}\n{EVIDENCE_CLOSE}"


def test_a_clean_review_skips_a_fixer_but_still_produces_a_final_response(
    gate_env, monkeypatch
):
    """A worker its reviewers cleared must not spend an xhigh Fix turn."""

    monkeypatch.setenv("SERENA_FLEET_ISOLATION", "off")
    spawned: list[tuple[str, str]] = []

    def output(request):
        spawned.append((request.phase, request.worker_key))
        return _coding_evidence_text(request)

    monkeypatch.setattr(supervisor, "run_worker", _worker(output))
    run = _start(
        gate_env,
        task="fix the local retry path in core/x.py",
        activity="coding",
        provider_mode="balanced",
        worker_count=2,
    )

    finished = supervisor.run_supervisor(run["run_id"])

    assert finished["state"] == "completed", finished.get("error")
    assert str(finished.get("result_text") or "").strip()

    finalize_workers = {worker for phase, worker in spawned if phase == "finalize"}
    verify_workers = {worker for phase, worker in spawned if phase == "verify"}
    # Review ran for everyone; Fix ran only for the reporter.
    assert len(verify_workers) == 2
    assert len(finalize_workers) == 1

    skipped = _events(run["run_id"], "worker.finalize.skipped")
    assert len(skipped) == 1


def test_a_review_finding_keeps_its_owners_fix_leg_running(gate_env, monkeypatch):
    monkeypatch.setenv("SERENA_FLEET_ISOLATION", "off")
    spawned: list[tuple[str, str]] = []

    def output(request):
        spawned.append((request.phase, request.worker_key))
        # Every reviewer raises a finding against every unit, so no fixer is idle.
        return _coding_evidence_text(
            request,
            findings=[
                {
                    "unit_id": unit_id,
                    "severity": "major",
                    "summary": "the retry path drops the original error",
                    "evidence": "core/x.py:41 reassigns err before raising",
                }
                for unit_id in ("ws-1", "ws-2")
            ],
        )

    monkeypatch.setattr(supervisor, "run_worker", _worker(output))
    run = _start(
        gate_env,
        task="fix the local retry path in core/x.py",
        activity="coding",
        provider_mode="balanced",
        worker_count=2,
    )

    finished = supervisor.run_supervisor(run["run_id"])

    assert finished["state"] == "completed", finished.get("error")
    assert len({worker for phase, worker in spawned if phase == "finalize"}) == 2
    assert _events(run["run_id"], "worker.finalize.skipped") == []
