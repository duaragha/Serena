"""Capability-brokered laptop MCP tools for the resident brain."""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from collections.abc import Mapping
from contextlib import contextmanager

from claude_agent_sdk import create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations

from core.laptop_actions import execute_laptop_action, read_laptop_context

_CURRENT_TURN: contextvars.ContextVar[Mapping[str, object] | None] = contextvars.ContextVar(
    "serena_current_brain_turn",
    default=None,
)
# The SDK dispatches in-process MCP tool handlers from its own receive task,
# which does not inherit the context the daemon set around the turn, so the
# contextvar alone always read empty inside a tool and every brokered action
# was refused as "no originating spoken turn" (observed live 2026-07-24).
# The daemon serializes turns under one lock, so a module-level mirror is a
# safe fallback and keeps the broker able to see the real words.
_ACTIVE_TURN: dict[str, object] | None = None

# A short rolling window of his recent turns, so brokers can judge intent the
# way a person does, across the conversation instead of one sentence at a
# time. "delete that one" two turns before "the memory we just talked about"
# is one instruction, not two unrelated utterances.
_RECENT_TURNS: list[dict[str, object]] = []
_RECENT_TURNS_LIMIT = 8
_PREVIOUS_USER_TURN_KEY = "_previous_user_turn"
_TURN_SCOPE_KEY = "_turn_scope"

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
    global _ACTIVE_TURN
    import time as _time

    bound_payload = dict(payload)
    # This is broker-owned context, never caller-supplied authority. Only the
    # immediately preceding genuine user turn is exposed so an explicit retry
    # cannot reach farther back through conversation history.
    bound_payload.pop(_PREVIOUS_USER_TURN_KEY, None)
    turn_scope = _turn_scope(payload)
    if _RECENT_TURNS and _RECENT_TURNS[-1].get(_TURN_SCOPE_KEY) == turn_scope:
        bound_payload[_PREVIOUS_USER_TURN_KEY] = dict(_RECENT_TURNS[-1])

    text = " ".join(str(payload.get("text") or "").split())
    if text:
        _RECENT_TURNS.append(
            {"at": _time.time(), "text": text, _TURN_SCOPE_KEY: turn_scope}
        )
        del _RECENT_TURNS[:-_RECENT_TURNS_LIMIT]
    _ACTIVE_TURN = bound_payload
    return _CURRENT_TURN.set(bound_payload)


def reset_current_turn(token) -> None:
    global _ACTIVE_TURN
    _ACTIVE_TURN = None
    _CURRENT_TURN.reset(token)


def current_turn() -> Mapping[str, object]:
    """The turn a brokered tool is running inside, contextvar or mirror."""

    return _CURRENT_TURN.get() or _ACTIVE_TURN or {}


def _turn_scope(payload: Mapping[str, object]) -> str:
    """Bind recent context to a stable local conversation when one exists."""

    for key in ("call_id", "session_id"):
        value = " ".join(str(payload.get(key) or "").split())[:256]
        if value:
            return f"{key}:{value}"
    protocol = " ".join(str(payload.get("protocol") or "plain").split())[:64]
    return f"protocol:{protocol or 'plain'}"


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
    action = str(args.get("action") or "")
    target = str(args.get("target") or "")
    # The broker's own audit stays the authoritative record of what ran. This
    # only tracks the obligation to return a result, so a call that dies with
    # the process is visible afterwards instead of vanishing.
    with _tool_call(action, target):
        result = await asyncio.to_thread(
            execute_laptop_action,
            action,
            target,
            origin=current_turn(),
        )
    return {"content": [{"type": "text", "text": str(result)}]}


@contextmanager
def _tool_call(action: str, target: str):
    """Own the promise to return this tool call's result, best effort."""

    try:
        from core.surface_journal import journal

        entry = journal("tool")
    except Exception:
        yield
        return
    call_id = f"laptop.{action or 'unknown'}.{uuid.uuid4().hex[:16]}"
    try:
        with entry.track(call_id, summary=f"run the {action or 'laptop'} action"):
            yield
    except Exception:
        raise


LAPTOP_TOOLS = (laptop_context, laptop_action)
LAPTOP_TOOL_NAMES = [
    f"mcp__serena-laptop__{item.name}" for item in LAPTOP_TOOLS
]


def laptop_tools_server():
    return create_sdk_mcp_server(name="serena-laptop", tools=list(LAPTOP_TOOLS))


def recent_turn_texts(*, max_age_seconds: float = 240.0, limit: int = 6) -> list[str]:
    """His last few spoken turns, newest last. Genuine turns only, never model
    output, so anything a broker finds here is grounded in something he said."""
    import time as _time

    cutoff = _time.time() - max_age_seconds
    texts = [str(t["text"]) for t in _RECENT_TURNS if float(t["at"]) >= cutoff]
    return texts[-limit:]


def previous_user_turn_text(*, max_age_seconds: float = 240.0) -> str:
    """The immediately preceding bound user turn, never a process-global guess."""

    import time as _time

    previous = current_turn().get(_PREVIOUS_USER_TURN_KEY)
    if not isinstance(previous, Mapping):
        return ""
    try:
        if float(previous.get("at") or 0.0) < _time.time() - max_age_seconds:
            return ""
    except (TypeError, ValueError):
        return ""
    return " ".join(str(previous.get("text") or "").split())
