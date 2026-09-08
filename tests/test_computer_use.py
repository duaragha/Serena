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
def controller(tmp_path):
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


def test_gideon_capture_consumes_real_turn_proof(controller):
    from core.computer_visual import ComputerVisualAdapter
    from core.visual_context import CAPTURE_SCOPE, CaptureConsent, ConsentRequired

    class Client:
        def ensure_running(self):
            return controller.status()

        def call(self, method, **params):
            if method == "begin":
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


def test_old_session_stop_cannot_stop_new_session(controller):
    sid, _ = begin(controller)
    controller.stop()
    new_sid, _ = begin(controller)
    controller.stop(session_id=sid)
    assert controller.current(new_sid).state == "active"


def test_modal_dialog_is_in_scope_only_when_owned(controller):
    c = controller
    sid, _ = begin(c, target="window:123")
    c.desktop.focus = "dialog"
    c.desktop.window_in_scope = lambda active, target: active == "dialog" and target == "123"
    assert c.observe(sid)["context"]["id"] == "dialog"
    c.desktop.focus = "unrelated"
    with pytest.raises(ComputerError, match="foreground"):
        c.observe(sid)
