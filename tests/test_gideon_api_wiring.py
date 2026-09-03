from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core import brain_daemon, brain_gideon_tools
from core.action_authority import ActionAuthority
from core.adapters.base import AdapterRegistry
from core.brain_lifetime import LifetimeLedger, RecentThreadJournal
from core.commitments import CommitmentStore
from core.device_actions import DeviceActionRunner
from core.gideon_api import GideonAPI
from core.local_model_fallback import LocalModelUnavailable
from core.provider_health import ContinuityStore
from core.runtime_readiness import RuntimeLedger
from core.state_graph import StateGraphStore
from core.supportive_mode import SupportiveModeStore
from core.voice_transcripts import VoiceTranscriptStore


class CapturedOptions:
    def __init__(self, **values) -> None:
        self.__dict__.update(values)


def _capacity(*, claude: bool, codex: bool) -> dict:
    return {
        "claude": {
            "status": "available" if claude else "unavailable",
            "usable": claude,
            "reason": "test",
        },
        "codex": {
            "status": "available" if codex else "unavailable",
            "usable": codex,
            "reason": "test",
        },
    }


def _api(tmp_path: Path) -> GideonAPI:
    authority = ActionAuthority(
        tmp_path / "authority.sqlite3",
        audit_path=tmp_path / "authority.jsonl",
        publish_events=False,
    )
    devices = DeviceActionRunner(
        authority=authority,
        adapters=AdapterRegistry(),
        scenes={},
        default_dry_run=True,
    )
    return GideonAPI(
        commitments=CommitmentStore(tmp_path / "commitments.sqlite3"),
        state_graph=StateGraphStore(tmp_path / "state.sqlite3"),
        continuity=ContinuityStore(tmp_path / "continuity.sqlite3"),
        runtime_ledger=RuntimeLedger(tmp_path / "runtime.sqlite3"),
        devices=devices,
        supportive=SupportiveModeStore(tmp_path / "support.sqlite3"),
    )


