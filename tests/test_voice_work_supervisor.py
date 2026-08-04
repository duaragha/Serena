from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import threading
import time

import pytest

from core import voice_work_supervisor as supervisor_module
from core.coding_job_contract import CodingJobBrief, capture_git_snapshot
from core.voice_inbox import VoiceInboxStore
from core.voice_work_supervisor import (
    VoiceWorkSupervisor,
    resident_worker_available,
    verify_claude_subscription,
    verify_codex_subscription,
)


def _codex_available():
    """State the capacity a test is exercising rather than inheriting this
    laptop's live accounts, which on 2026-08-04 had Codex fully rate-limited."""
    from core.coding_provider import choose_providers

    healthy = {
        name: {"provider": name, "status": "available", "usable": True,
               "reason": "below limit"}
        for name in ("codex", "claude")
    }
    return lambda: choose_providers(healthy)


def test_resident_marker_requires_live_fresh_process(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "worker.json"
    marker.write_text(
        json.dumps({"pid": os.getpid(), "heartbeat": time.time()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(supervisor_module, "_pid_alive", lambda pid: pid == os.getpid())

    assert resident_worker_available(marker)
    assert not resident_worker_available(marker, now=time.time() + 10)


def _accepted_item(
    store,
    repo,
    *,
    item_id="accepted-worker-job",
    work_route=None,
    call_id="call-worker",
    turn_id="call-worker:1",
):
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Serena Test",
            "-c",
            "user.email=serena@example.test",
            "commit",
            "-qm",
            "baseline",
        ],
        check=True,
    )
    baseline = capture_git_snapshot(repo, item_id=item_id, label="baseline")
    brief = CodingJobBrief.create(
        item_id=item_id,
        exact_request="update the README",
        triggering_request=f"update the README in {repo}",
        project_root=repo,
        initial_git=baseline,
        work_route=work_route,
    )
    return store.enqueue_accepted(brief.to_dict(), call_id=call_id, turn_id=turn_id)


def _append_rollout(path, *events) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def test_subscription_guard_strips_metered_environment(monkeypatch) -> None:
    metered = {"OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"}
    for name in list(os.environ):
        if name.startswith(("OPENAI_", "CODEX_", "ANTHROPIC_", "CLAUDE_CODE_")):
            monkeypatch.delenv(name, raising=False)
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
    for name in list(os.environ):
        if name.startswith(("ANTHROPIC_", "CLAUDE_CODE_")):
            monkeypatch.delenv(name, raising=False)
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


def test_persisted_resume_uses_the_same_codex_session_and_pinned_identity(tmp_path) -> None:
    command = VoiceWorkSupervisor._codex_command(
        "/fake/codex",
        tmp_path,
        resume_session_id="persisted-session-id",
    )

    assert command[-3:] == ["resume", "persisted-session-id", "-"]
    assert "-C" not in command
    assert "--skip-git-repo-check" not in command
    assert command[command.index("-m") + 1] == "gpt-5.6-sol"
    assert 'model_reasoning_effort="xhigh"' in command


def test_isolated_supervisor_store_never_broadcasts_into_live_overlay(
    tmp_path,
    monkeypatch,
) -> None:
    """A pytest coding snapshot must never appear as Raghav's real running job."""

    sent: list[dict] = []
    monkeypatch.setattr(supervisor_module, "_send_overlay_event", sent.append)
    supervisor = VoiceWorkSupervisor(
        provider_chooser=_codex_available(),
        store=VoiceInboxStore(tmp_path / "voice.sqlite3"),
        marker_path=tmp_path / "worker.json",
    )

    supervisor._overlay({"type": "code_snapshot", "snapshot": {"item_id": "test"}})

    assert sent == []


def test_historical_resume_rechecks_project_binding_before_process_spawn(
    tmp_path,
    monkeypatch,
) -> None:
    """A closed transcript must be bound to the accepted repo before Codex owns it."""

    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    repo = tmp_path / "serena"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    item = _accepted_item(store, repo, item_id="historical-resume-binding")
    bindings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "core.metadata.set_work_project_root",
        lambda sid, root: bindings.append((sid, str(root))) or str(root),
    )
    monkeypatch.setattr(
        supervisor_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("spawn stopped")),
    )
    supervisor = VoiceWorkSupervisor(
        provider_chooser=_codex_available(),
        store=store,
        marker_path=tmp_path / "worker.json",
        codex_bin="/fake/codex",
    )

    with pytest.raises(RuntimeError, match="spawn stopped"):
        supervisor._run_codex_attempt(
            item,
            repo,
            "continue",
            resume_session_id="019fcaaa-1111-7222-8333-123456789abc",
            commands=[],
        )

    assert bindings == [
        ("019fcaaa-1111-7222-8333-123456789abc", str(repo))
    ]
    attempt = store.job_snapshot(item.item_id)["attempts"][-1]
    assert attempt["state"] == "failed"
    assert attempt["finished_at"] is not None
    assert attempt["last_error"] == "spawn stopped"


