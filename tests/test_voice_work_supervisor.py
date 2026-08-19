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
    configured_job_concurrency,
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
            "--allow-empty",
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


def test_concurrency_configuration_is_bounded(monkeypatch, capsys) -> None:
    monkeypatch.delenv("SERENA_CODING_JOB_CONCURRENCY", raising=False)
    assert configured_job_concurrency() == 2
    assert configured_job_concurrency("4") == 4
    with pytest.raises(ValueError, match="between 1 and 4"):
        configured_job_concurrency(0)
    with pytest.raises(ValueError, match="between 1 and 4"):
        configured_job_concurrency(5)
    monkeypatch.setenv("SERENA_CODING_JOB_CONCURRENCY", "typo")
    assert configured_job_concurrency() == 2
    monkeypatch.setenv("SERENA_CODING_JOB_CONCURRENCY", "99")
    assert configured_job_concurrency() == 2
    assert "using 2" in capsys.readouterr().out


def test_scheduler_runs_independent_projects_in_parallel(
    tmp_path,
    monkeypatch,
) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    repos = [tmp_path / "weather", tmp_path / "serena"]
    for index, repo in enumerate(repos):
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        _accepted_item(
            store,
            repo,
            item_id=f"parallel-job-{index}",
            call_id=f"parallel-call-{index}",
            turn_id=f"parallel-turn-{index}",
        )

    monkeypatch.setattr(supervisor_module, "verify_codex_subscription", lambda **_kwargs: None)
    supervisor = VoiceWorkSupervisor(
        store=store,
        marker_path=tmp_path / "worker.json",
        notifier=lambda _message: None,
        overlay_sender=lambda _message: None,
        max_concurrency=2,
    )
    both_started = threading.Event()
    release = threading.Event()
    entered: list[str] = []
    lock = threading.Lock()

    def run_item(item, target_sid):
        assert store.acknowledge_started(
            item.item_id,
            target_sid=target_sid,
            cwd=str(item.brief["project_root"]),
        )
        with lock:
            entered.append(str(item.brief["project_root"]))
            if len(entered) == 2:
                both_started.set()
        release.wait(3)

    monkeypatch.setattr(supervisor, "_run_item", run_item)
    runner = threading.Thread(target=supervisor.run)
    runner.start()
    try:
        assert both_started.wait(2), "two independent jobs never entered active work"
        assert set(entered) == {str(repo.resolve()) for repo in repos}
        assert store.working_count() == 2
        assert {
            store.job_snapshot(f"parallel-job-{index}")["work"]["state"]
            for index in range(2)
        } == {"working"}
    finally:
        supervisor.request_stop()
        release.set()
        runner.join(timeout=3)
    assert not runner.is_alive()


def test_stopping_the_supervisor_terminates_every_active_job_process(
    tmp_path,
    monkeypatch,
) -> None:
    supervisor = VoiceWorkSupervisor(
        store=VoiceInboxStore(tmp_path / "voice.sqlite3"),
        marker_path=tmp_path / "worker.json",
        max_concurrency=2,
    )
    terminated: list[tuple[int, int]] = []

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return None

    monkeypatch.setattr(
        supervisor_module.os,
        "killpg",
        lambda pid, sig: terminated.append((pid, sig)),
    )
    supervisor._set_active_process("first", Process(101))
    supervisor._set_active_process("second", Process(202))

    supervisor.request_stop()

    assert set(terminated) == {
        (101, supervisor_module.signal.SIGTERM),
        (202, supervisor_module.signal.SIGTERM),
    }


