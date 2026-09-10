"""Exercise the source or frozen HTTP workspace using an isolated seeded session."""
import hashlib
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
    source = Path(binary).suffix == ".py"
    label_prefix = "source" if source else "frozen"
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
        command = ([sys.executable] if source else []) + [str(Path(binary).resolve()), "--host", "127.0.0.1", "--port", str(port)]
        process = subprocess.Popen(command,
                                   env=env, cwd=root, stdout=output, stderr=output, start_new_session=True)
        children = []
        windows_job = None
        try:
            if os.name == "nt":
                from core.workspace_windows_job import WindowsJob
                windows_job = WindowsJob()
                windows_job.assign(process.pid)
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
            lease_path = Path(env["SERENA_RUNTIME_LEASE_DIR"]) / (hashlib.sha256(sid.encode()).hexdigest() + ".json")
            lease = json.loads(lease_path.read_text())
            native = psutil.Process(lease["child"]["pid"])
            assert native.create_time() == lease["child"]["born"]
            children = [native]
            parent = native.parent()
            while parent is not None and parent.pid != process.pid:
                children.append(parent)
                parent = parent.parent()
            assert parent is not None, "Native lease is not owned by this server"
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
            print(f"PASS: {label_prefix} HTTP app served real workspace/assets, explicit exact-session attach, native local-command output/completion, page reload retained owner")
            screenshots = Path(__file__).resolve().parents[1] / "apps/desktop/build/workspace-proof"
            screenshots.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    channel=os.environ.get("SERENA_PROOF_BROWSER_CHANNEL") or None,
                    executable_path=os.environ.get("SERENA_PROOF_BROWSER_EXECUTABLE") or None)
                try:
                    for label, width, height in (("desktop", 1440, 1000), ("mobile", 390, 844)):
                        page = browser.new_page(viewport={"width": width, "height": height})
                        errors = []
                        page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                        page.on("console", lambda message, errors=errors: errors.append(f"{message.text} {message.location}") if message.type == "error" else None)
                        page.on("response", lambda response, errors=errors: errors.append(f"HTTP {response.status} {response.url}") if response.status >= 400 else None)
                        page.goto(f"{base}/workspace/{sid}")
                        page.get_by_role("button", name="Resume session", exact=True).click()
                        expect(page.get_by_role("button", name="Resume session", exact=True)).to_be_hidden()
                        skill = config / "skills/browser-proof/SKILL.md"
                        skill.parent.mkdir(parents=True, exist_ok=True)
                        skill.write_text("---\nname: browser-proof\ndescription: Isolated browser reload proof\n---\nReturn proof.\n")
                        page.get_by_role("button", name="Commands and skills", exact=True).click()
                        dialog = page.get_by_role("dialog", name="Commands and skills", exact=True)
                        dialog.get_by_role("button", name="Reload plugins from disk", exact=True).click()
                        expect(dialog.get_by_role("status")).to_contain_text("0 plugin errors", timeout=15000)
                        assert all(child.is_running() for child in children)
                        print(f"PASS: {label_prefix} {label} explicit native plugin reload returned zero errors with existing owner alive")
                        reload = dialog.get_by_role("button", name="Reload skills from disk", exact=True)
                        reload.click()
                        expect(reload).to_be_enabled(timeout=15000)
                        expect(dialog.locator(".aw-command").filter(has_text="/browser-proof")).to_have_count(1)
                        assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth"), "Command picker overflow"
                        page.screenshot(path=str(screenshots / f"{label_prefix}-skills-{label}.png"))
                        skill.unlink()
                        reload.click()
                        expect(dialog.locator(".aw-command").filter(has_text="/browser-proof")).to_have_count(0)
                        dialog.get_by_role("button", name="Close commands", exact=True).click()
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
                        assert "<command-name>" not in page.locator(".aw-transcript").inner_text()
                        result_text = turn["providerOriginal"]["result"]
                        matching = [item for item in events if item["event"]["method"] == "item/completed"
                                    and item["event"]["params"].get("turnId") == turn["id"]
                                    and item["event"]["params"]["item"].get("text") == result_text]
                        assert len(matching) == 1, "Frozen native command result duplicated"
                        mention_file = root / "workspace-claude-mention-proof.py"
                        mention_file.write_text("# Isolated names-only file picker proof\n")
                        try:
                            page.get_by_role("button", name="Mention project file", exact=True).click()
                            picker = page.get_by_role("dialog", name="Mention project file")
                            search = picker.get_by_role("searchbox", name="Find project file")
                            search.fill("workspace-claude-mention-proof")
                            search.press("Enter")
                            picker.get_by_role("button", name="workspace-claude-mention-proof.py", exact=True).click()
                            expect(page.get_by_role("textbox", name="Message Claude", exact=True)).to_have_value("@workspace-claude-mention-proof.py ")
                            composer = page.get_by_role("textbox", name="Message Claude", exact=True)
                            composer.fill("inspect @workspace-claude-mention")
                            page.get_by_role("option", name="workspace-claude-mention-proof.py", exact=True).wait_for()
                            page.screenshot(path=str(screenshots / f"{label_prefix}-mentions-{label}.png"))
                            composer.press("Tab")
                            expect(composer).to_have_value("inspect @workspace-claude-mention-proof.py ")
                            assert all(child.is_running() for child in children)
                            print(f"PASS: {label_prefix} {label} Claude project file picker inserted a draft mention with existing owner alive")
                        finally:
                            mention_file.unlink()
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), label
                        assert not errors, errors
                        page.screenshot(path=str(screenshots / f"{label_prefix}-{label}.png"))
                        if source:
                            page.get_by_role("button", name="Fork conversation", exact=True).click()
                            fork_dialog = page.get_by_role("dialog", name="Fork conversation", exact=True)
                            if label == "mobile":
                                # Fail only this isolated catalog's open; native copy and
                                # the separate command journal remain available.
                                index = Path(env["CHATS_DATA_DIR"]) / "index.db"
                                backup = index.with_suffix(".proof-backup")
                                index.rename(backup)
                                try:
                                    index.mkdir()
                                    fork_dialog.get_by_role("button", name="Create fork", exact=True).click()
                                    expect(fork_dialog.get_by_role("button", name="Retry fork registration", exact=True)).to_be_visible(timeout=15000)
                                    saved_sid = fork_dialog.locator("code").inner_text()
                                finally:
                                    if index.is_dir():
                                        index.rmdir()
                                    backup.rename(index)
                                page.reload()
                                page.get_by_role("button", name="Resume session", exact=True).click()
                                page.get_by_role("button", name="Fork conversation", exact=True).click()
                                expect(fork_dialog.locator("code")).to_have_text(saved_sid)
                                fork_dialog.get_by_role("button", name="Retry fork registration", exact=True).click()
                                expect(fork_dialog.locator("code")).to_have_text(saved_sid)
                                print("PASS: real catalog-open failure recovered same native fork after page reload")
                            else:
                                fork_dialog.get_by_role("button", name="Create fork", exact=True).click()
                            expect(fork_dialog.get_by_role("status")).to_have_text("Fork created", timeout=15000)
                            fork_sid = fork_dialog.locator("code").inner_text()
                            assert fork_sid != sid
                            assert json.loads(request(f"/api/workspace/{fork_sid}/events"))["events"] == []
                            page.screenshot(path=str(screenshots / f"source-fork-{label}.png"))
                            fork_dialog.get_by_role("button", name="Open fork", exact=True).click()
                            page.wait_for_url(f"{base}/workspace/{fork_sid}")
                            expect(page.get_by_role("button", name="Resume session", exact=True)).to_be_visible()
                            assert json.loads(request(f"/api/workspace/{fork_sid}/events"))["events"] == []
                            assert set(child.pid for child in psutil.Process(process.pid).children(recursive=True)) == set(child.pid for child in children)
                            print(f"PASS: {label_prefix} {label} browser created native fork and opened exact view without launching its owner")
                        page.close()
                        assert not errors, errors
                        assert all(child.is_running() for child in children), "Closing a view killed its session"
                        print(f"PASS: {label_prefix} {label} browser reloaded added/removed native skill and sent local command; completed, no page/console/HTTP errors or horizontal overflow; closing view retained owner")
                finally:
                    browser.close()
        except HTTPError as error:
            raise AssertionError(f"HTTP {error.code}: {error.read().decode()}\n{log.read_text()}") from error
        finally:
            if process.poll() is None:
                if windows_job is not None:
                    windows_job.terminate()
                elif os.name == "nt":
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            if windows_job is not None:
                windows_job.close()
            _, alive = psutil.wait_procs(children, timeout=3)
            for child in alive:
                child.kill()
            psutil.wait_procs(alive, timeout=3)


if __name__ == "__main__":
    main()