def test_reused_attempt_closes_when_durable_setup_fails(tmp_path, monkeypatch) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    repo = tmp_path / "serena"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    sid = "019fcaaa-1111-7222-8333-setupfail1234"
    route = {
        "mode": "reuse",
        "preference": "auto",
        "project_root": str(repo.resolve()),
        "session_id": sid,
        "bridge_port": 45678,
    }
    item = _accepted_item(store, repo, item_id="reused-setup-failure", work_route=route)
    monkeypatch.setattr(
        store,
        "set_attempt_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("attempt setup failed")
        ),
    )
    supervisor = VoiceWorkSupervisor(
        provider_chooser=_codex_available(),
        store=store,
        marker_path=tmp_path / "worker.json",
        codex_bin="/fake/codex",
    )

    with pytest.raises(RuntimeError, match="attempt setup failed"):
        supervisor._run_reused_codex_attempt(
            item,
            repo,
            "continue",
            route=route,
            commands=[],
        )

    attempt = store.job_snapshot(item.item_id)["attempts"][-1]
    assert attempt["state"] == "failed"
    assert attempt["finished_at"] is not None
    assert attempt["last_error"] == "attempt setup failed"


def test_dead_committed_reuse_route_fails_fast_and_releases_next_job(
    tmp_path, monkeypatch
) -> None:
    """A dead exact owner must not hold the resident queue for six hours."""

    from core import codex_bridge

    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    weather_repo = tmp_path / "weather"
    weather_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(weather_repo)], check=True)
    sid = "019fc3d6-1111-7222-8333-deadbeef4457"
    route = {
        "mode": "reuse",
        "preference": "auto",
        "project_root": str(weather_repo.resolve()),
        "session_id": sid,
        "bridge_port": 44577,
        "reason": "live exact-project chat at acceptance",
    }
    first = _accepted_item(
        store,
        weather_repo,
        item_id="79097afa-dead-route",
        work_route=route,
    )
    first_claim = store.claim_next("headless-voice-before-restart")
    assert first_claim is not None
    assert store.acknowledge_started(
        first.item_id,
        target_sid="headless-voice-before-restart",
        cwd=str(weather_repo),
    )
    assert store.set_work_session(first.item_id, sid)
    assert store.requeue_work_item(first.item_id, error="resident worker restarted")
    assert store.prepare_route_dispatch(first.item_id, "committed-weather-digest")
    assert store.mark_route_dispatch(first.item_id, "committed", start_offset=1)

    ui_repo = tmp_path / "ui"
    ui_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(ui_repo)], check=True)
    second = _accepted_item(
        store,
        ui_repo,
        item_id="0794b319-next-job",
        call_id="call-next-job",
        turn_id="call-next-job:1",
    )

    rollout_root = tmp_path / "sessions" / "2026" / "08" / "04"
    rollout_root.mkdir(parents=True)
    rollout = rollout_root / f"rollout-2026-08-04T10-19-00-{sid}.jsonl"
    rollout.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_bridge, "CODEX_SESSIONS_ROOT", tmp_path / "sessions")

    resumed = store.claim_next("headless-voice-after-restart")
    assert resumed is not None and resumed.item_id == first.item_id
    supervisor = VoiceWorkSupervisor(
        provider_chooser=_codex_available(),
        store=store,
        marker_path=tmp_path / "worker.json",
        notifier=lambda _message: None,
        overlay_sender=lambda _message: None,
        codex_bin="/fake/codex",
        reused_turn_timeout=21_600,
    )
    monkeypatch.setattr(
        supervisor,
        "_reused_runtime_error",
        lambda port, current_sid: (
            "the existing-chat bridge is unavailable: connection refused"
            if (port, current_sid) == (44577, sid)
            else ""
        ),
    )
    monkeypatch.setattr(
        supervisor,
        "_post_local_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("committed reconciliation dispatched the prompt twice")
        ),
    )
    monkeypatch.setattr(
        supervisor_module,
        "wait_for_turn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dead transcript entered the six-hour wait")
        ),
    )

    supervisor._run_item(resumed, "headless-voice-after-restart")

    snapshot = store.job_snapshot(first.item_id)
    assert snapshot["work"]["state"] == "failed"
    assert "bridge is unavailable" in snapshot["work"]["last_error"]
    assert snapshot["route"]["state"] == "uncertain"
    assert snapshot["attempts"][-1]["state"] == "failed"
    assert snapshot["attempts"][-1]["finished_at"] is not None
    overlay = store.overlay_snapshot(first.item_id)
    assert overlay["state"] == "failed"
    assert overlay["progress"]["attempt_state"] == "failed"
    assert overlay["progress"]["route_state"] == "uncertain"

    released = store.claim_next("headless-voice-next-job")
    assert released is not None and released.item_id == second.item_id


