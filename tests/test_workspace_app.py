import asyncio
import threading
from pathlib import Path

import pytest
from flask import Flask
from werkzeug.serving import make_server

from ui.workspace_app import install_workspace


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch(tmp_path, provider):
    playwright = pytest.importorskip("playwright.sync_api")
    owners = []

    class Owner:
        state = "closed"
        active_turn = None

        def __init__(self, *, session_id, cwd, publish):
            self.sid, self.publish = session_id, publish
            self.sent, self.closed = [], False
            owners.append(self)

        async def open(self):
            self.state = "ready"
            await self.publish(
                {"method": "workspace/history", "params": {"thread": {"id": self.sid, "turns": []}}}
            )

        async def submit(self, inputs, options=None):
            self.sent.append(inputs)
            await asyncio.sleep(0.01)
            await self.publish(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": self.sid,
                        "turnId": "t",
                        "itemId": "i",
                        "delta": "controlled provider output",
                    },
                }
            )
            return {"turn": {"id": "t"}}

        async def close(self):
            self.closed = True

        async def list_models(self):
            return {"data": []}

    app = Flask(__name__, static_folder=str(Path(__file__).resolve().parents[1] / "ui/static"))
    host = install_workspace(
        app,
        tmp_path / "events.db",
        resolve=lambda sid: {"session_id": sid, "provider": provider, "cwd": str(tmp_path)},
        factories={provider: Owner},
        describe=lambda sid: {"session_id": sid, "agent": provider},
    )
    from ui.web import HTML

    start = HTML.index("function _startStructuredPane(")
    end = HTML.index("async function startLiveTerminal(", start)
    mount_source = HTML[start:end]

    @app.get("/parent")
    def parent_page():
        return (
            """<!doctype html><html><body style="margin:0;background:#000">
<main id="termMounts" style="height:100vh"></main><script>
const termSessions=new Map();let activeTermSid=null;
function _activateTermPane(sid){activeTermSid=sid;}
function _startLinkedTerminals(sid){}
function _markActive(sid){}
function setTermStatus(status){window.lastStatus=status;}
"""
            + mount_source
            + """</script></body></html>"""
        )

    with app.test_client() as client:
        denied = client.get("/workspace/exact", base_url="http://evil.test")
        assert denied.status_code == 403
        assert b"workspace-boot" not in denied.data
        assert (
            client.get(
                "/workspace/exact",
                base_url="http://127.0.0.1",
                environ_base={"REMOTE_ADDR": "10.0.0.4"},
            ).status_code
            == 403
        )
        page = client.get("/workspace/exact", base_url="http://127.0.0.1")
        assert page.status_code == 200
        assert page.headers["Cache-Control"] == "no-store"
        assert "frame-ancestors 'self'" in page.headers["Content-Security-Policy"]
        assert owners == [] and host._loop is None

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_default_timeout(5000)
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{server.server_port}/workspace/exact")
            page.get_by_role("textbox", name=f"Message {provider.capitalize()}").wait_for()
            assert not owners
            page.get_by_role("button", name="Resume session").click()
            page.get_by_role("button", name="Resume session").wait_for(state="hidden")
            page.get_by_role("textbox", name=f"Message {provider.capitalize()}").fill(
                "real mounted page control"
            )
            page.get_by_role("button", name="Send message", exact=True).click()
            page.get_by_text("controlled provider output", exact=True).wait_for()
            assert owners[0].sent == [[{"type": "text", "text": "real mounted page control"}]]
            page.screenshot(path=str(tmp_path / "mounted-workspace.png"))
            page.reload()
            page.get_by_role("button", name="Resume session").click()
            page.get_by_text("controlled provider output", exact=True).wait_for()
            assert len(owners) == 1 and not owners[0].closed
            assert not errors
            page.goto(f"http://127.0.0.1:{server.server_port}/parent")
            page.evaluate("_startStructuredPane('exact', {})")
            nested = page.frame_locator("iframe")
            nested.get_by_role("button", name="Resume session").click()
            nested.get_by_text("controlled provider output", exact=True).wait_for()
            assert page.evaluate("termSessions.get('exact').structured")
            page.evaluate("_startStructuredPane('exact', {})")
            assert page.locator("iframe").count() == 1
            assert nested.locator(".xterm").count() == 0
            assert page.evaluate("termSessions.get('exact').state") == "ready"
            page.evaluate(
                "termSessions.get('exact').cancelOutput(); termSessions.get('exact').mount.remove()"
            )
            assert not owners[0].closed
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        host.shutdown()
