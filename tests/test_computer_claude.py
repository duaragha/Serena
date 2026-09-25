"""The Claude worker client: subscription-only, tool-scoped, no saved transcript."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from claude_agent_sdk.types import AssistantMessage, ResultMessage, StreamEvent, ToolUseBlock

from core.computer_claude import ClaudeComputerClient
from core.computer_platform import ComputerError


def result(text="done", *, error=False):
    return ResultMessage(
        subtype="error_during_execution" if error else "success",
        duration_ms=10,
        duration_api_ms=9,
        is_error=error,
        num_turns=1,
        session_id="s",
        result=text,
    )


def delta(text):
    return StreamEvent(
        uuid="u",
        session_id="s",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
    )


class FakeSDK:
    made = []

    def __init__(self, options):
        self.options = options
        self.queries = []
        self.script = []
        self.interrupts = 0
        self.connected = False
        FakeSDK.made.append(self)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def query(self, prompt):
        if isinstance(prompt, str):
            self.queries.append(prompt)
        else:
            self.queries.append([item async for item in prompt])

    async def receive_response(self):
        for message in self.script:
            yield message

    async def interrupt(self):
        self.interrupts += 1


async def observe(_args):
    return {"content": [{"type": "text", "text": "frame"}]}


TOOLS = [
    SimpleNamespace(
        name="observe",
        description="screenshot",
        input_schema={"type": "object", "properties": {}},
        handler=observe,
    )
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    FakeSDK.made.clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-be-used")
    return ClaudeComputerClient(
        cwd=tmp_path / "agent", instructions="be serena", tools=TOOLS, sdk_client=FakeSDK
    )


def test_options_keep_the_worker_on_its_own_tools_and_subscription(client):
    asyncio.run(client.start())
    options = FakeSDK.made[0].options
    assert options.model == "claude-opus-5-5" and options.effort == "low"
    assert options.tools == [] and options.setting_sources == []
    assert options.allowed_tools == ["mcp__serena_computer__observe"]
    assert options.extra_args == {"no-session-persistence": None}  # never a sidebar chat
    assert options.env["ANTHROPIC_API_KEY"] == ""  # an inherited key cannot bill the API
    assert options.permission_mode == "dontAsk"


def test_turn_streams_deltas_sends_the_image_and_returns_the_result(client):
    asyncio.run(client.start())
    sdk = FakeSDK.made[0]
    sdk.script = [
        delta("clicking "),
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name="mcp__serena_computer__act", input={})],
            model="claude-opus-5-5",
        ),
        delta("done"),
        result("clicked continue"),
    ]
    seen = []
    reply = asyncio.run(
        client.turn(
            "next step",
            images=[{"media_type": "image/jpeg", "data": "QUJD"}],
            on_delta=seen.append,
        )
    )
    assert reply == {"text": "clicked continue", "tool_calls": ["mcp__serena_computer__act"]}
    assert seen == ["clicking ", "done"]
    content = sdk.queries[0][0]["message"]["content"]
    assert content[0]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}
    assert content[1] == {"type": "text", "text": "next step"}
    assert client.active_turn_id is None


def test_failed_or_empty_turns_raise_and_steer_needs_a_live_turn(client):
    asyncio.run(client.start())
    sdk = FakeSDK.made[0]
    sdk.script = [result("interrupted", error=True)]
    with pytest.raises(ComputerError, match="error_during_execution"):
        asyncio.run(client.turn("x"))
    sdk.script = [result("")]
    with pytest.raises(ComputerError, match="empty"):
        asyncio.run(client.turn("x"))
    with pytest.raises(ComputerError, match="no active turn"):
        asyncio.run(client.steer("go left"))


def test_reset_starts_a_fresh_session(client):
    async def scenario():
        await client.start()
        await client.reset_thread()
        await client.close()

    asyncio.run(scenario())
    assert len(FakeSDK.made) == 2 and not FakeSDK.made[0].connected