def test_reused_chat_attempt_uses_live_owner_and_captures_exact_turn(
    tmp_path, monkeypatch
) -> None:
    """Voice work must enter the selected pane, never spawn a second owner."""

    from core import codex_bridge
    from core.codex_turn_capture import line_count

    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    repo = tmp_path / "serena"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    sid = "019fcaaa-1111-7222-8333-123456789abc"
    route = {
        "mode": "reuse",
        "preference": "auto",
        "project_root": str(repo.resolve()),
        "session_id": sid,
        "group_id": "g_exact",
        "bridge_port": 45678,
        "title": "Tightening Serena",
        "reason": "focused exact-project chat",
        "bound_focus": True,
    }
    _accepted_item(store, repo, item_id="reused-chat-job", work_route=route)
    item = store.claim_next("headless-voice-reused")
    assert item is not None
    assert store.acknowledge_started(
        item.item_id,
        target_sid="headless-voice-reused",
        cwd=str(repo),
    )

    rollout_root = tmp_path / "sessions" / "2026" / "08" / "03"
    rollout_root.mkdir(parents=True)
    rollout = rollout_root / f"rollout-2026-08-03T00-00-00-{sid}.jsonl"
    rollout.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_bridge, "CODEX_SESSIONS_ROOT", tmp_path / "sessions")
    dispatched: list[dict] = []

    def fake_bridge(port, path, payload, *, timeout):
        assert port == 45678
        assert path == "/api/codex-work-bridge"
        assert timeout > 0
        dispatched.append(payload)
        prompt = payload["prompt"]
        start = line_count(rollout)
        _append_rollout(
            rollout,
            {
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-sol", "effort": "max"},
            },
            {"type": "event_msg", "payload": {"type": "user_message", "message": prompt}},
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "exec-1",
                    "input": (
                        'const result = await tools.exec_command({cmd:"pytest -q"}); '
                        "text(JSON.stringify(result));"
                    ),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "exec-1",
                    "output": '{"exit_code":0,"output":"2 passed"}',
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "implemented and tested"},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": "implemented and tested",
                },
            },
        )
        end = line_count(rollout)
        return {
            "ok": True,
            "response": "implemented and tested",
            "message": "complete",
            "committed": True,
            "start_offset": start,
            "end_offset": end,
        }

    monkeypatch.setattr(
        supervisor_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reused chat spawned a second Codex owner")
        ),
    )
    supervisor = VoiceWorkSupervisor(
        provider_chooser=_codex_available(),
        store=store,
        marker_path=tmp_path / "worker.json",
        reused_turn_timeout=2,
    )
    monkeypatch.setattr(supervisor, "_post_local_json", fake_bridge)
    commands: list[dict] = []

    result = supervisor._run_reused_codex_attempt(
        item,
        repo,
        "continue the accepted job",
        route=route,
        commands=commands,
    )
    reconciled_commands: list[dict] = []
    reconciled = supervisor._run_reused_codex_attempt(
        item,
        repo,
        "a restart builds a different wrapper, but must not resend",
        route=route,
        commands=reconciled_commands,
        reconcile_existing=True,
    )

    assert len(dispatched) == 1
    assert dispatched[0]["target_sid"] == sid
    assert str(repo) in dispatched[0]["prompt"]
    assert result["session_id"] == sid
    assert result["message"] == "implemented and tested"
    assert reconciled["message"] == "implemented and tested"
    assert reconciled_commands == commands
    assert commands == [
        {
            "command": "pytest -q",
            "exit_code": 0,
            "output": '{"exit_code":0,"output":"2 passed"}',
        }
    ]
    assert store.route_record(item.item_id)["state"] == "completed"


