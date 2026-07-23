"""Capability-brokered laptop MCP tools for the resident brain."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Mapping

from claude_agent_sdk import create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations

from core.laptop_actions import execute_laptop_action, read_laptop_context

_CURRENT_TURN: contextvars.ContextVar[Mapping[str, object] | None] = contextvars.ContextVar(
    "serena_current_brain_turn",
    default=None,
)

_LOCAL_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_LOCAL_BROKERED_ACTION = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)


def set_current_turn(payload: Mapping[str, object]):
    return _CURRENT_TURN.set(dict(payload))


def reset_current_turn(token) -> None:
    _CURRENT_TURN.reset(token)


@tool(
    "laptop_context",
    "Read the local laptop's active window title and current audio state. "
    "Read-only. detail may be empty.",
    {"detail": str},
    annotations=_LOCAL_READ_ONLY,
)
async def laptop_context(_args):
    value = await asyncio.to_thread(read_laptop_context)
    return {"content": [{"type": "text", "text": str(value)}]}


@tool(
    "laptop_action",
    "Execute one reversible local desk action through Serena's capability "
    "broker. Supported actions: volume_up, volume_down, mute, unmute, "
    "toggle_mute, media_play_pause, media_next, media_previous, open_app, "
    "open_url. The broker independently checks the original local desk voice "
    "turn for fresh direct authority. Consequential actions are impossible.",
    {"action": str, "target": str},
    annotations=_LOCAL_BROKERED_ACTION,
)
async def laptop_action(args):
    result = await asyncio.to_thread(
        execute_laptop_action,
        str(args.get("action") or ""),
        str(args.get("target") or ""),
        origin=_CURRENT_TURN.get() or {},
    )
    return {"content": [{"type": "text", "text": str(result)}]}


LAPTOP_TOOLS = (laptop_context, laptop_action)
LAPTOP_TOOL_NAMES = [
    f"mcp__serena-laptop__{item.name}" for item in LAPTOP_TOOLS
]


def laptop_tools_server():
    return create_sdk_mcp_server(name="serena-laptop", tools=list(LAPTOP_TOOLS))
