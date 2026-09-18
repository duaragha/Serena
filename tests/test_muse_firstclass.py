"""Muse is a first-class agent: automatic routing, brain turns, panes.

Where the first Muse patch made it an explicit-only choice, this suite pins
the full seat: last-resort automatic failover in every lane, daemon brain
turns through the same paths Codex uses, Fleet auto mode carrying runs on
Muse alone, indexed sidebar chats, usage-pill plumbing, and structured
workspace panes.
"""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

from core import muse_scanner
from core.brain_provider import (
    BrainProviderUnavailable,
    choose_brain_provider,
    is_usage_limit_error,
)
from core.coding_provider import choose_providers
from core.muse_brain import MuseBrainClient, MuseBrainError
from core.muse_usage_reader import parse_usage as parse_muse_usage
from core.muse_usage_reader import read_muse_usage
from core.serena_policy import resolve_policy
from fleet.policy import build_policy
from fleet.workers import WorkerRequest

WEB_SOURCE = Path(__file__).resolve().parents[1] / "ui" / "web.py"


def _capacity(codex=True, claude=True, muse=True):
    out = {}
    for name, state in (("codex", codex), ("claude", claude), ("muse", muse)):
        out[name] = {
            "provider": name,
            "status": "available" if state else "unavailable",
            "usable": state,
            "reason": "ok" if state else f"{name} usage is exhausted",
        }
    return out


def _executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# --- automatic routing --------------------------------------------------------


def test_coding_fails_over_to_muse_only_when_the_pair_is_out() -> None:
    healthy = choose_providers(_capacity())
    assert healthy.usable
    assert healthy.implement_provider == "codex"

    carried = choose_providers(_capacity(codex=False, claude=False))
    assert carried.usable
    assert carried.implement_provider == "muse"
    assert carried.implement_model == "muse-spark"
    assert "unavailable" in carried.reason


def test_nothing_is_dispatched_when_all_three_are_out() -> None:
    plan = choose_providers(_capacity(codex=False, claude=False, muse=False))
    assert not plan.usable
    assert not plan.implement_provider
    assert "codex" in plan.reason and "claude" in plan.reason and "muse" in plan.reason


def test_brain_lanes_resolve_muse_when_both_clouds_are_out() -> None:
    decision = resolve_policy(
        "brain",
        activity="chat",
        capacity=_capacity(codex=False, claude=False),
    )
    assert (decision.provider, decision.model) == ("muse", "muse-spark")
    assert "unavailable" in decision.fallback_reason or "exhausted" in decision.fallback_reason


def test_brain_provider_prefers_claude_then_codex_then_muse() -> None:
    assert choose_brain_provider(_capacity()) == "claude"
    assert choose_brain_provider(_capacity(claude=False)) == "codex"
    assert choose_brain_provider(_capacity(claude=False, codex=False)) == "muse"
    assert choose_brain_provider(_capacity(muse=False)) == "claude"
    try:
        choose_brain_provider(_capacity(claude=False, codex=False, muse=False))
    except BrainProviderUnavailable as exc:
        assert "Claude, Codex, and Muse" in str(exc)
    else:
        raise AssertionError("three exhausted providers were accepted")


def test_brain_override_and_preference_accept_muse() -> None:
    assert choose_brain_provider(_capacity(), override="muse") == "muse"
    assert choose_brain_provider(_capacity(), preferred_provider="muse") == "muse"
    assert choose_brain_provider(_capacity(muse=False), preferred_provider="muse") == "claude"


def test_muse_limit_phrases_fail_over_like_the_other_clouds() -> None:
    assert is_usage_limit_error("muse subscription quota exceeded, resets soon")
    assert is_usage_limit_error("credits exhausted for this billing window")
    assert not is_usage_limit_error("explain how usage limits work")