def test_opus_5_review_identity_never_accepts_another_generation() -> None:
    assert supervisor_module._claude_identity_error("claude-opus-5", "xhigh") == ""
    assert "fallback" in supervisor_module._claude_identity_error(
        "claude-opus-4-1-20250805", "xhigh"
    )
    command = VoiceWorkSupervisor._claude_review_command(
        "/fake/claude", "22222222-2222-4222-8222-222222222222"
    )
    assert command[command.index("--model") + 1] == "claude-opus-5"
    assert command[command.index("--effort") + 1] == "xhigh"
    assert "--fallback-model" not in command
    assert "--safe-mode" in command
    assert "--no-session-persistence" in command


def test_opus_review_is_verified_without_creating_a_sidebar_chat(
    tmp_path, monkeypatch
) -> None:
    """Independent review evidence belongs in the job record, not a new chat."""

    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    repo = tmp_path / "serena"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    item = _accepted_item(store, repo, item_id="ephemeral-review-job")
    commands: list[list[str]] = []

    class ReviewProcess:
        pid = 987654
        returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self, _prompt, timeout=None):
            assert timeout
            return (
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"model": "claude-opus-5"},
                        "session_id": "ephemeral-review-session",
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "result",
                        "session_id": "ephemeral-review-session",
                        "model": "claude-opus-5",
                        "result": json.dumps(
                            {"approved": True, "findings": [], "summary": "clean"}
                        ),
                    }
                ),
                "",
            )

    monkeypatch.setattr(supervisor_module, "verify_claude_subscription", lambda **_kwargs: None)
    monkeypatch.setattr(
        supervisor_module.subprocess,
        "Popen",
        lambda command, **_kwargs: commands.append(command) or ReviewProcess(),
    )
    monkeypatch.setattr(supervisor_module, "_claude_actual_identity", lambda *_args: ("", ""))
    supervisor = VoiceWorkSupervisor(
        provider_chooser=_codex_available(),
        store=store,
        marker_path=tmp_path / "worker.json",
        claude_bin="/fake/claude",
    )

    result = supervisor._run_private_review(item, repo, {})

    assert commands and "--no-session-persistence" in commands[0]
    assert result["reported_model"] == "claude-opus-5"
    assert result["reported_effort"] == "xhigh"
    assert result["approved"] is True


