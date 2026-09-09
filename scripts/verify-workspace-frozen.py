"""Exercise the actual frozen HTTP workspace using an isolated preseeded session."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psutil


class BootParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.text = ""

    def handle_starttag(self, tag, attrs):
        self.active = tag == "script" and dict(attrs).get("id") == "workspace-boot"

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.text += data


def main():
    # Only the proof imports optional browser tooling; the frozen server stays isolated.
    browser_packages = os.environ.pop("SERENA_PROOF_PYTHONPATH", "")
    if browser_packages:
        sys.path.append(browser_packages)
    from playwright.sync_api import expect, sync_playwright

    binary, sdk, electron, sid, directory = sys.argv[1:]
    root = Path(directory).resolve()
    assert root.name.startswith("serena-claude-driver-") and Path(os.environ["HOME"]).resolve() == root
    config = root / "config"
    env = {**os.environ, "CLAUDE_DIR": str(config), "CLAUDE_CONFIG_DIR": str(config),
           "CHATS_DATA_DIR": str(root / "frozen-data"), "SERENA_STRUCTURED_WORKSPACE": "1",
           "SERENA_CALL_RUNTIME": "lazy", "SERENA_RUNTIME_LEASE_DIR": str(root / "frozen-leases"),
           "SERENA_WORKSPACE_RUNTIME_ROOT": str(Path(sdk).resolve().parents[3]),
           "SERENA_WORKSPACE_NODE": str(Path(electron).resolve()), "SERENA_WORKSPACE_NODE_MODE": "electron",
           "DBUS_SESSION_BUS_ADDRESS": f"unix:path={root}/unavailable-bus", "XDG_RUNTIME_DIR": str(root / "xdg-runtime")}
    Path(env["XDG_RUNTIME_DIR"]).mkdir(exist_ok=True)
    os.environ.update(env)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core.indexer import _get_db

    transcripts = list((config / "projects").rglob(f"{sid}.jsonl"))
    assert len(transcripts) == 1
    transcript = transcripts[0]
    db = _get_db()
    db.execute("INSERT INTO sessions (session_id, project_dir, cwd, file_path, agent, title) VALUES (?, ?, ?, ?, ?, ?)",
               (sid, transcript.parent.name, str(root), str(transcript), "claude", "Frozen isolated proof"))
    db.commit()
    db.close()
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    token = ""

    def request(path, payload=None):
        headers = {"Content-Type": "application/json", "X-Serena-Workspace-Token": token}
        data = json.dumps(payload).encode() if payload is not None else None
        with urlopen(Request(base + path, data=data, headers=headers), timeout=15) as response:
            return response.read().decode()

    log = root / "frozen-server.log"
    with log.open("w") as output:
        process = subprocess.Popen([str(Path(binary).resolve()), "--host", "127.0.0.1", "--port", str(port)],
                                   env=env, cwd=root, stdout=output, stderr=output, start_new_session=True)
        children = []
        try:
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None:
                    raise AssertionError(log.read_text())
                try:
                    request("/api/health")
                    break
                except (URLError, TimeoutError):
                    if time.monotonic() > deadline:
                        raise AssertionError(log.read_text()) from None
                    time.sleep(0.1)
            page = request(f"/workspace/{sid}")
            parser = BootParser()
            parser.feed(page)
            boot = json.loads(parser.text)
            assert boot["sessionId"] == sid
            token = boot["token"]
            for asset in ("workspace-page.mjs", "workspace-pane.mjs", "workspace-pane.css"):
                assert request(f"/static/{asset}")
            assert json.loads(request(f"/api/workspace/{sid}/events"))["events"] == []
            attached = json.loads(request(f"/api/workspace/{sid}/attach", {}))
            assert attached["ok"], attached
            children = psutil.Process(process.pid).children(recursive=True)
            assert children, "No native session process started"
            sent = json.loads(request(f"/api/workspace/{sid}/commands", {
                "request_id": "frozen-local-command", "action": "submit",
                "payload": {"inputs": [{"type": "text", "text": "/effort low"}]}}))
            assert sent["ok"], sent
            deadline = time.monotonic() + 15
            while True:
                events = json.loads(request(f"/api/workspace/{sid}/events"))["events"]
                completed = [item["event"] for item in events if item["event"]["method"] == "turn/completed"]
                if completed:
                    turn = completed[-1]["params"]["turn"]
                    assert turn["status"] == "completed", turn
                    assert turn["providerOriginal"]["num_turns"] == 0
                    break
                assert time.monotonic() < deadline, events
                time.sleep(0.05)
            request(f"/workspace/{sid}")
            assert all(child.is_running() for child in children)
            print("PASS: frozen HTTP app served real workspace/assets, explicit exact-session attach, native local-command output/completion, page reload retained owner")
            screenshots = Path(__file__).resolve().parents[1] / "apps/desktop/build/workspace-proof"
            screenshots.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    for label, width, height in (("desktop", 1440, 1000), ("mobile", 390, 844)):
                        page = browser.new_page(viewport={"width": width, "height": height})
                        errors = []
                        page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                        page.on("console", lambda message, errors=errors: errors.append(message.text) if message.type == "error" else None)
                        page.on("response", lambda response, errors=errors: errors.append(f"HTTP {response.status} {response.url}") if response.status >= 400 else None)
                        page.goto(f"{base}/workspace/{sid}")
                        page.get_by_role("button", name="Resume session", exact=True).click()
                        expect(page.get_by_role("button", name="Resume session", exact=True)).to_be_hidden()
                        before = len(completed)
                        page.get_by_role("textbox", name="Message Claude", exact=True).fill("/effort medium")
                        page.get_by_role("button", name="Send message", exact=True).click()
                        deadline = time.monotonic() + 15
                        while True:
                            events = json.loads(request(f"/api/workspace/{sid}/events"))["events"]
                            completed = [item["event"] for item in events if item["event"]["method"] == "turn/completed"]
                            if len(completed) > before:
                                turn = completed[-1]["params"]["turn"]
                                assert turn["status"] == "completed", turn
                                assert turn["providerOriginal"]["num_turns"] == 0
                                break
                            assert time.monotonic() < deadline, events
                            page.wait_for_timeout(50)
                        expect(page.locator(".aw-state")).to_have_text("completed")
                        expect(page.get_by_role("textbox", name="Message Claude", exact=True)).to_have_value("")
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), label
                        assert not errors, errors
                        page.screenshot(path=str(screenshots / f"frozen-{label}.png"))
                        page.close()
                        assert all(child.is_running() for child in children), "Closing a view killed its session"
                        print(f"PASS: frozen {label} browser sent native local command; completed, no page/console/HTTP errors or horizontal overflow; closing view retained owner")
                finally:
                    browser.close()
        except HTTPError as error:
            raise AssertionError(f"HTTP {error.code}: {error.read().decode()}\n{log.read_text()}") from error
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            _, alive = psutil.wait_procs(children, timeout=3)
            for child in alive:
                child.kill()
            psutil.wait_procs(alive, timeout=3)


if __name__ == "__main__":
    main()
