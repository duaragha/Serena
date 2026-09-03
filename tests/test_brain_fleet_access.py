"""Regressions for voice Serena's bounded access to the Fleet tab."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from mcp import types

from core import brain_daemon, brain_fleet_tools, fleet_supervisor
from core.fleet_workers import WorkerResult


def _summary(
    run_id: str,
    *,
    state: str,
    task: str = "improve Fleet access",
    cwd: str = "/tmp/serena",
) -> dict:
    return {
        "run_id": run_id,
        "state": state,
        "task": task,
        "cwd": cwd,
        "activity": "coding",
        "current_phase": "finalize",
        "current_phase_display": "Fix",
        "progress": {"completed": 7, "total": 8},
        "dry_run": False,
        "error": None,
        "completed_at": None,
    }


def _full_run(run_id: str, *, state: str = "running") -> dict:
    run = _summary(run_id, state=state)
    run["policy"] = {
        "requested_provider_mode": "auto",
        "provider_mode": "codex",
        "scaling": {"reason": "Claude usage was confirmed exhausted"},
    }
    run["phases"] = [
        {
            "name": "finalize",
            "display_name": "Fix",
            "legs": [
                {
                    "worker_key": "codex:0",
                    "worker_label": "Codex 1",
                    "assignment": "parser ownership",
                    "assignment_ids": ["parser"],
                    "review_target_ids": ["integration"],
                    "role": "functional-fixer",
                    "provider": "codex",
                    "model": "gpt-5.6-sol",
                    "state": state,
                    "current_attempt": {
                        "actual_model": "gpt-5.6-sol",
                        "actual_effort": "xhigh",
                        "output_text": "private worker transcript",
                        "event_log_path": "/private/events.jsonl",
                    },
                }
            ],
        }
    ]
    run["result_text"] = "bounded result"
    return run


def test_empty_fleet_reference_prefers_the_one_active_run() -> None:
    """A completed history must not make 'how is Fleet going' ambiguous."""

    active = _summary("aaaaaaaa-1111", state="running")
    old = _summary("bbbbbbbb-2222", state="completed")

    assert brain_fleet_tools.resolve_fleet_run("", runs=[old, active])["run_id"] == active["run_id"]


def test_two_active_fleet_runs_require_one_short_disambiguation() -> None:
    """Serena must not cancel or describe the wrong simultaneous Fleet run."""

    first = _summary("aaaaaaaa-1111", state="running", cwd="/tmp/serena")
    second = _summary("bbbbbbbb-2222", state="queued", cwd="/tmp/locket")

    try:
        brain_fleet_tools.resolve_fleet_run("that one", action="cancel", runs=[first, second])
    except brain_fleet_tools.FleetResolutionError as error:
        assert "which Fleet run" in str(error)
        assert "serena" in str(error)
        assert "locket" in str(error)
    else:
        raise AssertionError("ambiguous Fleet control selected a run")


def test_fleet_status_exposes_actual_identity_without_worker_transcripts(monkeypatch) -> None:
    """Reading Fleet must not pour private worker output into the resident context."""

    summary = _summary("aaaaaaaa-1111", state="running")
    full = _full_run(summary["run_id"])
    monkeypatch.setattr(brain_fleet_tools, "list_runs", lambda limit=50: [summary])
    monkeypatch.setattr(brain_fleet_tools, "get_run", lambda _run_id: full)

    status = brain_fleet_tools.fleet_status("")
    encoded = json.dumps(status)

    assert status["workers"][0]["actual_model"] == "gpt-5.6-sol"
    assert status["workers"][0]["actual_effort"] == "xhigh"
    assert status["workers"][0]["worker_key"] == "codex:0"
    assert status["workers"][0]["worker_label"] == "Codex 1"
    assert status["workers"][0]["assignment"] == "parser ownership"
    assert status["workers"][0]["assignment_ids"] == ["parser"]
    assert status["workers"][0]["review_target_ids"] == ["integration"]
    assert status["step_progress"] == {
        "completed_steps": 7,
        "total_steps": 8,
    }
    assert status["phase_progress"] == {
        "completed_phases": 0,
        "total_phases": 1,
    }
    assert status["provider_routing"] == {
        "requested_mode": "auto",
        "selected_mode": "codex",
        "provider_counts": {"codex": 1, "claude": 0},
        "reason": "Claude usage was confirmed exhausted",
    }
    assert "progress" not in status
    assert "private worker transcript" not in encoded
    assert "/private/events.jsonl" not in encoded


def test_fleet_control_requires_the_live_spoken_turn(monkeypatch) -> None:
    """A stale model intention must never cancel the run in the Fleet tab."""

    called = []
    summary = _summary("aaaaaaaa-1111", state="running")
    monkeypatch.setattr(brain_fleet_tools, "list_runs", lambda limit=50: [summary])
    monkeypatch.setattr(brain_fleet_tools, "stop_run", lambda run_id: called.append(run_id))

    refused = brain_fleet_tools.control_fleet_run(
        "cancel",
        origin={"protocol": "plain", "text": "cancel Fleet"},
    )

    assert refused["ok"] is False
    assert refused["changed"] is False
    assert called == []


def test_explicit_spoken_cancel_changes_only_the_resolved_fleet_run(monkeypatch) -> None:
    """A direct 'cancel Fleet' request must reach the durable supervisor once."""

    summary = _summary("aaaaaaaa-1111", state="running")
    stopped = {**summary, "state": "stopping"}
    called = []
    monkeypatch.setattr(brain_fleet_tools, "list_runs", lambda limit=50: [summary])
    monkeypatch.setattr(
        brain_fleet_tools,
        "stop_run",
        lambda run_id: called.append(run_id) or stopped,
    )

    result = brain_fleet_tools.control_fleet_run(
        "cancel",
        origin={"protocol": "voice", "text": "cancel that Fleet run"},
    )

    assert result["ok"] is True
    assert result["run"]["state"] == "stopping"
    assert called == [summary["run_id"]]


def test_ordinary_coding_words_cannot_start_fleet(monkeypatch, tmp_path: Path) -> None:
    """Fleet must remain explicit instead of replacing Serena's normal coding path."""

    called = []
    monkeypatch.setattr(brain_fleet_tools, "start_run", lambda *_args, **_kwargs: called.append(True))

    result = brain_fleet_tools.start_fleet(
        "fix transcript search",
        activity="coding",
        project="serena",
        origin={"protocol": "voice", "text": "fix transcript search in Serena"},
    )

    assert result["started"] is False
    assert "name Fleet" in result["error"]
    assert called == []