def test_fleet_auto_carries_a_run_on_muse_alone() -> None:
    policy = build_policy(
        "coding",
        task="Task 1: move the helper into its own module",
        provider_capacity={"codex": False, "claude": False},
    )
    assert policy.provider_mode == "muse"
    assert policy.phases[1].workers[0].provider == "muse"


def test_fleet_auto_still_refuses_when_nothing_is_usable() -> None:
    import pytest

    with pytest.raises(ValueError, match="no usable Fleet providers"):
        build_policy(
            "coding",
            task="Task 1: move the helper into its own module",
            provider_capacity={"codex": False, "claude": False, "muse": False},
        )


# --- daemon brain turns ---------------------------------------------------------


class CapturedOptions:
    def __init__(self, **values) -> None:
        self.__dict__.update(values)


class FakeMuseBrain:
    model = "muse-spark"
    effort = "high"

    def __init__(self) -> None:
        self.started = 0
        self.closed = 0
        self.messages: list[str] = []

    async def start(self) -> None:
        self.started += 1

    def set_route(self, model: str, effort: str) -> None:
        self.model = model
        self.effort = effort

    async def turn(self, message: str, *, on_delta=None, images=None) -> dict:
        self.messages.append(message)
        if on_delta is not None:
            await on_delta("muse reply")
        return {
            "text": "muse reply",
            "thread_id": "muse-thread",
            "turn_id": "muse-turn",
        }

    async def interrupt(self) -> None:
        return None

    async def close(self) -> None:
        self.closed += 1

    def snapshot(self) -> dict:
        return {"enabled": True, "running": True, "model": self.model}


def _manager(tmp_path: Path, capacity: dict):
    from core import brain_daemon
    from core.brain_lifetime import LifetimeLedger, RecentThreadJournal
    from core.voice_transcripts import VoiceTranscriptStore

    muse = FakeMuseBrain()
    manager = brain_daemon.ResidentClientManager(
        CapturedOptions,
        CapturedOptions,
        lambda: {},
        [],
        journal=RecentThreadJournal(tmp_path / "thread.json"),
        lifetime=LifetimeLedger(tmp_path / "lifetime.json"),
        voice_transcripts=VoiceTranscriptStore(tmp_path / "voice.jsonl"),
        muse_brain_factory=lambda: muse,
        capacity_reader=lambda: capacity,
        capacity_cache_seconds=0,
    )
    return manager, muse


