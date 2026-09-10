import asyncio
import base64
import io
import threading
from pathlib import Path

import pytest
from flask import Flask, jsonify, request
from PIL import Image
from werkzeug.serving import make_server

from ui.workspace_app import install_workspace


@pytest.mark.parametrize("width", [1440, 390])
def test_corrupt_receipts_keep_page_viewable_without_sending_commands(tmp_path, width):
    playwright = pytest.importorskip("playwright.sync_api")
    app = Flask(__name__, static_folder=str(Path(__file__).resolve().parents[1] / "ui/static"))
    host = install_workspace(app, tmp_path / "events.db", describe=lambda sid: {"session_id": sid, "agent": "claude"})
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": width, "height": 900})
                errors, calls = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script("sessionStorage.setItem('serena-workspace-pending:exact','{')")

                def api(route):
                    calls.append(route.request.url)
                    route.fulfill(json={"ok": True, "events": [], "has_more": False})

                page.route("**/api/workspace/**", api)
                page.goto(f"http://127.0.0.1:{server.server_port}/workspace/exact")
                button = page.get_by_role("button", name="Resume session", exact=True)
                playwright.expect(button).to_be_enabled()
                assert calls == []
                button.click()
                playwright.expect(page.get_by_role("alert")).to_contain_text("receipts are unreadable")
                assert any("/events?" in call for call in calls)
                assert not any("/commands" in call or "/uploads" in call for call in calls)
                assert page.evaluate("sessionStorage.getItem('serena-workspace-pending:exact')") == "{"
                assert not errors and host._loop is None
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        host.shutdown()


@pytest.mark.parametrize("saved", ['{"request_id":"broken"}', '{'])
def test_corrupt_creation_record_cannot_launch_replacement(tmp_path, saved):
    from urllib.parse import urlencode
    playwright = pytest.importorskip("playwright.sync_api")
    app = Flask(__name__, static_folder=str(Path(__file__).resolve().parents[1] / "ui/static"))
    host = install_workspace(app, tmp_path / "corrupt.db", describe=lambda sid: None)
    calls = []
    host.create = lambda *args, **kwargs: calls.append((args, kwargs))
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                base = f"http://127.0.0.1:{server.server_port}"
                page.goto(base + "/workspace/new?" + urlencode({"source": "corrupt", "provider": "codex", "cwd": str(tmp_path)}))
                page.evaluate("saved => sessionStorage.setItem('serena-workspace-create:corrupt', saved)", saved)
                page.reload()
                button = page.locator("#creation-submit")
                playwright.expect(button).to_be_disabled()
                assert page.get_by_role("status").inner_text()
                button.dispatch_event("click")
                page.wait_for_timeout(50)
                assert not calls and host._loop is None
                assert page.evaluate("sessionStorage.getItem('serena-workspace-create:corrupt')") == saved
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        host.shutdown()


