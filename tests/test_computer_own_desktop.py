"""Pause instead of kill, handoff, and Serena's own desktop beside his screen."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from PIL import Image
from test_computer_use import Desktop, act, begin

from core import computer_use
from core.action_authority import ActionAuthority
from core.computer_platform import ComputerError, ComputerPaused
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


def settle(c, timeout=2):
    deadline = time.monotonic() + timeout
    while not c.settle_resume():
        assert time.monotonic() < deadline, "resume never settled"
        time.sleep(0.01)


def test_takeover_pauses_and_resume_waits_for_quiet_hands(controller):
    c = controller
    sid, frame = begin(c)
    c.physical_input()
    assert c.session.state == "paused" and c.session.pauses == 1
    assert c.status()["session"]["paused_reason"] == "you took over with the mouse or keyboard"
    with pytest.raises(ComputerPaused):
        act(c, sid, frame)
    c.resume(sid)
    assert c.session.state == "resuming"
    # The click on resume is itself input: keep holding while his hands move.
    c.physical_input()
    assert not c.settle_resume() and c.session.state == "resuming"
    settle(c)
    assert c.session.state == "active" and not c.session.input_hold.is_set()
    fresh = c.observe(sid)
    assert act(c, sid, fresh, request_id="after-resume-1")["ok"]
    kinds = [event["type"] for event in c.events]
    assert kinds.index("paused") < kinds.index("resuming") < kinds.index("resumed")


def test_watch_ignores_physical_input_and_stopped_sessions_cannot_resume(controller):
    c = controller
    sid, _ = begin(c, mode="watch")
    c.physical_input()
    assert c.session.state == "active"
    c.stop()
    with pytest.raises(ComputerError, match="stopped"):
        c.resume(sid)


def test_handoff_pauses_for_raghav_without_a_stale_frame(controller):
    c = controller
    sid, frame = begin(c)
    result = act(
        c,
        sid,
        frame,
        [
            {"type": "click", "x": 400, "y": 300},
            {"type": "handoff", "reason": "type your okta password"},
        ],
    )
    assert result["ok"] and result["status"] == "handed_off" and "frame" not in result
    assert c.session.state == "paused" and c.session.paused_by_agent
    assert c.session.paused_reason == "serena needs you: type your okta password"
    with pytest.raises(ComputerError, match="last action"):
        c._validate_actions(
            [{"type": "handoff", "reason": "x"}, {"type": "click", "x": 1, "y": 1}], frame, []
        )


def test_launch_is_only_for_her_own_desktop_and_validates_urls(controller):
    c = controller
    sid, frame = begin(c)
    with pytest.raises(ComputerError, match="own desktop"):
        act(c, sid, frame, [{"type": "launch", "app": "browser"}])
    launched = []
    c.launcher = lambda app, url=None: launched.append((app, url))
    for bad in (
        {"type": "launch", "app": "shell"},
        {"type": "launch", "app": "browser", "url": "file:///etc/passwd"},
        {"type": "launch", "app": "terminal", "url": "https://example.com"},
    ):
        with pytest.raises(ComputerError, match="launch"):
            act(c, sid, frame, [bad], request_id="bad-launch-1")
    result = act(
        c, sid, frame, [{"type": "launch", "app": "browser", "url": "https://example.com"}],
        request_id="launch-browser-1",
    )
    assert result["ok"] and launched == [("browser", "https://example.com")]


def test_receipts_separate_decision_input_and_settle_time(controller):
    c = controller
    sid, frame = begin(c)
    time.sleep(0.05)
    timing = act(c, sid, frame)["timing"]
    assert timing["decision_ms"] >= 40
    assert {"preflight_ms", "input_ms", "settle_ms", "capture_ms"} <= set(timing)
    assert c.status()["session"]["last_timing"] == timing


def test_post_action_frame_returns_once_the_ui_has_painted_and_settled(controller):
    c = controller
    page = ["white"]
    c.desktop.capture = lambda rect: Image.new("RGB", (rect.width, rect.height), page[0])
    real_button = c.desktop.button

    def click(key, down):
        real_button(key, down)
        if not down:
            page[0] = "gray"

    c.desktop.button = click
    sid, frame = begin(c)
    result = act(c, sid, frame)
    assert result["timing"]["settle_ms"] < computer_use.POST_ACTION_QUIET_SECONDS * 1000
    with Image.open(__import__("io").BytesIO(__import__("base64").b64decode(result["frame"]["data"]))) as image:
        assert image.getpixel((5, 5))[0] < 200  # the gray page, not the pre-click white one
    # Nothing visible happened: wait out the quiet window, never the full cap.
    quiet = act(c, sid, result["frame"], request_id="request-0002")
    assert (
        computer_use.POST_ACTION_QUIET_SECONDS * 1000
        <= quiet["timing"]["settle_ms"]
        < computer_use.POST_ACTION_SETTLE_SECONDS * 1000
    )


class Runtime:
    def __init__(self):
        self.alive = False
        self.launched = []

    def viewer_window(self):
        return "4242"

    def running(self):
        return {"display": ":70"} if self.alive else None

    def start(self):
        self.alive = True
        return {"display": ":70"}

    def env(self, info=None):
        return {"DISPLAY": ":70"}

    def launch(self, app, url=None):
        self.launched.append((app, url))
        return {"ok": True, "app": app}

    def status(self):
        return {"running": self.alive}

    def stop(self):
        self.alive = False
        return self.status()


class IsolatedDesktop(Desktop):
    name = "x11"
    env = {"DISPLAY": ":70"}

    def __init__(self):
        super().__init__()
        self.monitor_callbacks = None
        self.closed = False

    def start_input_monitor(self, on_input, on_stop, *, motion=True, shortcut=True):
        assert motion is False  # his pointer crossing the viewer is not a takeover
        assert shortcut is False  # the host grab covers her desktop
        self.monitor_callbacks = (on_input, on_stop)

    def close(self):
        self.closed = True


@pytest.fixture
def server(controller):
    from core.computer_service import ComputerServer

    runtime = Runtime()
    desktops = []

    def make(env):
        assert env["DISPLAY"] == ":70"
        desktops.append(IsolatedDesktop())
        return desktops[-1]

    bridges = []

    class Bridge:
        def __init__(self, host, nested, viewer):
            self.args, self.stopped = (host, nested, viewer), False
            bridges.append(self)

        def stop(self):
            self.stopped = True

    server = ComputerServer(controller, isolated=runtime, isolated_desktop=make, clipboard=Bridge)
    server.start_isolated_indicator = lambda c: None
    server.runtime, server.desktops, server.bridges = runtime, desktops, bridges
    yield server
    server.close_isolated()
    server.server_close()


def start(server, mode, target, request):
    return server.dispatch(
        "begin",
        {"request": request, "mode": mode, "target": target, "interactive": True},
        operator=True,
    )["session"]


def test_her_desktop_runs_beside_his_watch_and_routes_by_session(server, controller):
    host = start(server, "watch", "display:left", "coach me through aws")
    mine = start(server, "control", "isolated", "log into the shopify cli")
    assert mine["desk"] == "isolated" and mine["target"] == "desktop"
    assert server.isolated.launcher == server.runtime.launch
    status = server.dispatch("status", {}, operator=False)
    assert {item["id"] for item in status["sessions"]} == {host["id"], mine["id"]}
    assert status["isolated_desktop"] == {"running": True}
    frame = server.dispatch("observe", {"session_id": mine["id"]}, operator=False)
    result = server.dispatch(
        "act",
        {
            "session_id": mine["id"],
            "frame_id": frame["frame_id"],
            "request_id": "isolated-click-1",
            "intent": "click in her own browser",
            "actions": [{"type": "click", "x": 20, "y": 20}],
        },
        operator=False,
    )
    assert result["ok"]
    assert server.desktops[0].inputs and not controller.desktop.inputs
    # A click inside her viewer pauses only her task.
    on_input, _ = server.desktops[0].monitor_callbacks
    on_input()
    assert server.isolated.session.state == "paused"
    assert controller.session.state == "active"
    server.dispatch("resume", {}, operator=True)
    settle(server.isolated)
    assert server.isolated.session.state == "active"
    server.dispatch("stop", {"session_id": mine["id"]}, operator=False)
    assert server.isolated.session.state == "stopped" and controller.session.state == "active"
    with pytest.raises(ComputerError, match="operator"):
        server.dispatch("resume", {}, operator=False)


def test_closing_her_viewer_stops_her_task_and_close_releases_it(server, controller):
    start(server, "control", "isolated", "fill the form in your browser")
    isolated = server.isolated
    server.runtime.alive = False
    worker = threading.Thread(target=server.supervise, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while not server.desktops[0].closed and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.isolated is None
        assert isolated.session.reason == "serena's desktop was closed"
        assert server.desktops[0].closed
    finally:
        controller.shutdown.set()
        worker.join(timeout=2)
    controller.shutdown.clear()
    server.dispatch("desktop", {"action": "open"}, operator=True)
    assert server.isolated is not None and server.runtime.alive
    assert server.dispatch("desktop", {"action": "close"}, operator=True) == {"running": False}
    assert server.isolated is None
    # A clipboard bridge lives exactly as long as her controller.
    assert len(server.bridges) == 2 and all(bridge.stopped for bridge in server.bridges)
    host, nested, viewer = server.bridges[-1].args
    assert nested == ":70" and viewer() == "4242"


def test_mcp_control_defaults_to_her_own_desktop(controller, monkeypatch):
    from core import computer_mcp

    calls = []

    class Client:
        def ensure_running(self):
            return {}

        def call(self, method, **params):
            calls.append((method, params))
            return {"session": {"id": "x"}}

    monkeypatch.setattr(computer_mcp, "ComputerClient", Client)
    asyncio.run(computer_mcp.computer_start("log into the cli for me", mode="control"))
    asyncio.run(computer_mcp.computer_start("watch my screen"))
    assert calls[0][1]["target"] == "isolated"
    assert calls[1][1]["target"] == "desktop"


def test_control_worker_continues_the_same_task_after_takeover(controller, monkeypatch):
    from core import computer_agent

    monkeypatch.setattr(computer_agent, "build_task_pack", lambda request: "")
    prompts = []
    interrupted = threading.Event()

    class Model:
        def __init__(self, **kwargs):
            assert kwargs["turn_timeout"] == computer_use.MAX_SESSION_SECONDS
            self.active_turn_id = None
            self.stop_turn = None

        async def start(self):
            pass

        async def turn(self, message, **kwargs):
            prompts.append(message)
            self.active_turn_id = f"turn-{len(prompts)}"
            try:
                if len(prompts) == 1:
                    self.stop_turn = asyncio.Event()
                    controller.physical_input()  # he grabs the mouse mid-task
                    await self.stop_turn.wait()
                    interrupted.set()
                    raise RuntimeError("turn interrupted")
                return {"text": "logged in and verified", "tool_calls": []}
            finally:
                self.active_turn_id = None

        async def interrupt(self):
            self.stop_turn.set()

        async def close(self):
            pass

    begin(controller)
    controller.session.driver = "astra"
    agent = computer_agent.ComputerAgent(controller, client_factory=Model)
    controller.agent = agent
    agent.start()
    try:
        assert interrupted.wait(5)
        assert controller.session.state == "paused"
        controller.resume()
        settle(controller)
        agent.thread.join(timeout=5)
        assert not agent.thread.is_alive()
        assert controller.session.state == "stopped"
        assert controller.session.reason == "visual task finished"
        assert len(prompts) == 2 and "handed control back" in prompts[1]
        assert controller.session.request in prompts[1]
        assert controller.session.observation == "logged in and verified"
    finally:
        controller.stop()
        agent.thread.join(timeout=3)


def test_clipboard_bridge_only_takes_her_copies_from_the_viewer():
    from core.computer_clipboard import ClipboardBridge

    class Side:
        def __init__(self):
            self.offers, self.cleared = [], 0

        def offer(self, data):
            self.offers.append(data)

        def clear(self):
            self.cleared += 1

    bridge = ClipboardBridge(":0", ":70", viewer=lambda: "7")
    bridge.host, bridge.nested = Side(), Side()
    focused = {"value": False}
    bridge._viewer_focused = lambda: focused["value"]
    bridge._host_copied(b"his password")
    assert bridge.nested.offers == [b"his password"]
    bridge._nested_copied(b"astra pressed ctrl+c")
    assert bridge.host.offers == []  # never clobbers what he copied
    focused["value"] = True
    bridge._nested_copied(b"he copied in the viewer")
    assert bridge.host.offers == [b"he copied in the viewer"]
    bridge._host_cleared()
    assert bridge.nested.cleared == 1  # a manager's timeout clears hers too
