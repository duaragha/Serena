"""Exercise real upload, journal, HTTP and browser paths with controlled history."""
import io
import sys
import tempfile
import threading
from pathlib import Path

from flask import Flask
from PIL import Image
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.workspace_app import install_workspace


def main():
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="serena-history-image-proof-") as temporary:
        owners = []
        class HistoryOwner:
            state, active_turn = "closed", None
            def __init__(self, *, session_id, cwd, publish):
                self.sid, self.publish = session_id, publish
                self.closed, self.pages = False, 0
                owners.append(self)
            async def open(self):
                self.state = "ready"
                await self.publish({"method": "workspace/history", "params": {
                    "thread": {"id": self.sid, "turns": []}, "historyCursor": "old-page"}})
            async def list_models(self):
                return {"data": []}
            async def load_earlier(self, cursor):
                assert cursor == "old-page"
                self.pages += 1
                page = {"historyCursor": None, "turns": [{"id": "old-turn", "status": "completed", "items": [
                    {"id": "old-upload", "type": "userMessage", "content": [
                        {"type": "text", "text": "Earlier attachment"},
                        {"type": "localImage", "path": str(image_path)},
                        {"type": "localImage", "path": str(foreign_path)}]}]}]}
                await self.publish({"method": "workspace/historyPage", "params": {"threadId": self.sid, **page}})
                return page
            async def close(self):
                self.closed = True
        class Quiet(WSGIRequestHandler):
            def log_request(self, *args):
                pass
        app = Flask(__name__, static_folder=str(repo / "ui/static"))
        host = install_workspace(app, Path(temporary) / "events.db",
            resolve=lambda sid: {"session_id": sid, "provider": "codex", "cwd": temporary},
            describe=lambda sid: {"session_id": sid, "agent": "codex"}, factories={"codex": HistoryOwner})
        raw = io.BytesIO()
        Image.new("RGB", (96, 64), "#ff85cb").save(raw, format="PNG")
        own = host.uploads.save("exact", "earlier-photo.png", io.BytesIO(raw.getvalue()), "image/png")
        foreign = host.uploads.save("foreign", "foreign-photo.png", io.BytesIO(raw.getvalue()), "image/png")
        image_path, _ = host.uploads.resolve("exact", own["token"])
        foreign_path, _ = host.uploads.resolve("foreign", foreign["token"])
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=Quiet)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    for label, width in (("desktop", 1440), ("mobile", 390)):
                        page = browser.new_page(viewport={"width": width, "height": 900})
                        errors, previews = [], []
                        page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                        page.on("response", lambda response, previews=previews: previews.append(response.url) if "/attachments/" in response.url else None)
                        page.goto(f"http://127.0.0.1:{server.server_port}/workspace/exact")
                        page.get_by_role("button", name="Resume session", exact=True).click()
                        if label == "desktop":
                            page.get_by_role("button", name="Load earlier messages", exact=True).click()
                        image = page.get_by_role("img", name="earlier-photo.png", exact=True)
                        expect(image).to_be_visible()
                        page.wait_for_function("() => document.querySelector('.aw-history-image')?.naturalWidth === 96")
                        assert image.evaluate("el=>el.naturalWidth===96 && el.naturalHeight===64")
                        assert any(url.endswith(own["token"]) for url in previews)
                        assert not any(url.endswith(foreign["token"]) for url in previews)
                        assert len(owners) == 1 and owners[0].pages == 1
                        assert page.locator("body").evaluate("el=>el.scrollWidth<=innerWidth")
                        target = repo / "apps/desktop/build/workspace-proof" / f"history-images-{label}.png"
                        target.parent.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(target))
                        page.close()
                        assert not owners[0].closed and not errors, errors
                        print(f"PASS: {label} earlier-page image decoded through session-bound HTTP; foreign preview refused; replay retained one owner and one history request")
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            host.shutdown()
    print("PASS: controlled provider history only; no CLI, model inference or user files used")


if __name__ == "__main__":
    main()
