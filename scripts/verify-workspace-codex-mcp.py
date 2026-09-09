"""Isolated native MCP OAuth/reload proof, with a loopback-only identity server."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_codex import CodexWorkspace
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpc


class OAuthFixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, data, **headers):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        base = self.server.base
        if self.path.startswith("/.well-known/oauth-protected-resource"):
            self.reply(200, {"resource": base + "/mcp", "authorization_servers": [base]})
        elif self.path.startswith("/.well-known/oauth-authorization-server"):
            self.reply(200, {"issuer": base, "authorization_endpoint": base + "/authorize",
                            "token_endpoint": base + "/token", "registration_endpoint": base + "/register",
                            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"]})
        elif self.path == "/mcp":
            self.reply(405, {})
        elif self.path.startswith("/authorize?"):
            query = parse_qs(urlsplit(self.path).query)
            callback = query["redirect_uri"][0]
            if urlsplit(callback).hostname not in {"127.0.0.1", "localhost"}:
                self.reply(400, {})
                return
            self.send_response(302)
            self.send_header("Location", callback + "?" + urlencode({"code": "isolated-code", "state": query["state"][0]}))
            self.end_headers()
        else:
            self.reply(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/register":
            request = json.loads(raw)
            self.reply(201, {"client_id": "isolated-proof", "redirect_uris": request["redirect_uris"], "token_endpoint_auth_method": "none"})
        elif self.path == "/token":
            self.server.tokens += 1
            self.reply(200, {"access_token": "isolated-proof-token", "token_type": "Bearer", "expires_in": 3600})
        elif self.path == "/mcp":
            if self.headers.get("Authorization") != "Bearer isolated-proof-token":
                self.reply(401, {}, **{"WWW-Authenticate": f'Bearer resource_metadata="{self.server.base}/.well-known/oauth-protected-resource"'})
                return
            message = json.loads(raw)
            if "id" not in message:
                self.reply(202, {})
                return
            result = {"tools": []}
            if message["method"] == "initialize":
                result = {"protocolVersion": message["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "isolated-proof", "version": "1"}}
            self.reply(200, {"jsonrpc": "2.0", "id": message["id"], "result": result})
        else:
            self.reply(404, {})


def browser_proof(sid, root, project, env, binary):
    from flask import Flask
    from playwright.sync_api import expect, sync_playwright
    from werkzeug.serving import WSGIRequestHandler, make_server

    from ui.workspace_app import install_workspace

    class NativeOwner(CodexWorkspace):
        async def open(self):
            return await super().open(binary=binary, env=env)

    class Quiet(WSGIRequestHandler):
        def log_request(self, *args):
            pass

    repo = Path(__file__).resolve().parents[1]
    app = Flask(__name__, static_folder=str(repo / "ui" / "static"))
    host = install_workspace(app, root / "browser.db",
        resolve=lambda requested: {"session_id": sid, "provider": "codex", "cwd": str(project)} if requested == sid else None,
        describe=lambda requested: {"session_id": sid, "agent": "codex"} if requested == sid else None,
        factories={"codex": lambda **kwargs: NativeOwner(**kwargs, lease_factory=lambda session: SessionLease(session, directory=root / "leases"))})
    server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    artifacts = repo / "apps" / "desktop" / "build" / "workspace-proof"
    artifacts.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                for label, width in (("desktop", 1440), ("mobile", 390)):
                    page = browser.new_page(viewport={"width": width, "height": 900})
                    errors = []
                    page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                    page.on("console", lambda message, errors=errors: errors.append(message.text) if message.type == "error" else None)
                    page.on("response", lambda response, errors=errors: errors.append(f"HTTP {response.status}: {response.url}") if response.status >= 400 and not response.url.endswith("favicon.ico") else None)
                    page.goto(f"http://127.0.0.1:{server.server_port}/workspace/{sid}")
                    if label == "desktop":
                        assert not host._sessions
                    page.get_by_role("button", name="Resume session", exact=True).click()
                    page.get_by_role("button", name="MCP connections", exact=True).click()
                    button = page.get_by_role("button", name="Sign in to proof", exact=True)
                    button.wait_for()
                    owner = host._sessions[sid][0]
                    pid = owner.rpc.process.pid
                    button.click()
                    link = page.get_by_role("link", name="Continue authorization")
                    link.wait_for()
                    assert urlsplit(link.get_attribute("href")).hostname == "127.0.0.1"
                    page.screenshot(path=str(artifacts / f"codex-mcp-{label}.png"))
                    with page.expect_popup() as popup:
                        link.click()
                    popup.value.wait_for_load_state()
                    page.get_by_text("Login: succeeded", exact=True).wait_for(timeout=20000)
                    expect(link).to_have_count(0)
                    page.get_by_role("button", name="Reload MCP configuration").click()
                    expect(page.get_by_role("button", name="Reload MCP configuration")).to_be_enabled()
                    assert page.get_by_role("dialog").evaluate("el=>el.scrollWidth<=el.clientWidth")
                    popup.value.close()
                    page.close()
                    assert owner.rpc.process.pid == pid and owner.rpc.process.returncode is None
                    assert not errors, errors
                    print(f"PASS: native {label} OAuth browser flow and reload; no auto-authorization, same owner after closing view")
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        host.shutdown()


async def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), OAuthFixture)
    server.base = f"http://127.0.0.1:{server.server_port}"
    server.tokens = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="serena-mcp-proof-") as temporary:
            root = Path(temporary)
            home, project = root / "home", root / "project"
            home.mkdir()
            project.mkdir()
            (home / "config.toml").write_text(f'mcp_oauth_credentials_store = "file"\n[mcp_servers.proof]\nurl = "{server.base}/mcp"\nstartup_timeout_sec = 3\n')
            env = {"PATH": os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"), "OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}
            binary = shutil.which("codex")
            rpc = WorkspaceRpc()
            owner = None
            try:
                await rpc.start([binary, "app-server", "--stdio"], cwd=project, env=env)
                await rpc.request("initialize", {"clientInfo": {"name": "serena-mcp-proof", "version": "1"}, "capabilities": {"experimentalApi": True}})
                await rpc.notify("initialized", {})
                sid = (await rpc.request("thread/start", {"cwd": str(project)}))["thread"]["id"]
                await rpc.request("thread/shellCommand", {"threadId": sid, "command": "printf MCP_PROOF", "timeoutMs": 5000})
                async with asyncio.timeout(15):
                    while (await rpc.events.get()).get("method") != "turn/completed":
                        pass
                await rpc.close()
                events = []
                async def publish(event):
                    events.append(event)
                owner = CodexWorkspace(session_id=sid, cwd=project, publish=publish, lease_factory=lambda value: SessionLease(value, directory=root / "leases"))
                await owner.open(binary=binary, env=env)
                pid = owner.rpc.process.pid
                login = await owner.login_mcp("proof")
                assert login["status"] == "pending", login
                assert await owner.login_mcp("proof") == login
                assert server.tokens == 0, "Login authorized itself"
                query = parse_qs(urlsplit(login["authorizationUrl"]).query)
                callback = query["redirect_uri"][0]
                assert urlsplit(callback).hostname in {"127.0.0.1", "localhost"}
                callback += "?" + urlencode({"code": "isolated-code", "state": query["state"][0]})
                def authorize():
                    with urlopen(callback, timeout=10) as response:
                        response.read()
                await asyncio.to_thread(authorize)
                async with asyncio.timeout(15):
                    while owner._mcp_logins["proof"]["status"] == "pending":
                        await asyncio.sleep(0.05)
                assert owner._mcp_logins["proof"]["status"] == "succeeded", owner._mcp_logins
                assert server.tokens == 1
                result = await owner.reload_mcp()
                assert result["data"][0]["authStatus"] == "oAuth", result
                assert owner.rpc.process.pid == pid and owner.session_id == sid
                assert not any(event.get("method") == "turn/started" for event in events)
                print("PASS: native exact-session OAuth remained pending until explicit loopback callback; one token exchange; native completion and configuration reload preserved owner; no inference or user credentials")
            finally:
                await rpc.close()
                if owner:
                    await owner.close()
            await asyncio.to_thread(browser_proof, sid, root, project, env, binary)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
