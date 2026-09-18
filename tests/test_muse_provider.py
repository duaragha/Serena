"""Muse sits beside Codex, Claude, and Gemini as an explicit-only provider.

Muse is never a silent fallback: automatic routing keeps its existing
choices, and Muse runs only when it is asked for by model, provider mode,
or task directive.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from core.coding_model_preferences import (
    MUSE_MODEL,
    normalise_coding_model,
    preferred_provider_for,
)
from core.coding_provider import choose_providers
from core.serena_policy import load_policy, resolve_policy
from fleet.capacity import read_fleet_capacity
from fleet.muse import EFFORT as MUSE_EFFORT
from fleet.muse import MODEL as FLEET_MUSE_MODEL
from fleet.muse import MuseStream
from fleet.policy import build_policy
from fleet.workers import WorkerRequest, run_worker, worker_command


def _request(tmp_path: Path, **overrides) -> WorkerRequest:
    values = {
        "run_id": "run-1",
        "leg_id": "muse-leg",
        "attempt_id": "muse-attempt",
        "task": "test task",
        "activity": "coding",
        "phase": "execute",
        "role": "core-co-implementer",
        "provider": "muse",
        "model": MUSE_MODEL,
        "effort": "high",
        "access_mode": "write",
        "cwd": str(tmp_path),
        "prompt": "do the controlled test",
    }
    values.update(overrides)
    return WorkerRequest(**values)


def _executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_muse_model_is_selectable_and_routes_to_muse() -> None:
    assert MUSE_MODEL == "muse-spark"
    assert MUSE_MODEL == FLEET_MUSE_MODEL
    assert normalise_coding_model("muse") == MUSE_MODEL
    assert normalise_coding_model("spark") == MUSE_MODEL
    assert preferred_provider_for(MUSE_MODEL) == "muse"


def test_muse_is_last_resort_not_first_choice() -> None:
    capacity = {
        "codex": {"usable": True, "reason": "available"},
        "claude": {"usable": True, "reason": "available"},
        "muse": {"usable": True, "reason": "available"},
    }
    auto = choose_providers(capacity)
    assert auto.usable
    assert auto.implement_provider == "codex"

    explicit = choose_providers(capacity, preferred_model=MUSE_MODEL)
    assert explicit.usable
    assert explicit.implement_provider == "muse"
    assert explicit.implement_model == MUSE_MODEL


def test_shared_policy_resolves_an_explicit_muse_override() -> None:
    decision = resolve_policy(
        "coding",
        activity="implement",
        role="implement",
        manual_override="muse-spark",
        capacity=None,
    )
    assert decision.provider == "muse"
    assert decision.model == "muse-spark"

    assert "muse-spark" in load_policy()["profiles"]["coding"]["manual_models"]


def test_fleet_builds_a_muse_only_run(tmp_path: Path) -> None:
    policy = build_policy(
        "coding",
        task="Task 1: move the helper into its own module",
        provider_mode="muse",
    )
    assert policy.provider_mode == "muse"
    assert policy.requested_provider_mode == "muse"
    worker = policy.phases[1].workers[0]
    assert worker.provider == "muse"
    assert worker.model == "muse-spark"
    assert worker.effort == MUSE_EFFORT == "max"


def test_fleet_task_directive_selects_muse_only(tmp_path: Path) -> None:
    policy = build_policy(
        "coding",
        task="Use muse only\n\nTask 1: move the helper into its own module",
    )
    assert policy.provider_mode == "muse"


def test_muse_worker_argv_uses_exec_json_and_bounds_authority(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SERENA_FLEET_MUSE_BIN", "/opt/bin/muse")

    write = worker_command(_request(tmp_path))
    assert write[:3] == ["/opt/bin/muse", "exec", "--json"]
    # Muse 1.3 refuses --no-session-log alongside the --session-id Fleet pins.
    assert "--no-session-log" not in write
    assert "--session-id" in write
    # One worker session spans phases, and Fleet moves it into a worktree.
    assert "--allow-workspace-switch" in write
    assert "--model" not in write  # default Serena identity uses the CLI default
    assert write[write.index("--reasoning-effort") + 1] == "high"
    assert write[write.index("--workspace") + 1] == str(tmp_path)
    assert write[write.index("--approval-mode") + 1] == "never"
    assert "--disable-write" not in write
    assert write[-1] == "do the controlled test"

    read_only = worker_command(_request(tmp_path, access_mode="read_only"))
    assert "--disable-write" in read_only

    pinned = worker_command(_request(tmp_path, model="meta-llama-9", effort="xhigh"))
    assert pinned[pinned.index("--model") + 1] == "meta-llama-9"
    assert pinned[pinned.index("--reasoning-effort") + 1] == "xhigh"


def test_muse_stream_reports_real_identity_and_answer(tmp_path, monkeypatch) -> None:
    muse_bin = _executable(
        tmp_path / "fake-muse",
        """#!/usr/bin/env python3