def test_daemon_selects_muse_when_the_pair_is_out(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager, muse = _manager(tmp_path, _capacity(claude=False, codex=False))
        selected = await manager.select_provider(preferred_provider="muse")
        assert selected == "muse"
        assert manager.snapshot()["provider"] == "muse"
        assert muse.started >= 1
        await manager.stop()
        assert muse.closed == 1

    asyncio.run(scenario())


def test_daemon_runs_a_muse_turn_end_to_end(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        from core import brain_daemon

        manager, muse = _manager(tmp_path, _capacity(claude=False, codex=False))
        monkeypatch.setattr(brain_daemon, "_state_block", lambda force=False: "")
        monkeypatch.setattr(brain_daemon, "_clock_block", lambda: "")
        monkeypatch.setattr(brain_daemon, "_recalled_voice_history_block", lambda _text: "")
        out = await brain_daemon._run_turn(
            manager,
            {"protocol": "voice", "text": "are you there?", "turn_id": "voice-1"},
        )
        assert out["provider"] == "muse"
        assert out["model"] == "muse-spark"
        assert out["say"] == "muse reply"
        assert muse.messages and "are you there?" in muse.messages[0]
        await manager.stop()

    asyncio.run(scenario())


def test_daemon_marks_muse_unavailable_and_falls_through(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager, _muse = _manager(tmp_path, _capacity())
        await manager.mark_provider_unavailable("muse", "quota exhausted")
        assert manager.snapshot()["provider"] == "claude"
        await manager.stop()

    asyncio.run(scenario())


# --- the exec-backed brain --------------------------------------------------------


def test_muse_brain_turn_runs_one_headless_exec(tmp_path: Path) -> None:
    async def scenario() -> None:
        binary = _executable(
            tmp_path / "fake-muse",
            """#!/usr/bin/env python3
import json, sys
argv = sys.argv
assert argv[1:3] == ["exec", "--json"], argv
assert argv[argv.index("--reasoning-effort") + 1] == "high", argv
assert argv[argv.index("--workspace") + 1] == "WORKSPACE", argv
assert argv[argv.index("--approval-mode") + 1] == "never", argv
assert "--disable-write" in argv, argv
assert "--model" not in argv, argv
assert argv[-1].endswith("are you there?"), argv
print(json.dumps({"type": "result", "result": "brain answer",
                  "status": "SUCCESS"}), flush=True)
""".replace("WORKSPACE", str(tmp_path)),
        )
        deltas: list[str] = []
        brain = MuseBrainClient(
            cwd=tmp_path,
            developer_instructions="You are Serena.",
            binary=str(binary),
        )
        result = await brain.turn("are you there?", on_delta=deltas.append)
        assert result["text"] == "brain answer"
        assert result["thread_id"] and result["turn_id"]
        assert deltas == ["brain answer"]
        await brain.close()

    asyncio.run(scenario())


def test_muse_brain_limit_text_becomes_a_usage_limit(tmp_path: Path) -> None:
    import pytest

    from core.brain_provider import BrainProviderUsageLimit

    async def scenario() -> None:
        binary = _executable(
            tmp_path / "limited-muse",
            """#!/usr/bin/env python3
import json
print(json.dumps({"type": "result", "result": "usage limit reached, resets soon",
                  "status": "SUCCESS"}))
""",
        )
        brain = MuseBrainClient(cwd=tmp_path, binary=str(binary))
        with pytest.raises(BrainProviderUsageLimit) as exc:
            await brain.turn("hello")
        assert exc.value.provider == "muse"
        await brain.close()

    asyncio.run(scenario())


def test_muse_brain_rejects_a_bad_route_and_a_missing_binary(tmp_path: Path) -> None:
    import pytest

    async def scenario() -> None:
        brain = MuseBrainClient(cwd=tmp_path, binary="/nonexistent/muse")
        with pytest.raises(MuseBrainError):
            brain.set_route("muse-spark", "turbo")
        with pytest.raises(MuseBrainError, match="could not start|not installed"):
            await brain.turn("hello")
        await brain.close()

    asyncio.run(scenario())


# --- scanner, usage, indexer ------------------------------------------------------


def _muse_sessions(tmp_path: Path, monkeypatch, session_id: str) -> Path:
    root = tmp_path / "muse-home"
    log_dir = root / "sessions" / "2026" / "09" / "15" / session_id
    log_dir.mkdir(parents=True)
    log = log_dir / "session.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [
                {
                    "type": "user",
                    "message": {"content": "restructure the parser"},
                    "workspace": str(tmp_path),
                    "timestamp": "2026-09-15T10:00:00Z",
                },
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "done"}]},
                    "timestamp": "2026-09-15T10:01:00Z",
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(muse_scanner, "MUSE_ROOT", root)
    monkeypatch.setattr(muse_scanner, "SESSIONS_DIR", root / "sessions")
    return log


def test_a_session_is_discovered_and_described(tmp_path, monkeypatch) -> None:
    sid = "11111111-2222-3333-4444-555555555555"
    log = _muse_sessions(tmp_path, monkeypatch, sid)

    found = list(muse_scanner.scan_muse_sessions())
    assert found == [("muse", log)]

    meta = muse_scanner.parse_muse_metadata(log)
    assert meta.session_id == sid
    assert meta.cwd == str(tmp_path)
    assert meta.first_message == "restructure the parser"
    assert meta.message_count == 2
    assert muse_scanner.resumable_session_path(sid) == log
    assert muse_scanner.transcript_path(sid) == log