def test_scheduler_skips_a_conflicting_checkout_and_never_exceeds_capacity(
    tmp_path,
    monkeypatch,
) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    shared = tmp_path / "shared"
    independent = tmp_path / "independent"
    for repo in (shared, independent):
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
    jobs = [
        _accepted_item(
            store,
            shared,
            item_id="shared-first",
            call_id="shared-first-call",
            turn_id="shared-first-turn",
        ),
        _accepted_item(
            store,
            shared,
            item_id="shared-second",
            call_id="shared-second-call",
            turn_id="shared-second-turn",
        ),
        _accepted_item(
            store,
            independent,
            item_id="independent-third",
            call_id="independent-call",
            turn_id="independent-turn",
        ),
    ]

    monkeypatch.setattr(supervisor_module, "verify_codex_subscription", lambda **_kwargs: None)
    supervisor = VoiceWorkSupervisor(
        store=store,
        marker_path=tmp_path / "worker.json",
        notifier=lambda _message: None,
        overlay_sender=lambda _message: None,
        max_concurrency=2,
    )
    releases = {job.item_id: threading.Event() for job in jobs}
    first_wave = threading.Event()
    second_shared_started = threading.Event()
    lock = threading.Lock()
    active_roots: set[str] = set()
    entered: list[str] = []
    conflicts: list[str] = []
    max_active = 0

    def run_item(item, _target_sid):
        nonlocal max_active
        root = str(item.brief["project_root"])
        with lock:
            if root in active_roots:
                conflicts.append(root)
            active_roots.add(root)
            entered.append(item.item_id)
            max_active = max(max_active, len(active_roots))
            if len(entered) == 2:
                first_wave.set()
            if item.item_id == "shared-second":
                second_shared_started.set()
        releases[item.item_id].wait(3)
        with lock:
            active_roots.remove(root)

    monkeypatch.setattr(supervisor, "_run_item", run_item)
    runner = threading.Thread(target=supervisor.run)
    runner.start()
    try:
        assert first_wave.wait(2)
        assert set(entered[:2]) == {"shared-first", "independent-third"}
        assert "shared-second" not in entered[:2]
        releases["shared-first"].set()
        assert second_shared_started.wait(2)
        assert conflicts == []
        assert max_active == 2
    finally:
        supervisor.request_stop()
        for event in releases.values():
            event.set()
        runner.join(timeout=3)
    assert not runner.is_alive()


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


def test_private_launch_commands_use_the_model_frozen_by_shared_policy(tmp_path) -> None:
    codex = VoiceWorkSupervisor._codex_command(
        "/fake/codex",
        tmp_path,
        model="gpt-5.6-terra",
        effort="high",
    )
    claude = VoiceWorkSupervisor._claude_implement_command(
        "/fake/claude",
        model="claude-sonnet-5",
        effort="high",
    )

    assert codex[codex.index("-m") + 1] == "gpt-5.6-terra"
    assert 'model_reasoning_effort="high"' in codex
    assert claude[claude.index("--model") + 1] == "claude-sonnet-5"
    assert claude[claude.index("--effort") + 1] == "high"
    assert supervisor_module._codex_identity_error(
        "gpt-5.6-terra",
        "high",
        requested_model="gpt-5.6-terra",
        requested_effort="high",
    ) == ""


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