@pytest.mark.parametrize("width", [1440, 390])
def test_seeded_frame_requires_context_and_explicit_click_preserves_receipt(tmp_path, width):
    from urllib.parse import urlencode
    playwright = pytest.importorskip("playwright.sync_api")
    app = Flask(__name__, static_folder=str(Path(__file__).resolve().parents[1] / "ui/static"))
    host = install_workspace(app, tmp_path / "seed-ui.db", describe=lambda sid: None)
    calls = []
    target = "11111111-2222-4333-8444-555555555555"
    seed = "Required linked context\n<script>not markup</script>"
    def create(request, provider, cwd, *, confirmed, seed):
        calls.append((request, provider, cwd, confirmed, seed))
        return {"ok": True, "result": {"session_id": target, "provider": provider, "cwd": cwd},
                "initial_message": {"ok": False, "error": "Native delivery unconfirmed"}}
    host.create = create
    @app.get("/parent")
    def parent():
        return '<iframe style="width:100%;height:700px;border:0" src="/workspace/new?' + urlencode({"source": "seed-ui", "provider": "claude", "cwd": str(tmp_path), "seeded": "1"}) + '"></iframe>'
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": width, "height": 900})
                page.goto(f"http://127.0.0.1:{server.server_port}/parent")
                frame = page.frame_locator("iframe")
                button = frame.get_by_role("button", name="Create and send", exact=True)
                button.wait_for()
                assert button.is_disabled() and not calls
                page.frames[1].evaluate("seed => window.postMessage({type:'serena-workspace-seed',sid:'seed-ui',seed},location.origin)", seed)
                page.wait_for_timeout(30)
                assert button.is_disabled()
                page.evaluate("seed => document.querySelector('iframe').contentWindow.postMessage({type:'serena-workspace-seed',sid:'seed-ui',seed},location.origin)", seed)
                page.frames[1].wait_for_function("() => !document.querySelector('#creation-submit').disabled")
                assert frame.get_by_role("textbox", name="Initial context").input_value() == seed
                assert not calls and host._loop is None
                button.click()
                frame.get_by_role("button", name="Open conversation", exact=True).wait_for()
                assert len(calls) == 1 and calls[0][1:] == ("claude", str(tmp_path), True, seed)
                assert frame.get_by_role("alert").inner_text() == "Native delivery unconfirmed"
                page.reload()
                frame.get_by_role("button", name="Open conversation", exact=True).wait_for()
                assert frame.get_by_role("alert").inner_text() == "Native delivery unconfirmed"
                assert frame.get_by_role("textbox", name="Initial context").input_value() == seed
                assert len(calls) == 1 and host._loop is None
                assert page.frames[1].evaluate("document.documentElement.scrollWidth <= innerWidth")
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        host.shutdown()


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_new_chat_ui_is_explicit_retains_request_on_reload_and_opens_exact_target(tmp_path, width, provider):
    playwright = pytest.importorskip("playwright.sync_api")
    app = Flask(__name__, static_folder=str(Path(__file__).resolve().parents[1] / "ui/static"))
    target = "11111111-2222-4333-8444-555555555555"
    host = install_workspace(app, tmp_path / "create.db", describe=lambda sid: {
        "session_id": target, "agent": provider, "cwd": str(tmp_path)} if sid == target else None)
    calls = []
    def create(request, provider, cwd, *, confirmed):
        calls.append((request, provider, cwd, confirmed))
        if len(calls) == 1:
            return {"ok": False, "pending": True, "error": "Still pending"}
        return {"ok": True, "result": {"session_id": target, "provider": provider, "cwd": str(tmp_path)}}
    host.create = create
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": width, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                from urllib.parse import urlencode
                base = f"http://127.0.0.1:{server.server_port}"
                page.goto(base + "/workspace/new?" + urlencode({"source": "new-" + provider, "provider": provider, "cwd": str(tmp_path)}))
                assert page.get_by_role("textbox", name="Project").input_value() == str(tmp_path)
                assert not calls and host._loop is None
                page.get_by_role("button", name=f"Create {provider.title()} chat", exact=True).click()
                page.get_by_role("status").filter(has_text="Still pending").wait_for()
                assert len(calls) == 1
                page.reload()
                page.get_by_role("button", name="Check creation", exact=True).click()
                page.get_by_role("button", name="Open conversation", exact=True).wait_for()
                assert len(calls) == 2 and calls[0] == calls[1]
                page.reload()
                page.get_by_role("button", name="Open conversation", exact=True).wait_for()
                assert len(calls) == 2
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.get_by_role("button", name="Open conversation", exact=True).click()
                page.wait_for_url(base + "/workspace/" + target)
                assert not errors and host._loop is None
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        host.shutdown()