def test_a_session_with_no_readable_turns_does_not_list(tmp_path, monkeypatch) -> None:
    sid = "22222222-3333-4444-5555-666666666666"
    root = tmp_path / "muse-home"
    log_dir = root / "sessions" / "2026" / "09" / "15" / sid
    log_dir.mkdir(parents=True)
    log = log_dir / "session.jsonl"
    log.write_text('{"type":"ping"}\nnot json\n', encoding="utf-8")
    monkeypatch.setattr(muse_scanner, "MUSE_ROOT", root)
    monkeypatch.setattr(muse_scanner, "SESSIONS_DIR", root / "sessions")

    meta = muse_scanner.parse_muse_metadata(log)
    assert meta is None


def test_unparseable_usage_reports_unavailable_rather_than_zero() -> None:
    assert parse_muse_usage("")["available"] is False
    assert parse_muse_usage("something went wrong")["available"] is False
    assert parse_muse_usage("")["model"] == "muse-spark"


def test_the_model_is_named_even_when_the_cli_is_not_installed(monkeypatch) -> None:
    import core.muse_usage_reader as reader

    monkeypatch.setattr(reader, "_binary", lambda: None)
    monkeypatch.setattr(reader, "_CACHE", {"at": 0.0, "data": None})
    result = read_muse_usage(force=True)
    assert result["available"] is False
    assert result["model"] == "muse-spark"
    assert "not installed" in result["reason"]


def test_the_indexer_actually_calls_the_scanner() -> None:
    import inspect

    from core import indexer

    body = inspect.getsource(indexer._update_index_locked)
    assert "scan_muse_sessions()" in body
    assert "parse_muse_metadata(" in body
    assert 'if agent == "muse"' in inspect.getsource(indexer._discovered_session_id)


# --- structured workspace panes -----------------------------------------------------


