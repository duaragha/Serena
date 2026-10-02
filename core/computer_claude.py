"""Claude as the visual worker, behind the interface the agent loop already uses.

The computer agent drives one model client through start/turn/interrupt/steer/
reset_thread/close. This runs Claude through the Agent SDK against the installed
`claude` CLI on Raghav's subscription. The session's observe/act tools are its
only tools, served in-process over MCP: no built-in tools, no user settings or
hooks, and no saved transcript, so a worker never appears as a chat.

Opus 5.5 replaced GPT-6 Astra on 2026-09-25. On the same screenshot and prompt it
decided in 1.9-3.2 s against Astra's 3.2-3.5 s, and it has the best published
OSWorld 2.0 score. SERENA_COMPUTER_MODEL switches the model (e.g. claude-sonnet-5,
which measured 0.8 s) without a code change.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import shutil
from pathlib import Path

from core.billing import METERED_AUTH_ENV_VARS, strip_metered_auth_env
from core.computer_platform import ComputerError

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "low"
MODEL_ENV = "SERENA_COMPUTER_MODEL"
EFFORT_ENV = "SERENA_COMPUTER_EFFORT"
SERVER = "serena_computer"
START_TIMEOUT_SECONDS = 30.0
TURN_TIMEOUT_SECONDS = 180.0
# Claude downsamples larger screenshots before the model sees them, so the
# coordinates it answers with would not map back to the frame. 1280 px wide
# stays under that limit for any 16:9 or 16:10 screen.
FRAME_WIDTH = 1280


def computer_model():
    return os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


def computer_effort():
    return os.environ.get(EFFORT_ENV, "").strip() or DEFAULT_EFFORT


def _sdk_tool(spec):
    from claude_agent_sdk import SdkMcpTool

    async def handler(args):
        try:
            result = await spec.handler(args or {})
        except Exception as exc:
            # The model sees the refusal and decides; the turn keeps going.
            return {"content": [{"type": "text", "text": str(exc)[:500]}], "is_error": True}
        return {
            "content": result["content"],
            "is_error": bool(result.get("isError") or result.get("is_error")),
        }

    return SdkMcpTool(spec.name, spec.description, spec.input_schema, handler)


class ClaudeComputerClient:
    """One Claude session for one computer session; reset starts a fresh one."""

    accepted_service_tier = None

    def __init__(
        self,
        *,
        cwd,
        instructions,
        tools,
        model=None,
        effort=None,
        turn_timeout=TURN_TIMEOUT_SECONDS,
        cli_path=None,
        sdk_client=None,
    ):
        self.cwd = Path(cwd)
        self.instructions = instructions
        self.tools = list(tools)
        self.model = model or computer_model()
        self.effort = effort or computer_effort()
        self.turn_timeout = float(turn_timeout)
        self.cli_path = cli_path or shutil.which("claude")
        self.sdk_client = sdk_client  # tests inject a fake ClaudeSDKClient class
        self.client = None
        self.active_turn_id = None
        self._turns = 0

    def _options(self):
        from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

        server = create_sdk_mcp_server(SERVER, tools=[_sdk_tool(spec) for spec in self.tools])
        inherited = dict(os.environ)
        clean = strip_metered_auth_env(inherited)
        # The SDK overlays env onto os.environ: blank the billing routes so an
        # inherited API key can never move the worker off the subscription.
        blanked = {
            key: "" for key in set(METERED_AUTH_ENV_VARS) | (inherited.keys() - clean.keys())
        }
        return ClaudeAgentOptions(
            model=self.model,
            effort=self.effort,
            system_prompt=self.instructions,
            tools=[],
            mcp_servers={SERVER: server},
            strict_mcp_config=True,
            allowed_tools=[f"mcp__{SERVER}__{spec.name}" for spec in self.tools],
            permission_mode="dontAsk",
            setting_sources=[],
            skills=[],
            include_partial_messages=True,
            cwd=str(self.cwd),
            cli_path=self.cli_path,
            env=blanked,
            extra_args={"no-session-persistence": None},
            max_turns=400,
        )

    async def start(self):
        if self.client is not None:
            return
        if not self.cli_path and self.sdk_client is None:
            raise ComputerError("computer use needs the installed claude CLI")
        self.cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.sdk_client is None:
            from claude_agent_sdk import ClaudeSDKClient

            factory = ClaudeSDKClient
        else:
            factory = self.sdk_client
        client = factory(options=self._options())
        try:
            await asyncio.wait_for(client.connect(), START_TIMEOUT_SECONDS)
        except BaseException:
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise
        self.client = client

    async def turn(self, message, *, images=None, on_delta=None):
        await self.start()
        content = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": image["media_type"], "data": image["data"]},
            }
            for image in images or []
        ]
        content.append({"type": "text", "text": message})
        self._turns += 1
        self.active_turn_id = f"turn-{self._turns}"
        try:
            return await asyncio.wait_for(self._run(content, on_delta), self.turn_timeout)
        except BaseException:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.interrupt(), timeout=2)
            raise
        finally:
            self.active_turn_id = None

    async def _run(self, content, on_delta):
        from claude_agent_sdk.types import (
            AssistantMessage,
            ResultMessage,
            StreamEvent,
            ToolUseBlock,
        )

        async def stream():
            yield {
                "type": "user",
                "message": {"role": "user", "content": content},
                "parent_tool_use_id": None,
                "session_id": "default",
            }

        await self.client.query(stream())
        tool_calls = []
        async for message in self.client.receive_response():
            if isinstance(message, StreamEvent):
                event = message.event
                delta = event.get("delta") or {}
                if (
                    on_delta is not None
                    and event.get("type") == "content_block_delta"
                    and delta.get("type") == "text_delta"
                    and delta.get("text")
                ):
                    emitted = on_delta(delta["text"])
                    if inspect.isawaitable(emitted):
                        await emitted
            elif isinstance(message, AssistantMessage):
                tool_calls.extend(
                    block.name for block in message.content if isinstance(block, ToolUseBlock)
                )
            elif isinstance(message, ResultMessage):
                if message.is_error:
                    raise ComputerError(
                        f"Claude turn {message.subtype}: {message.result or ''}".strip()[:300]
                    )
                text = (message.result or "").strip()
                if not text:
                    raise ComputerError("Claude returned an empty reply")
                return {"text": text, "tool_calls": tool_calls}
        raise ComputerError("Claude ended the turn without a result")

    async def interrupt(self):
        if self.client is not None and self.active_turn_id:
            await self.client.interrupt()

    async def steer(self, message):
        # The CLI takes a user message sent mid-turn at the next tool boundary,
        # inside the same turn; the turn still ends with a single result.
        if self.client is None or not self.active_turn_id:
            raise ComputerError("no active turn to steer")
        await self.client.query(message)

    async def reset_thread(self):
        """A fresh Claude session: bounds screenshot history in long watches."""
        if self.active_turn_id:
            raise ComputerError("cannot reset while a turn is active")
        await self.close()
        await self.start()

    async def close(self):
        client, self.client = self.client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
