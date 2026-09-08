"""Full renderer checks with isolated HTTP/socket doubles, never real CLIs."""

from pathlib import Path
from urllib.parse import urlparse

import pytest

from ui import web

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("entry,fail", [
    ("claude", False), ("codex", False), ("gemini", False),
    ("solo", False), ("solo", True), ("gemini", True), ("claude", True),
])
def test_full_renderer_opens_group_even_if_gemini_cannot_resume(entry, fail):
    rows = [
        {"session_id": f"00000000-0000-4000-8000-{i:012d}", "agent": agent,
         "group": "group" if i < 4 else None, "cwd": "/tmp",
         "title": agent, "display_title": agent}
        for i, agent in enumerate(["claude", "codex", "gemini", "gemini"], 1)
    ]
    selected = dict(zip(["claude", "codex", "gemini", "solo"], rows, strict=True))[entry]
    by_sid = {r["session_id"]: r for r in rows}
    calls, errors = [], []
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).is_file():
            pytest.skip("Playwright Chromium is not installed")
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("""window.WebSocket = class {
              constructor(url) {
                this.url=url; this.readyState=0;
                setTimeout(()=>{this.readyState=1; if(this.onopen)this.onopen();},100);
              }
              send() {} close() {this.readyState=3;}
              addEventListener() {} removeEventListener() {}
            };""")

            def route(request):
                path = urlparse(request.request.url).path
                if path == "/":
                    return request.fulfill(body=web.HTML, content_type="text/html")
                if path.startswith("/static/"):
                    asset = Path(web.__file__).parent / path.lstrip("/")
                    if asset.is_file():
                        return request.fulfill(path=str(asset))
                if path == "/api/sessions":
                    return request.fulfill(json=rows)
                if path == "/api/spawn-terminal":
                    sid = request.request.post_data_json["session_id"]
                    calls.append(sid)
                    if fail and by_sid[sid]["agent"] == "gemini":
                        return request.fulfill(status=409, json={
                            "ok": False, "error": "native conversation file is missing",
                        })
                    return request.fulfill(json={
                        "ok": True, "terminal_id": sid, "agent": by_sid[sid]["agent"], "cwd": "/tmp",
                    })
                return request.fulfill(json={})

            page.route("**/*", route)
            page.goto("http://serena-test.local/")
            page.wait_for_function("allSessions.length === 4")
            page.evaluate("(sid) => openConv(sid)", selected["session_id"])
            expected_calls = 1 if entry == "solo" else 3
            page.wait_for_function("_termStarting.size === 0")
            page.wait_for_timeout(300)
            visible = page.locator(".term-pane[data-sid]:not(.hidden)")
            expected_panes = expected_calls - int(fail)
            assert len(calls) == len(set(calls)) == expected_calls
            assert visible.count() == expected_panes
            assert visible.evaluate_all("nodes => nodes.every(n => getComputedStyle(n).visibility === 'visible')")
            assert not errors
            if fail:
                assert "native conversation file is missing" in page.locator("body").inner_text()
        finally:
            browser.close()
