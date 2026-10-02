"""Sign-in mode for her browser, and a chat's fast paths on her desktop."""

from __future__ import annotations

import asyncio
import time

import pytest
from test_computer_own_desktop import IsolatedDesktop as FakeIsolatedDesktop
from test_computer_own_desktop import Runtime
from test_computer_use import Desktop, act, begin

from core import computer_use
from core.action_authority import ActionAuthority
from core.computer_nested import SIGNING_IN, IsolatedDesktop
from core.computer_platform import ComputerError
from core.computer_use import ComputerController


@pytest.fixture
def controller(tmp_path, monkeypatch):
    monkeypatch.setattr("core.computer_mcp.origin_arguments", lambda *args: {})
    monkeypatch.setattr(computer_use, "RESUME_QUIET_SECONDS", 0.05)
    c = ComputerController(
        Desktop(), authority=ActionAuthority(tmp_path / "authority.sqlite", publish_events=False)
    )
    c.desktop.env = {"DISPLAY": ":0"}
    yield c
    c.close()


class Browser:
    """A stand-in for her Edge processes, with or without the automation port."""

    def __init__(self, port):
        self.info = {"cmdline": ["msedge", "--user-data-dir=/profile"]
                     + (["--remote-debugging-port=0"] if port else [])}

    def terminate(self):
        pass


@pytest.fixture
def desk(tmp_path, monkeypatch):
    runtime = IsolatedDesktop(tmp_path / "isolated", host_env={"XAUTHORITY": str(tmp_path / "xa")})
    runtime._prepare()
    runtime._write({"display": ":70", "number": 70, "size": "1920x1080", "started_at": 0})
    runtime.running = runtime._read
    runtime.shell_env = lambda info=None: {"DISPLAY": ":70"}
    runtime.viewer_window = lambda: None
    runtime.browsers = [Browser(port=True)]
    runtime.calls = []

    def stop():
        runtime.calls.append(("stop",))
        runtime.browsers.clear()

    def spawn(info, env, extra, *, debuggable=True):
        runtime.calls.append(("spawn", tuple(extra), debuggable))
        runtime.browsers.append(Browser(port=debuggable))

    runtime._browser_processes = lambda: list(runtime.browsers)
    runtime._stop_browser = stop
    runtime._spawn_browser = spawn
    monkeypatch.setattr(
        "core.computer_browser._active_port",
        lambda path: 9555 if any("--remote-debugging-port=0" in b.info["cmdline"] for b in runtime.browsers)
        else None,
    )
    monkeypatch.setattr("core.computer_browser._verify_profile_listener", lambda port, profile: None)
    return runtime


def test_sign_in_mode_drops_the_port_and_nothing_restarts_it_under_him(desk):
    assert desk.ensure_debuggable() == 9555 and desk.calls == []
    assert desk.sign_in()["signing_in"] is True
    assert desk.calls == [("stop",), ("spawn", ("--restore-last-session",), False)]
    desk.calls.clear()
    # Her page and browser steps prepare the port this way; it refuses, never restarts.
    with pytest.raises(ComputerError) as refused:
        desk.ensure_debuggable()
    assert str(refused.value) == SIGNING_IN and desk.calls == []
    # A second sign-in keeps his browser and only opens the page as a tab.
    desk.sign_in("https://accounts.shopify.com/lookup")
    assert desk.calls == [("spawn", ("https://accounts.shopify.com/lookup",), False)]
    desk.calls.clear()
    desk.launch("browser", "https://example.com")
    assert desk.calls == [("spawn", ("https://example.com",), False)]
    desk.calls.clear()
    # Handing back restarts it with the port and his tabs; logins stay in the profile.
    assert desk.signed_in()["signing_in"] is False
    assert desk.calls == [("stop",), ("spawn", ("--restore-last-session",), True)]
    assert desk.ensure_debuggable() == 9555


def test_sign_in_adopts_a_browser_already_running_without_the_port(desk):
    # Like his live Edge today: relaunched by hand without the port, mid sign-in.
    desk.browsers[:] = [Browser(port=False)]
    desk.sign_in()
    assert desk.calls == [] and desk.status()["signing_in"] is True
    # The flag alone can be cleared; the next page step restores the port.
    desk.signed_in(restart=False)
    assert desk.calls == [] and desk.status()["signing_in"] is False
    assert desk.ensure_debuggable() == 9555
    assert desk.calls == [("stop",), ("spawn", ("--restore-last-session",), True)]


def _wait(predicate, seconds=2):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_a_sign_in_handoff_on_her_desktop_switches_modes_until_resume(controller):
    c = controller
    c.desk = "isolated"
    calls = []
    c.sign_in = lambda url=None: calls.append("sign_in")
    c.signed_in = lambda restart=True: calls.append(("signed_in", restart))
    sid, frame = begin(c)
    result = act(c, sid, frame, [{"type": "handoff", "reason": "sign in to Shopify with your email code"}])
    assert result["status"] == "handed_off" and c.session.signing_in
    _wait(lambda: calls == ["sign_in"])
    assert any(event["type"] == "sign_in_mode" for event in c.events)
    c.resume(sid)
    # Resume never waits on a browser restart: only the flag clears.
    assert calls == ["sign_in", ("signed_in", False)] and not c.session.signing_in
    c.stop()
    calls.clear()
    # Other handoffs leave her browser alone.
    sid, frame = begin(c)
    act(c, sid, frame, [{"type": "handoff", "reason": "choose which card design to keep"}],
        request_id="request-0002")
    time.sleep(0.2)
    assert calls == [] and not c.session.signing_in
    c.stop()
    # A task that stops mid sign-in hands the mode back too.
    sid, frame = begin(c)
    act(c, sid, frame, [{"type": "handoff", "reason": "log in with Google"}], request_id="request-0003")
    _wait(lambda: calls == ["sign_in"])
    c.stop()
    assert calls == ["sign_in", ("signed_in", False)]


