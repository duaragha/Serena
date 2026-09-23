"""Her hands for coding: open her own terminals, watch them, steer them.

Three ways for her to get code written, and she picks:

- open_coding_session: a real terminal in his Serena app, briefed as the
  orchestrator for one task. It can use subagents, the linked Codex and Fleet,
  ships through a PR, and texts him when done. He can open it and watch.
- start_fleet_run (serena-fleet): a multi-agent Fleet run for a big change
  that splits into workstreams.
- start_coding_work (serena-work): the quiet headless worker for a small job
  he asked for out loud.

Every write here needs a live turn from him -- she cannot open, steer or stop
a session on her own initiative.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations

from core.brain_laptop_tools import current_turn

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                             idempotentHint=True, openWorldHint=False)
_WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                          idempotentHint=False, openWorldHint=True)


def _ok(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, indent=2)
    return {"content": [{"type": "text", "text": text}]}


def _failed(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _needs_him() -> dict[str, Any] | None:
    turn = current_turn()
    if not str(turn.get("text") or "").strip():
        return _failed("no live turn from Raghav; coding sessions only start, change or "
                       "stop because he asked")
    if str(turn.get("protocol") or "") == "soak":
        return _failed("a soak-test turn cannot touch real coding sessions")
    return None


async def _run(fn, *args, **kwargs) -> dict[str, Any]:
    from core.serena_coding import CodingSessionError

    try:
        return _ok(await asyncio.to_thread(fn, *args, **kwargs))
    except CodingSessionError as exc:
        return _failed(str(exc))
    except Exception as exc:  # a tool that dies silently reads as "done"
        return _failed(f"{type(exc).__name__}: {exc}")


@tool("open_coding_session",
      "Open a real coding terminal in Raghav's Serena app and hand it one task to run "
      "to completion as your orchestrator: it plans, uses subagents and the linked "
      "Codex, can start a Fleet run itself for a big change, ships through a PR, and "
      "texts him DONE/BLOCKED when it finishes. It also texts him where to watch it the "
      "moment it opens; when you tell him it started, say where too -- the result's 'where' "
      "(his Serena app, the project, the chat title) -- so he can see it working. "
      "Use this when he asks you to build, fix "
      "or change something in one of his projects. 'task' is the full brief: what to "
      "change, why, what done looks like, and any files you already know are involved "
      "-- the session starts cold. 'project' names the repo when the task does not "
      "(e.g. 'locket', 'serena', 'unified'). 'agent' is 'claude' (default) or 'codex'. "
      "For a large change that clearly splits into several workstreams, prefer "
      "start_fleet_run.",
      {"task": str, "project": str, "agent": str, "title": str}, annotations=_WRITES)
async def open_coding_session(args):
    if (blocked := _needs_him()) is not None:
        return blocked
    from core.serena_coding import open_session

    args = args or {}
    task = str(args.get("task") or "").strip()
    if len(task) < 12:
        return _failed("give the session a real brief: what to change and what done looks like")
    return await _run(open_session, task, project=str(args.get("project") or ""),
                      agent=str(args.get("agent") or "claude"),
                      title=str(args.get("title") or ""))


@tool("run_on_laptop",
      "Get something done on Raghav's laptop itself -- install a program or package, run "
      "commands, change a setting, clean something up, check what is installed -- by "
      "opening a visible terminal session in his Serena app that does it and proves it "
      "worked. Use this for anything on the machine that is not a change to one of his "
      "projects (for those, open_coding_session). 'task' is the full brief: what to do "
      "and what done looks like. sudo needs his password, which the session cannot "
      "type: when root is unavoidable it hands him the exact command. It texts him where "
      "to watch it and texts DONE/BLOCKED/NEEDS YOU when finished; tell him where too.",
      {"task": str, "title": str}, annotations=_WRITES)
async def run_on_laptop(args):
    if (blocked := _needs_him()) is not None:
        return blocked
    from core.serena_coding import open_session

    args = args or {}
    task = str(args.get("task") or "").strip()
    if len(task) < 8:
        return _failed("say what to do on the laptop and what done looks like")
    return await _run(open_session, task, title=str(args.get("title") or ""),
                      on_machine=True)


@tool("coding_sessions",
      "Your coding sessions, newest first: each one's task, project and state -- "
      "starting, working, waiting (quiet, likely waiting on input), done, blocked or stopped -- "
      "with its last message. Use it when he asks how the coding is going.",
      {"include_finished": bool}, annotations=_READ_ONLY)
async def coding_sessions(args):
    from core.serena_coding import list_sessions

    include = (args or {}).get("include_finished")
    return await _run(list_sessions, include_finished=True if include is None else bool(include))


@tool("read_coding_session",
      "What one of your coding sessions has been saying: its state and its last few "
      "messages. 'session' is its id, title words, or session id prefix.",
      {"session": str, "messages": int}, annotations=_READ_ONLY)
async def read_coding_session(args):
    from core.serena_coding import read_session

    args = args or {}
    return await _run(read_session, str(args.get("session") or ""),
                      messages=int(args.get("messages") or 6))


@tool("steer_coding_session",
      "Type a message into one of your running coding sessions, the way he would: a "
      "correction, a new requirement, an answer to its question, 'stop and ship what "
      "you have'. Returns whatever it replies within ~20s; it keeps working after.",
      {"session": str, "message": str}, annotations=_WRITES)
async def steer_coding_session(args):
    if (blocked := _needs_him()) is not None:
        return blocked
    from core.serena_coding import steer_session

    args = args or {}
    message = str(args.get("message") or "").strip()
    if not message:
        return _failed("steer needs a message")
    return await _run(steer_session, str(args.get("session") or ""), message)


@tool("stop_coding_session",
      "Stop one of your coding sessions by closing its terminal. Its transcript stays.",
      {"session": str}, annotations=_WRITES)
async def stop_coding_session(args):
    if (blocked := _needs_him()) is not None:
        return blocked
    from core.serena_coding import stop_session

    return await _run(stop_session, str((args or {}).get("session") or ""))


CODE_TOOLS = (open_coding_session, run_on_laptop, coding_sessions, read_coding_session,
              steer_coding_session, stop_coding_session)
CODE_TOOL_NAMES = [f"mcp__serena-code__{t.name}" for t in CODE_TOOLS]


def code_tools_server():
    return create_sdk_mcp_server(name="serena-code", tools=list(CODE_TOOLS))
