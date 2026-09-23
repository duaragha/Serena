"""Her voice's hands in the app's browser."""

from __future__ import annotations

import asyncio
import base64

from core import brain_browser_tools as tools


def _run(tool, args):
    return asyncio.run(tool.handler(args))


def test_acting_needs_a_live_turn_but_looking_does_not(monkeypatch):
    monkeypatch.setattr(tools, "current_turn", lambda: {})
    assert _run(tools.browser_open, {"url": "localhost:3000"}).get("is_error")
    assert _run(tools.browser_click, {"target": "e1"}).get("is_error")
    assert _run(tools.browser_type, {"target": "e1", "text": "x"}).get("is_error")

    async def fake_call(command, **args):
        return {"title": "Bench", "url": "http://localhost:8911/", "text": "Hi", "elements": []}

    monkeypatch.setattr(tools, "_call", fake_call)
    looked = _run(tools.browser_look, {})
    assert not looked.get("is_error")
    assert "Bench -- http://localhost:8911/" in looked["content"][0]["text"]


def test_a_missing_app_comes_back_as_an_error_she_can_say(monkeypatch):
    from core.app_browser import AppBrowserError

    async def no_app(command, **args):
        raise AppBrowserError("the Serena app is not running, or its build has no in-app browser yet")

    monkeypatch.setattr(tools, "_call", no_app)
    result = _run(tools.browser_logs, {})
    assert result["is_error"] and "not running" in result["content"][0]["text"]


def test_click_and_type_pass_the_target_through(monkeypatch):
    monkeypatch.setattr(tools, "current_turn", lambda: {"text": "sign me up", "protocol": "voice"})
    calls = []

    async def fake_call(command, **args):
        calls.append((command, args))
        return {"clicked": "e3"}

    monkeypatch.setattr(tools, "_call", fake_call)
    _run(tools.browser_click, {"target": "e3"})
    _run(tools.browser_type, {"target": "#email", "text": "a@b.c", "submit": True})
    assert calls[0] == ("click", {"ref": "e3"})
    assert calls[1] == ("type", {"text": "a@b.c", "submit": True, "selector": "#email"})


def test_screenshot_is_an_image_she_can_see(monkeypatch, tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG fake")

    async def fake_call(command, **args):
        return {"tab": "t1", "path": str(png), "width": 10, "height": 10}

    monkeypatch.setattr(tools, "_call", fake_call)
    result = _run(tools.browser_screenshot, {})
    image = result["content"][0]
    assert image["type"] == "image" and image["mimeType"] == "image/png"
    assert base64.b64decode(image["data"]) == b"\x89PNG fake"