def test_dead_committed_reuse_route_recovers_privately_and_releases_next_job(
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
    attempt_id, _number = store.start_attempt(
        first.item_id,
        provider="codex",
        model="gpt-5.6-sol",
        effort="high",
    )
    assert store.set_attempt_session(attempt_id, sid)
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
    assert snapshot["work"]["state"] == "resume_queued"
    assert "bridge is unavailable" in snapshot["work"]["last_error"]
    assert snapshot["route"]["mode"] == "private"
    assert snapshot["route"]["state"] == "uncertain"
    assert snapshot["attempts"][-1]["state"] == "failed"
    assert snapshot["attempts"][-1]["finished_at"] is not None
    overlay = store.overlay_snapshot(first.item_id)
    assert overlay["state"] == "resume_queued"
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
                "payload": {"model": "gpt-5.6-sol", "effort": "high"},
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
    assert {
        attempt["requested_effort"]
        for attempt in store.job_snapshot(item.item_id)["attempts"]
    } == {"high"}


def test_busy_automatic_reuse_route_is_requeued_privately_instead_of_failed(
    tmp_path, monkeypatch
) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    repo = tmp_path / "serena"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    route = {
        "mode": "reuse",
        "preference": "auto",
        "project_root": str(repo.resolve()),
        "session_id": "019fcaaa-1111-7222-8333-busy00000002",
        "group_id": "g_busy",
        "bridge_port": 45678,
        "title": "busy chat",
        "reason": "automatic exact-project reuse",
        "effort": "high",
    }
    queued = _accepted_item(store, repo, item_id="busy-fallback-job", work_route=route)
    target = "headless-voice-busy-fallback"
    item = store.claim_next(target)
    assert item is not None
    notices: list[str] = []
    supervisor = VoiceWorkSupervisor(
        provider_chooser=_codex_available(),
        store=store,
        marker_path=tmp_path / "worker.json",
        notifier=notices.append,
        overlay_sender=lambda _message: None,
    )
    monkeypatch.setattr(
        supervisor,
        "_run_reused_codex_attempt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("runtime has an active turn")
        ),
    )

    supervisor._run_item(item, target)

    snapshot = store.job_snapshot(queued.item_id)
    assert snapshot["queue"]["state"] == "queued"
    assert snapshot["work"]["state"] == "failed"
    assert snapshot["work"]["session_id"] == ""
    assert snapshot["route"]["mode"] == "private"
    recovery_events = [
        event for event in snapshot["events"]
        if event["kind"] == "automatic_recovery.queued"
    ]
    assert len(recovery_events) == 1
    assert recovery_events[0]["payload"]["kind"] == "route"
    assert recovery_events[0]["payload"]["private_route"] is True
    assert notices == [
        "that coding attempt hit a recoverable failure. "
        "i queued a bounded continuation instead of abandoning it."
    ]


def test_recovery_attempt_receives_the_durable_failure_context(
    tmp_path, monkeypatch
) -> None:
    clock = [5_000.0]
    monkeypatch.setattr(supervisor_module.time, "time", lambda: clock[0])
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    repo = tmp_path / "serena"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    queued = _accepted_item(store, repo, item_id="recovery-prompt-job")
    first_target = "headless-voice-recovery-first"
    first = store.claim_next(first_target)
    assert first is not None
    assert store.acknowledge_started(
        queued.item_id,
        target_sid=first_target,
        cwd=str(repo),
    )
    assert store.set_work_session(queued.item_id, "persisted-recovery-session")
    recovery = store.queue_automatic_recovery(
        queued.item_id,
        error="Codex exited with status 1",
        kind="provider",
        max_recoveries=3,
    )
    assert recovery["queued"] is True

    clock[0] += 2.0
    target = "headless-voice-recovery-second"
    item = store.claim_next(target)
    assert item is not None
    prompts: list[str] = []
    supervisor = VoiceWorkSupervisor(
        provider_chooser=_codex_available(),
        store=store,
        marker_path=tmp_path / "worker.json",
        notifier=lambda _message: None,
        overlay_sender=lambda _message: None,
    )

    def cancel_after_capture(_item, _cwd, prompt, **_kwargs):
        prompts.append(prompt)
        return {
            "attempt_no": 2,
            "session_id": "persisted-recovery-session",
            "message": "",
            "control": {"action": "cancel", "control_id": ""},
        }

    monkeypatch.setattr(supervisor, "_run_codex_attempt", cancel_after_capture)
    supervisor._run_item(item, target)

    assert len(prompts) == 1
    assert "bounded automatic continuation of the same logical coding job" in prompts[0]
    assert "recovery 1 of 3" in prompts[0]
    assert "Codex exited with status 1" in prompts[0]
    assert store.latest_automatic_recovery(queued.item_id)["state"] == "cancelled"


