from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from core.fleet_workers import (
    WorkerRequest,
    _claude_actual_identity,
    _codex_actual_identity,
    _event_summary,
    run_worker,
    runtime_doctor,
    worker_command,
)


def _request(
    tmp_path: Path,
    provider: str,
    *,
    access_mode: str = "read_only",
    activity: str | None = None,
    phase: str = "execute",
) -> WorkerRequest:
    return WorkerRequest(
        run_id="run-1",
        leg_id=f"{provider}-leg",
        attempt_id=f"{provider}-attempt",
        task="test task",
        activity=activity or ("coding" if access_mode == "write" else "research"),
        phase=phase,
        role="tester",
        provider=provider,
        model="gpt-5.6-sol" if provider == "codex" else "opus",
        effort="xhigh",
        access_mode=access_mode,
        cwd=str(tmp_path),
        prompt="do the controlled test",
    )


def _executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_runtime_doctor_is_operable_with_one_healthy_provider(tmp_path, monkeypatch):
    codex_bin = _executable(
        tmp_path / "doctor-codex",
        """#!/usr/bin/env python3
import sys
if "--help" in sys.argv:
    print("--ignore-user-config --disable --sandbox --model")
else:
    print("codex 1")
""",
    )
    claude_bin = _executable(
        tmp_path / "doctor-claude",
        """#!/usr/bin/env python3
raise SystemExit(1)
""",
    )
    monkeypatch.setenv("SERENA_FLEET_CODEX_BIN", str(codex_bin))
    monkeypatch.setenv("SERENA_FLEET_CLAUDE_BIN", str(claude_bin))

    report = runtime_doctor()

    assert report["ok"] is True
    assert report["degraded"] is True
    assert report["full_mixed_ready"] is False
    assert report["ready_providers"] == ["codex"]