def test_any_phrasing_that_names_fleet_starts_it(monkeypatch, tmp_path: Path) -> None:
    """2026-08-12: "create a fleet to look into this and fix it" was refused
    because "create" was not on a four-verb whitelist, and she told him to
    rephrase. Naming Fleet is the contract; the verb is his business."""

    root = tmp_path / "serena"
    root.mkdir()
    monkeypatch.setattr(
        brain_fleet_tools, "resolve_repository_root", lambda *_args, **_kwargs: root
    )
    monkeypatch.setattr(
        brain_fleet_tools,
        "start_run",
        lambda *_args, **_kwargs: _summary("aaaaaaaa-1111", state="queued", cwd=str(root)),
    )

    monkeypatch.setattr(
        brain_fleet_tools,
        "recent_turn_texts",
        lambda **_kwargs: ["i tried clicking open live terminal but it didn't work"],
    )
    for phrasing in (
        "create a fleet to look into this and fix it",
        "spin up a fleet for the live terminal button",
        "fleet this one",
    ):
        result = brain_fleet_tools.start_fleet(
            "fix the live terminal button",
            activity="coding",
            project="serena",
            origin={"protocol": "voice", "text": phrasing},
        )
        assert result["started"] is True, phrasing


def test_explicit_fleet_start_resolves_one_real_repository(monkeypatch, tmp_path: Path) -> None:
    """A spoken Fleet launch must never inherit the brain cache as its cwd."""

    root = tmp_path / "serena"
    root.mkdir()
    started = _summary("aaaaaaaa-1111", state="queued", cwd=str(root))
    captured = {}
    monkeypatch.setattr(brain_fleet_tools, "resolve_repository_root", lambda *_args, **_kwargs: root)

    def fake_start(task, **kwargs):
        captured.update({"task": task, **kwargs})
        return started

    monkeypatch.setattr(brain_fleet_tools, "start_run", fake_start)
    result = brain_fleet_tools.start_fleet(
        "fix transcript search",
        activity="coding",
        project="serena",
        origin={"protocol": "voice", "text": "run Fleet to fix transcript search in Serena"},
    )

    assert result["started"] is True
    assert captured["cwd"] == str(root)
    assert captured["origin_agent"] == "serena"
    assert captured["provider_mode"] == "auto"
    assert captured["worker_count"] is None