def test_pending_native_clear_page_uses_durable_identity_without_launch(tmp_path):
    app = Flask(__name__)
    host = install_workspace(app, tmp_path / "clear.db", describe=lambda sid: None,
                             resolve=lambda sid: pytest.fail("page must not attach"))
    target = "11111111-2222-4333-8444-555555555555"
    try:
        host.journal.claim_command("source", "clear", {"action": "clear_session", "payload": {"confirmed": True}})
        host.journal.prepare_clear("source", "clear", {"session_id": target, "provider": "claude", "cwd": str(tmp_path)})
        response = app.test_client().get(f"/workspace/{target}")
        assert response.status_code == 200 and target.encode() in response.data
        assert b'"provider": "Claude"' in response.data
        assert host._loop is None and not host._sessions
        assert app.test_client().get("/workspace/missing").status_code == 404
        host.journal.complete_clear("source", "clear")
        host.journal.mark_clear_cataloged(target)
        assert app.test_client().get(f"/workspace/{target}").status_code == 404
    finally:
        host.shutdown()


@pytest.mark.parametrize("width", [1440, 390])
def test_failed_attachment_retry_is_explicit_and_does_not_stop_uncertain_owner(tmp_path, width):
    playwright = pytest.importorskip("playwright.sync_api")
    owners = []
    class Owner:
        active_turn = None
        def __init__(self, *, session_id, cwd, publish):
            self.sid, self.publish = session_id, publish
            self.state, self.closed = "closed", False
            owners.append(self)
        async def open(self):
            if len(owners) == 1:
                raise RuntimeError("Controlled preflight failure; no process launched")
            self.state = "ready"
            await self.publish({"method": "workspace/history", "params": {"thread": {"id": self.sid, "turns": []}}})
        def can_retry_attachment(self):
            return self.state == "closed"
        async def list_models(self):
            return {"data": []}
        async def close(self):
            self.closed = True
    app = Flask(__name__, static_folder=str(Path(__file__).resolve().parents[1] / "ui/static"))
    host = install_workspace(app, tmp_path / "retry.db",
        resolve=lambda sid: {"session_id": sid, "provider": "codex", "cwd": str(tmp_path)},
        factories={"codex": Owner}, describe=lambda sid: {"session_id": sid, "agent": "codex"})
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": width, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/workspace/exact")
                assert not owners
                page.get_by_role("button", name="Resume session").click()
                retry = page.get_by_role("button", name="Retry connection")
                retry.wait_for()
                assert len(owners) == 1 and not owners[0].closed
                retry.click()
                retry.wait_for(state="hidden")
                assert len(owners) == 2 and owners[1].sid == "exact"
                owners[1].state = "unavailable"
                host.journal.append("exact", {"method": "workspace/transportClosed", "params": {"reason": "Controlled uncertain runtime"}})
                retry.wait_for()
                retry.click()
                page.get_by_text("Session runtime is unavailable; retry is refused until its cleanup and ownership are confirmed", exact=True).wait_for()
                assert len(owners) == 2 and not any(owner.closed for owner in owners)
                assert page.locator("body").evaluate("el=>el.scrollWidth<=innerWidth")
                page.close()
                assert not any(owner.closed for owner in owners)
                assert not errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        host.shutdown()


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
                        "itemId": f"i-{len(self.sent)}",
                        "delta": "controlled provider output"
                        if len(self.sent) == 1
                        else "controlled upload output",
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

    start = HTML.index("function _adoptStructuredIdentity(")
    end = HTML.index("async function startLiveTerminal(", start)
    mount_source = HTML[start:end]

    @app.get("/parent")
    def parent_page():
        return (
            """<!doctype html><html><body style="margin:0;background:#000">
<main id="termMounts" style="height:100vh"></main><script>
const termSessions=new Map();let activeTermSid=null;
const _pseudoSessions=[{session_id:'new-proof',pending_rename_title:'My named conversation'}];
let sessionSource=[..._pseudoSessions];
const _pendingTermPartners=new Map();const _fdPairResolved={};window.linked=[];
function _pendingPartnersOf(sid){const value=_pendingTermPartners.get(sid);return Array.isArray(value)?value:value?[value]:[];}
function _setPendingPartners(sid,partners){_pendingTermPartners.set(sid,[...new Set(partners)].filter(value=>value && value!==sid));}
function _fdLinkPair(sids){window.linked.push(sids);}
function setSessionSource(rows){sessionSource=rows;}
function _patchClientSession(sid,patch){Object.assign(sessionSource.find(row=>row.session_id===sid)||{},patch);}
const currentProject=null;window.openedForks=[];
async function loadSessions(){}
function _findClientSession(sid){return sessionSource.find(row=>row.session_id===sid)||{session_id:sid};}
async function openConv(sid){window.openedForks.push(sid);}
function showToast(message){throw Error(message);}
function _activateTermPane(sid){activeTermSid=sid;}
function _startLinkedTerminals(sid){}
function _markActive(sid){}
function _unmarkActive(sid){window.retiredPseudo=sid;}
function setTermStatus(status){window.lastStatus=status;}
"""
            + mount_source
            + """</script></body></html>"""
        )

    renames = []
    @app.post("/api/rename/<sid>")
    def rename_created(sid):
        renames.append((sid, request.get_json()["title"]))
        return jsonify(ok=True)

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
        assert b'<link rel="icon" href="/static/icons/serena-icon.ico">' in page.data
        assert client.get('/static/icons/serena-icon.ico').status_code == 200
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
            def delay_boot(route):
                if "?ready=1" in route.request.url:
                    route.continue_()
                else:
                    route.fulfill(status=200, content_type="text/javascript", body="await new Promise(resolve=>window.startWorkspace=resolve); await import('/static/workspace-page.mjs?ready=1');")
            page.route("**/workspace-page.mjs*", delay_boot)
            page.goto(f"http://127.0.0.1:{server.server_port}/workspace/exact", wait_until="commit")
            page.wait_for_function("() => typeof window.startWorkspace === 'function'")
            assert page.get_by_role("button", name="Resume session").is_disabled()
            assert not owners
            page.evaluate("window.startWorkspace()")
            page.unroute("**/workspace-page.mjs*", delay_boot)
            page.get_by_role("textbox", name=f"Message {provider.capitalize()}").wait_for()
            assert not owners
            if provider == "claude":
                page.evaluate("sessionStorage.setItem('serena-workspace-clear:exact',JSON.stringify({session_id:'11111111-1111-4111-8111-111111111111'}))")
                page.reload()
                page.get_by_role("button", name="Resume original conversation", exact=True).click()
                page.get_by_role("button", name="Resume original conversation", exact=True).wait_for(state="hidden")
                assert page.evaluate("sessionStorage.getItem('serena-workspace-clear:exact')") == "null"
                assert not page.get_by_role("button", name="Send message", exact=True).is_disabled()
                page.reload()
            page.get_by_role("button", name="Session events", exact=True).click()
            inspector = page.get_by_role("dialog", name="Session events")
            if provider == "claude":
                inspector.get_by_text("1 workspace/history", exact=True).wait_for()
                assert len(owners) == 1
            else:
                inspector.get_by_text("No events", exact=True).wait_for()
                assert not owners and host._loop is None
            inspector.get_by_role("button", name="Close session events").click()
            page.get_by_role("button", name="Resume session").click()
            page.get_by_role("button", name="Resume session").wait_for(state="hidden")
            page.get_by_role("textbox", name=f"Message {provider.capitalize()}").fill(
                "real mounted page control"
            )
            page.get_by_role("button", name="Send message", exact=True).click()
            page.get_by_text("controlled provider output", exact=True).wait_for()
            assert owners[0].sent == [[{"type": "text", "text": "real mounted page control"}]]
            image = io.BytesIO()
            Image.new("RGB", (8, 8), "pink").save(image, format="PNG")
            raw = image.getvalue()
            page.locator('input[type="file"]').set_input_files(
                {"name": "screenshot.png", "mimeType": "image/png", "buffer": raw}
            )
            page.wait_for_function(
                "() => document.querySelector('.aw-attachment img').naturalWidth === 8"
            )
            assert len(owners[0].sent) == 1
            with page.expect_response(lambda r: r.url.endswith("/uploads")) as uploaded:
                page.get_by_role("button", name="Send message", exact=True).click()
            token = uploaded.value.json()["upload"]["token"]
            page.get_by_text("controlled upload output", exact=True).wait_for()
            assert len(owners) == 1 and owners[0].sid == "exact"
            if provider == "codex":
                assert owners[0].sent[1] == [
                    {"type": "localImage", "path": str(host.uploads.resolve("exact", token)[0])}
                ]
                assert Path(owners[0].sent[1][0]["path"]).read_bytes() == raw
            else:
                assert owners[0].sent[1] == [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(raw).decode("ascii"),
                        },
                    }
                ]
            previews = page.evaluate(
                """async token => {
              const boot = JSON.parse(document.querySelector('#workspace-boot').textContent);
              const options = {headers:{'X-Serena-Workspace-Token':boot.token}};
              const own = await fetch(`/api/workspace/exact/attachments/${token}`, options);
              const foreign = await fetch(`/api/workspace/another-session/attachments/${token}`, options);
              return [own.status, own.headers.get('content-type'), (await own.arrayBuffer()).byteLength, foreign.status];
            }""",
                token,
            )
            assert previews == [200, "image/png", len(raw), 400]
            page.get_by_role("button", name="Session events", exact=True).click()
            inspector = page.get_by_role("dialog", name="Session events")
            inspector.get_by_text("1 workspace/history", exact=True).click()
            assert '"id": "exact"' in inspector.locator("pre").inner_text()
            inspector.get_by_role("button", name="Close session events").click()
            page.screenshot(path=str(tmp_path / "mounted-workspace.png"))
            page.reload()
            page.get_by_role("button", name="Resume session").click()
            page.get_by_text("controlled provider output", exact=True).wait_for()
            assert len(owners) == 1 and not owners[0].closed
            assert len(owners[0].sent) == 2
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
            handoffs = []
            original_bridge = host.bridge
            host.bridge = lambda sid, agent, prompt, request_id, **kwargs: handoffs.append((sid, agent, prompt, request_id)) or {"ok": True, "queued": True, "pending": True}
            nested.get_by_role("textbox", name=f"Message {provider.capitalize()}").fill("Keep the unsent draft")
            result = page.evaluate("termSessions.get('exact').handoff('Exact linked briefing')")
            assert result == {"ok": True, "queued": True, "pending": True}
            assert handoffs[0][:3] == ("exact", provider, "Exact linked briefing")
            assert len(handoffs[0][3]) == 36
            assert len(owners) == 1 and len(owners[0].sent) == 2
            assert nested.get_by_role("textbox", name=f"Message {provider.capitalize()}").input_value() == "Keep the unsent draft"
            assert not owners[0].closed
            host.bridge = original_bridge
            reads = []
            page.on("request", lambda request: reads.append(request.url) if "/api/workspace/exact/events?" in request.url else None)
            page.evaluate("termSessions.get('exact').mount.style.display='none'")
            page.wait_for_timeout(400)
            before = len(reads)
            page.wait_for_timeout(1200)
            assert len(reads) - before <= 1
            with page.expect_response(lambda response: "/api/workspace/exact/events?" in response.url, timeout=1000):
                page.evaluate("termSessions.get('exact').mount.style.display=''")
            assert len(owners) == 1 and not owners[0].closed and len(owners[0].sent) == 2
            page.evaluate("""() => window.postMessage({type:'serena-workspace-open-fork',sid:'exact',target:'11111111-1111-4111-8111-111111111111'},location.origin)""")
            page.wait_for_timeout(50)
            assert page.evaluate("openedForks") == []
            page.frames[1].evaluate("""() => parent.postMessage({type:'serena-workspace-open-fork',sid:'exact',target:'11111111-1111-4111-8111-111111111111'},location.origin)""")
            page.wait_for_function("() => openedForks.length === 1")
            assert page.evaluate("openedForks") == ["11111111-1111-4111-8111-111111111111"]
            page.frames[1].evaluate("""() => parent.postMessage({type:'serena-workspace-open-cleared',sid:'exact',target:'22222222-2222-4222-8222-222222222222'},location.origin)""")
            page.wait_for_function("() => openedForks.length === 2")
            assert page.evaluate("openedForks[1]") == "22222222-2222-4222-8222-222222222222"
            assert len(owners) == 1 and not owners[0].closed
            page.evaluate(
                "termSessions.get('exact').cancelOutput(); termSessions.get('exact').mount.remove()"
            )
            assert not owners[0].closed
            assert not errors
            page.evaluate("(cwd) => _startStructuredPane('seeded-proof', {isNew:true,agent:'codex',cwd,seed:'Required context'})", str(tmp_path))
            seeded_frame = page.frames[-1]
            seeded_frame.get_by_role("button", name="Create and send", exact=True).wait_for()
            assert seeded_frame.get_by_role("textbox", name="Initial context").input_value() == "Required context"
            assert "Required" not in seeded_frame.url
            assert len(owners) == 1
            page.evaluate("termSessions.get('seeded-proof').cancelOutput(); termSessions.get('seeded-proof').mount.remove(); termSessions.delete('seeded-proof')")
            page.evaluate("(cwd) => _startStructuredPane('new-proof', {isNew:true,agent:'codex',cwd})", str(tmp_path))
            created_frame = page.frames[-1]
            created_frame.get_by_role("button", name="Create Codex chat", exact=True).wait_for()
            assert page.evaluate("_pseudoSessions[0].structured_pending")
            assert len(owners) == 1
            created_frame.evaluate("""() => parent.postMessage({type:'serena-workspace-open-created',sid:'new-proof',target:'33333333-3333-4333-8333-333333333333'},location.origin)""")
            page.wait_for_function("() => openedForks.length === 3")
            assert page.evaluate("openedForks[2]") == "33333333-3333-4333-8333-333333333333"
            assert page.evaluate("retiredPseudo") == "new-proof"
            assert renames == [("33333333-3333-4333-8333-333333333333", "My named conversation")]
            assert not page.evaluate("termSessions.has('new-proof')")
            assert len(owners) == 1 and not owners[0].closed
            page.evaluate("""cwd=>{
              for(const agent of ['claude','codex']){
                const sid='linked-'+agent;
                const pseudo={session_id:sid,agent,cwd,fd_pair_id:'linked-proof',group:'provisional',pending_rename_title:'Linked title'};
                _pseudoSessions.push(pseudo);sessionSource.push(pseudo);
                _setPendingPartners(sid,['linked-claude','linked-codex']);
                _startStructuredPane(sid,{isNew:true,agent,cwd,background:agent==='codex'});
              }
            }""", str(tmp_path))
            first = page.frame_locator('iframe[src*="source=linked-claude"]')
            first.get_by_role('button', name='Create Claude chat', exact=True).wait_for()
            first.locator('body').evaluate("""()=>parent.postMessage({type:'serena-workspace-open-created',sid:'linked-claude',target:'44444444-4444-4444-8444-444444444444'},location.origin)""")
            page.wait_for_function("openedForks.length===4")
            assert page.evaluate("_pendingPartnersOf('linked-codex')") == ['44444444-4444-4444-8444-444444444444']
            assert page.evaluate('linked') == []
            second = page.frame_locator('iframe[src*="source=linked-codex"]')
            second.get_by_role('button', name='Create Codex chat', exact=True).wait_for()
            second.locator('body').evaluate("""()=>parent.postMessage({type:'serena-workspace-open-created',sid:'linked-codex',target:'55555555-5555-4555-8555-555555555555'},location.origin)""")
            page.wait_for_function("openedForks.length===5")
            assert page.evaluate('linked') == [['44444444-4444-4444-8444-444444444444', '55555555-5555-4555-8555-555555555555']]
            assert page.evaluate("_pendingPartnersOf('44444444-4444-4444-8444-444444444444')") == ['55555555-5555-4555-8555-555555555555']
            assert page.evaluate('_pseudoSessions.length') == 0
            assert renames[-2:] == [('44444444-4444-4444-8444-444444444444','Linked title'), ('55555555-5555-4555-8555-555555555555','Linked title')]
            assert len(owners) == 1 and not owners[0].closed and not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        host.shutdown()