def test_workspace_create_submit_close_publishes_turn_events(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        from core.workspace_muse import MuseWorkspace

        binary = _executable(
            tmp_path / "fake-muse",
            """#!/usr/bin/env python3
import json, sys
argv = sys.argv
assert argv[1:] == ["serve"], argv
sid = "11111111-2222-4333-8444-555555555555"
def emit(value): print(json.dumps(value), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req: continue
    method, p = req["method"], req["params"]
    result = {}
    if method == "initialize": result = {"schema": {"version": 1}}
    elif method == "session/start": result = {"session": {"sessionId": sid, "modelId": "muse-spark"}}
    elif method == "model/list": result = {"models": [{"modelId": "muse-spark", "displayLabel": "Muse Spark"}]}
    elif method == "turn/start":
        assert p["input"] == [{"type": "text", "text": "do the pane test"}]
        tid = p["commandId"]
        result = {"turnId": tid}
        emit({"method": "turn/started", "params": {"sessionId": sid, "turnId": tid}})
        emit({"method": "item/completed", "params": {"sessionId": sid, "item": {"itemId": "answer", "turnId": tid, "kind": "agentMessage", "revision": 1, "status": "completed", "text": "pane answer"}}})
        emit({"method": "turn/completed", "params": {"sessionId": sid, "turnId": tid, "terminal": "completed"}})
    emit({"id": req["id"], "result": result})
""",
        )
        monkeypatch.setenv("SERENA_RUNTIME_LEASE_DIR", str(tmp_path / "leases"))
        events: list[dict] = []

        async def publish(event):
            events.append(event)

        owner = MuseWorkspace(
            session_id="new:request-1",
            cwd=tmp_path,
            publish=publish,
            binary=str(binary),
        )
        created: dict = {}

        async def checkpoint(target):
            created.update(target)

        await owner.create(checkpoint=checkpoint)
        assert created["provider"] == "muse"
        assert owner.state == "ready"
        result = await owner.submit([{"type": "text", "text": "do the pane test"}])
        async with asyncio.timeout(3):
            while not any(e.get("method") == "turn/completed" for e in events):
                await asyncio.sleep(0.01)
        turn_id = result["turn"]["id"]
        methods = [event.get("method") for event in events]
        assert methods == ["workspace/history", "turn/started", "item/completed", "turn/completed"]
        completed = next(event for event in events if event.get("method") == "turn/completed")
        assert completed["params"]["turn"]["status"] == "completed"
        assert owner.state == "ready"
        assert owner.active_turn is None
        models = await owner.list_models()
        assert models["settings"]["model"] == "muse-spark"
        assert await owner.list_background_tasks() == {"data": []}
        await owner.close()
        assert owner.state == "closed"
        assert owner.can_retry_attachment() is True
        del turn_id

    asyncio.run(scenario())


def test_workspace_submit_validates_inputs_and_options(tmp_path, monkeypatch) -> None:
    import pytest

    from core.workspace_muse import MuseWorkspace, MuseWorkspaceError

    async def scenario() -> None:
        monkeypatch.setenv("SERENA_RUNTIME_LEASE_DIR", str(tmp_path / "leases"))
        owner = MuseWorkspace(
            session_id="33333333-4444-5555-6666-777777777777",
            cwd=tmp_path,
            publish=lambda event: None,
            binary="/nonexistent/muse",
        )
        with pytest.raises(MuseWorkspaceError):
            await owner.submit([{"type": "text", "text": "  "}])
        with pytest.raises(MuseWorkspaceError):
            await owner.submit([{"type": "text", "text": "hi"}], options={"mode": "plan"})
        await owner.close()

    asyncio.run(scenario())


# --- the app wiring ---------------------------------------------------------------


def _page() -> str:
    return WEB_SOURCE.read_text(encoding="utf-8")


def test_muse_launches_and_resumes_by_session_id() -> None:
    page = _page()
    body = page[page.index("def _muse_argv(") : page.index("def _ensure_resumable(")]
    assert '"muse"' in body
    assert '"resume"' in body, "no way to resume a specific chat"


def test_the_spawn_endpoint_routes_muse() -> None:
    page = _page()
    start = page.index("def api_spawn_terminal(")
    body = page[start : start + 5200]
    assert body.count("_muse_argv(") == 2, "both the resume and fresh paths need it"
    assert "conversation=sid" in body


def test_muse_appears_everywhere_the_other_agents_do() -> None:
    page = _page()
    assert "picker.innerHTML = _AGENT_PANE_ORDER.map(a =>" in page
    assert "filterMuse" in page, "no sidebar filter"
    assert "liveUsageCompactHtml('muse'" in page, "no limits pill"
    assert "_MUSE_SVG" in page, "no agent badge"
    assert "const _AGENT_PANE_ORDER = ['claude', 'codex', 'gemini', 'muse'];" in page


def test_the_menu_offers_every_agent_in_both_directions() -> None:
    page = _page()
    assert "const _HANDOFF_AGENTS = ['claude', 'codex', 'gemini', 'muse']" in page


def test_the_handoff_endpoint_accepts_muse() -> None:
    page = _page()
    start = page.index("def api_handoff(")
    body = page[start : page.index("@app.route", start + 10)]
    assert '("claude", "codex", "gemini", "muse")' in body


def test_muse_can_receive_a_context_fork() -> None:
    import pytest

    from chats.context_fork import build_context_fork

    with pytest.raises(ValueError) as bad:
        build_context_fork("", "muse")
    assert "target agent" not in str(bad.value), "muse was rejected as a destination"


def test_muse_contributes_readable_turns_to_a_fork(tmp_path, monkeypatch) -> None:
    from chats import context_fork

    sid = "44444444-5555-6666-7777-888888888888"
    log = _muse_sessions(tmp_path, monkeypatch, sid)
    assert "muse" in context_fork._FORKABLE_AGENTS
    rendered, count = context_fork._render_context(
        [
            {
                "session_id": sid,
                "agent": "muse",
                "file_path": str(log),
                "cwd": str(tmp_path),
                "title": "restructure the parser",
            }
        ],
        group_id="group-1",
    )
    assert count == 2
    assert "restructure the parser" in rendered
    assert "## Muse Chat" in rendered


def test_muse_can_be_briefed_from_like_the_transcript_agents(tmp_path, monkeypatch) -> None:
    from unittest.mock import patch

    from chats import handoff

    sid = "55555555-6666-7777-8888-999999999999"
    log = _muse_sessions(tmp_path, monkeypatch, sid)
    session = {
        "session_id": sid,
        "agent": "muse",
        "file_path": str(log),
        "cwd": str(tmp_path),
        "title": "restructure the parser",
    }
    with patch.object(handoff, "get_session", return_value=session):
        result = handoff.build_handoff_briefing(session["session_id"])
    assert result["ok"] is True
    assert result["agent"] == "muse"
    assert "restructure the parser" in result["briefing"]


def test_muse_worker_request_flows_through_the_native_provider() -> None:
    request = WorkerRequest(
        run_id="run-1",
        leg_id="muse-leg",
        attempt_id="muse-attempt",
        task="test task",
        activity="coding",
        phase="execute",
        role="core-co-implementer",
        provider="muse",
        model="muse-spark",
        effort="high",
        access_mode="write",
        cwd="/tmp",
        prompt="do it",
    )
    assert request.provider == "muse"


def test_continuity_counts_muse_as_a_cloud_subscription() -> None:
    from core.provider_health import assess_continuity

    state = assess_continuity(
        _capacity(claude=False, codex=False), now=1_786_000_000.0, probe_local=False
    )
    assert state.mode == "full"
    assert state.selected_provider == "muse"
    assert state.usable_cloud == ("muse",)


def test_all_three_out_is_degraded_or_offline() -> None:
    from core.local_model_fallback import LocalModelProfile, LocalModelStatus
    from core.provider_health import DEGRADED, OFFLINE, assess_continuity

    profile = LocalModelProfile(
        role="conversation",
        model_id="qwen2.5:14b-instruct-q4_K_M",
        parameters_b=14.0,
        quantization="Q4_K_M",
        approx_vram_gb=9.6,
        context_tokens=16_384,
    )
    local = (
        profile,
        LocalModelStatus(True, "served locally", base_url="http://127.0.0.1:11434/v1"),
    )
    state = assess_continuity(
        _capacity(claude=False, codex=False, muse=False),
        local=local,
        now=1_786_000_000.0,
    )
    assert state.mode == DEGRADED
    assert "all subscriptions are out" in state.fallback_reason

    offline = assess_continuity(
        _capacity(claude=False, codex=False, muse=False),
        local=(None, LocalModelStatus(False, "nothing loaded")),
        now=1_786_000_000.0,
    )
    assert offline.mode == OFFLINE


def test_coding_jobs_accept_muse_sessions(tmp_path, monkeypatch) -> None:
    from core.coding_jobs_query import (
        _session_file,
        _terminal_target_from_values,
    )

    sid = "66666666-7777-8888-9999-000000000000"
    log = _muse_sessions(tmp_path, monkeypatch, sid)
    found = _session_file(
        "muse",
        sid,
        codex_sessions_root=tmp_path / "codex",
        claude_projects_root=tmp_path / "claude",
    )
    assert found == log

    item_id = "77777777-8888-9999-0000-111111111111"
    target, reason = _terminal_target_from_values(
        item_id=item_id,
        state="completed",
        session_id=sid,
        project_root=str(tmp_path),
        provider="muse",
        route_mode="private",
        session_has_active_job=False,
        session_has_reuse_route=False,
        attempt_session_id=sid,
        attempt_started_at="2026-09-15T10:00:00Z",
        metadata_dir=tmp_path / "meta",
        codex_sessions_root=tmp_path / "codex",
        claude_projects_root=tmp_path / "claude",
    )
    assert "not supported" not in reason, reason
