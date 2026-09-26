"""Her work in his real apps through accessibility, beside him rather than instead of him."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from test_computer_use import Desktop, act, begin

from core import computer_use
from core.action_authority import ActionAuthority
from core.computer_apps import AppsError, HisApps, validate_steps
from core.computer_platform import ComputerError
from core.computer_use import ComputerController

FIXTURE = Path(__file__).parent / "fixtures" / "atspi_app.py"


class FakeApps:
    def __init__(self):
        self.runs = []
        self.scope = None

    def list(self):
        return {"ok": True, "windows": [{"window": "w1", "app": "gedit", "title": "notes"}]}

    def snapshot(self, window):
        return {"ok": True, "window": window, "snapshot": '- push button "Save" [ref=a1]'}

    def run(self, window, steps, *, cancelled=lambda: False):
        self.runs.append((window, steps))
        return {"ok": True, "steps": [{"step": 1, "ok": True, "did": "click"}], "snapshot": ""}


@pytest.fixture
def controller(tmp_path, monkeypatch):
    monkeypatch.setattr("core.computer_mcp.origin_arguments", lambda *args: {})
    monkeypatch.setattr(computer_use, "HANDS_QUIET_SECONDS", 0.2)
    monkeypatch.setattr(computer_use, "HANDS_WAIT_SECONDS", 0.6)
    c = ComputerController(
        Desktop(), authority=ActionAuthority(tmp_path / "authority.sqlite", publish_events=False)
    )
    c.desktop.env = {"DISPLAY": ":0"}
    c.apps = FakeApps()
    yield c
    c.close()


@pytest.mark.parametrize(
    "steps",
    [
        [],
        [{"press": {"ref": "12"}}],
        [{"press": {"role": "button"}, "set_text": {"name": "x"}}],
        [{"set_text": {"name": "Name"}}],
        [{"select": {"role": "combobox"}}],
        [{"menu": []}],
        [{"wait_for": {"target": {"name": "a"}, "gone": {"name": "b"}}}],
        [{"press": {"name": "Save", "selector": "#x"}}],
        [{"press": {"name": "Save"}, "timeout": 99}],
    ],
)
def test_malformed_app_steps_are_rejected_before_anything_runs(steps):
    with pytest.raises(AppsError):
        validate_steps(steps)


def test_beside_him_his_input_pauses_her_only_during_her_pixel_input(controller):
    c = controller
    sid, _frame = begin(c)
    c.physical_input()
    # She is working through accessibility: his typing is not a takeover.
    assert c.session.state == "active"
    result = c.apps_run(
        sid, "notes", [{"press": {"role": "button", "name": "Save"}}],
        request_id="apps-save-0001", intent="save his notes",
    )
    assert result["ok"] and c.apps.runs[-1][0] == "notes"
    c.physical_input()
    assert c.session.state == "active"
    # While her own mouse input shares his desk, his input is a collision.
    c.session.pixel_until = time.monotonic() + 5
    c.physical_input()
    assert c.session.state == "paused"


def test_her_pixel_input_waits_for_his_hands_and_a_frame_taken_after(controller):
    c = controller
    sid, frame = begin(c)
    c.session.last_physical_at = c.clock() + 30  # his hands keep moving
    with pytest.raises(ComputerError, match="using his mouse and keyboard"):
        act(c, sid, frame, request_id="pixel-busy-0001")
    c.session.last_physical_at = c.clock() - 5
    stale = frame
    c.session.last_physical_at = stale["captured_at"] + 0.01
    time.sleep(0.25)
    with pytest.raises(ComputerError, match="observe again"):
        act(c, sid, stale, request_id="pixel-stale-0001")
    fresh = c.observe(sid)
    result = act(c, sid, fresh, request_id="pixel-quiet-0001")
    assert result["ok"]
    # Her input finished moments ago: his next move still pauses her.
    assert c.session.pixel_until > time.monotonic()
    c.physical_input()
    assert c.session.state == "paused"


def test_app_steps_need_control_and_never_repeat_on_retry(controller):
    c = controller
    watch, _frame = begin(c, mode="watch")
    assert c.apps_view(watch)["windows"][0]["window"] == "w1"
    with pytest.raises(ComputerError, match="cannot send input"):
        c.apps_run(watch, "notes", [{"press": {"name": "Save"}}], request_id="apps-watch-0001",
                   intent="save")
    c.stop()
    sid, _frame = begin(c)
    steps = [{"press": {"name": "Save"}}]
    first = c.apps_run(sid, "notes", steps, request_id="apps-once-0001", intent="save")
    again = c.apps_run(sid, "notes", steps, request_id="apps-once-0001", intent="save")
    assert first["ok"] and again["replayed"] and len(c.apps.runs) == 1
    with pytest.raises(ComputerError, match="reused"):
        c.apps_run(sid, "notes", [{"press": {"name": "Open"}}], request_id="apps-once-0001",
                   intent="save")


def test_her_own_desktop_offers_no_app_steps(controller):
    from core.computer_tools import visual_tools

    c = controller
    sid, _frame = begin(c)
    names = {tool.name for tool in visual_tools(c, sid)}
    assert {"apps", "app", "act"} <= names
    c.stop()
    c.desk = "isolated"
    sid, _frame = begin(c)
    assert not {"apps", "app"} & {tool.name for tool in visual_tools(c, sid)}


# -- a real GTK app on a private display ------------------------------------------


def _needs(*names):
    return pytest.mark.skipif(
        not all(shutil.which(name) for name in names) or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
        reason="needs " + ", ".join(names) + " and a session bus with accessibility",
    )


@pytest.fixture
def display(tmp_path):
    xauthority = tmp_path / "xauthority"
    xauthority.touch()
    number = next(n for n in range(140, 199) if not Path(f"/tmp/.X11-unix/X{n}").exists())
    name = f":{number}"
    subprocess.run(
        ["xauth", "-f", str(xauthority), "add", name, "MIT-MAGIC-COOKIE-1", secrets.token_hex(16)],
        check=True,
    )
    env = {k: v for k, v in os.environ.items() if k != "NO_AT_BRIDGE"}
    env.update(DISPLAY=name, XAUTHORITY=str(xauthority))
    processes = [
        subprocess.Popen(
            ["Xvfb", name, "-auth", str(xauthority), "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    ]
    deadline = time.monotonic() + 5
    while not Path(f"/tmp/.X11-unix/X{number}").exists():
        assert time.monotonic() < deadline, "Xvfb did not start"
        time.sleep(0.05)
    processes.append(
        subprocess.Popen(["metacity", "--replace", "--sm-disable"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    )
    time.sleep(0.5)
    yield env, processes
    for process in reversed(processes):
        process.terminate()
    for process in processes:
        process.wait(timeout=5)


def _launch(env, processes, state, *extra):
    processes.append(
        subprocess.Popen(["/usr/bin/python3", str(FIXTURE), str(state), *extra], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    )
    deadline = time.monotonic() + 10
    while not state.exists():
        assert time.monotonic() < deadline, "the test app did not start"
        time.sleep(0.05)


def _x(env, *args):
    return subprocess.run(["xdotool", *args], env=env, capture_output=True, text=True).stdout.strip()


@_needs("Xvfb", "metacity", "xdotool", "xauth", "/usr/bin/python3")
def test_she_works_in_his_app_while_he_types_in_another(display, tmp_path):
    env, processes = display
    app_state, his_state = tmp_path / "app.json", tmp_path / "his.json"
    _launch(env, processes, app_state)
    _launch(env, processes, his_state, "other")
    apps = HisApps(env)
    try:
        deadline = time.monotonic() + 10
        while True:
            titles = {row["title"] for row in apps.list()["windows"]}
            if {"Serena apps test", "His other app"} <= titles:
                break
            assert time.monotonic() < deadline, f"windows never appeared: {titles}"
            time.sleep(0.3)
        assert _x(env, "getactivewindow", "getwindowname") == "His other app"
        pointer = _x(env, "getmouselocation")
        snapshot = apps.snapshot("Serena apps test")["snapshot"]
        assert '- push button "Add one" [ref=' in snapshot
        assert '- password text "Password"' in snapshot

        # He types into his own window the whole time she works in hers.
        typing = threading.Thread(
            target=_x, args=(env, "type", "--delay", "40", "notes from raghav"), daemon=True
        )
        typing.start()
        result = apps.run("Serena apps test", [
            {"press": {"role": "button", "name": "Add one"}},
            {"press": {"role": "button", "name": "Add one"}},
            {"set_text": {"role": "textbox", "name": "Name"}, "text": "Serena"},
            {"check": {"role": "checkbox", "name": "Subscribe"}},
            {"select": {"role": "combobox", "name": "Plan"}, "option": "Pro"},
            {"press": {"role": "button", "name": "Open dialog"}},
        ])
        typing.join(timeout=10)
        assert result["ok"], result["steps"]
        dialog = apps.run("Confirm", [{"press": {"role": "button", "name": "OK"}}])
        assert dialog["ok"], dialog["steps"]
        refused = apps.run("Serena apps test", [{"set_text": {"name": "Password"}, "text": "hunter2"}])
        assert not refused["ok"] and "hand off" in refused["steps"][0]["detail"]
        time.sleep(0.3)

        state = json.loads(app_state.read_text())
        assert state == {"count": 2, "name": "Serena", "subscribe": True, "plan": "Pro",
                         "confirmed": True, "typed": ""}
        # His window kept focus and every key he typed; his pointer never moved.
        assert json.loads(his_state.read_text())["typed"] == "notes from raghav"
        assert _x(env, "getactivewindow", "getwindowname") == "His other app"
        assert _x(env, "getmouselocation") == pointer
    finally:
        apps.close()
