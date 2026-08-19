from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from core import brain_daemon
from core.brain_lifetime import LifetimeLedger, RecentThreadJournal
from core.brain_provider import (
    BrainProviderUnavailable,
    choose_brain_provider,
    is_usage_limit_error,
)
from core.fleet_capacity import ProviderCapacity
from core.voice_transcripts import VoiceTranscriptStore


class CapturedOptions:
    def __init__(self, **values) -> None:
        self.__dict__.update(values)


def _message(kind: str, **values):
    return type(kind, (), values)()


def _capacity(*, claude: bool, codex: bool):
    return {
        "claude": ProviderCapacity(
            provider="claude",
            status="available" if claude else "unavailable",
            usable=claude,
            source="test",
            reason="test",
        ),
        "codex": ProviderCapacity(
            provider="codex",
            status="available" if codex else "unavailable",
            usable=codex,
            source="test",
            reason="test",
        ),
    }


class FakeCodexBrain:
    model = "gpt-5.6-terra"
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

    async def turn(self, message: str, *, on_delta=None) -> dict:
        self.messages.append(message)
        if on_delta is not None:
            await on_delta("fallback reply")
        return {
            "text": "fallback reply",
            "thread_id": "codex-thread",
            "turn_id": "codex-turn",
        }

    async def interrupt(self) -> None:
        return None

    async def close(self) -> None:
        self.closed += 1

    def snapshot(self) -> dict:
        return {"enabled": True, "running": True, "model": self.model}


class LimitReplyClaude:
    async def query(self, _text: str) -> None:
        return None

    async def receive_response(self):
        yield _message(
            "AssistantMessage",
            content=[
                SimpleNamespace(
                    text="You've hit your session limit · resets 1:40pm (America/Toronto)"
                )
            ],
        )
        yield _message("ResultMessage", session_id="claude-session", total_cost_usd=0)

    async def set_model(self, _model: str) -> None:
        return None

    async def interrupt(self) -> None:
        return None


class StartupLimitClaude:
    def __init__(self, *, options) -> None:
        self.options = options
        self.responses: list[list[object]] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, _text: str) -> None:
        self.responses.append(
            [
                _message(
                    "ResultMessage",
                    session_id=self.options.session_id,
                    is_error=True,
                    result="You've hit your session limit · resets 1:40pm",
                    errors=[],
                    api_error_status=429,
                ),
            ]
        )

    async def receive_response(self):
        for message in self.responses.pop(0):
            yield message


def _manager(tmp_path: Path, claude_type=StartupLimitClaude):
    codex = FakeCodexBrain()
    manager = brain_daemon.ResidentClientManager(
        CapturedOptions,
        claude_type,
        lambda: {},
        [],
        journal=RecentThreadJournal(tmp_path / "thread.json"),
        lifetime=LifetimeLedger(tmp_path / "lifetime.json"),
        voice_transcripts=VoiceTranscriptStore(tmp_path / "voice.jsonl"),
        codex_brain_factory=lambda: codex,
        capacity_reader=lambda: _capacity(claude=True, codex=True),
        capacity_cache_seconds=0,
    )
    return manager, codex


def test_auto_provider_prefers_claude_and_falls_back_only_when_exhausted() -> None:
    assert choose_brain_provider(_capacity(claude=True, codex=True)) == "claude"
    assert choose_brain_provider(_capacity(claude=False, codex=True)) == "codex"
    assert choose_brain_provider(None) == "claude"
    try:
        choose_brain_provider(_capacity(claude=False, codex=False))
    except BrainProviderUnavailable as exc:
        assert "Claude and Codex" in str(exc)
    else:
        raise AssertionError("two exhausted providers were accepted")


def test_shared_policy_preference_can_choose_codex_without_disabling_fallback() -> None:
    assert choose_brain_provider(
        _capacity(claude=True, codex=True),
        preferred_provider="codex",
    ) == "codex"
    assert choose_brain_provider(
        _capacity(claude=True, codex=False),
        preferred_provider="codex",
    ) == "claude"


def test_usage_limit_recognizes_the_normal_claude_reply() -> None:
    assert is_usage_limit_error("You've hit your session limit · resets 1:40pm")
    assert is_usage_limit_error(RuntimeError("rate limit exceeded: 429"))
    assert not is_usage_limit_error("explain how usage limits work")


def test_normal_limit_reply_is_replaced_by_codex_and_never_journaled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        manager, codex = _manager(tmp_path)
        manager.provider_override = "claude"
        manager.client = LimitReplyClaude()
        manager.session_id = "claude-session"
        monkeypatch.setattr(brain_daemon, "_state_block", lambda force=False: "")
        monkeypatch.setattr(brain_daemon, "_clock_block", lambda: "")
        monkeypatch.setattr(brain_daemon, "_recalled_voice_history_block", lambda _text: "")
        out = await brain_daemon._run_turn(
            manager,
            {"protocol": "voice", "text": "are you there?", "turn_id": "voice-1"},
        )

        assert out["provider"] == "codex"
        assert out["model"] == "gpt-5.6-terra"
        assert out["say"] == "fallback reply"
        assert manager.session_id == "claude-session"
        assert codex.messages and "are you there?" in codex.messages[0]
        handoff = manager.journal.render_handoff()
        assert "session limit" not in handoff
        assert "fallback reply" not in handoff
        assert manager.journal.snapshot()["pending_entries"] == 1
        await manager.response_committed(out)
        assert "fallback reply" in manager.journal.render_handoff()
        assert "session limit" not in manager.journal.render_handoff()
        await manager._close_codex_brain()

    asyncio.run(scenario())


def test_complex_brain_turn_switches_the_codex_worker_for_that_turn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        manager, codex = _manager(tmp_path)
        monkeypatch.setattr(brain_daemon, "_state_block", lambda force=False: "")
        monkeypatch.setattr(brain_daemon, "_clock_block", lambda: "")
        monkeypatch.setattr(brain_daemon, "_recalled_voice_history_block", lambda _text: "")

        out = await brain_daemon._run_turn(
            manager,
            {
                "protocol": "voice",
                "text": "design the architecture and reason through the tradeoffs",
                "turn_id": "voice-hard",
            },
        )

        assert out["provider"] == "codex"
        assert out["model"] == "gpt-5.6-sol"
        assert out["effort"] == "high"
        assert out["route_lane"] == "complex"
        assert codex.model == "gpt-5.6-sol"
        assert codex.effort == "high"
        await manager._close_codex_brain()

    asyncio.run(scenario())


def test_startup_limit_keeps_daemon_available_through_codex(
    monkeypatch,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        manager, codex = _manager(tmp_path)
        monkeypatch.setattr(brain_daemon, "_state_block", lambda force=False: "")
        monkeypatch.setattr(brain_daemon, "_persona_context", lambda: "persona")
        monkeypatch.setattr(
            brain_daemon,
            "process_tree_snapshot",
            lambda: {
                "available": True,
                "root_pid": 1,
                "rss_bytes": 0,
                "descendants": 0,
                "processes": [],
            },
        )

        async def no_survivors(*_args, **_kwargs):
            return []

        monkeypatch.setattr(brain_daemon, "reap_processes", no_survivors)
        await manager.start()
        assert manager.client is None
        assert manager.snapshot()["provider"] == "codex"
        assert codex.started >= 1
        await manager.stop()
        assert codex.closed == 1

    asyncio.run(scenario())
