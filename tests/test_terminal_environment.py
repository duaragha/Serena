"""Interactive panes must not inherit an automation host's plain-text mode."""

import json
import os
import sys
import time

import pytest

from ui import pty_terminal


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("term", ["dumb", "", "vt100", "xterm-256color"])
def test_interactive_terminal_capabilities_are_owned_by_the_pane(monkeypatch, windows, term):
    monkeypatch.setattr(pty_terminal, "_IS_WINDOWS", windows)
    inherited = {
        "PATH": os.environ.get("PATH", ""),
        "TERM": term,
        "COLORTERM": "",
        "NO_COLOR": "1",
        "CLICOLOR": "0",
        "FORCE_COLOR": "0",
        "CLICOLOR_FORCE": "0",
        "PROVIDER_SETTING": "preserved",
    }
    env = pty_terminal._terminal_environment(inherited)
    assert env["TERM"] == "xterm-256color"
    assert env["COLORTERM"] == "truecolor"
    assert not {"NO_COLOR", "CLICOLOR", "FORCE_COLOR", "CLICOLOR_FORCE"} & env.keys()
    assert env["PROVIDER_SETTING"] == "preserved"
    assert inherited["TERM"] == term
    assert inherited["NO_COLOR"] == "1"


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY integration")
def test_real_pty_has_color_and_its_own_dimensions(monkeypatch, tmp_path):
    monkeypatch.setattr(pty_terminal, "_scope_supported", False)
    script = """
import json, os, sys
size = os.get_terminal_size()
print(json.dumps({
    'tty': sys.stdin.isatty() and sys.stdout.isatty(),
    'term': os.environ.get('TERM'), 'color': os.environ.get('COLORTERM'),
    'no_color': 'NO_COLOR' in os.environ,
    'size': list(size),
    'env_size': [os.environ.get('COLUMNS'), os.environ.get('LINES')],
}), flush=True)
if os.environ.get('TERM') == 'xterm-256color' and 'NO_COLOR' not in os.environ:
    print('\\033[32mNative terminal color\\033[0m', flush=True)
"""
    env = dict(os.environ, TERM="dumb", COLORTERM="", NO_COLOR="1", COLUMNS="80", LINES="24")
    tid = pty_terminal.spawn([sys.executable, "-c", script], str(tmp_path), cols=123, rows=37, env=env)
    output = b""
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            chunk = pty_terminal.read_available(tid, timeout=0.1)
            if chunk is None:
                break
            output += chunk
        payload = json.loads(output.splitlines()[0])
        assert payload == {
            "tty": True, "term": "xterm-256color", "color": "truecolor",
            "no_color": False, "size": [123, 37], "env_size": ["123", "37"],
        }
        assert b"\x1b[32mNative terminal color\x1b[0m" in output
    finally:
        pty_terminal.kill(tid)