def _tool_json(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_api_exposes_one_provider_neutral_status_and_briefing(tmp_path: Path) -> None:
    api = _api(tmp_path)
    status = api.status(capacity=_capacity(claude=True, codex=True), probe_local=False)

    assert status["continuity"]["mode"] == "full"
    assert status["commitments"] == {"open": 0, "overdue": 0}
    assert status["visual"]["available"] is False
    assert status["devices"]["default_dry_run"] is True
    assert api.briefing("morning")["spoken"] == "nothing on the books today, you're clear"


def test_commitment_tool_requires_live_matching_words_and_records_authority(
    monkeypatch,
    tmp_path: Path,
) -> None:
    api = _api(tmp_path)
    monkeypatch.setattr(brain_gideon_tools, "_api", lambda: api)
    monkeypatch.setattr(
        brain_gideon_tools,
        "current_turn",
        lambda: {
            "protocol": "voice",
            "call_id": "call-1",
            "turn_id": "turn-1",
            "text": "remind me to call mom tomorrow",
        },
    )
    created = asyncio.run(
        brain_gideon_tools.gideon_commitments.handler(
            {
                "action": "create",
                "commitment_id": "",
                "payload_json": json.dumps({"title": "call mom tomorrow"}),
            }
        )
    )
    payload = _tool_json(created)

    assert payload["ok"] is True
    assert payload["changed"] is True
    assert payload["commitment"]["state"] == "accepted"
    assert api.device_runner.authority.history(capability="commitment.create")[0][
        "outcome_status"
    ] == "completed"

    monkeypatch.setattr(
        brain_gideon_tools,
        "current_turn",
        lambda: {
            "protocol": "voice",
            "call_id": "call-2",
            "turn_id": "turn-2",
            "text": "what is on my list?",
        },
    )
    refused = asyncio.run(
        brain_gideon_tools.gideon_commitments.handler(
            {
                "action": "create",
                "commitment_id": "",
                "payload_json": json.dumps({"title": "buy a television"}),
            }
        )
    )
    assert _tool_json(refused) == {
        "ok": False,
        "changed": False,
        "error": "Raghav's live turn did not directly request this action",
    }
    assert len(api.list_commitments()) == 1


def test_support_tool_never_returns_raw_reflection_text(monkeypatch, tmp_path: Path) -> None:
    api = _api(tmp_path)
    monkeypatch.setattr(brain_gideon_tools, "_api", lambda: api)
    monkeypatch.setattr(
        brain_gideon_tools,
        "current_turn",
        lambda: {
            "protocol": "chat",
            "turn_id": "support-1",
            "text": "turn on supportive reflections and pattern insights",
        },
    )
    enabled = asyncio.run(
        brain_gideon_tools.gideon_support.handler(
            {
                "action": "configure",
                "payload_json": json.dumps(
                    {"enabled": True, "allow_pattern_insights": True}
                ),
            }
        )
    )
    assert _tool_json(enabled)["ok"] is True

    monkeypatch.setattr(
        brain_gideon_tools,
        "current_turn",
        lambda: {
            "protocol": "chat",
            "turn_id": "support-2",
            "text": "save this reflection in my journal: today felt much calmer",
        },
    )
    reflected = asyncio.run(
        brain_gideon_tools.gideon_support.handler(
            {
                "action": "reflect",
                "payload_json": json.dumps(
                    {"body": "today felt much calmer", "mood": "steady"}
                ),
            }
        )
    )
    assert _tool_json(reflected)["ok"] is True

    status = asyncio.run(
        brain_gideon_tools.gideon_support.handler(
            {"action": "status", "payload_json": "{}"}
        )
    )
    text = status["content"][0]["text"]
    assert "today felt much calmer" not in text
    assert "reflections" not in _tool_json(status)


def test_support_context_reaches_every_provider_without_raw_reflection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import core.supportive_mode as supportive_mode

    path = tmp_path / "provider-support.sqlite3"
    monkeypatch.setattr(supportive_mode, "DEFAULT_SUPPORT_DB", path)
    store = SupportiveModeStore(path)
    store.configure(
        enabled=True,
        allow_pattern_insights=True,
        relationship_context="Use the approved calm coaching style.",
    )
    store.add_reflection(
        "raw private words must not reach a provider",
        source="test",
        mood="steady",
        tags=("grounded",),
    )
    monkeypatch.setattr(brain_daemon, "_state_block", lambda force=False: "")
    monkeypatch.setattr(brain_daemon, "_clock_block", lambda: "")

    message = brain_daemon._compose_message({"protocol": "plain", "text": "talk to me"})

    assert "Use the approved calm coaching style." in message
    assert "raw private words must not reach a provider" not in message


class FakeLocalBrain:
    model = "qwen2.5:14b-instruct-q4_K_M"

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.started = False
        self.closed = False
        self.messages: list[str] = []

    async def start(self) -> None:
        if not self.available:
            raise LocalModelUnavailable("test local model is not loaded")
        self.started = True

    async def turn(self, message: str, *, on_delta=None) -> dict:
        self.messages.append(message)
        if on_delta is not None:
            await on_delta("local answer")
        return {
            "text": "local answer",
            "provider": "local",
            "model": self.model,
            "origin": "local-model",
        }

    async def interrupt(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    def snapshot(self) -> dict:
        return {"enabled": True, "running": self.started, "model": self.model}


def _manager(tmp_path: Path, local: FakeLocalBrain) -> brain_daemon.ResidentClientManager:
    return brain_daemon.ResidentClientManager(
        CapturedOptions,
        object,
        lambda: {},
        [],
        journal=RecentThreadJournal(tmp_path / "thread.json"),
        lifetime=LifetimeLedger(tmp_path / "lifetime.json"),
        voice_transcripts=VoiceTranscriptStore(tmp_path / "voice.jsonl"),
        local_brain_factory=lambda: local,
        continuity_store=ContinuityStore(tmp_path / "continuity.sqlite3"),
        capacity_reader=lambda: _capacity(claude=False, codex=False),
        capacity_cache_seconds=0,
    )


def test_both_cloud_limits_route_the_real_turn_to_local_model(
    monkeypatch,
    tmp_path: Path,
) -> None:
    local = FakeLocalBrain()
    manager = _manager(tmp_path, local)
    monkeypatch.setattr(brain_daemon, "_state_block", lambda force=False: "")
    monkeypatch.setattr(brain_daemon, "_clock_block", lambda: "")
    monkeypatch.setattr(brain_daemon, "_recalled_voice_history_block", lambda _text: "")

    result = asyncio.run(
        brain_daemon._run_turn_answered(
            manager,
            {"protocol": "plain", "text": "are you still there?", "turn_id": "local-1"},
        )
    )

    assert result["ok"] is True
    assert result["provider"] == "local"
    assert result["model"] == local.model
    assert result["say"] == "local answer"
    assert local.messages and "are you still there?" in local.messages[0]


def test_no_cloud_or_local_model_keeps_turn_and_returns_honest_offline_reply(
    monkeypatch,
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, FakeLocalBrain(available=False))
    monkeypatch.setattr(brain_daemon, "_state_block", lambda force=False: "")
    monkeypatch.setattr(brain_daemon, "_clock_block", lambda: "")
    monkeypatch.setattr(brain_daemon, "_recalled_voice_history_block", lambda _text: "")

    result = asyncio.run(
        brain_daemon._run_turn_answered(
            manager,
            {"protocol": "plain", "text": "finish this later", "turn_id": "offline-1"},
        )
    )

    assert result["ok"] is True
    assert result["provider"] == "offline"
    assert result["deferred_work_id"]
    assert "saved this turn" in result["say"]
    pending = manager.continuity_store.pending()
    assert len(pending) == 1
    assert pending[0].payload["text"] == "finish this later"


def test_second_live_cloud_limit_respects_first_runtime_block_and_uses_local(
    tmp_path: Path,
) -> None:
    local = FakeLocalBrain()
    manager = _manager(tmp_path, local)
    manager.capacity_reader = lambda: _capacity(claude=True, codex=True)
    manager._claude_blocked_until = brain_daemon.time.time() + 300

    asyncio.run(manager.mark_provider_unavailable("codex", "live usage limit"))

    assert manager._active_provider == "local"
    assert local.started is True


def test_gideon_tools_are_available_to_both_provider_runtimes(tmp_path: Path) -> None:
    server = brain_gideon_tools.gideon_tools_server()
    options = brain_daemon._build_agent_options(
        CapturedOptions,
        {},
        [],
        gideon_tools=server,
        gideon_tool_names=brain_gideon_tools.GIDEON_TOOL_NAMES,
    )

    assert "serena-gideon" in options.mcp_servers
    assert options.allowed_tools == brain_gideon_tools.GIDEON_TOOL_NAMES

    from core.codex_brain_tools import build_serena_codex_brain_tools

    registry = build_serena_codex_brain_tools()
    assert "serena_gideon.gideon_status" in registry.names()
    assert "serena_gideon.gideon_commitments" in registry.names()