import json, sys
argv = sys.argv
assert argv[1:3] == ["exec", "--json"], argv
sid = argv[argv.index("--session-id") + 1]
assert argv[-1] == "do the controlled test", argv
print(json.dumps({"type": "init", "session_id": sid, "model": "muse-spark",
                  "effort": "high"}), flush=True)
print(json.dumps({"type": "result", "session_id": sid, "result": "muse answer",
                  "model": "muse-spark", "effort": "high", "status": "SUCCESS"}),
      flush=True)
""",
    )
    monkeypatch.setenv("SERENA_FLEET_MUSE_BIN", str(muse_bin))
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(tmp_path / "state"))

    events: list[tuple[str, dict]] = []
    result = run_worker(
        _request(tmp_path),
        cancel_requested=lambda: False,
        on_event=lambda event, payload: events.append((event, payload)),
    )
    assert result.ok is True
    assert result.output_text == "muse answer"
    assert result.actual_model == "muse-spark"
    assert result.actual_effort == "high"
    assert Path(result.event_log_path).is_file()
    assert any(event == "session.started" for event, _ in events)


def test_muse_zero_exit_without_an_answer_fails_closed(tmp_path, monkeypatch) -> None:
    muse_bin = _executable(
        tmp_path / "quiet-muse",
        """#!/usr/bin/env python3
raise SystemExit(0)
""",
    )
    monkeypatch.setenv("SERENA_FLEET_MUSE_BIN", str(muse_bin))
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(tmp_path / "state"))

    result = run_worker(
        _request(tmp_path),
        cancel_requested=lambda: False,
        on_event=lambda _event, _payload: None,
    )
    assert result.ok is False
    assert "without a final response" in (result.error or "")


def test_muse_stream_rejects_an_error_status() -> None:
    stream = MuseStream()
    stream.accept({"type": "init", "session_id": "sid-1", "model": "muse-spark"})
    stream.accept({"type": "result", "status": "FAILED", "error": "boom"})
    assert stream.completion_error(0) == "boom"


def test_muse_capacity_override_is_explicit_and_compatible(tmp_path) -> None:
    del tmp_path  # override path needs no files
    states = read_fleet_capacity(
        environ={
            "SERENA_FLEET_CAPACITY_JSON": json.dumps(
                {
                    "codex": {"status": "available"},
                    "claude": {"status": "available"},
                    "muse": {
                        "status": "unavailable",
                        "reason": "muse quota exhausted",
                    },
                }
            )
        }
    )
    assert states["muse"].usable is False
    assert "quota" in states["muse"].reason

    legacy = read_fleet_capacity(
        environ={
            "SERENA_FLEET_CAPACITY_JSON": json.dumps(
                {
                    "codex": {"status": "available"},
                    "claude": {"status": "available"},
                }
            )
        }
    )
    assert legacy["muse"].status == "unknown"
    assert legacy["muse"].usable is True


def test_muse_effort_is_a_real_cli_effort() -> None:
    # `muse exec --reasoning-effort` takes none|minimal|low|medium|high|xhigh|
    # max|ultra; Fleet may only ask for one that is also a Serena effort.
    from fleet.policy import EFFORTS

    cli_efforts = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
    assert MUSE_EFFORT in cli_efforts
    assert MUSE_EFFORT in EFFORTS
    assert MUSE_EFFORT == "max"


def test_gated_effort_downgrade_is_read_back_from_stderr() -> None:
    # Verbatim from `muse exec --reasoning-effort ultra` on 2026-09-18.
    from fleet.muse import downgraded_effort

    stderr = (
        "tbh: reasoning effort ultra is not available "
        "(gate ultra_reasoning_effort is closed); using xhigh\n"
    )
    assert downgraded_effort(stderr) == "xhigh"
    assert downgraded_effort("") is None
    assert downgraded_effort(None) is None
    assert downgraded_effort("muse: workspace trust: trusted source=user-config") is None