def test_his_screen_never_switches_her_browser_into_sign_in_mode(controller):
    c = controller
    calls = []
    c.sign_in = lambda url=None: calls.append("sign_in")
    sid, frame = begin(c)
    act(c, sid, frame, [{"type": "handoff", "reason": "sign in to Okta"}])
    time.sleep(0.2)
    assert calls == [] and not c.session.signing_in


class SignInRuntime(Runtime):
    def __init__(self):
        super().__init__()
        self.sign_ins = []

    def sign_in(self, url=None):
        self.sign_ins.append(("sign_in", url))
        return self.status()

    def signed_in(self, restart=True):
        self.sign_ins.append(("signed_in", restart))
        return self.status()


def test_desktop_sign_in_routes_through_the_service_and_wires_the_handoff(controller):
    from core.computer_service import ComputerServer

    runtime = SignInRuntime()
    server = ComputerServer(
        controller,
        isolated=runtime,
        isolated_desktop=lambda env: FakeIsolatedDesktop(),
        clipboard=None,
        isolated_tools=lambda runtime, info: (None, None),
    )
    server.start_isolated_indicator = lambda c: None
    try:
        server.dispatch("desktop", {"action": "sign-in", "url": "https://accounts.google.com/"},
                        operator=True)
        server.dispatch("desktop", {"action": "signed_in"}, operator=True)
        assert runtime.sign_ins == [("sign_in", "https://accounts.google.com/"), ("signed_in", True)]
        assert server.isolated.sign_in == runtime.sign_in
        with pytest.raises(ComputerError, match="one http"):
            server.dispatch("desktop", {"action": "sign_in", "url": "file:///etc/passwd"}, operator=True)
        with pytest.raises(ComputerError, match="operator"):
            server.dispatch("desktop", {"action": "sign_in"}, operator=False)
    finally:
        server.close_isolated()
        server.server_close()


class FakeWeb:
    def __init__(self):
        self.runs = []

    def snapshot(self):
        return {"ok": True, "url": "https://example.com/", "snapshot": '- heading "Example Domain" [ref=e3]'}

    def run(self, steps, cancelled):
        self.runs.append(steps)
        return {"ok": True, "steps": [{"step": 1, "ok": True}], "url": "https://example.com/"}


class FakeTerminal:
    def __init__(self):
        self.commands = []

    def run(self, command, *, timeout=30.0, cancelled=lambda: False):
        self.commands.append(command)
        return {"ok": True, "output": "hello", "exit_code": 0}

    def send(self, text, *, enter=True):
        return {"ok": True}

    def read(self):
        return {"ok": True, "screen": "$ echo hello\nhello"}


def test_a_chat_driving_her_desktop_gets_page_browser_and_shell(controller, monkeypatch):
    from core import computer_mcp
    from core.computer_service import ComputerServer

    controller.desk = "isolated"
    controller.web, controller.terminal = FakeWeb(), FakeTerminal()
    server = ComputerServer(controller)

    class Client:
        def ensure_running(self):
            return server.dispatch("status", {}, operator=False)

        def call(self, method, **params):
            return server.dispatch(method, params, operator=method in {"begin", "run"})

    monkeypatch.setattr(computer_mcp, "ComputerClient", Client)

    async def scenario():
        names = {tool.name for tool in await computer_mcp.mcp.list_tools()}
        assert {"computer_page", "computer_browser", "computer_shell"} <= names
        started = await computer_mcp.computer_start(
            "read example.com in your browser", mode="control", target="display:left", background=False
        )
        sid = started["session"]["id"]
        page = await computer_mcp.computer_page(sid)
        assert "Example Domain" in page["snapshot"]
        done = await computer_mcp.computer_browser(
            sid, [{"goto": "https://example.com"}], "mcp-browse-0001", "open example.com"
        )
        assert done["ok"] and controller.web.runs == [[{"goto": "https://example.com"}]]
        again = await computer_mcp.computer_browser(
            sid, [{"goto": "https://example.com"}], "mcp-browse-0001", "open example.com"
        )
        assert again["replayed"] and len(controller.web.runs) == 1
        ran = await computer_mcp.computer_shell(
            sid, request_id="mcp-shell-0001", intent="say hello", command="echo hello"
        )
        assert ran["ok"] and ran["output"] == "hello" and controller.terminal.commands == ["echo hello"]
        screen = await computer_mcp.computer_shell(sid, read=True)
        assert "hello" in screen["screen"]
        await computer_mcp.computer_stop()

    try:
        asyncio.run(scenario())
    finally:
        server.server_close()
