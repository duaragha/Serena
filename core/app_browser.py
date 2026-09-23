"""Drive the in-app browser from outside the app.

The desktop app draws browser tabs inside its own window (apps/desktop/
app-browser.js) and serves a token-guarded control server on 127.0.0.1. This
is the other end: her terminal sessions use it through `chats page ...`,
her voice through the brain's browser tools. Both drive the tabs he is
looking at, so he watches the same page they read and click.

    chats page open localhost:5173
    chats page look                 # page text + numbered elements (e1, e2, ...)
    chats page click e12
    chats page type e5 "hello" --submit
    chats page logs                 # console errors and failed requests
    chats page screenshot
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

EDITIONS = ("stable", "dev")


class AppBrowserError(RuntimeError):
    pass


def _config_root() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _alive(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(pid)
    except ImportError:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True


def control() -> dict[str, Any]:
    """The running app's control file. Main (stable) first: it is the window
    he lives in; SERENA_BROWSER_EDITION=dev picks Serena Dev."""

    override = os.environ.get("SERENA_BROWSER_CONTROL", "").strip()
    candidates = [Path(override)] if override else []
    preferred = os.environ.get("SERENA_BROWSER_EDITION", "").strip().lower()
    order = sorted(EDITIONS, key=lambda edition: edition != preferred) if preferred else EDITIONS
    candidates += [_config_root() / f"serena-desktop-{edition}" / "app-browser.json" for edition in order]
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("port") and data.get("token") \
                and _alive(int(data.get("pid") or 0)):
            return data
    raise AppBrowserError("the Serena app is not running, or its build has no in-app browser yet")


def call(command: str, timeout: float = 30, **args: Any) -> dict[str, Any]:
    info = control()
    body = json.dumps({key: value for key, value in args.items() if value is not None}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}/browser/{command}", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {info['token']}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise AppBrowserError(f"browser {command}: HTTP {exc.code} {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AppBrowserError(f"could not reach the app's browser: {exc}") from exc
    if not result.get("ok"):
        raise AppBrowserError(str(result.get("error") or f"browser {command} failed"))
    result.pop("ok", None)
    return result


def target(value: str) -> dict[str, Any]:
    """'e12' is a ref from look, '120,340' a point, anything else a CSS selector."""

    text = str(value).strip()
    if text[:1] == "e" and text[1:].isdigit():
        return {"ref": text}
    x, sep, y = text.partition(",")
    if sep and x.strip().lstrip("-").isdigit() and y.strip().lstrip("-").isdigit():
        return {"x": int(x), "y": int(y)}
    return {"selector": text}


def render_look(page: dict[str, Any], *, text_limit: int = 4000) -> str:
    lines = [f"{page.get('title') or '(untitled)'} -- {page.get('url')}"]
    elements = page.get("elements") or []
    if elements:
        lines.append("")
        for item in elements:
            bits = [item["ref"], item.get("role") or item.get("tag", "")]
            if item.get("type") and item.get("tag") == "input":
                bits.append(f"[{item['type']}]")
            if item.get("name"):
                bits.append(json.dumps(item["name"]))
            if item.get("disabled"):
                bits.append("(disabled)")
            if item.get("checked"):
                bits.append("(checked)")
            if item.get("offscreen"):
                bits.append("(scroll)")
            lines.append("  " + " ".join(bits))
    text = (page.get("text") or "").strip()
    if text:
        lines += ["", text[:text_limit] + ("\n..." if len(text) > text_limit else "")]
    return "\n".join(lines)


def render_logs(logs: dict[str, Any]) -> str:
    lines = []
    for entry in logs.get("console") or []:
        lines.append(f"console {entry.get('level')}: {entry.get('message')}"
                     + (f"  ({entry.get('source')}:{entry.get('line')})" if entry.get("source") else ""))
    for entry in logs.get("network") or []:
        what = entry.get("status") or entry.get("error")
        lines.append(f"network {what}: {entry.get('method') or ''} {entry.get('url')}".replace("  ", " "))
    return "\n".join(lines) or "no console messages or failed requests"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chats page", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tab", help="browser tab id (default: the active one)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("tabs", help="list tabs")
    p = sub.add_parser("open", help="open a URL, dev server (localhost:5173) or file path")
    p.add_argument("url")
    p.add_argument("--new", action="store_true", help="in a new tab")
    sub.add_parser("look", help="page text and numbered elements")
    p = sub.add_parser("click", help="click e12, a CSS selector, or x,y")
    p.add_argument("target")
    p = sub.add_parser("type", help="type into e5 / a selector (focused element if omitted)")
    p.add_argument("target", nargs="?")
    p.add_argument("text")
    p.add_argument("--submit", action="store_true", help="press Enter after")
    p.add_argument("--append", action="store_true", help="keep what is already in the field")
    p = sub.add_parser("press", help="press a key: Enter, Tab, Escape, ArrowDown, a")
    p.add_argument("key")
    p = sub.add_parser("eval", help="run JavaScript in the page and print the result")
    p.add_argument("js")
    sub.add_parser("screenshot", help="save a PNG of the page and print its path")
    p = sub.add_parser("logs", help="console messages and failed requests")
    p.add_argument("--since", type=float, default=0)
    for name in ("back", "forward", "reload", "close"):
        sub.add_parser(name)
    p = sub.add_parser("wait", help="wait for text (or --selector) to appear")
    p.add_argument("text")
    p.add_argument("--selector", action="store_true", help="treat TEXT as a CSS selector")
    p.add_argument("--timeout", type=float, default=10)
    return parser


def run(args: argparse.Namespace) -> str:
    tab = args.tab
    command = args.command
    if command == "tabs":
        tabs = call("tabs")["tabs"]
        return "\n".join(f"{t['id']}{' *' if t['active'] else ''}  {t['title'] or '(untitled)'}  {t['url']}"
                         + ("  (unloaded)" if t.get("unloaded") else "") for t in tabs) or "no tabs open"
    if command == "open":
        opened = call("open", url=args.url, newTab=args.new or None, tab=tab)
        return f"{opened['id']}  {opened.get('title') or ''}  {opened['url']}".strip()
    if command == "look":
        return render_look(call("look", tab=tab))
    if command == "click":
        clicked = call("click", tab=tab, **target(args.target))
        return f"clicked {clicked['clicked']}"
    if command == "type":
        where = target(args.target) if args.target else {}
        call("type", tab=tab, text=args.text, submit=args.submit or None,
             clear=False if args.append else None, **where)
        return f"typed {len(args.text)} characters" + (" and pressed Enter" if args.submit else "")
    if command == "press":
        return f"pressed {call('press', tab=tab, key=args.key)['pressed']}"
    if command == "eval":
        return call("eval", tab=tab, js=args.js)["result"]
    if command == "screenshot":
        shot = call("screenshot", tab=tab)
        return f"{shot['path']}  ({shot.get('width')}x{shot.get('height')})"
    if command == "logs":
        return render_logs(call("logs", tab=tab, since=args.since or None))
    if command in ("back", "forward", "reload"):
        moved = call(command, tab=tab)
        return f"{command}: {moved['url']}"
    if command == "close":
        return f"closed {call('close', tab=tab)['closed']}"
    if command == "wait":
        found = call("wait", timeout=args.timeout + 10, tab=tab, timeoutMs=int(args.timeout * 1000),
                     **({"selector": args.text} if args.selector else {"text": args.text}))
        return "found" if found["found"] else f"not there after {args.timeout:g}s"
    raise AppBrowserError(f"unknown command {command}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        print(run(args))
    except AppBrowserError as exc:
        print(f"browser: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
