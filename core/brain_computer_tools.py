"""Bind computer session creation to the resident brain's real user turn."""

from __future__ import annotations

import asyncio
import json
import re

from claude_agent_sdk import tool

from core.brain_laptop_tools import current_turn
from core.computer_client import ComputerClient
from core.computer_platform import ComputerError


def text(value):
    return {"content": [{"type": "text", "text": json.dumps(value)}]}


@tool(
    "computer_session",
    "Start Astra screen watching or a GUI task requested in the actual user turn; or stop/status. "
    "Use active by default. Scope to desktop only if the user explicitly says desktop/all screens. "
    "The dedicated visual runner streams observations to the desktop indicator and chats computer events.",
    {"operation": str, "target": str, "seconds": int, "speak": bool},
)
async def computer_session(args):
    operation = args.get("operation", "status")
    client = ComputerClient()
    try:
        if operation == "status":
            return text(await asyncio.to_thread(client.call, "status"))
        if operation == "stop":
            return text(await asyncio.to_thread(client.call, "stop", reason="stopped from Serena"))
        if operation not in {"watch", "run"}:
            raise ComputerError("operation must be watch, run, stop, or status")
        origin = current_turn()
        request = str(origin.get("text") or "").strip()
        # The model cannot authorize itself using its tool arguments. The bound
        # user request is also the worker's task, never a model-written expansion.
        subject = (
            r"\b(screen|window|desktop|display|computer|mouse|keyboard|click|type|scroll|drag)\b"
        )
        verb = r"\b(watch|look|see|view|check|read|inspect|control|use|click|type|scroll|drag|open|fill)\b"
        if (
            not request
            or not re.search(subject, request, re.I)
            or not re.search(verb, request, re.I)
        ):
            raise ComputerError("the live user turn did not request computer use")
        if operation == "run" and not re.search(
            r"\b(control|use|click|type|scroll|drag|open|fill)\b", request, re.I
        ):
            raise ComputerError("the user authorized observation only")
        target = args.get("target", "active")
        if (
            target != "active"
            and target not in request
            and (
                target != "desktop"
                or not re.search(
                    r"\b(desktop|all screens|both screens|both monitors)\b", request, re.I
                )
            )
        ):
            raise ComputerError("this broader screen scope was not requested")
        seconds = args.get("seconds", 300)
        if not isinstance(seconds, int) or not 1 <= seconds <= 300:
            raise ComputerError(
                "resident screen sessions last at most five minutes; CLI leases may last longer"
            )
        await asyncio.to_thread(client.ensure_running)
        result = await asyncio.to_thread(
            client.call,
            "run",
            mode="watch" if operation == "watch" else "control",
            target=target,
            request=request,
            seconds=seconds,
            owner=str(origin.get("session_id") or origin.get("call_id") or "resident"),
            speak=bool(args.get("speak", False)),
        )
        return text(result)
    except ComputerError as exc:
        return text({"ok": False, "error": str(exc)})


COMPUTER_TOOLS = (computer_session,)
