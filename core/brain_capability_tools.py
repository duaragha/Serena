"""Small resident-brain surface for Serena's on-demand MCP catalog."""

from __future__ import annotations

import json

from claude_agent_sdk import create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations

from core.brain_laptop_tools import current_turn
from core.mcp.capability_broker import find_capabilities, invoke_capability

_DISCOVERY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
_BROKERED_USE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)


def _text(value: object) -> dict:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return {"content": [{"type": "text", "text": value}]}


@tool(
    "find_pc_capability",
    "Find the right live MCP tool for information or an action. Search here "
    "when Raghav asks about data or services outside Serena's built-in memory, "
    "including Beeper, Railway, Amazon, Shopify, Google Ads, Merchant Center, "
    "DNS, NetSuite, Supabase, current package docs, shopping, or restaurants. "
    "This loads only matching tool schemas, so use a short description of the "
    "actual need rather than guessing a server name.",
    {"query": str},
    annotations=_DISCOVERY,
)
async def find_pc_capability(args):
    return _text(await find_capabilities(str(args.get("query") or "")))


@tool(
    "use_pc_capability",
    "Invoke one capability returned by find_pc_capability. Pass its exact "
    "server and tool names plus a JSON object matching input_schema. Reads run "
    "silently. Writes run only when Raghav's current real turn directly asks "
    "for that action, and the broker refuses otherwise. Never claim a refused "
    "or failed call happened.",
    {"server": str, "tool": str, "arguments_json": str},
    annotations=_BROKERED_USE,
)
async def use_pc_capability(args):
    try:
        arguments = json.loads(str(args.get("arguments_json") or "{}"))
    except json.JSONDecodeError:
        return _text({"ok": False, "called": False, "error": "arguments_json is not valid JSON"})
    if not isinstance(arguments, dict):
        return _text({"ok": False, "called": False, "error": "arguments_json must contain a JSON object"})
    result = await invoke_capability(
        str(args.get("server") or ""),
        str(args.get("tool") or ""),
        arguments,
        origin=current_turn(),
    )
    return _text(result)


CAPABILITY_TOOLS = (find_pc_capability, use_pc_capability)
CAPABILITY_TOOL_NAMES = [
    f"mcp__serena-capabilities__{item.name}" for item in CAPABILITY_TOOLS
]


def capability_tools_server():
    return create_sdk_mcp_server(
        name="serena-capabilities",
        tools=list(CAPABILITY_TOOLS),
    )