def test_explicit_voice_provider_and_worker_constraints_are_forwarded(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "serena"
    root.mkdir()
    captured = {}
    monkeypatch.setattr(brain_fleet_tools, "resolve_repository_root", lambda *_args, **_kwargs: root)
    monkeypatch.setattr(
        brain_fleet_tools,
        "start_run",
        lambda task, **kwargs: captured.update({"task": task, **kwargs})
        or _summary("aaaaaaaa-1111", state="queued", cwd=str(root)),
    )

    result = brain_fleet_tools.start_fleet(
        "research the provider routing",
        activity="research",
        project="serena",
        origin={"protocol": "voice", "text": "start Fleet to research provider routing"},
        provider_mode="claude",
        worker_count=1,
    )

    assert result["started"] is True
    assert captured["provider_mode"] == "claude"
    assert captured["worker_count"] == 1


def test_resident_brain_mounts_only_the_bounded_fleet_surface(monkeypatch, tmp_path: Path) -> None:
    """Fleet access must not expose worker process or filesystem tools to Serena."""

    class CapturedOptions:
        def __init__(self, **values):
            self.__dict__.update(values)

    monkeypatch.setattr(brain_daemon, "_persona_context", lambda: "persona")
    monkeypatch.setattr(brain_daemon, "BRAIN_SYSTEM_PROMPT_FILE", tmp_path / "prompt.md")
    server = brain_fleet_tools.fleet_tools_server()
    response = asyncio.run(
        server["instance"].request_handlers[types.ListToolsRequest](
            types.ListToolsRequest(method="tools/list")
        )
    )
    options = brain_daemon._build_agent_options(
        CapturedOptions,
        {},
        [],
        fleet_tools=server,
        fleet_tool_names=brain_fleet_tools.FLEET_TOOL_NAMES,
    )

    assert [tool.name for tool in response.root.tools] == [
        "fleet_run_status",
        "control_fleet_run",
        "start_fleet_run",
    ]
    start_tool = next(tool for tool in response.root.tools if tool.name == "start_fleet_run")
    assert "provider_mode" in start_tool.inputSchema["properties"]
    assert "worker_count" in start_tool.inputSchema["properties"]
    assert "provider_mode" not in start_tool.inputSchema["required"]
    assert "worker_count" not in start_tool.inputSchema["required"]
    assert set(options.mcp_servers) == {"serena-ro", "serena-fleet"}
    assert options.allowed_tools == brain_fleet_tools.FLEET_TOOL_NAMES


def test_voice_fleet_status_must_override_recalled_conversation(monkeypatch) -> None:
    """A prior wrong answer must not replace the durable Fleet tab state."""

    monkeypatch.setattr(brain_daemon, "_state_block", lambda: "")
    monkeypatch.setattr(
        brain_daemon,
        "_recalled_voice_history_block",
        lambda _text: '<recalled-serena-history>{"text":"eight phases"}</recalled-serena-history>',
    )

    message = brain_daemon._compose_message(
        {
            "protocol": "voice",
            "text": "how many phases finished in Fleet 12503b47?",
            "call_id": "desk-test",
            "turn_id": "desk-test:1",
        }
    )

    assert "recalled Fleet answer is stale" in message
    assert "call fleet_run_status" in message
    assert "repeat that tool's spoken field exactly" in message


def test_completed_fleet_run_sends_one_bounded_alert_and_records_delivery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A finished Fleet run must alert once without leaking its worker output."""

    database = tmp_path / "fleet.sqlite3"
    monkeypatch.setenv("SERENA_FLEET_DB_PATH", str(database))
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SERENA_FLEET_NO_AUTOSTART", "1")
    monkeypatch.setenv("SERENA_FLEET_ISOLATION", "off")
    monkeypatch.setenv(
        "SERENA_FLEET_CAPACITY_JSON",
        json.dumps(
            {
                "codex": {"status": "available"},
                "claude": {"status": "available"},
            }
        ),
    )
    monkeypatch.delenv("SERENA_FLEET_INLINE", raising=False)
    monkeypatch.setattr(fleet_supervisor, "_STORE_INSTANCE", None)
    monkeypatch.setattr(fleet_supervisor, "_surface_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(fleet_supervisor, "_release_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(fleet_supervisor, "_refresh_index", lambda: None)
    # These tests cover Fleet's terminal alert path with fake workers that
    # return bare transcripts. The completion-contract gate has its own suites
    # (tests/test_fleet_completion.py, tests/test_fleet_completion_gate.py).
    monkeypatch.setattr(
        fleet_supervisor, "_completion_verdict", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        fleet_supervisor,
        "_send_spoken_notice",
        lambda _run, _token: False,
    )
    messages = []
    monkeypatch.setattr(
        fleet_supervisor,
        "_send_raghav_text",
        lambda message: messages.append(message) or True,
    )

    def successful(request, *, cancel_requested, on_event):
        assert cancel_requested() is False
        sid = f"session-{request.leg_id}"
        on_event("process.started", {"pid": os.getpid(), "event_log_path": "/private/event"})
        on_event("session.started", {"session_id": sid})
        return WorkerResult(
            True,
            "private worker transcript",
            sid,
            request.model,
            request.effort,
            0,
        )

    monkeypatch.setattr(fleet_supervisor, "run_worker", successful)
    run = fleet_supervisor.start_run(
        "implement a harmless alert marker",
        activity="coding",
        cwd=str(tmp_path),
    )
    completed = fleet_supervisor.run_supervisor(run["run_id"])
    fleet_supervisor._terminal_outcome(fleet_supervisor._store(), completed)

    assert completed["state"] == "completed"
    assert len(messages) == 1
    assert "finished" in messages[0]
    # A plain task with no explicit workstreams is one agent across four phases.
    assert "4/4 agent steps" in messages[0]
    assert "private worker transcript" not in messages[0]
    events = fleet_supervisor._store().events(run["run_id"], limit=2_000)
    assert len([event for event in events if event["type"] == "run.notification.delivered"]) == 1


def test_fleet_terminal_alert_prefers_local_speech_over_telegram(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A live laptop must hear Serena instead of receiving the default phone ping."""

    database = tmp_path / "fleet.sqlite3"
    monkeypatch.setenv("SERENA_FLEET_DB_PATH", str(database))
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SERENA_FLEET_NO_AUTOSTART", "1")
    monkeypatch.setattr(fleet_supervisor, "_STORE_INSTANCE", None)
    spoken = []
    texts = []
    monkeypatch.setattr(
        fleet_supervisor,
        "_send_spoken_notice",
        lambda run, token: spoken.append((run["run_id"], token)) or True,
    )
    monkeypatch.setattr(
        fleet_supervisor,
        "_send_raghav_text",
        lambda message: texts.append(message) or True,
    )
    run = fleet_supervisor.start_run(
        "exercise the spoken terminal alert",
        activity="coding",
        cwd=str(tmp_path),
    )
    store = fleet_supervisor._store()
    failed = store.fail_run(run["run_id"], "safe test failure")

    fleet_supervisor._terminal_outcome(store, failed)
    fleet_supervisor._terminal_outcome(store, failed)

    assert len(spoken) == 1
    assert texts == []
    events = store.events(run["run_id"], limit=2_000)
    voice_deliveries = [
        event
        for event in events
        if event["type"] == "run.notification.delivered"
        and event["payload"]["channel"] == "voice"
    ]
    assert len(voice_deliveries) == 1
