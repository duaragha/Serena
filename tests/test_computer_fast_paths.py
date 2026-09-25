"""Her browser by element and her shell as text: real Chromium, real tmux."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_computer_use import Desktop, begin

from core import computer_shell
from core.action_authority import ActionAuthority
from core.computer_platform import ComputerError
from core.computer_use import ComputerController
from core.computer_web import HerBrowser, WebError, validate_steps

FORM = b"""<!doctype html><title>Setup</title>
<h1>Create project</h1>
<label>Project name <input name="name"></label>
<label>Password <input type="password" name="pw"></label>
<select aria-label="Plan"><option>Spark</option><option>Blaze</option></select>
<label><input type="checkbox" name="analytics" checked> Enable analytics</label>
<button onclick="document.body.insertAdjacentHTML('beforeend','<p>Project '+
 document.querySelector('[name=name]').value+' on '+document.querySelector('select').value+
 (document.querySelector('[name=analytics]').checked?' with':' without')+' analytics is ready</p>')">Continue</button>
<a href="/popup" target="_blank">Open docs</a>"""


class Pages(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = FORM if self.path == "/" else b"<title>Docs</title><h1>Docs page</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def chromium(tmp_path):
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            binary = p.chromium.executable_path
    except Exception:
        pytest.skip("Playwright Chromium is required")
    if not os.path.exists(binary):
        pytest.skip("Playwright Chromium is not installed")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Pages)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tmp_path / "profile"
    profile.mkdir()
    process = subprocess.Popen(
        [
            binary,
            "--headless=new",
            f"--user-data-dir={profile}",
            "--remote-debugging-port=0",
            "--no-first-run",
            f"http://127.0.0.1:{server.server_port}/",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    web = HerBrowser(profile)
    try:
        yield web, f"http://127.0.0.1:{server.server_port}"
    finally:
        web.close()
        os.killpg(process.pid, 15)
        process.wait(timeout=5)
        server.shutdown()


def test_one_call_runs_a_whole_form_flow_and_reads_the_result(chromium):
    web, base = chromium
    deadline = time.monotonic() + 10
    while True:
        try:
            snapshot = web.snapshot()
            if "Create project" in snapshot["snapshot"]:
                break
        except WebError:
            pass
        assert time.monotonic() < deadline, "headless Chromium never published its page"
        time.sleep(0.2)
    assert "[ref=e" in snapshot["snapshot"]  # element refs for targeting
    started = time.monotonic()
    result = web.run(
        [
            {"fill": {"label": "Project name"}, "value": "Locket Push"},
            {"select": {"role": "combobox", "name": "Plan"}, "value": "Blaze"},
            {"uncheck": {"label": "Enable analytics"}},
            {"click": {"role": "button", "name": "Continue"}},
            {"wait_for": {"text": "is ready"}},
            {"read": {"text": "is ready"}},
        ],
        cancelled=lambda: False,
    )
    elapsed = time.monotonic() - started
    assert result["ok"], result["steps"]
    assert result["steps"][-1]["detail"] == "Project Locket Push on Blaze without analytics is ready"
    assert elapsed < 5  # machine speed: no model call between steps
    popup = web.run([{"click": {"text": "Open docs"}}, {"wait_for": {"text": "Docs page"}}], cancelled=lambda: False)
    assert popup["ok"] and popup["url"].endswith("/popup") and len(popup["tabs"]) == 2, {k: popup[k] for k in ("ok", "steps", "url", "tabs")}
    back = web.run([{"tab": {"title_contains": "Setup"}}], cancelled=lambda: False)
    assert back["ok"] and back["title"] == "Setup"


def test_failures_stop_the_batch_and_password_fields_refuse_fill(chromium):
    web, base = chromium
    deadline = time.monotonic() + 10
    while True:
        try:
            if "Create project" in web.snapshot()["snapshot"]:
                break
        except WebError:
            pass
        assert time.monotonic() < deadline
        time.sleep(0.2)
    result = web.run(
        [
            {"fill": {"label": "Password"}, "value": "hunter2"},
            {"click": {"role": "button", "name": "Continue"}},
        ],
        cancelled=lambda: False,
    )
    assert not result["ok"] and len(result["steps"]) == 1
    assert "handoff" in result["steps"][0]["detail"]
    missing = web.run([{"click": {"role": "button", "name": "Nope"}, "timeout": 0.5}], cancelled=lambda: False)
    assert not missing["ok"]
    held = web.run([{"click": {"role": "button", "name": "Continue"}}], cancelled=lambda: True)
    assert not held["ok"] and "held" in held["steps"][0]["detail"]


@pytest.mark.parametrize(
    "steps",
    [
        [],
        [{"click": {"ref": "12"}}],
        [{"goto": "file:///etc/passwd"}],
        [{"fill": {"label": "x"}}],
        [{"click": {"text": "a"}, "goto": "https://x.test"}],
        [{"wait_for": {"text": "a", "url": "b"}}],
        [{"click": {"text": "a"}, "timeout": 99}],
    ],
)
def test_malformed_batches_are_rejected_before_anything_runs(steps):
    with pytest.raises(WebError):
        validate_steps(steps)


@pytest.fixture
def shell(monkeypatch):
    if not shutil.which("tmux"):
        pytest.skip("tmux is required")
    socket = f"serena-test-{os.getpid()}"
    monkeypatch.setattr(computer_shell, "SOCKET", socket)
    runner = computer_shell.HerShell({**os.environ, "PS1": "$ "})
    yield runner
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)


def test_shell_returns_output_and_exit_code_as_text(shell):
    done = shell.run("echo fast-path; printf 'two\\n'", timeout=10)
    assert done["status"] == "done" and done["exit_code"] == 0
    assert done["output"].splitlines()[-2:] == ["fast-path", "two"]
    failed = shell.run("false", timeout=10)
    assert failed["exit_code"] == 1
    waiting = shell.run("read answer; echo got-$answer", timeout=0.5)
    assert waiting["status"] == "running"
    assert "got-yes" in shell.send("yes")["output"]
    with pytest.raises(ComputerError, match="one foreground command"):
        shell.run("sleep 5 &")


class FakeWeb:
    def __init__(self):
        self.runs = []

    def snapshot(self):
        return {"url": "https://x.test", "snapshot": "- button \"Go\" [ref=e1]"}

    def run(self, steps, cancelled):
        self.runs.append(steps)
        return {"ok": True, "steps": [{"step": 1, "ok": True}], "url": "https://x.test", "snapshot": "page text"}


class FakeTerminal:
    def run(self, command, timeout, cancelled):
        return {"status": "done", "exit_code": 0, "output": f"ran {command}"}

    def send(self, text, enter):
        return {"output": text}

    def read(self):
        return {"output": "recent"}


@pytest.fixture
def controller(tmp_path):
    c = ComputerController(
        Desktop(), authority=ActionAuthority(tmp_path / "authority.sqlite", publish_events=False)
    )
    yield c
    c.close()


def test_structured_input_is_authorized_idempotent_and_kept_out_of_events(controller):
    c = controller
    with pytest.raises(ComputerError, match="own desktop"):
        begin(c)
        c.browser(c.session.id, [{"goto": "https://x.test"}], request_id="web-batch-01", intent="open")
    c.stop()
    c.web, c.terminal = FakeWeb(), FakeTerminal()
    sid, _ = begin(c)
    steps = [{"click": {"ref": "e1"}}, {"wait_for": {"text": "done"}}]
    first = c.browser(sid, steps, request_id="web-batch-01", intent="press go")
    assert first["ok"] and first["snapshot"] == "page text" and "run_ms" in first["timing"]
    again = c.browser(sid, steps, request_id="web-batch-01", intent="press go")
    assert again["replayed"] and len(c.web.runs) == 1
    with pytest.raises(ComputerError, match="reused"):
        c.browser(sid, steps[:1], request_id="web-batch-01", intent="press go")
    ran = c.shell(sid, command="uname -r", request_id="shell-cmd-01", intent="kernel")
    assert ran["output"] == "ran uname -r" and ran["exit_code"] == 0
    assert c.shell(sid, read=True) == {"output": "recent"}
    assert c.browser_snapshot(sid)["url"] == "https://x.test"
    finished = [e for e in c.events if e["type"] == "action_finished"]
    assert finished and all("snapshot" not in e and "output" not in e for e in finished)
    c.stop()
    sid, _ = begin(c, mode="watch")
    with pytest.raises(ComputerError, match="cannot send input"):
        c.shell(sid, command="ls", request_id="shell-cmd-02", intent="list")
