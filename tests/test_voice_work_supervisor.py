from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import time

import pytest

from core import voice_work_supervisor as supervisor_module
from core.voice_inbox import VoiceInboxStore
from core.voice_work_supervisor import (
    VoiceWorkSupervisor,
    resident_worker_available,
    resolve_work_cwd,
    verify_claude_subscription,
    verify_codex_subscription,
)


def test_resident_marker_requires_live_fresh_process(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "worker.json"
    marker.write_text(
        json.dumps({"pid": os.getpid(), "heartbeat": time.time()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(supervisor_module, "_pid_alive", lambda pid: pid == os.getpid())

    assert resident_worker_available(marker)
    assert not resident_worker_available(marker, now=time.time() + 10)


def test_work_directory_prefers_named_project(tmp_path, monkeypatch) -> None:
    projects = tmp_path / "Projects"
    serena = projects / "serena"
    locket = projects / "personal_projects" / "locket"
    serena.mkdir(parents=True)
    locket.mkdir(parents=True)
    monkeypatch.setattr(supervisor_module, "HOME", tmp_path)
    monkeypatch.setattr(supervisor_module, "PROJECTS_ROOT", projects)
    monkeypatch.setattr(supervisor_module, "SERENA_ROOT", serena)
    monkeypatch.setattr(supervisor_module, "_project_roots", lambda: [serena, locket])

    assert resolve_work_cwd("fix the notification bug in locket") == locket
    assert resolve_work_cwd("tighten the wake word listener") == serena


def test_subscription_guard_strips_metered_environment(monkeypatch) -> None:
    metered = {"OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"}
    for name in metered:
        monkeypatch.delenv(name, raising=False)
    captured: dict[str, object] = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT\n", "")

    verify_codex_subscription(codex_bin="/fake/codex", runner=runner)

    assert captured["command"] == ["/fake/codex", "login", "status"]
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert not metered.intersection(environment)


def test_subscription_guard_rejects_metered_auth(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")

    with pytest.raises(RuntimeError, match="metered provider authentication"):
        verify_codex_subscription(codex_bin="/fake/codex")


def test_claude_subscription_guard_uses_first_party_oauth(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    captured: dict[str, object] = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                }
            ),
            "",
        )

    verify_claude_subscription(claude_bin="/fake/claude", runner=runner)

    assert captured["command"] == ["/fake/claude", "auth", "status"]
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert "ANTHROPIC_API_KEY" not in environment


def test_resident_worker_runs_codex_and_persists_result(tmp_path, monkeypatch) -> None:
    database = tmp_path / "voice.sqlite3"
    store = VoiceInboxStore(database)
    queued = store.enqueue(
        "fix the voice response",
        call_id="call-worker",
        turn_id="call-worker:1",
    )
    target = "headless-voice-test"
    item = store.claim_next(target)
    assert item is not None

    session_id = "019f7b19-2f0d-7eb3-8270-6a6ed90b5dcc"
    events = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": session_id}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "fixed and tested"},
                }
            ),
            json.dumps({"type": "turn.completed"}),
        )
    ) + "\n"
    captured: dict[str, object] = {"commands": [], "processes": []}

    class CapturingStdin(io.StringIO):
        def close(self) -> None:
            self.flush()

    class FakeCodexProcess:
        def __init__(self) -> None:
            self.stdin = CapturingStdin()
            self.stdout = io.StringIO(events)
            self.pid = os.getpid()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    class FakeClaudeProcess:
        def __init__(self) -> None:
            self.pid = os.getpid()
            self.returncode = None
            self.stdin = None
            self.stdout = None
            self.stderr = None

        def poll(self):
            return self.returncode

        def communicate(self, prompt, timeout=None):
            assert "second private coding runtime" in prompt
            assert "fixed and tested" in prompt
            self.returncode = 0
            return (
                json.dumps(
                    {
                        "is_error": False,
                        "result": "reviewed, tightened, and all tests pass",
                        "session_id": "22222222-2222-4222-8222-222222222222",
                    }
                ),
                "",
            )

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

    real_popen = subprocess.Popen

    def fake_popen(command, **kwargs):
        if command[0] not in {"/fake/codex", "/fake/claude"}:
            return real_popen(command, **kwargs)
        captured["commands"].append(command)
        captured["environment"] = kwargs["env"]
        process = (
            FakeCodexProcess()
            if command[0] == "/fake/codex"
            else FakeClaudeProcess()
        )
        captured["processes"].append(process)
        return process

    titles: list[tuple[str, str]] = []
    residents: list[str] = []
    linked: list[list[str]] = []
    notices: list[str] = []
    overlay_events: list[dict] = []
    monkeypatch.setattr(supervisor_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(supervisor_module, "_session_ids", lambda: set())
    monkeypatch.setattr(
        "core.metadata.set_custom_title",
        lambda sid, title: titles.append((sid, title)),
    )
    monkeypatch.setattr(
        "core.metadata.set_resident_work",
        lambda sid: residents.append(sid),
    )
    monkeypatch.setattr(
        "core.metadata.link_sessions",
        lambda sids: linked.append(sids) or "group-test",
    )

    supervisor = VoiceWorkSupervisor(
        store=store,
        marker_path=tmp_path / "worker.json",
        notifier=notices.append,
        overlay_sender=overlay_events.append,
        indexer=lambda: None,
        codex_bin="/fake/codex",
        claude_bin="/fake/claude",
    )
    supervisor._run_item(item, target)

    process = captured["processes"][0]
    assert isinstance(process, FakeCodexProcess), captured["commands"]
    prompt = process.stdin.getvalue()
    assert "fix the voice response" in prompt
    assert "Persona.md" in prompt
    assert captured["commands"][0][-1] == "-"
    assert captured["commands"][1][0] == "/fake/claude"
    assert titles and titles[0][0] == session_id
    assert residents == [session_id, "22222222-2222-4222-8222-222222222222"]
    assert linked == [[session_id, "22222222-2222-4222-8222-222222222222"]]
    assert notices == ["done. reviewed, tightened, and all tests pass"]
    assert overlay_events[0] == {
        "type": "code_start",
        "project": "serena",
        "item_id": queued.item_id,
    }
    assert {event["type"] for event in overlay_events} == {
        "code_start",
        "code_event",
        "code_done",
    }
    assert overlay_events[-1] == {
        "type": "code_done",
        "summary": "reviewed, tightened, and all tests pass",
    }
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT state, session_id, summary FROM voice_work WHERE item_id=?",
            (queued.item_id,),
        ).fetchone()
    assert row == ("completed", session_id, "reviewed, tightened, and all tests pass")
