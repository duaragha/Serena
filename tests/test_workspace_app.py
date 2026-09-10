import asyncio
import base64
import io
import os
import threading
from pathlib import Path
from types import SimpleNamespace

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
                errors, calls, contexts = [], [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script("sessionStorage.setItem('serena-workspace-pending:exact','{')")

                def api(route):
                    calls.append(route.request.url)
                    if route.request.url.endswith('/view-context'):
                        contexts.append(route.request.post_data_json)
                    route.fulfill(json={"session_id": "exact", "observing": False} if route.request.url.endswith("/observe")
                                  else {"ok": True, "events": [], "has_more": False})

                page.route("**/api/workspace/**", api)
                page.goto(f"http://127.0.0.1:{server.server_port}/workspace/exact")
                button = page.get_by_role("button", name="Resume session", exact=True)
                playwright.expect(button).to_be_enabled()
                assert sum(call.endswith('/observe') for call in calls) == 1
                assert all(call.endswith(('/observe', '/view-context')) for call in calls)
                button.click()
                playwright.expect(page.get_by_role("alert")).to_contain_text("receipts are unreadable")
                assert any("/events?" in call for call in calls)
                assert not any("/commands" in call or "/uploads" in call for call in calls)
                page.wait_for_timeout(2100)
                assert contexts and all(context['draft'] is True and not context.get('sleep_peers') for context in contexts)
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
    closed_view = threading.Event()

    class Transport:
        suspended = False
        def wake(self):
            self.suspended = False

    class Owner:
        state = "closed"
        active_turn = None

        def __init__(self, *, session_id, cwd, publish):
            self.sid, self.publish = session_id, publish
            self.cwd = cwd
            self.sent, self.closed = [], False
            self.rpc = Transport()
            self.client = SimpleNamespace(transport=SimpleNamespace(rpc=self.rpc))
            owners.append(self)

        async def open(self):
            self.state = "ready"
            await self.publish(
                {"method": "workspace/history", "params": {"thread": {"id": self.sid, "turns": []}}}
            )

        async def list_apps(self):
            return {'data': [{'id': 'demo', 'name': 'Demo App', 'description': '',
                              'accessible': True, 'enabled': True, 'callable': True}]}

        async def rename(self, name):
            self.renamed = name
            return {'session_id': self.sid, 'name': name}

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
    @app.after_request
    def observe_view_close(response):
        if (request.path == '/api/workspace/exact/view-context' and response.is_json
                and response.get_json().get('closed') is True):
            closed_view.set()
        return response
    from ui.web import HTML

    start = HTML.index("function _adoptStructuredIdentity(")
    end = HTML.index("async function startLiveTerminal(", start)
    mount_source = HTML[start:end]
    new_chat_source = HTML[HTML.index('async function newChatInline('):HTML.index('let _lastNewChatAgent')]
    active_source = HTML[HTML.index("function _markActive("):HTML.index("function _rememberActive(")]

    @app.get("/parent")
    def parent_page():
        return (
            """<!doctype html><html><body style="margin:0;background:#000">
<main id="termMounts" style="height:100vh"></main><script>
const termSessions=new Map();let activeTermSid=null;
let convMode='live',currentTab='chats',_webRuntimeFocusSid=null;
let _gtkSplitActive=false,_gtkSplitSids=null,_gtkCurrentGroup=null;
const _gtkPinnedGroups=new Set();
const _pseudoSessions=[{session_id:'new-proof',pending_rename_title:'My named conversation'}];
let sessionSource=[..._pseudoSessions];
const _pendingTermPartners=new Map();const _fdPairResolved={};window.linked=[];
const _seenSids=new Set(),_freshSids=new Set(),_autoSwitched=new Set();
function _pendingPartnersOf(sid){const value=_pendingTermPartners.get(sid);return Array.isArray(value)?value:value?[value]:[];}
function _setPendingPartners(sid,partners){_pendingTermPartners.set(sid,[...new Set(partners)].filter(value=>value && value!==sid));}
function _fdLinkPair(sids){window.linked.push(sids);}
function setSessionSource(rows){sessionSource=rows;}
function _patchClientSession(sid,patch){Object.assign(sessionSource.find(row=>row.session_id===sid)||{},patch);}
const currentProject=null;window.openedForks=[];
let _lastNewChatAgent='claude',_lastNewChatAgents=['claude','codex'];window.newChatPrompts=[];
async function showPrompt(options){window.newChatPrompts.push(options);return null;}
async function loadSessions(){}
function _findClientSession(sid){return sessionSource.find(row=>row.session_id===sid)||{session_id:sid};}
async function openConv(sid){window.openedForks.push(sid);}
function showToast(message){throw Error(message);}
function _activateTermPane(sid){activeTermSid=sid;}
function _startLinkedTerminals(sid){}
const _activeTerms=new Set(),_attentionSids=new Set();
function _clearAttention(sid){_attentionSids.delete(sid);}
function _rememberActive(sid){_activeTerms.add(sid);}
function renderSessionList(){}
function _ensureActiveRefresh(){}
function _unmarkActive(sid){window.retiredPseudo=sid;}
function setTermStatus(status){window.lastStatus=status;}
"""
            + active_source + mount_source + new_chat_source
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
            browser = p.chromium.launch(executable_path=os.environ.get("SERENA_PROOF_BROWSER"))
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
            if not page.get_by_role('button', name="Session events", exact=True).first.is_visible():
                page.get_by_role('button', name='Session actions', exact=True).first.click()
            page.get_by_role("button", name="Session events", exact=True).click()
            inspector = page.get_by_role("dialog", name="Session events")
            if provider == "claude":
                inspector.get_by_text("1 workspace/history", exact=True).wait_for()
                assert len(owners) == 1
            else:
                inspector.get_by_text("No events", exact=True).wait_for()
                assert not owners and host._loop is None
            inspector.get_by_role("button", name="Close session events").click()
            if provider != "claude":
                page.get_by_role("button", name="Resume session").click()
            page.get_by_role("button", name="Resume session").wait_for(state="hidden")
            page.route('**/api/workspace/exact/view-context', lambda route: route.fulfill(json={"ok": True}))
            owners[0].rpc.suspended = True
            playwright.expect(page.locator('.aw-state')).to_have_text('sleeping')
            assert owners[0].state == 'ready' and not owners[0].sent and len(owners) == 1
            assert not page.get_by_role("button", name="Send message", exact=True).is_disabled()
            owners[0].rpc.suspended = False
            playwright.expect(page.locator('.aw-state')).to_have_text('ready')
            page.unroute('**/api/workspace/exact/view-context')
            if provider == 'codex':
                if not page.get_by_role('button', name="Apps and connectors", exact=True).first.is_visible():
                    page.get_by_role('button', name='Session actions', exact=True).first.click()
                page.get_by_role('button', name='Apps and connectors', exact=True).click()
                with page.expect_response(lambda response: response.url.endswith('/view-context')
                                          and response.request.post_data_json.get('draft') is True):
                    page.get_by_role('button', name='Select app Demo App', exact=True).click()
                assert host.runtime_context_snapshot()['runtimes'][0]['draft']
                assert not owners[0].sent and len(owners) == 1
                with page.expect_response(lambda response: response.url.endswith('/view-context')
                                          and response.request.post_data_json.get('draft') is False):
                    page.get_by_role('button', name='Remove app Demo App', exact=True).click()
            with page.expect_response(lambda response: response.url.endswith('/view-context')
                                      and response.request.post_data_json.get('draft') is True):
                page.get_by_role("textbox", name=f"Message {provider.capitalize()}").fill(
                    "real mounted page control"
                )
            context = host.runtime_context_snapshot()
            assert context['focused_sid'] == 'exact'
            assert context['runtimes'][0]['draft'] and context['runtimes'][0]['draft_known']
            assert not owners[0].sent
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
            if not page.get_by_role('button', name="Session events", exact=True).first.is_visible():
                page.get_by_role('button', name='Session actions', exact=True).first.click()
            page.get_by_role("button", name="Session events", exact=True).click()
            inspector = page.get_by_role("dialog", name="Session events")
            inspector.get_by_text("1 workspace/history", exact=True).click()
            assert '"id": "exact"' in inspector.locator("pre").inner_text()
            inspector.get_by_role("button", name="Close session events").click()
            page.screenshot(path=str(tmp_path / "mounted-workspace.png"))
            observations = []
            page.on("request", lambda request: observations.append(request.method)
                    if "/api/workspace/" in request.url and not request.url.endswith('/view-context') else None)
            page.reload()
            page.get_by_role("button", name="Resume session").wait_for(state="hidden")
            page.get_by_text("controlled provider output", exact=True).wait_for()
            assert len(owners) == 1 and not owners[0].closed
            assert len(owners[0].sent) == 2
            assert observations and set(observations) == {"GET"}
            assert not errors
            closed_view.clear()
            page.goto(f"http://127.0.0.1:{server.server_port}/parent")
            assert closed_view.wait(3), 'Server did not accept the real pagehide close report'
            assert not host._active_views('exact')
            assert len(owners) == 1 and not owners[0].closed and len(owners[0].sent) == 2
            page.go_back()
            page.get_by_role("button", name="Resume session").wait_for(state="hidden")
            page.get_by_text("controlled provider output", exact=True).wait_for()
            assert len(owners) == 1 and len(owners[0].sent) == 2
            closed_view.clear()
            page.go_forward()
            assert closed_view.wait(3)
            assert not host._active_views('exact')
            page.evaluate("_startStructuredPane('exact', {})")
            nested = page.frame_locator("iframe")
            nested.get_by_role("button", name="Resume session").wait_for(state="hidden")
            nested.get_by_text("controlled provider output", exact=True).wait_for()
            assert page.evaluate("termSessions.get('exact').structured")
            page.evaluate("_startStructuredPane('exact', {})")
            assert page.locator("iframe").count() == 1
            assert nested.locator(".xterm").count() == 0
            page.evaluate("data=>sessionSource.push(data)", {'session_id':'exact','cwd':str(tmp_path),'agent':provider})
            page.evaluate("window.postMessage({type:'serena-workspace-new-conversation',sid:'exact',title:'Spoof'},location.origin)")
            assert page.evaluate('newChatPrompts') == []
            nested.locator('textarea').fill('Keep original draft')
            nested.get_by_role('button',name='Session actions',exact=True).click()
            nested.get_by_role('button',name='New conversation',exact=True).click()
            page.wait_for_function('newChatPrompts.length===1')
            prompt=page.evaluate('newChatPrompts[0]')
            assert prompt['defaultAgent'] == provider and prompt['defaultAgents'] == [provider]
            assert nested.locator('textarea').input_value() == 'Keep original draft'
            assert len(owners) == 1 and not owners[0].closed
            if provider == 'codex':
                nested.locator('textarea').fill('/new Named conversation')
                nested.get_by_role('button',name='Send message',exact=True).click()
                page.wait_for_function('newChatPrompts.length===2')
                assert page.evaluate('newChatPrompts[1].defaultValue') == 'Named conversation'
                assert nested.locator('textarea').input_value() == '/new Named conversation'
            nested.locator('textarea').fill('')
            assert page.evaluate("termSessions.get('exact').state") == "ready"
            page.evaluate("activeTermSid='other';_attentionSids.add('exact')")
            nested.get_by_role('textbox', name=f'Message {provider.capitalize()}').click()
            page.wait_for_function("activeTermSid==='exact' && _webRuntimeFocusSid==='exact'")
            assert not page.evaluate("_attentionSids.has('exact')")
            assert page.evaluate("termSessions.get('exact').mount.classList.contains('runtime-focused')")
            page.evaluate("""()=>{
              _gtkSplitActive=true;_gtkSplitSids=['exact','partner'];
              termSessions.set('partner',{mount:document.createElement('div')});
            }""")
            with page.expect_response(lambda response: response.url.endswith('/view-context')
                                      and response.request.post_data_json.get('split_sids') == ['exact', 'partner']):
                nested.get_by_role('textbox', name=f'Message {provider.capitalize()}').click()
            assert len(owners) == 1
            assert host.runtime_context_snapshot()['split_pair'] == []  # No owner for the unmounted partner.
            with page.expect_response(lambda response: response.url.endswith('/view-context')
                                      and response.request.post_data_json.get('sleep_peers') is True):
                nested.get_by_role('textbox', name=f'Message {provider.capitalize()}').click()
            page.evaluate("_gtkSplitActive=false;_gtkSplitSids=null;termSessions.delete('partner')")
            if not nested.get_by_role('button', name="Session events", exact=True).first.is_visible():
                nested.get_by_role('button', name='Session actions', exact=True).first.click()
            nested.get_by_role('button', name='Session events', exact=True).click()
            nested.get_by_role('dialog', name='Session events').wait_for()
            assert not nested.get_by_role('textbox', name=f'Message {provider.capitalize()}').evaluate('el=>el===document.activeElement')
            nested.get_by_role('button', name='Close session events', exact=True).click()
            page.evaluate("activeTermSid='other';_attentionSids.add('exact');termSessions.get('exact').mount.style.display='none'")
            page.frames[1].evaluate("""()=>parent.postMessage({type:'serena-workspace-focused',sid:'exact'},location.origin)""")
            page.wait_for_timeout(50)
            assert page.evaluate("activeTermSid==='other' && _attentionSids.has('exact')")
            page.evaluate("termSessions.get('exact').mount.style.display=''")
            page.evaluate("window.postMessage({type:'serena-workspace-focused',sid:'exact'},location.origin)")
            page.wait_for_timeout(50)
            assert page.evaluate("activeTermSid==='other' && _attentionSids.has('exact')")
            page.evaluate("_attentionSids.add('exact'); activeTermSid='another-chat'")
            page.frames[1].evaluate("""()=>parent.postMessage({type:'serena-workspace-state',sid:'exact',state:'running'},location.origin)""")
            page.wait_for_function("termSessions.get('exact').busy")
            assert page.evaluate("_activeTerms.has('exact') && _attentionSids.has('exact')")
            page.frames[1].evaluate("""()=>parent.postMessage({type:'serena-workspace-state',sid:'exact',state:'completed'},location.origin)""")
            page.wait_for_function("termSessions.get('exact').state==='completed'")
            assert page.evaluate("_attentionSids.has('exact')")
            page.evaluate("_markActive('exact')")
            assert not page.evaluate("_attentionSids.has('exact')")
            page.evaluate("Object.defineProperty(document,'hasFocus',{configurable:true,value:()=>window.proofHasFocus})")
            for mode, tab, focused, acknowledged in [
                ('read', 'chats', True, False), ('live', 'memory', True, False),
                ('live', 'chats', False, False), ('live', 'chats', True, True),
            ]:
                page.evaluate("""({mode,tab,focused})=>{
                  activeTermSid='exact';convMode=mode;currentTab=tab;window.proofHasFocus=focused;
                  _attentionSids.add('exact');termSessions.get('exact').state='waiting-for-proof';
                }""", {"mode": mode, "tab": tab, "focused": focused})
                page.frames[1].evaluate("""()=>parent.postMessage({type:'serena-workspace-state',sid:'exact',state:'completed'},location.origin)""")
                page.wait_for_function("termSessions.get('exact').state==='completed'")
                assert page.evaluate("!_attentionSids.has('exact')") is acknowledged
            handoffs = []
            original_bridge = host.bridge
            host.bridge = lambda sid, agent, prompt, request_id, **kwargs: handoffs.append((sid, agent, prompt, request_id)) or {"ok": True, "queued": True, "pending": True}
            nested.get_by_role("textbox", name=f"Message {provider.capitalize()}").fill("Keep the unsent draft")
            page.evaluate("""()=>{
              currentSessionId='exact';
              if(!document.getElementById('convTitle')){
                const title=document.createElement('h2');title.id='convTitle';document.body.append(title);
              }
              setSessionSource([{session_id:'exact',display_title:'Before rename',agent:'claude'}]);
            }""")
            page.evaluate("window.postMessage({type:'serena-workspace-catalog',sid:'exact',title:'Spoofed'},location.origin)")
            page.wait_for_timeout(50)
            assert page.evaluate("_findClientSession('exact').display_title") == "Before rename"
            host.journal.append('exact', {'method': 'workspace/catalog', 'params': {
                'session_id': 'exact', 'indexed': True, 'display_title': '<b>Native rename</b>'}})
            page.wait_for_function("_findClientSession('exact').display_title==='<b>Native rename</b>'")
            assert page.locator('#convTitle').inner_text() == '<b>Native rename</b>'
            assert page.locator('#convTitle b').count() == 0
            page.evaluate("_patchClientSession('exact',{custom_title:'My newer name',display_title:'My newer name'})")
            page.frames[1].evaluate("""()=>parent.postMessage({type:'serena-workspace-catalog',sid:'exact',title:'Older native name'},location.origin)""")
            page.wait_for_function("document.getElementById('convTitle').textContent==='My newer name'")
            assert page.evaluate("_findClientSession('exact').display_title") == 'My newer name'
            if provider == 'claude':
                page.evaluate("""()=>{
                  window.originalLoadSessions=loadSessions;window.titleRefreshes=0;
                  loadSessions=async(project,options)=>{
                    if(options.refresh!==true)throw Error('Expected fresh title read');
                    window.titleRefreshes++;setSessionSource([{session_id:'exact',agent:'claude',custom_title:'Newest saved Claude title',display_title:'Newest saved Claude title'}]);
                  };
                }""")
                for stale in ('Confirmed native title', 'Older replayed title'):
                    before = page.evaluate('titleRefreshes')
                    host.journal.append('exact', {'method': 'workspace/catalog', 'params': {
                        'session_id': 'exact', 'indexed': True, 'native_rename': True, 'display_title': stale}})
                    page.wait_for_function('before=>titleRefreshes>before', arg=before)
                    assert page.locator('#convTitle').inner_text() == 'Newest saved Claude title'
                    assert nested.get_by_role('textbox', name='Message Claude').input_value() == 'Keep the unsent draft'
                page.evaluate('loadSessions=originalLoadSessions')
            if provider == 'codex':
                host.register_fork = lambda target: {'display_title': target['confirmed_native_name']}
                page.evaluate("""()=>{
                  window.originalLoadSessions=loadSessions;window.titleRefreshes=0;
                  loadSessions=async(project,options)=>{
                    if(options.refresh!==true)throw Error('Expected fresh title read');
                    window.titleRefreshes++;setSessionSource([{session_id:'exact',agent:'codex',custom_title:'Newest saved title',display_title:'Newest saved title'}]);
                  };
                }""")
                page.evaluate("window.postMessage({type:'serena-workspace-title-changed',sid:'exact'},location.origin)")
                page.wait_for_timeout(50)
                assert page.evaluate('titleRefreshes') == 0
                if not nested.get_by_role('button', name="Rename conversation", exact=True).first.is_visible():
                    nested.get_by_role('button', name='Session actions', exact=True).first.click()
                nested.get_by_role('button', name='Rename conversation', exact=True).click()
                rename = nested.get_by_role('dialog', name='Rename conversation', exact=True)
                rename.get_by_role('textbox', name='Conversation title').fill('Requested native title')
                rename.get_by_role('button', name='Rename', exact=True).click()
                page.wait_for_function("document.getElementById('convTitle').textContent==='Newest saved title'")
                assert owners[0].renamed == 'Requested native title'
                assert page.evaluate('titleRefreshes') == 1
                page.evaluate('loadSessions=window.originalLoadSessions')
            assert nested.get_by_role('textbox', name=f'Message {provider.capitalize()}').input_value() == 'Keep the unsent draft'
            assert len(owners) == 1 and not owners[0].closed
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
            page.evaluate("_freshSids.add('33333333-3333-4333-8333-333333333333')")
            created_frame.evaluate("""() => parent.postMessage({type:'serena-workspace-open-created',sid:'new-proof',target:'33333333-3333-4333-8333-333333333333'},location.origin)""")
            page.wait_for_function("() => openedForks.length === 3")
            assert page.evaluate("openedForks[2]") == "33333333-3333-4333-8333-333333333333"
            assert page.evaluate("retiredPseudo") == "new-proof"
            assert renames == [("33333333-3333-4333-8333-333333333333", "My named conversation")]
            assert not page.evaluate("termSessions.has('new-proof')")
            assert page.evaluate("_seenSids.has('33333333-3333-4333-8333-333333333333') && !_freshSids.has('33333333-3333-4333-8333-333333333333') && _autoSwitched.has('33333333-3333-4333-8333-333333333333')")
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