def test_every_implementation_prompt_prefers_scoped_tests_and_labels_python_proof(
    tmp_path,
) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    repo = tmp_path / "serena"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    item = _accepted_item(store, repo, item_id="scoped-proof")

    private = supervisor_module._private_prompt(item).casefold()
    reused = supervisor_module._reused_prompt(item).casefold()
    assert "whole suite only" in private
    assert "full suite only" in reused
    assert "scoped run that exits zero" in private
    assert "scoped run that exits zero" in reused
    assert "serena_evidence_kind=live" in private
    assert "serena_evidence_kind=live" in reused
    assert "never put the marker on tests or static inspection" in private
    assert "never put that marker on tests or static inspection" in reused


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

    sonnet = VoiceWorkSupervisor._claude_review_command(
        "/fake/claude",
        "33333333-3333-4333-8333-333333333333",
        model="claude-sonnet-5",
        effort="high",
    )
    assert sonnet[sonnet.index("--model") + 1] == "claude-sonnet-5"
    assert sonnet[sonnet.index("--effort") + 1] == "high"


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
                        # An unjudged job is an ordinary job, so this is the
                        # tier the accepted brief really froze.
                        "reasoning_effort": "high",
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
                    "settings": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
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
    # Ordinary work runs at high. It used to run at the ceiling no matter what
    # it was, which bought maximum deliberation over routine orientation.
    assert 'model_reasoning_effort="high"' in captured["commands"][0]
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


def _warm_supervisor(tmp_path, store):
    return VoiceWorkSupervisor(
        provider_chooser=_codex_available(),
        store=store,
        marker_path=tmp_path / "worker.json",
        claude_bin="/fake/claude",
    )


def test_a_warm_claude_session_is_reused_only_when_every_guard_holds(
    tmp_path,
    monkeypatch,
) -> None:
    """A cold worker re-reads the tree it was already told about.

    On 2026-08-05 a job looked at code three seconds in and did not edit
    anything until 457 seconds in, spending ninety Bash calls relearning a
    layout. Resuming a session that already read this repository skips that.
    Every refusal below is free, because the caller just starts cold.
    """

    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    repo = tmp_path / "serena"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _accepted_item(store, repo, item_id="warm-job")
    item = store.claim_next("headless-voice-warm")
    assert item is not None
    supervisor = _warm_supervisor(tmp_path, store)

    bound: dict[str, str] = {}

    class FakeMetadata:
        @staticmethod
        def set_work_project_root(session_id, project_root):
            if session_id == "owned-elsewhere":
                raise ValueError("session is already bound to a different project")
            bound[session_id] = str(project_root)
            return str(project_root)

    class FakeBridge:
        @staticmethod
        def find_claude_jsonl(session_id):
            return None if session_id == "deleted-session" else tmp_path / "x.jsonl"

    monkeypatch.setitem(__import__("sys").modules, "core.metadata", FakeMetadata)
    monkeypatch.setitem(__import__("sys").modules, "core.claude_bridge", FakeBridge)

    # Nothing warm on record yet, so the job starts cold rather than guessing.
    assert supervisor._warm_claude_session(item, repo) == ""

    store.enqueue_accepted(
        CodingJobBrief.create(
            item_id="warm-job-earlier",
            exact_request="earlier work",
            triggering_request=f"earlier work in {repo}",
            project_root=repo,
            initial_git=capture_git_snapshot(
                repo, item_id="warm-job-earlier", label="baseline"
            ),
        ).to_dict(),
        call_id="call-earlier",
        turn_id="call-earlier:1",
    )
    earlier = store.claim_next("headless-voice-earlier")
    assert earlier is not None and earlier.item_id == "warm-job-earlier"
    assert store.acknowledge_started(
        earlier.item_id,
        target_sid="headless-voice-earlier",
        cwd=str(repo),
    )
    attempt_id, _no = store.start_attempt(
        earlier.item_id, provider="claude", model="claude-opus-5", effort="high"
    )
    store.set_attempt_session(attempt_id, "warm-claude-session")
    store.finish_attempt(attempt_id, state="completed", exit_code=0)
    store.record_evidence(earlier.item_id, {"complete": True})
    assert store.finish_work_item(earlier.item_id)

    assert supervisor._warm_claude_session(item, repo) == "warm-claude-session"
    # Reuse goes through the same immutable binding a reused Codex chat does.
    assert bound["warm-claude-session"] == str(repo)

    # A session id with no transcript left on disk would fail --resume outright.
    assert supervisor._warm_claude_session(
        item, repo, preferred_session_id="deleted-session"
    ) == ""
    # A session another project already owns is never retargeted.
    assert supervisor._warm_claude_session(
        item, repo, preferred_session_id="owned-elsewhere"
    ) == ""
    # And a path that is not a validated Git root reuses nothing at all.
    assert supervisor._warm_claude_session(item, tmp_path / "not-a-repo") == ""


