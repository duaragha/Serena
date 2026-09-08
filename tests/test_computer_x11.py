"""Optional real XTEST test on a private X server; never uses the user's screen."""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.computer_platform import X11Desktop


@pytest.mark.parametrize("typed_text", ["Serena Ω café Привет", "lowercase ω and Йй", "你好 😀"])
def test_unicode_input_and_key_release_on_isolated_x11(tmp_path, monkeypatch, typed_text):
    binary = os.environ.get("SERENA_TEST_XVFB") or shutil.which("Xvfb")
    if not binary:
        pytest.skip("Xvfb is required for the isolated live X11 acceptance test")
    read_fd, write_fd = os.pipe()
    server = subprocess.Popen(
        [
            binary,
            "-displayfd",
            str(write_fd),
            "-screen",
            "0",
            "1600x1000x24",
            "-nolisten",
            "tcp",
            "-ac",
        ],
        pass_fds=(write_fd,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(write_fd)
    fixture = None
    desktop = None
    try:
        assert select.select([read_fd], [], [], 5)[0], "Xvfb did not start"
        display = ":" + os.read(read_fd, 32).decode().strip()
        monkeypatch.setenv("DISPLAY", display)
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        root = Path(__file__).resolve().parents[1]
        state = tmp_path / "fixture.json"
        fixture = subprocess.Popen(
            [
                sys.executable,
                str(root / "scripts/computer_smoke.py"),
                "--fixture",
                str(state),
                "--x",
                "200",
            ]
        )
        deadline = time.monotonic() + 5
        while not state.exists() and time.monotonic() < deadline:
            time.sleep(0.03)
        values = json.loads(state.read_text())
        desktop = X11Desktop()
        window = desktop._run(
            "xdotool", "search", "--onlyvisible", "--name", "^Serena computer acceptance test$"
        ).splitlines()[-1]
        desktop._run("xdotool", "windowfocus", "--sync", window)
        rect = desktop.window_info(window)["rect"]
        desktop.move(rect["x"] + values["entry"][0], rect["y"] + values["entry"][1])
        desktop.button(1, True)
        desktop.button(1, False)
        desktop.type_text(typed_text, lambda: False)
        deadline = time.monotonic() + 3
        while json.loads(state.read_text())["text"] != typed_text and time.monotonic() < deadline:
            time.sleep(0.05)
        assert json.loads(state.read_text())["text"] == typed_text
        desktop.key("CTRL", True)
        desktop.key("a", True)
        desktop.release()
        desktop.type_text("verified", lambda: False)
        deadline = time.monotonic() + 3
        while json.loads(state.read_text())["text"] != "verified" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert json.loads(state.read_text())["text"] == "verified"
        assert not desktop.held_keys and not desktop.held_buttons
    finally:
        os.close(read_fd)
        if desktop:
            desktop.close()
        if fixture:
            fixture.terminate()
            fixture.wait(timeout=3)
        server.terminate()
        server.wait(timeout=3)
