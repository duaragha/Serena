"""Read-only browser proof against the actual local index and Flask routes.

Starts its own loopback server, blocks mutations, opens only Read mode, then
closes its browser and server. Never attaches to or launches a coding runtime.
"""

import argparse
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from ui import web


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshots", type=Path, required=True)
    args = parser.parse_args()
    args.screenshots.mkdir(parents=True, exist_ok=True)

    def readonly(environ, start_response):
        if environ["REQUEST_METHOD"] not in ("GET", "HEAD", "OPTIONS"):
            start_response("403 Forbidden", [("Content-Type", "application/json")])
            return [b'{"error":"Read-only verification"}']
        return web.app(environ, start_response)

    server = make_server("127.0.0.1", 0, readonly, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1600, "height": 1000})
                errors, spawns = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "request",
                    lambda request: (
                        spawns.append(request.url) if "/api/spawn-terminal" in request.url else None
                    ),
                )
                page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="domcontentloaded")
                page.wait_for_function(
                    "allSessions.length > 0 && !!window.SerenaWorkspace", timeout=30000
                )
                count = page.evaluate("allSessions.length")
                sid = page.evaluate(
                    "allSessions.find(s=>!s.is_done && s.agent==='codex').session_id"
                )
                page.evaluate("sid=>openConv(sid,{mode:'read'})", sid)
                page.locator("#convBody").wait_for()
                page.wait_for_timeout(500)
                page.screenshot(path=str(args.screenshots / "desktop.png"))
                page.locator("#workspaceChanges").click()
                page.wait_for_function(
                    "!document.getElementById('workspaceChangesList').textContent.includes('Reading working tree')"
                )
                assert "Unable to read" not in page.locator("#workspaceChangesList").inner_text()
                page.screenshot(path=str(args.screenshots / "changes.png"))
                page.locator('.tab[data-tab="tooling"]').click()
                page.locator("#toolingText").wait_for()
                page.screenshot(path=str(args.screenshots / "tooling.png"))
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator('.tab[data-tab="chats"]').click()
                page.screenshot(path=str(args.screenshots / "mobile.png"))
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert not spawns, spawns
                assert not errors, errors
                print(
                    f"PASS: real index ({count} sessions), Read transcript, Git inspector, Tooling, desktop/mobile; no coding runtime launches; no JS errors"
                )
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    main()
