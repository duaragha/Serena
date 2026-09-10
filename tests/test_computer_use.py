from __future__ import annotations

import asyncio
import threading
import time

import pytest
from PIL import Image

from core.action_authority import ActionAuthority
from core.computer_platform import ComputerError
from core.computer_use import ComputerController, frame_content


class Desktop:
    name = "fixture"

    def __init__(self):
        self.focus = "123"
        self.locked_value = False
        self.windows = []
        self.inputs = []
        self.width = 2000
        self.releases = 0

    def monitors(self):
        return [
            {"name": "left", "rect": {"x": -2000, "y": 0, "width": self.width, "height": 1200}},
            {"name": "right", "rect": {"x": 0, "y": 0, "width": 2000, "height": 1200}},
        ]

    def active_window(self):
        return self.focus

    def window_info(self, identifier):
        return {
            "id": identifier,
            "app": "fixture",
            "title": "test",
            "visible": True,
            "rect": {"x": -1800, "y": 100, "width": 1000, "height": 800},
        }

    def context(self):
        return self.window_info(self.focus)

    def visible_windows(self):
        return self.windows

    def locked(self):
        return self.locked_value

    def capture(self, rect):
        return Image.new("RGB", (rect.width, rect.height), "white")

    def release(self):
        self.releases += 1

    def close(self):
        pass

    def move(self, x, y):
        self.inputs.append(("move", x, y))

    def button(self, key, down):
        self.inputs.append(("button", key, down))

    def key(self, key, down):
        self.inputs.append(("key", key, down))

    def type_text(self, text, cancelled):
        assert not cancelled()
        self.inputs.append(("type", text))


@pytest.fixture
def controller(tmp_path, monkeypatch):
    # Transport tests use a synthetic chat; never bind them to the real test runner.
    monkeypatch.setattr("core.computer_mcp.origin_arguments", lambda *args: {})
    controller = ComputerController(
        Desktop(), authority=ActionAuthority(tmp_path / "authority.sqlite", publish_events=False)
    )
    yield controller
    controller.close()


def begin(c, mode="control", target="display:left", **kwargs):
    c.begin(
        mode=mode,
        target=target,
        request="click inside the test fixture",
        operator_confirmed=True,
        **kwargs,
    )
    return c.session.id, c.observe(c.session.id)


def act(c, sid, frame, actions=None, request_id="request-0001"):
    return c.act(
        sid,
        frame["frame_id"],
        actions or [{"type": "click", "x": 400, "y": 300}],
        request_id=request_id,
        intent="click the fixture",
    )


def test_coordinate_mapping_and_retries_do_not_repeat_input(controller):
    c = controller
    sid, frame = begin(c)
    result = act(c, sid, frame)
    assert result["ok"] and result["frame"]["frame_id"] != frame["frame_id"]
    assert c.desktop.inputs[0] == ("move", -1583, 312)
    inputs = list(c.desktop.inputs)
    replay = act(c, sid, frame)
    assert replay["replayed"] and c.desktop.inputs == inputs
    assert "frame" not in replay  # receipts never retain raw image data
    with pytest.raises(ComputerError, match="reused"):
        act(c, sid, frame, [{"type": "click", "x": 40, "y": 30}])
    assert all("data" not in str(event) for event in c.events)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True, 1920, "100"])
def test_invalid_coordinates_never_dispatch(controller, bad):
    sid, frame = begin(controller)
    with pytest.raises(ComputerError):
        act(controller, sid, frame, [{"type": "click", "x": bad, "y": 20}])
    assert not controller.desktop.inputs


def test_invalid_later_action_is_rejected_before_first_action(controller):
    sid, frame = begin(controller)
    with pytest.raises(ComputerError):
        act(
            controller,
            sid,
            frame,
            [{"type": "click", "x": 20, "y": 20}, {"type": "shell", "text": "bad"}],
        )
    assert not controller.desktop.inputs


def test_session_scope_expiry_foreground_and_geometry(controller):
    c = controller
    sid, frame = begin(c)
    with pytest.raises(ComputerError, match="already owns"):
        begin(c)
    c.desktop.focus = "999"
    with pytest.raises(ComputerError, match="foreground changed"):
        act(c, sid, frame)
    c.desktop.focus = "123"
    c.desktop.width = 1999
    with pytest.raises(ComputerError, match="geometry changed"):
        act(c, sid, frame)
    c.desktop.width = 2000
    frame["expires_at"] = time.time() - 1
    with pytest.raises(ComputerError, match="expired"):
        act(c, sid, frame)
    assert not c.desktop.inputs