def test_ordinary_work_implements_at_high_and_hard_work_at_the_ceiling(
    tmp_path,
) -> None:
    """Effort is a property of the job, read from the brief it was accepted on."""

    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    repo = tmp_path / "serena"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _accepted_item(store, repo, item_id="ordinary-job")
    item = store.claim_next("headless-voice-effort")
    assert item is not None
    supervisor = _warm_supervisor(tmp_path, store)

    assert supervisor._implement_effort(item) == "high"
    command = VoiceWorkSupervisor._claude_implement_command("/fake/claude", effort="high")
    assert command[command.index("--effort") + 1] == "high"

    tampered = item.__class__(
        item_id=item.item_id,
        request=item.request,
        call_id=item.call_id,
        turn_id=item.turn_id,
        state=item.state,
        created_at=item.created_at,
        brief={**(item.brief or {}), "complexity": "hard"},
    )
    # The accepted policy wins over a later field mutation. A model must not
    # silently change depth after the job was frozen.
    assert supervisor._implement_effort(tampered) == "high"
    legacy_hard = item.__class__(
        item_id=item.item_id,
        request=item.request,
        call_id=item.call_id,
        turn_id=item.turn_id,
        state=item.state,
        created_at=item.created_at,
        brief={
            key: value
            for key, value in {**(item.brief or {}), "complexity": "hard"}.items()
            if key != "model_policy"
        },
    )
    assert supervisor._implement_effort(legacy_hard) == "xhigh"
    assert 'model_reasoning_effort="xhigh"' in VoiceWorkSupervisor._codex_command(
        "/fake/codex", repo, effort="xhigh"
    )


def test_claude_implementation_records_the_identity_it_reported(
    tmp_path,
    monkeypatch,
) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    repo = tmp_path / "serena"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _accepted_item(store, repo, item_id="claude-identity-job")
    target = "headless-voice-claude-identity"
    item = store.claim_next(target)
    assert item is not None
    assert store.acknowledge_started(item.item_id, target_sid=target, cwd=str(repo))
    session_id = "017f7b19-2f0d-7eb3-8270-6a6ed90b5dcc"
    stdout = "\n".join(
        [
            json.dumps({"type": "system", "session_id": session_id}),
            json.dumps(
                {
                    "type": "assistant",
                    "session_id": session_id,
                    "effort": "high",
                    "message": {"model": "claude-opus-5", "content": []},
                }
            ),
            json.dumps({"type": "result", "result": "done"}),
        ]
    )

    class FakeClaudeProcess:
        returncode = 0

        def communicate(self, _prompt, timeout=None):
            return stdout, ""

    monkeypatch.setattr(
        supervisor_module, "verify_claude_subscription", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        supervisor_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: FakeClaudeProcess(),
    )
    supervisor = _warm_supervisor(tmp_path, store)

    result = supervisor._run_claude_attempt(
        item,
        repo,
        "continue",
        commands=[],
        effort="high",
    )

    assert result["session_id"] == session_id
    attempt = store.job_snapshot(item.item_id)["attempts"][-1]
    assert attempt["reported_model"] == "claude-opus-5"
    assert attempt["reported_effort"] == "high"
    assert attempt["state"] == "completed"
    assert "model mismatch" in supervisor_module._claude_implement_identity_error(
        "claude-sonnet-5", "high", requested_effort="high"
    )
    assert "effort mismatch" in supervisor_module._claude_implement_identity_error(
        "claude-opus-5", "xhigh", requested_effort="high"
    )