def test_cancellation_interrupts_a_running_conditional_review(tmp_path, monkeypatch) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    repo = tmp_path / "serena"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _accepted_item(store, repo, item_id="review-cancel-job")
    target = "headless-voice-review-cancel"
    item = store.claim_next(target)
    assert item is not None
    assert store.acknowledge_started(item.item_id, target_sid=target, cwd=str(repo))
    control_id = store.request_cancel(item.item_id)
    terminated = threading.Event()

    class BlockingReviewProcess:
        pid = 987654
        returncode = None

        def poll(self):
            return -15 if terminated.is_set() else None

        def communicate(self, _prompt, timeout=None):
            assert terminated.wait(timeout=2)
            self.returncode = -15
            return "", "cancelled"

    process = BlockingReviewProcess()
    monkeypatch.setattr(supervisor_module, "verify_claude_subscription", lambda **_kwargs: None)
    monkeypatch.setattr(supervisor_module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(supervisor_module.os, "killpg", lambda _pid, _signal: terminated.set())
    supervisor = VoiceWorkSupervisor(
        provider_chooser=_codex_available(),
        store=store,
        marker_path=tmp_path / "worker.json",
        claude_bin="/fake/claude",
        notifier=lambda _message: None,
    )

    with pytest.raises(supervisor_module._ReviewControlInterrupted) as interrupted:
        supervisor._run_private_review(item, repo, {})

    assert interrupted.value.control["control_id"] == control_id
    supervisor._finish_or_requeue_review_control(item, interrupted.value.control)
    assert store.work_record(item.item_id)["state"] == "cancelled"
    assert store.pending_controls(item.item_id) == []


def test_steering_during_review_requeues_the_persisted_codex_session(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    repo = tmp_path / "serena"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _accepted_item(store, repo, item_id="review-steer-job")
    target = "headless-voice-review-steer"
    item = store.claim_next(target)
    assert item is not None
    assert store.acknowledge_started(item.item_id, target_sid=target, cwd=str(repo))
    assert store.set_work_session(item.item_id, "persisted-codex-session")
    control_id = store.add_steering(item.item_id, "keep the API compatible")
    control = store.pending_controls(item.item_id)[0]
    supervisor = VoiceWorkSupervisor(
        provider_chooser=_codex_available(),
        store=store,
        marker_path=tmp_path / "worker.json",
    )

    supervisor._finish_or_requeue_review_control(item, control)

    work = store.work_record(item.item_id)
    assert work["state"] == "resume_queued"
    assert work["session_id"] == "persisted-codex-session"
    assert store.pending_controls(item.item_id)[0]["control_id"] == control_id
    reclaimed = store.claim_next("headless-voice-review-resume")
    assert reclaimed is not None
    assert reclaimed.item_id == item.item_id
    cancelled, steering = supervisor._pre_start_controls(reclaimed)
    assert cancelled is False
    assert steering == ["keep the API compatible"]
    assert store.pending_controls(item.item_id) == []


def test_steering_before_thread_started_waits_for_resumable_verified_identity(
    tmp_path,
    monkeypatch,
) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    repo = tmp_path / "serena"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _accepted_item(store, repo, item_id="early-steer-job")
    target = "headless-voice-early-steer"
    item = store.claim_next(target)
    assert item is not None
    assert store.acknowledge_started(item.item_id, target_sid=target, cwd=str(repo))
    control_id = store.add_steering(item.item_id, "keep the empty case")
    terminated = threading.Event()
    session_id = "019f7b19-2f0d-7eb3-8270-6a6ed90b5dcc"
    lines = iter(
        [
            json.dumps({"type": "thread.started", "thread_id": session_id}) + "\n",
            json.dumps(
                {
                    "type": "thread.settings",
                    "settings": {
                        "model": "gpt-5.6-sol",
                        "reasoning_effort": "xhigh",
                    },
                }
            )
            + "\n",
        ]
    )

    class BlockingStdout:
        def __iter__(self):
            return self

        def __next__(self):
            try:
                return next(lines)
            except StopIteration:
                assert terminated.wait(timeout=2)
                raise

    class FakeProcess:
        pid = 987654

        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = BlockingStdout()
            self.returncode = None

        def poll(self):
            return -15 if terminated.is_set() else None

        def wait(self, timeout=None):
            assert terminated.wait(timeout=2)
            self.returncode = -15
            return self.returncode

    process = FakeProcess()
    runtime_claims: list[tuple[str, str, int]] = []
    runtime_releases: list[tuple[str, int]] = []
    project_bindings: list[tuple[str, str]] = []
    monkeypatch.setattr(supervisor_module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(supervisor_module.os, "killpg", lambda _pid, _signal: terminated.set())
    monkeypatch.setattr(supervisor_module, "_session_ids", lambda: set())
    monkeypatch.setattr(
        "core.metadata.set_external_runtime",
        lambda sid, *, kind, pid, **_kwargs: runtime_claims.append((sid, kind, pid)) or {},
    )
    monkeypatch.setattr(
        "core.metadata.clear_external_runtime",
        lambda sid, *, pid, **_kwargs: runtime_releases.append((sid, pid)) or True,
    )
    monkeypatch.setattr(
        "core.metadata.set_work_project_root",
        lambda sid, root: project_bindings.append((sid, str(root))) or str(root),
    )
    supervisor = VoiceWorkSupervisor(
        provider_chooser=_codex_available(),
        store=store,
        marker_path=tmp_path / "worker.json",
        codex_bin="/fake/codex",
    )

    result = supervisor._run_codex_attempt(
        item,
        repo,
        "continue",
        resume_session_id="",
        commands=[],
    )

    assert result["session_id"] == session_id
    assert result["control"]["control_id"] == control_id
    assert store.pending_controls(item.item_id) == []
    assert runtime_claims == [(session_id, "voice-work", process.pid)]
    assert runtime_releases == [(session_id, process.pid)]
    assert project_bindings == [(session_id, str(repo))]


def test_a_cancel_that_landed_while_queued_never_spawns_codex(tmp_path, monkeypatch) -> None:
    """He called it off before it started, so no worker process may exist."""

    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    repo = tmp_path / "serena"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    queued = _accepted_item(store, repo, item_id="prestart-cancel-job")
    store.request_cancel(queued.item_id)
    target = "headless-voice-prestart"
    assert store.claim_next(target) is None, "a cancelled job must not be claimable"

    spawned: list[list[str]] = []
    real_popen = subprocess.Popen

    def fake_popen(command, **kwargs):
        if command[0] == "/fake/codex":
            spawned.append(command)
            raise AssertionError("Codex was spawned for a cancelled job")
        return real_popen(command, **kwargs)

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", fake_popen)
    notices: list[str] = []
    supervisor = VoiceWorkSupervisor(
        provider_chooser=_codex_available(),
        store=store,
        marker_path=tmp_path / "worker.json",
        notifier=notices.append,
        overlay_sender=lambda _message: None,
        codex_bin="/fake/codex",
    )
    supervisor._run_item(queued, target)

    assert spawned == []
    assert store.work_record(queued.item_id)["state"] == "cancelled"
    assert notices == ["stopped. that coding job was cancelled before it started."]


def test_resident_worker_runs_codex_and_persists_result(tmp_path, monkeypatch) -> None:
    database = tmp_path / "voice.sqlite3"
    store = VoiceInboxStore(database)
    repo = tmp_path / "serena"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    queued = _accepted_item(store, repo)
    target = "headless-voice-test"
    item = store.claim_next(target)
    assert item is not None

    session_id = "019f7b19-2f0d-7eb3-8270-6a6ed90b5dcc"
    events = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": session_id}),
            json.dumps(
                {
                    "type": "thread.settings",
                    "settings": {"model": "gpt-5.6-sol", "reasoning_effort": "xhigh"},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "pytest -q",
                        "exit_code": 0,
                        "aggregated_output": "1 passed",
                    },
                }
            ),
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
            (repo / "README.md").write_text("updated\n", encoding="utf-8")
            self.stdin = CapturingStdin()
            self.stdout = io.StringIO(events)
            self.pid = os.getpid()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    real_popen = subprocess.Popen

    def fake_popen(command, **kwargs):
        if command[0] not in {"/fake/codex", "/fake/claude"}:
            return real_popen(command, **kwargs)
        captured["commands"].append(command)
        captured["environment"] = kwargs["env"]
        process = FakeCodexProcess()
        captured["processes"].append(process)
        return process

    titles: list[tuple[str, str]] = []
    residents: list[str] = []
    notices: list[str] = []
    overlay_events: list[dict] = []
    monkeypatch.setattr(supervisor_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(supervisor_module, "_session_ids", lambda: set())
    monkeypatch.setattr("core.metadata.set_external_runtime", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("core.metadata.clear_external_runtime", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "core.metadata.set_work_project_root",
        lambda _sid, root: str(root),
    )
    monkeypatch.setattr(
        "core.metadata.set_custom_title",
        lambda sid, title: titles.append((sid, title)),
    )
    monkeypatch.setattr(
        "core.metadata.set_resident_work",
        lambda sid: residents.append(sid),
    )

    supervisor = VoiceWorkSupervisor(
        provider_chooser=_codex_available(),
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
    assert "update the README" in prompt
    assert "Persona.md" in prompt
    assert captured["commands"][0][-1] == "-"
    assert "--skip-git-repo-check" not in captured["commands"][0]
    assert captured["commands"][0][captured["commands"][0].index("-m") + 1] == "gpt-5.6-sol"
    assert 'model_reasoning_effort="xhigh"' in captured["commands"][0]
    assert "--ignore-user-config" in captured["commands"][0]
    assert titles and titles[0][0] == session_id
    assert residents == [session_id]
    assert notices == ["done. fixed and tested"]
    assert overlay_events[0]["type"] == "code_start"
    assert overlay_events[0]["item_id"] == queued.item_id
    assert overlay_events[0]["snapshot"]["brief"]["request"] == "update the README"
    assert {event["type"] for event in overlay_events} == {
        "code_start",
        "code_event",
        "code_snapshot",
        "code_done",
    }
    assert overlay_events[-1]["type"] == "code_done"
    assert overlay_events[-1]["summary"] == "fixed and tested"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT state, session_id, summary FROM voice_work WHERE item_id=?",
            (queued.item_id,),
        ).fetchone()
    assert row == ("completed", session_id, "fixed and tested")
    evidence = store.evidence(queued.item_id)
    assert evidence is not None
    assert evidence["complete"] is True
    assert evidence["changed_files"] == ["README.md"]
    snapshot = store.job_snapshot(queued.item_id)
    assert snapshot is not None
    assert snapshot["reviews"][-1]["state"] == "skipped"