def test_watch_and_operator_scope(controller):
    c = controller
    with pytest.raises(ComputerError, match="scoped session"):
        c.begin(mode="control", target="desktop", request="do something")
    sid, frame = begin(c, mode="watch")
    with pytest.raises(ComputerError, match="cannot send input"):
        act(c, sid, frame)
    c.physical_input()
    assert c.session.state == "active"


def test_stop_during_batch_cancels_wait_and_releases_input(controller):
    c = controller
    sid, frame = begin(c)
    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            act(
                c, sid, frame, [{"type": "wait", "seconds": 3}, {"type": "click", "x": 40, "y": 40}]
            )
        )
    )
    worker.start()
    deadline = time.monotonic() + 2
    while not c.session.results and time.monotonic() < deadline:
        time.sleep(0.005)
    started = time.monotonic()
    c.physical_input()
    worker.join(timeout=1)
    assert not worker.is_alive() and time.monotonic() - started < 1
    assert not result[0]["ok"] and not c.desktop.inputs
    assert not c.session.frames and c.desktop.releases
    with pytest.raises(ComputerError, match="stopped"):
        c.observe(sid)


def test_private_nonfocused_overlap_is_not_captured(controller):
    c = controller
    sid, _ = begin(c)
    c.desktop.windows = [
        {
            "app": "Bitwarden",
            "title": "vault",
            "rect": {"x": -1000, "y": 20, "width": 400, "height": 600},
        }
    ]
    with pytest.raises(ComputerError, match="private application"):
        c.observe(sid)
    c.desktop.windows[0]["rect"]["x"] = 1000  # outside the authorized display
    assert c.observe(sid)["data"]


def test_locked_desktop_and_expired_session_stop_capture(controller):
    c = controller
    sid, _ = begin(c)
    c.desktop.locked_value = True
    with pytest.raises(ComputerError, match="locked"):
        c.observe(sid)
    assert c.session.state == "stopped"
    c.desktop.locked_value = False
    sid, _ = begin(c)
    c.session.expires_at = time.time() - 1
    with pytest.raises(ComputerError, match="expired"):
        c.observe(sid)


def test_mixed_mcp_image_and_dynamic_transport(controller):
    from core.codex_brain_tools import _handler_result
    from core.computer_tools import visual_tools

    sid, frame = begin(controller, mode="watch")
    result = _handler_result(frame_content(frame))
    assert [item["type"] for item in result["contentItems"]] == ["inputText", "inputImage"]
    assert [item.name for item in visual_tools(controller, sid)] == ["observe"]


def test_resident_tool_cannot_invent_permission(monkeypatch):
    from core import brain_computer_tools as module

    monkeypatch.setattr(module, "current_turn", lambda: {"text": "hello"})
    result = asyncio.run(module.computer_session.handler({"operation": "run"}))
    assert "did not request" in result["content"][0]["text"]
    monkeypatch.setattr(module, "current_turn", lambda: {"text": "watch this window"})
    result = asyncio.run(module.computer_session.handler({"operation": "run"}))
    assert "observation only" in result["content"][0]["text"]
    monkeypatch.setattr(
        module, "current_turn", lambda: {"text": "do not click anything on my screen"}
    )
    result = asyncio.run(module.computer_session.handler({"operation": "run"}))
    assert "asked not to start" in result["content"][0]["text"]
    monkeypatch.setattr(module, "current_turn", lambda: {"text": "don't look at my screen"})
    result = asyncio.run(module.computer_session.handler({"operation": "watch"}))
    assert "asked not to start" in result["content"][0]["text"]


def test_gideon_capture_consumes_real_turn_proof(controller):
    from core.computer_visual import ComputerVisualAdapter
    from core.visual_context import CAPTURE_SCOPE, CaptureConsent, ConsentRequired

    class Client:
        def ensure_running(self):
            return controller.status()

        def call(self, method, **params):
            if method == "begin":
                assert params.pop("interactive") is True
                params["operator_confirmed"] = True
            return getattr(controller, method)(**params)

    authority = controller.authority
    proof = authority.issue_turn_proof(
        source="chat",
        covers=[("screen.capture", "active_screen")],
        session_id="resident-1",
        turn_id="turn-1",
    )
    now = time.time()
    consent = CaptureConsent(
        "capture-1", "raghav", "chat", "resident-1", (CAPTURE_SCOPE,), now, now + 60, proof.proof_id
    )
    adapter = ComputerVisualAdapter(authority, client=Client())
    snapshot = adapter.capture(consent)
    assert snapshot.image.data and snapshot.provenance.screenshot_adapter == "serena-computer"
    assert controller.session.state == "stopped"
    with pytest.raises(ConsentRequired, match="already used"):
        adapter.capture(consent)


