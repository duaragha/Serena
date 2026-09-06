"""The status line prints one line, and still feeds the app.

Three agents share the screen, and whoever prints most wins least: Claude's
four-line status line pushed the Codex and Gemini panes down far enough that
the Claude pane got picked least often. The rate-limit bars, countdowns, model
names and CLI versions all live in the app's usage section, so the line keeps
only what is true of THIS session and appears nowhere else -- directory, cost,
context, session duration.

The tap is the part that must survive the trim. Claude's rate limits exist
nowhere but the status line's stdin payload; they are gone when the process
exits. Cutting the tap along with the printing would leave the usage section
showing Claude as waiting forever -- and it would look like the trim worked.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

BASH_STATUSLINE = Path.home() / ".claude" / "statusline.sh"
PY_STATUSLINE = Path(__file__).resolve().parents[1] / "scripts" / "claude-statusline.py"

PAYLOAD = {
    "cwd": "/home/raghav/Documents/Projects/serena/chats",
    "model": {"display_name": "Opus 5 (1M context)"},
    "version": "2.1.263",
    "cost": {"total_cost_usd": 4.2871, "total_duration_ms": 9084000},
    "context_window": {"used_percentage": 37, "context_window_size": 1000000},
    "rate_limits": {
        "five_hour": {"used_percentage": 39, "resets_at": 1788729000},
        "seven_day": {"used_percentage": 9, "resets_at": 1789207200},
    },
}

ANSI = re.compile(r"\033\[[0-9;]*m")


def _run(command: list[str], tmp_path: Path) -> str:
    env = dict(os.environ, XDG_DATA_HOME=str(tmp_path))
    done = subprocess.run(
        command, input=json.dumps(PAYLOAD), capture_output=True, text=True, timeout=90, env=env
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.parametrize("which", ["bash", "python"])
def test_it_prints_exactly_one_line(which: str, tmp_path: Path) -> None:
    if which == "bash":
        if not BASH_STATUSLINE.exists():
            pytest.skip("bash status line is installed per-machine")
        output = _run(["bash", str(BASH_STATUSLINE)], tmp_path)
    else:
        output = _run(["python3", str(PY_STATUSLINE)], tmp_path)

    assert output.count("\n") <= 1, f"{which} status line printed more than one line:\n{output}"
    assert output.strip(), "the status line printed nothing"


@pytest.mark.parametrize("which", ["bash", "python"])
def test_it_shows_the_session_facts_and_nothing_from_the_usage_panel(
    which: str, tmp_path: Path
) -> None:
    if which == "bash":
        if not BASH_STATUSLINE.exists():
            pytest.skip("bash status line is installed per-machine")
        output = _run(["bash", str(BASH_STATUSLINE)], tmp_path)
    else:
        output = _run(["python3", str(PY_STATUSLINE)], tmp_path)
    plain = ANSI.sub("", output).strip()

    # Kept: where it runs, what it cost, how full the context is, how long.
    assert "Serena" in plain
    assert "serena/chats" in plain
    assert "$4.29" in plain
    assert "37%" in plain and "370k/1000k" in plain
    assert "2H:31M" in plain

    # Moved to the usage section, so they must not reappear here.
    assert "Opus 5" not in plain, "the model is back on the status line"
    assert "2.1.263" not in plain, "the CLI version is back on the status line"
    assert "39%" not in plain and "9%" not in plain, "rate limits are back on the status line"
    assert "Codex" not in plain, "the Codex line is back"


def test_the_bash_tap_still_reports_claude_usage_to_the_app(tmp_path: Path) -> None:
    """The trim must not take the tap with it."""
    if not BASH_STATUSLINE.exists():
        pytest.skip("bash status line is installed per-machine")

    _run(["bash", str(BASH_STATUSLINE)], tmp_path)

    state = json.loads((tmp_path / "chats" / "live-usage.json").read_text(encoding="utf-8"))
    claude = state["claude"]

    assert claude["model"] == "Opus 5", "the usage section lost the model name"
    assert claude["version"] == "2.1.263", "the usage section lost the CLI version"
    assert claude["five_hour"]["used_percentage"] == 39
    assert claude["seven_day"]["used_percentage"] == 9
