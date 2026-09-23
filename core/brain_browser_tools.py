"""Her eyes and hands in the app's browser, for her voice.

The Serena desktop app has its own browser tabs (apps/desktop/
app-browser.js); these tools drive them through core.app_browser, the same
client her terminal sessions use as `chats page`. Whatever she opens shows up
in his app window, so he sees the page she is reading.

Opening, clicking and typing change what is on his screen and can submit
things, so they need a live turn from him. Looking, reading logs and taking
a screenshot do not.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations

from core.brain_laptop_tools import current_turn

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                             idempotentHint=True, openWorldHint=False)
_ACTS = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                        idempotentHint=False, openWorldHint=True)


def _text(value: Any) -> dict[str, Any]:
    body = value if isinstance(value, str) else json.dumps(value, indent=2)
    return {"content": [{"type": "text", "text": body}]}


def _failed(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def _needs_him() -> dict[str, Any] | None:
    turn = current_turn()
    if not str(turn.get("text") or "").strip():
        return _failed("no live turn from Raghav; the browser only acts because he asked")
    if str(turn.get("protocol") or "") == "soak":
        return _failed("a soak-test turn cannot drive his browser")
    return None


async def _call(command: str, **args: Any) -> dict[str, Any]:
    from core.app_browser import call

    return await asyncio.to_thread(call, command, **args)


async def _guarded(fn) -> dict[str, Any]:
    from core.app_browser import AppBrowserError

    try:
        return await fn()
    except AppBrowserError as exc:
        return _failed(f"browser: {exc}")
    except Exception as exc:  # a silent failure reads as "done"
        return _failed(f"browser: {type(exc).__name__}: {exc}")


@tool("browser_open",
      "Open a page in the browser inside Raghav's Serena app: a site, a dev server "
      "('localhost:5173') or a local file path. It shows up in his app window. "
      "'new_tab' keeps the current page open.",
      {"url": str, "new_tab": bool}, annotations=_ACTS)
async def browser_open(args):
    if (blocked := _needs_him()) is not None:
        return blocked
    args = args or {}

    async def go():
        opened = await _call("open", url=str(args.get("url") or ""),
                             newTab=bool(args.get("new_tab")) or None)
        return _text(f"opened {opened['url']} ({opened.get('title') or 'untitled'}) in tab {opened['id']}")
    return await _guarded(go)


@tool("browser_look",
      "Read the page open in his app's browser: its title, URL, visible text, and "
      "numbered elements (e1, e2, ...) you can click or type into. Look before acting "
      "and again after, since a click can change the page.",
      {}, annotations=_READ_ONLY)
async def browser_look(_args):
    from core.app_browser import render_look

    async def go():
        return _text(render_look(await _call("look")))
    return await _guarded(go)


@tool("browser_click",
      "Click something on the page in his app's browser. 'target' is a ref from "
      "browser_look (e12), a CSS selector, or 'x,y'.",
      {"target": str}, annotations=_ACTS)
async def browser_click(args):
    if (blocked := _needs_him()) is not None:
        return blocked
    from core.app_browser import target

    async def go():
        clicked = await _call("click", **target(str((args or {}).get("target") or "")))
        return _text(f"clicked {clicked['clicked']}")
    return await _guarded(go)


@tool("browser_type",
      "Type into a field on the page in his app's browser. 'target' is a ref from "
      "browser_look or a CSS selector (empty types into whatever is focused); "
      "'submit' presses Enter after. Never type his passwords, card numbers or "
      "one-time codes -- he types those himself.",
      {"target": str, "text": str, "submit": bool}, annotations=_ACTS)
async def browser_type(args):
    if (blocked := _needs_him()) is not None:
        return blocked
    from core.app_browser import target

    args = args or {}
    where = target(args["target"]) if str(args.get("target") or "").strip() else {}

    async def go():
        await _call("type", text=str(args.get("text") or ""), submit=bool(args.get("submit")) or None,
                    **where)
        return _text("typed it" + (" and pressed Enter" if args.get("submit") else ""))
    return await _guarded(go)


@tool("browser_logs",
      "Console messages and failed network requests from the page in his app's "
      "browser: the first place to look when something on a page is broken.",
      {}, annotations=_READ_ONLY)
async def browser_logs(_args):
    from core.app_browser import render_logs

    async def go():
        return _text(render_logs(await _call("logs")))
    return await _guarded(go)


@tool("browser_screenshot",
      "See the page open in his app's browser as an image, for layout and visual "
      "problems text alone does not show.",
      {}, annotations=_READ_ONLY)
async def browser_screenshot(_args):
    async def go():
        shot = await _call("screenshot")
        data = base64.b64encode(Path(shot["path"]).read_bytes()).decode()
        return {"content": [{"type": "image", "data": data, "mimeType": "image/png"},
                            {"type": "text", "text": f"screenshot of tab {shot['tab']}: {shot['path']}"}]}
    return await _guarded(go)


BROWSER_TOOLS = (browser_open, browser_look, browser_click, browser_type, browser_logs,
                 browser_screenshot)
BROWSER_TOOL_NAMES = [f"mcp__serena-browser__{t.name}" for t in BROWSER_TOOLS]


def browser_tools_server():
    return create_sdk_mcp_server(name="serena-browser", tools=list(BROWSER_TOOLS))