def test_service_auth_rejects_browser_origin_and_unprivileged_lease(controller):
    import json
    import urllib.error
    import urllib.request

    from core.computer_service import ComputerServer

    server = ComputerServer(controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:

        def call(token, method, *, origin=None):
            headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
            if origin:
                headers["Origin"] = origin
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/rpc",
                data=json.dumps({"method": method}).encode(),
                headers=headers,
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                return json.load(response)

        assert call(server.token, "status")["ok"]
        assert not call(server.token, "begin")["ok"]
        for token, origin in [("wrong", None), (server.token, "http://malicious.test")]:
            with pytest.raises(urllib.error.HTTPError) as error:
                call(token, "status", origin=origin)
            assert error.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_mcp_chat_can_start_observe_and_stop_requested_watch(controller, monkeypatch):
    from core import computer_mcp
    from core.computer_service import ComputerServer

    server = ComputerServer(controller)

    class Client:
        def ensure_running(self):
            return server.dispatch("status", {}, operator=False)

        def call(self, method, **params):
            return server.dispatch(method, params, operator=method in {"begin", "run"})

    monkeypatch.setattr(computer_mcp, "ComputerClient", Client)

    async def scenario():
        tools = await computer_mcp.mcp.list_tools()
        assert "computer_start" in {tool.name for tool in tools}
        status = await computer_mcp.computer_status()
        assert status["session"] is None
        assert "No manual user terminal step" in status["session_start"]["guidance"]
        result = await computer_mcp.computer_start(
            "watch my left screen and guide me", target="display:left", seconds=30, background=False
        )
        sid = result["session"]["id"]
        assert result["session"]["mode"] == "watch"
        assert result["session"]["owner"] == "mcp"
        assert result["driver"]["kind"] == "connected_chat"
        assert controller.agent is None
        observed = await computer_mcp.computer_observe(sid)
        assert [item.type for item in observed.content] == ["text", "image"]
        with pytest.raises(ComputerError, match="already owns"):
            await computer_mcp.computer_start("watch this screen", target="display:left")
        await computer_mcp.computer_stop()
        assert controller.session.state == "stopped"

    try:
        asyncio.run(scenario())
    finally:
        server.server_close()


@pytest.mark.parametrize("mode", ["watch", "control"])
def test_mcp_background_start_uses_existing_astra_runner(controller, monkeypatch, mode):
    from core import computer_agent, computer_mcp
    from core.computer_service import ComputerServer

    server = ComputerServer(controller)
    started = []

    class Agent:
        thread = None

        def __init__(self, owner, *, speak):
            self.session = owner.session
            self.speak = speak

        def start(self):
            started.append((self.session.mode, self.speak))

        def cancel(self):
            pass

    class Client:
        def ensure_running(self):
            return server.dispatch("status", {}, operator=False)

        def call(self, method, **params):
            return server.dispatch(method, params, operator=True)

    monkeypatch.setattr(computer_mcp, "ComputerClient", Client)
    monkeypatch.setattr(computer_agent, "ComputerAgent", Agent)
    try:
        result = asyncio.run(
            computer_mcp.computer_start(
                "help me with this desktop task",
                mode=mode,
                target="display:left",
                background=True,
                speak=True,
            )
        )
        assert started == [(mode, True)]
        assert result["driver"] == {
            "kind": "astra",
            "model": "gpt-6-astra",
            "effort": "medium",
            "service_tier": "fast",
        }
    finally:
        server.server_close()


@pytest.mark.parametrize(
    "args",
    [
        {"request": ""},
        {"request": "x" * 4001},
        {"mode": "invalid"},
        {"seconds": 0},
        {"seconds": 1801},
        {"seconds": True},
        {"speak": True, "background": False},
    ],
)
def test_mcp_start_rejects_invalid_requests_before_launch(monkeypatch, args):
    from core import computer_mcp

    def unexpected_client():
        pytest.fail("invalid request must not start the helper or a session")

    monkeypatch.setattr(computer_mcp, "ComputerClient", unexpected_client)
    with pytest.raises(ComputerError):
        asyncio.run(computer_mcp.computer_start(**{"request": "watch my screen", **args}))


def test_old_session_stop_cannot_stop_new_session(controller):
    sid, _ = begin(controller)
    controller.stop()
    new_sid, _ = begin(controller)
    controller.stop(session_id=sid)
    assert controller.current(new_sid).state == "active"


def test_resized_indicator_is_fully_masked_without_hiding_extra_desktop(controller):
    import base64
    import io

    from core.computer_service import ComputerServer

    sid, _ = begin(controller, mode="watch")
    server = ComputerServer(controller)
    try:
        for height in (500, 160):
            server.dispatch(
                "indicator",
                {"rect": {"x": -1980, "y": 20, "width": 520, "height": height}},
                operator=False,
            )
            frame = controller.observe(sid, max_width=2560)
            with Image.open(io.BytesIO(base64.b64decode(frame["data"]))) as image:
                assert all(v < 50 for v in image.getpixel((100, height + 10)))
                assert all(v > 240 for v in image.getpixel((100, height + 40)))
        previous = controller.indicator_rect
        with pytest.raises(ComputerError, match="invalid indicator rectangle"):
            server.dispatch(
                "indicator",
                {"rect": {"x": 20, "y": 20, "width": 520, "height": -1}},
                operator=False,
            )
        assert controller.indicator_rect == previous
    finally:
        server.server_close()


def test_modal_dialog_is_in_scope_only_when_owned(controller):
    c = controller
    sid, _ = begin(c, target="window:123")
    c.desktop.focus = "dialog"
    c.desktop.window_in_scope = lambda active, target: active == "dialog" and target == "123"
    assert c.observe(sid)["context"]["id"] == "dialog"
    c.desktop.focus = "unrelated"
    with pytest.raises(ComputerError, match="foreground"):
        c.observe(sid)


def test_display_watch_uses_its_visible_app_when_chat_focus_is_on_other_monitor(controller):
    from core.computer_watch import changed, sample

    controller.desktop.windows = [
        {
            "id": "aws",
            "app": "browser",
            "title": "AWS Console",
            "rect": {"x": 0, "y": 0, "width": 2000, "height": 1100},
        }
    ]
    sid, first = begin(controller, mode="watch", target="display:right")
    assert first["context"]["id"] == "aws"
    controller.desktop.focus = "another-chat-on-left"
    second = controller.observe(sid)
    assert controller.status()["session"]["focused_window"]["title"] == "AWS Console"
    with sample(first) as old, sample(second) as new:
        assert not changed(first, second, old, new)


@pytest.mark.parametrize("entry", ["legacy_begin", "mcp_default"])
def test_watch_start_actually_produces_advice(controller, monkeypatch, tmp_path, entry):
    from test_computer_conversation import write_chat

    from core import computer_agent, computer_mcp
    from core.computer_conversation import ConversationStore
    from core.computer_service import ComputerServer

    store = ConversationStore(tmp_path / "state", home=tmp_path)
    parent, path = write_chat(tmp_path, "codex")
    store.register(parent, "codex", path)
    source = {"source_session_id": parent, "source_agent": "codex"}
    monkeypatch.setattr(computer_mcp, "origin_arguments", lambda *args: source)

    options = []
    model_events = []
    real_agent = computer_agent.ComputerAgent

    monkeypatch.setattr(
        computer_agent,
        "build_task_pack",
        lambda request: "\n--- saved aws runbook ---\nverify the current account before proceeding.",
    )

    class Model:
        active_turn_id = None

        def __init__(self, **kwargs):
            options.append(kwargs)

        async def start(self):
            model_events.append("start")

        async def turn(self, message, **kwargs):
            model_events.append(message)
            assert kwargs["images"][0]["data"]
            assert all(f"prior-message-{i}:" in message for i in range(30))
            assert "saved aws runbook" in message
            return {"text": "open the next setup step", "tool_calls": []}

        async def close(self):
            pass

    monkeypatch.setattr(
        computer_agent,
        "ComputerAgent",
        lambda c, speak: real_agent(c, speak=speak, client_factory=Model),
    )
    server = ComputerServer(controller, conversations=store)

    class Client:
        def ensure_running(self):
            return server.dispatch("status", {}, operator=False)

        def call(self, method, **params):
            return server.dispatch(method, params, operator=True)

    monkeypatch.setattr(computer_mcp, "ComputerClient", Client)
    try:
        if entry == "legacy_begin":
            result = server.dispatch(
                "begin",
                {
                    "request": "watch my screen and guide me",
                    "mode": "watch",
                    "target": "display:left",
                    "seconds": 30,
                    **source,
                },
                operator=True,
            )
        else:
            result = asyncio.run(
                computer_mcp.computer_start(
                    "watch my screen and guide me",
                    target="display:left",
                    seconds=30,
                )
            )
        assert result["session"]["driver"] == "astra"
        assert result["session"]["source_session_id"] == parent
        deadline = time.monotonic() + 3
        while not controller.session.observation and time.monotonic() < deadline:
            time.sleep(0.01)
        assert controller.session.observation == "open the next setup step"
        assert controller.session.last_inspected_at is not None
        assert options[0]["model"] == "gpt-6-astra" and options[0]["effort"] == "medium"
        assert options[0]["service_tier"] == "fast"
        assert options[0]["allow_user_hooks"] is False
        assert model_events[0] == "start"
        assert isinstance(model_events[1], str)
        assert store.coaching(parent)[0]["text"] == controller.session.observation
    finally:
        controller.stop()
        if controller.agent:
            controller.agent.thread.join(timeout=3)
            assert not controller.agent.thread.is_alive()
        server.server_close()


@pytest.mark.parametrize("params", [{"interactive": True}, {"owner": "resident-capture-123"}])
def test_explicit_sharing_and_legacy_single_capture_do_not_start_worker(
    controller, monkeypatch, params
):
    from core import computer_agent
    from core.computer_service import ComputerServer

    def unexpected(*args, **kwargs):
        pytest.fail("a sharing-only capture must not launch a model")

    monkeypatch.setattr(computer_agent, "ComputerAgent", unexpected)
    server = ComputerServer(controller)
    try:
        result = server.dispatch(
            "begin",
            {
                "request": "inspect this screen once",
                "mode": "watch",
                "target": "display:left",
                **params,
            },
            operator=True,
        )
        assert result["session"]["driver"] == "connected_chat"
        assert controller.agent is None
    finally:
        server.server_close()


@pytest.mark.parametrize("failed_interrupt", [False, True])
def test_live_watch_detects_changes_during_reasoning_and_discards_old_answers(
    controller, monkeypatch, failed_interrupt
):
    from core import computer_agent
    from core.computer_service import ComputerServer

    real_agent = computer_agent.ComputerAgent
    page = ["red"]
    controller.desktop.capture = lambda rect: Image.new("RGB", (rect.width, rect.height), page[0])
    second_started = threading.Event()
    interrupted = threading.Event()
    preview_started = threading.Event()
    release_preview = threading.Event()
    resets = []

    class Model:
        active_turn_id = None
        turns = 0

        def __init__(self, **kwargs):
            self.release = None

        async def turn(self, message, *, on_delta, **kwargs):
            self.turns += 1
            turn = self.turns
            self.active_turn_id = str(turn)
            try:
                if turn == 1:
                    assert "Do not reply UNCHANGED for this initial check" in message
                    on_delta("red-page ")
                    on_delta("advice")
                    preview_started.set()
                    while not release_preview.is_set():
                        await asyncio.sleep(0.01)
                if turn == 2:
                    self.release = asyncio.Event()
                    second_started.set()
                    await self.release.wait()
                    on_delta("obsolete green-page advice")
                    return {"text": "obsolete green-page advice"}
                await asyncio.sleep(0.1)
                return {
                    "text": "UNCHANGED"
                    if turn == 4
                    else ("red-page advice" if turn == 1 else "blue-page advice")
                }
            finally:
                self.active_turn_id = None

        async def interrupt(self):
            interrupted.set()
            if failed_interrupt:
                raise RuntimeError("interrupt transport failed")
            self.release.set()

        async def reset_thread(self):
            assert self.active_turn_id is None
            resets.append(True)

        async def close(self):
            assert controller.session.cancelled.is_set(), "warm process closed during recovery"

    monkeypatch.setattr(
        computer_agent,
        "ComputerAgent",
        lambda c, speak: real_agent(c, speak=speak, client_factory=Model),
    )
    server = ComputerServer(controller)

    def until(predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert predicate()

    try:
        server.dispatch(
            "begin",
            {
                "request": "watch this page and guide me",
                "mode": "watch",
                "target": "display:left",
            },
            operator=True,
        )
        until(lambda: preview_started.is_set())
        assert controller.status()["session"]["observation_preview"] == "red-page advice"
        assert controller.session.observation == ""  # Preview precedes completed advice.
        release_preview.set()
        until(lambda: controller.session.observation == "red-page advice")
        assert controller.session.observation_preview == ""
        assert any(e["type"] == "inspection_completed" for e in controller.events)
        page[0] = "green"
        until(lambda: second_started.is_set())
        assert controller.session.observation == ""
        changed_at = time.monotonic()
        page[0] = "blue"
        until(lambda: interrupted.is_set(), timeout=1)
        assert time.monotonic() - changed_at < 1
        until(lambda: controller.session.observation == "blue-page advice")
        assert not any("obsolete" in e.get("text", "") for e in controller.events)
        assert any(e["type"] == "superseded" for e in controller.events)
        assert bool(resets) == failed_interrupt
        page[0] = "gold"
        until(
            lambda: any(
                e["type"] == "inspection_completed" and e["unchanged"] for e in controller.events
            )
        )
        assert controller.session.observation == "blue-page advice"
    finally:
        release_preview.set()
        controller.stop()
        if controller.agent:
            controller.agent.thread.join(timeout=3)
            assert not controller.agent.thread.is_alive()
        server.server_close()


def test_watch_capture_retries_a_disappearing_foreground_without_losing_scope(controller):
    from core.computer_platform import ComputerTransientError

    context = controller.desktop.context
    calls = []

    def changed_once():
        calls.append(True)
        if len(calls) == 1:
            raise ComputerTransientError("foreground changed")
        return context()

    controller.desktop.context = changed_once
    sid, frame = begin(controller, mode="watch")
    assert frame["session_id"] == sid and controller.current(sid).state == "active"


def test_watch_changes_ignore_overlay_resize_and_caret_but_detect_page_content():
    from PIL import ImageDraw

    from core.computer_watch import changed

    previous = {
        "rect": {"x": -320, "y": 0, "width": 320, "height": 180},
        "context": {"id": "browser", "title": "setup"},
        "indicator_rect": {"x": -315, "y": 5, "width": 90, "height": 25},
    }
    current = {
        **previous,
        "indicator_rect": {"x": -315, "y": 5, "width": 130, "height": 45},
    }
    with Image.new("RGB", (320, 180), "black") as old:
        with old.copy() as new:
            draw = ImageDraw.Draw(new)
            draw.rectangle((5, 5, 135, 50), fill="white")  # Popup resized.
            draw.line((250, 100, 250, 110), fill="white")  # Blinking caret.
            assert not changed(previous, current, old, new)
            draw.rectangle((240, 110, 252, 122), fill="white")  # Small animated badge.
            assert not changed(previous, current, old, new)
            draw.rectangle((160, 90, 180, 110), fill="white")  # Page content.
            assert changed(previous, current, old, new)
        with Image.new("RGB", (320, 180), (3, 3, 3)) as noise:
            assert not changed(previous, previous, old, noise)
        title_change = {**previous, "context": {"id": "browser", "title": "next step"}}
        assert changed(previous, title_change, old, old)
        focus_change = {**previous, "context": {"id": "another-browser", "title": "setup"}}
        assert changed(previous, focus_change, old, old)


def test_supervisor_allows_first_indicator_handshake_but_stops_lost_heartbeat(controller):
    from types import SimpleNamespace

    from core.computer_service import ComputerServer

    server = ComputerServer(controller)
    server.indicator_process = SimpleNamespace(poll=lambda: None)
    begin(controller, mode="watch")
    worker = threading.Thread(target=server.supervise, daemon=True)
    worker.start()
    try:
        # begin already requires the first visible acknowledgement before capture.
        # The supervisor must not mistake its initial zero timestamp for a loss.
        time.sleep(0.15)
        assert controller.session.state == "active"
        server.indicator_seen = time.monotonic() - 4
        deadline = time.monotonic() + 1
        while controller.session.state == "active" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert controller.session.reason == "visible indicator disconnected"
    finally:
        controller.shutdown.set()
        worker.join(timeout=2)
        server.server_close()