@pytest.mark.parametrize(
    ("previous_provider", "next_provider"),
    [("codex", "claude"), ("claude", "codex")],
)
def test_restart_never_resumes_a_session_across_providers(
    tmp_path,
    monkeypatch,
    previous_provider,
    next_provider,
) -> None:
    from core.coding_provider import choose_providers

    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    repo = tmp_path / "serena"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    queued = _accepted_item(store, repo, item_id=f"{previous_provider}-resume-job")
    original_target = "headless-voice-before-provider-change"
    original = store.claim_next(original_target)
    assert original is not None
    assert store.acknowledge_started(
        queued.item_id,
        target_sid=original_target,
        cwd=str(repo),
    )
    persisted_session = f"persisted-{previous_provider}-session"
    attempt_id, _number = store.start_attempt(
        queued.item_id,
        provider=previous_provider,
        model="model",
        effort="high",
    )
    assert store.set_attempt_session(attempt_id, persisted_session)
    assert store.set_work_session(queued.item_id, persisted_session)
    store.finish_attempt(attempt_id, state="completed", exit_code=0)
    assert store.requeue_work_item(queued.item_id, error="supervisor restarted")
    target = "headless-voice-after-provider-change"
    resumed = store.claim_next(target)
    assert resumed is not None

    capacity = {
        "codex": {
            "provider": "codex",
            "status": "available" if next_provider == "codex" else "exhausted",
            "usable": next_provider == "codex",
            "reason": "test",
        },
        "claude": {
            "provider": "claude",
            "status": "available",
            "usable": True,
            "reason": "test",
        },
    }
    captured: list[str] = []

    def stop_after_capture(*_args, resume_session_id="", **_kwargs):
        captured.append(resume_session_id)
        raise RuntimeError("stop after provider-safe resume check")

    supervisor = VoiceWorkSupervisor(
        provider_chooser=lambda: choose_providers(capacity),
        store=store,
        marker_path=tmp_path / "worker.json",
        notifier=lambda _message: None,
        overlay_sender=lambda _message: None,
    )
    monkeypatch.setattr(supervisor, "_run_codex_attempt", stop_after_capture)
    monkeypatch.setattr(supervisor, "_run_claude_attempt", stop_after_capture)
    if next_provider == "claude":
        monkeypatch.setattr(
            supervisor,
            "_warm_claude_session",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("provider mismatch must start cold")
            ),
        )

    supervisor._run_item(resumed, target)

    assert captured == [""]
    events = store.job_snapshot(queued.item_id)["events"]
    assert any(event["kind"] == "provider_resume_fallback" for event in events)


def test_the_effort_gate_still_fails_a_job_whose_policy_disagrees(tmp_path) -> None:
    """Retargeted, not relaxed. A frozen effort that does not match the tier
    the brain judged is still a hard failure, so nothing can pick its own."""

    from pathlib import Path

    source = Path("core/voice_work_supervisor.py").read_text(encoding="utf-8")
    body = source.split("def _run_item", 1)[1].split("validate_repository_root", 1)[0]
    assert 'brief.get("codex_effort") != implement_effort' in body
    assert 'raise RuntimeError("accepted coding job has an invalid Codex model policy")' in body
    assert "with_implement_effort(implement_effort)" in body
    assert "route_effort != implement_effort" in source
    assert '"route_effort_fallback"' in source
