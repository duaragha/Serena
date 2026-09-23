"""The client her terminals and voice use to drive the app's browser."""

from __future__ import annotations

import json
import os

import pytest

from core import app_browser as ab


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("SERENA_BROWSER_CONTROL", raising=False)
    monkeypatch.delenv("SERENA_BROWSER_EDITION", raising=False)
    monkeypatch.setattr(ab.sys, "platform", "linux")
    return tmp_path


def _control(root, edition, *, pid, port=4000):
    folder = root / f"serena-desktop-{edition}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "app-browser.json").write_text(json.dumps(
        {"port": port, "token": "t" * 48, "pid": pid, "channel": edition}))


def test_main_app_wins_when_both_run_and_dev_can_be_asked_for(config, monkeypatch):
    _control(config, "stable", pid=os.getpid(), port=1111)
    _control(config, "dev", pid=os.getpid(), port=2222)
    assert ab.control()["port"] == 1111
    monkeypatch.setenv("SERENA_BROWSER_EDITION", "dev")
    assert ab.control()["port"] == 2222


def test_a_control_file_left_by_a_dead_app_is_ignored(config):
    _control(config, "stable", pid=2**22 + 12345, port=1111)
    _control(config, "dev", pid=os.getpid(), port=2222)
    assert ab.control()["port"] == 2222


def test_no_running_app_is_a_plain_error(config):
    with pytest.raises(ab.AppBrowserError, match="not running"):
        ab.control()


def test_targets_are_refs_points_or_selectors():
    assert ab.target("e12") == {"ref": "e12"}
    assert ab.target("120,340") == {"x": 120, "y": 340}
    assert ab.target("button.primary") == {"selector": "button.primary"}
    assert ab.target("email") == {"selector": "email"}


def test_look_reads_like_a_page_not_json():
    text = ab.render_look({"title": "Bench", "url": "http://localhost:8911/", "text": "Hello",
                           "elements": [{"ref": "e1", "tag": "input", "type": "text", "name": "your name"},
                                        {"ref": "e2", "tag": "button", "name": "Go", "disabled": True}]})
    assert text.splitlines()[0] == "Bench -- http://localhost:8911/"
    assert '  e1 input [text] "your name"' in text
    assert '  e2 button "Go" (disabled)' in text
    assert text.endswith("Hello")


def test_logs_say_what_failed():
    text = ab.render_logs({"console": [{"level": "error", "message": "boom", "source": "app.js", "line": 3}],
                           "network": [{"status": 404, "method": "GET", "url": "http://x/missing.json"}]})
    assert "console error: boom  (app.js:3)" in text
    assert "network 404: GET http://x/missing.json" in text
    assert ab.render_logs({}) == "no console messages or failed requests"