def test_worker_argv_isolated_from_nested_fleet_and_user_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_FLEET_CODEX_BIN", "/opt/bin/codex")
    monkeypatch.setenv("SERENA_FLEET_CLAUDE_BIN", "/opt/bin/claude")
    # No read catalog on disk, so every leg falls back to zero MCP.
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(tmp_path / "state"))

    codex = worker_command(_request(tmp_path, "codex", access_mode="write"))
    assert codex[:2] == ["/opt/bin/codex", "exec"]
    assert "--ignore-user-config" in codex
    assert codex[codex.index("--disable") + 1] == "multi_agent"
    assert codex[codex.index("--sandbox") + 1] == "workspace-write"
    assert not any("mcp_servers" in value for value in codex)
    assert codex[-3:] == ["-C", str(tmp_path), "-"]

    codex_read_only = worker_command(_request(tmp_path, "codex"))
    assert codex_read_only[codex_read_only.index("--sandbox") + 1] == "read-only"
    assert codex_read_only[codex_read_only.index("--enable") + 1] == "standalone_web_search"

    claude = worker_command(_request(tmp_path, "claude"), session_id="sid-1")
    assert claude[0] == "/opt/bin/claude"
    assert "--safe-mode" in claude
    assert "--strict-mcp-config" in claude
    assert claude[claude.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--disable-slash-commands" in claude
    denied = claude[claude.index("--disallowedTools") + 1]
    assert all(name in denied for name in ("Agent", "Task", "Skill", "Edit", "Write"))
    assert "--dangerously-skip-permissions" not in claude
    assert "WebSearch" in claude[claude.index("--tools") + 1]
    assert "WebFetch" in claude[claude.index("--tools") + 1]

    claude_write = worker_command(
        _request(tmp_path, "claude", access_mode="write"),
        session_id="sid-2",
    )
    assert "--dangerously-skip-permissions" in claude_write
    assert "Edit" not in claude_write[claude_write.index("--disallowedTools") + 1]


def test_read_legs_get_the_read_only_account_gateway_and_writers_do_not(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SERENA_FLEET_CODEX_BIN", "/opt/bin/codex")
    monkeypatch.setenv("SERENA_FLEET_CLAUDE_BIN", "/opt/bin/claude")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(state))
    monkeypatch.setenv("SERENA_FLEET_READ_MCP_SERVERS", "google-ads")
    monkeypatch.setenv("SERENA_FLEET_READ_MCP_COMMAND", "/opt/bin/chats fleet read-mcp")
    (state / "mcp-read-catalog.json").write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": time.time(),
                "servers": {},
                "tools": [
                    {
                        "name": "read_google_ads_search_search",
                        "server": "google-ads",
                        "tool": "search_search",
                        "description": "",
                        "input_schema": {"type": "object"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    claude = worker_command(
        _request(tmp_path, "claude", phase="discover"), session_id="research"
    )
    config = json.loads(claude[claude.index("--mcp-config") + 1])
    assert list(config["mcpServers"]) == ["serena_read"]
    # --safe-mode would disable MCP outright, so a granted leg swaps it for the
    # narrower isolation that still keeps hooks, plugins, and user settings out.
    assert "--safe-mode" not in claude
    assert claude[claude.index("--setting-sources") + 1] == ""
    assert (
        claude[claude.index("--allowedTools") + 1]
        == "mcp__serena_read__read_google_ads_search_search"
    )
    assert "--strict-mcp-config" in claude

    review = worker_command(
        _request(tmp_path, "claude", access_mode="review", phase="verify"),
        session_id="review",
    )
    assert "--allowedTools" in review

    writer = worker_command(
        _request(tmp_path, "claude", access_mode="write"), session_id="writer"
    )
    assert writer[writer.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--allowedTools" not in writer
    assert "--safe-mode" in writer
    assert "--setting-sources" not in writer

    codex = worker_command(_request(tmp_path, "codex", phase="discover"))
    assert 'mcp_servers.serena_read.command="/opt/bin/chats"' in codex
    assert (
        'mcp_servers.serena_read.enabled_tools=["read_google_ads_search_search"]' in codex
    )
    codex_writer = worker_command(_request(tmp_path, "codex", access_mode="write"))
    assert not any("mcp_servers" in value for value in codex_writer)


def test_coding_research_phase_enables_native_web_tools_for_both_providers(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SERENA_FLEET_CODEX_BIN", "/opt/bin/codex")
    monkeypatch.setenv("SERENA_FLEET_CLAUDE_BIN", "/opt/bin/claude")

    codex = worker_command(
        _request(tmp_path, "codex", activity="coding", phase="discover")
    )
    assert codex[codex.index("--enable") + 1] == "standalone_web_search"

    claude = worker_command(
        _request(tmp_path, "claude", activity="coding", phase="discover"),
        session_id="research-session",
    )
    tools = claude[claude.index("--tools") + 1]
    assert "WebSearch" in tools and "WebFetch" in tools

    review = worker_command(
        _request(tmp_path, "claude", activity="coding", phase="verify"),
        session_id="review-session",
    )
    review_tools = review[review.index("--tools") + 1]
    assert "WebSearch" not in review_tools and "WebFetch" not in review_tools


def test_codex_resume_places_subcommand_after_enforced_outer_options(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_FLEET_CODEX_BIN", "/opt/bin/codex")
    request = _request(tmp_path, "codex")
    request = WorkerRequest(
        **{
            field: getattr(request, field)
            for field in request.__dataclass_fields__
            if field != "resume_session_id"
        },
        resume_session_id="codex-session-1",
    )
    command = worker_command(request)
    assert command[-3:] == ["resume", "codex-session-1", "-"]
    assert command.index("--sandbox") < command.index("resume")
    assert command[command.index("-m") + 1] == "gpt-5.6-sol"
    assert 'model_reasoning_effort="xhigh"' in command


def test_claude_resume_keeps_phase_model_and_effort_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_FLEET_CLAUDE_BIN", "/opt/bin/claude")
    request = _request(tmp_path, "claude")
    request = WorkerRequest(
        **{
            field: getattr(request, field)
            for field in request.__dataclass_fields__
            if field not in {"model", "effort", "resume_session_id"}
        },
        model="claude-haiku-4-5",
        effort="high",
        resume_session_id="claude-session-1",
    )
    command = worker_command(request)
    assert command[command.index("--model") + 1] == "claude-haiku-4-5"
    assert command[command.index("--effort") + 1] == "high"
    assert command[-2:] == ["--resume", "claude-session-1"]


def test_direct_codex_and_claude_stream_parsers_report_real_identity(
    tmp_path,
    monkeypatch,
):
    codex_bin = _executable(
        tmp_path / "fake-codex",
        """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"type":"thread.started","thread_id":"11111111-1111-1111-1111-111111111111"}), flush=True)
print(json.dumps({"type":"thread.settings","settings":{"model":"gpt-5.6-sol","reasoning_effort":"xhigh"}}), flush=True)
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"codex answer"}}), flush=True)
""",
    )
    claude_bin = _executable(
        tmp_path / "fake-claude",
        """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
args = sys.argv
sid = args[args.index("--session-id") + 1]
print(json.dumps({"type":"system","subtype":"init","session_id":sid,"model":"claude-opus-5"}), flush=True)
print(json.dumps({"type":"assistant","effort":"xhigh","message":{"model":"claude-opus-5","content":[{"type":"text","text":"claude answer"}]}}), flush=True)
print(json.dumps({"type":"result","session_id":sid,"result":"claude answer","is_error":False}), flush=True)
""",
    )
    monkeypatch.setenv("SERENA_FLEET_CODEX_BIN", str(codex_bin))
    monkeypatch.setenv("SERENA_FLEET_CLAUDE_BIN", str(claude_bin))
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(tmp_path / "state"))

    codex_events: list[tuple[str, dict]] = []
    codex = run_worker(
        _request(tmp_path, "codex"),
        cancel_requested=lambda: False,
        on_event=lambda event, payload: codex_events.append((event, payload)),
    )
    assert codex.ok is True
    assert codex.output_text == "codex answer"
    assert codex.actual_model == "gpt-5.6-sol"
    assert codex.actual_effort == "xhigh"
    assert Path(codex.event_log_path).is_file()
    assert any(
        payload.get("effort") == "xhigh"
        for event, payload in codex_events
        if event == "worker.event"
    )

    claude_events: list[tuple[str, dict]] = []
    claude = run_worker(
        _request(tmp_path, "claude"),
        cancel_requested=lambda: False,
        on_event=lambda event, payload: claude_events.append((event, payload)),
    )
    assert claude.ok is True
    assert claude.output_text == "claude answer"
    assert claude.actual_model == "claude-opus-5"
    assert sum(event == "session.started" for event, _ in claude_events) == 1
    assert any(
        payload.get("effort") == "xhigh"
        for event, payload in claude_events
        if event == "worker.event"
    )


def test_claude_receives_prompt_before_slow_session_surface_callback(
    tmp_path,
    monkeypatch,
):
    claude_bin = _executable(
        tmp_path / "prompt-deadline-claude",
        """#!/usr/bin/env python3
import json, select, sys
if not select.select([sys.stdin], [], [], 0.25)[0]:
    print("prompt was not delivered before the deadline", file=sys.stderr)
    raise SystemExit(9)
prompt = sys.stdin.read()
if "do the controlled test" not in prompt:
    print("wrong prompt", file=sys.stderr)
    raise SystemExit(10)
args = sys.argv
sid = args[args.index("--session-id") + 1]
print(json.dumps({"type":"system","subtype":"init","session_id":sid,"model":"claude-opus-5"}), flush=True)
print(json.dumps({"type":"assistant","effort":"xhigh","message":{"model":"claude-opus-5","content":[{"type":"text","text":"prompt arrived"}]}}), flush=True)
print(json.dumps({"type":"result","session_id":sid,"result":"prompt arrived","is_error":False}), flush=True)
""",
    )
    monkeypatch.setenv("SERENA_FLEET_CLAUDE_BIN", str(claude_bin))
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(tmp_path / "state"))

    def slow_surface(event, _payload):
        if event == "process.started":
            time.sleep(0.6)

    result = run_worker(
        _request(tmp_path, "claude"),
        cancel_requested=lambda: False,
        on_event=slow_surface,
    )

    assert result.ok is True
    assert result.output_text == "prompt arrived"


def test_worker_cancellation_terminates_the_direct_process_group(tmp_path, monkeypatch):
    codex_bin = _executable(
        tmp_path / "slow-codex",
        """#!/usr/bin/env python3
import json, sys, time
sys.stdin.read()
print(json.dumps({"type":"thread.started","thread_id":"22222222-2222-2222-2222-222222222222"}), flush=True)
print(json.dumps({"type":"thread.settings","settings":{"model":"gpt-5.6-sol","reasoning_effort":"xhigh"}}), flush=True)
time.sleep(30)
""",
    )
    monkeypatch.setenv("SERENA_FLEET_CODEX_BIN", str(codex_bin))
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(tmp_path / "state"))
    started = time.monotonic()
    result = run_worker(
        _request(tmp_path, "codex"),
        cancel_requested=lambda: time.monotonic() - started > 0.2,
        on_event=lambda _event, _payload: None,
    )
    assert result.ok is False
    assert result.cancelled is True
    assert time.monotonic() - started < 5


def test_worker_exit_is_not_pinned_by_a_descendant_inheriting_output_pipes(
    tmp_path, monkeypatch
):
    codex_bin = _executable(
        tmp_path / "leaky-pipe-codex",
        """#!/usr/bin/env python3
import json, subprocess, sys
sys.stdin.read()
subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdout=sys.stdout,
    stderr=sys.stderr,
)
print(json.dumps({"type":"thread.started","thread_id":"33333333-3333-3333-3333-333333333333"}), flush=True)
print(json.dumps({"type":"thread.settings","settings":{"model":"gpt-5.6-sol","reasoning_effort":"xhigh"}}), flush=True)
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}), flush=True)
""",
    )
    monkeypatch.setenv("SERENA_FLEET_CODEX_BIN", str(codex_bin))
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SERENA_FLEET_EXIT_DRAIN_SECONDS", "0.1")

    started = time.monotonic()
    result = run_worker(
        _request(tmp_path, "codex"),
        cancel_requested=lambda: False,
        on_event=lambda _event, _payload: None,
    )

    assert result.ok is True
    assert result.output_text == "done"
    assert time.monotonic() - started < 3


def test_codex_rollout_identity_reads_actual_effort_field(tmp_path, monkeypatch):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        '{"type":"turn_context","payload":{"model":"gpt-5.6-sol","effort":"xhigh"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("core.codex_bridge.find_codex_jsonl", lambda _sid: rollout)
    assert _codex_actual_identity("session-1") == ("gpt-5.6-sol", "xhigh")


def test_claude_rollout_identity_reads_model_and_actual_effort(tmp_path, monkeypatch):
    rollout = tmp_path / "claude.jsonl"
    rollout.write_text(
        '{"type":"assistant","effort":"xhigh","message":{"model":"claude-opus-5"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("core.claude_bridge.find_claude_jsonl", lambda _sid: rollout)
    assert _claude_actual_identity("session-1") == ("claude-opus-5", "xhigh")


def test_claude_rollout_identity_does_not_carry_effort_across_model_switch(
    tmp_path,
    monkeypatch,
):
    rollout = tmp_path / "claude.jsonl"
    rollout.write_text(
        '{"type":"assistant","effort":"xhigh","message":{"model":"claude-opus-5"}}\n'
        '{"type":"assistant","message":{"model":"claude-haiku-4-5"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("core.claude_bridge.find_claude_jsonl", lambda _sid: rollout)

    assert _claude_actual_identity("session-1") == ("claude-haiku-4-5", None)


def test_claude_uses_invocation_effort_when_current_model_omits_telemetry(
    tmp_path,
    monkeypatch,
):
    claude_bin = _executable(
        tmp_path / "fake-claude",
        """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
args = sys.argv
sid = args[args.index("--session-id") + 1]
print(json.dumps({"type":"system","subtype":"init","session_id":sid,"model":"claude-haiku-4-5"}), flush=True)
print(json.dumps({"type":"assistant","message":{"model":"claude-haiku-4-5","content":[{"type":"text","text":"reviewed"}]}}), flush=True)
print(json.dumps({"type":"result","session_id":sid,"result":"reviewed","is_error":False}), flush=True)
""",
    )
    monkeypatch.setenv("SERENA_FLEET_CLAUDE_BIN", str(claude_bin))
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        "core.fleet_workers._claude_actual_identity",
        lambda _sid, _cwd=None: ("claude-haiku-4-5", None),
    )
    base = _request(tmp_path, "claude")
    request = WorkerRequest(
        **{
            field: getattr(base, field)
            for field in base.__dataclass_fields__
            if field not in {"model", "effort"}
        },
        model="claude-haiku-4-5",
        effort="high",
    )

    result = run_worker(
        request,
        cancel_requested=lambda: False,
        on_event=lambda _event, _payload: None,
    )

    assert result.ok is True
    assert result.actual_model == "claude-haiku-4-5"
    assert result.actual_effort == "high"


def test_synthetic_claude_error_never_overwrites_real_model_identity(tmp_path, monkeypatch):
    rollout = tmp_path / "claude.jsonl"
    rollout.write_text(
        '{"type":"assistant","effort":"xhigh","message":{"model":"claude-opus-5"}}\n'
        '{"type":"assistant","message":{"model":"<synthetic>"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("core.claude_bridge.find_claude_jsonl", lambda _sid: rollout)

    assert _claude_actual_identity("session-1") == ("claude-opus-5", "xhigh")
    assert "model" not in _event_summary(
        {"type": "assistant", "message": {"model": "<synthetic>"}}
    )


def test_callback_failure_always_terminates_and_reaps_worker_group(tmp_path, monkeypatch):
    codex_bin = _executable(
        tmp_path / "blocked-codex",
        """#!/usr/bin/env python3
import sys, time
sys.stdin.read()
time.sleep(30)
""",
    )
    monkeypatch.setenv("SERENA_FLEET_CODEX_BIN", str(codex_bin))
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(tmp_path / "state"))
    pids: list[int] = []

    def explode(event, payload):
        if event == "process.started":
            pids.append(int(payload["pid"]))
            raise RuntimeError("controlled callback failure")

    with pytest.raises(RuntimeError, match="controlled callback failure"):
        run_worker(
            _request(tmp_path, "codex"),
            cancel_requested=lambda: False,
            on_event=explode,
        )
    assert len(pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)
