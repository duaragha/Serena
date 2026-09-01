"""A pane must finish starting before anything is allowed to freeze it.

In a linked split the sibling is named standby the instant focus is elsewhere,
which means zero idle tolerance: the server freezes it on the very next sweep.
The only thing standing between a freshly spawned runtime and a SIGSTOP is the
prewarm window, and it was five seconds.

An agent CLI is not ready in five seconds. Codex reaches its input line in under
one but keeps starting MCP servers for another twelve, and an unreachable server
costs thirty seconds of timeout on top of that. So a Codex pane opened as the
sibling of a linked pair was routinely stopped part-way through its own startup,
rendered nothing, and never recovered until someone clicked into it. Three such
processes were found frozen, one of them less than two minutes old.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ui import pty_terminal


@pytest.fixture()
def pane():
    tid = pty_terminal.spawn(["bash", "-c", "sleep 300"], cwd="/tmp", cols=80, rows=24)
    yield tid
    try:
        pty_terminal.kill(tid)
    except Exception:
        pass


def _age(tid: str, seconds: float) -> None:
    """Pretend the runtime was spawned `seconds` ago."""
    pty_terminal.get(tid).started_at = time.monotonic() - seconds


def test_a_runtime_still_starting_is_not_frozen(pane) -> None:
    """Twelve seconds in, Codex is still bringing up its MCP servers."""
    pty_terminal.attach(pane)
    _age(pane, 12)

    assert pty_terminal.pause(pane, min_idle_seconds=0.0) is False


def test_a_slow_start_behind_an_unreachable_mcp_server_survives(pane) -> None:
    """One unreachable server costs thirty seconds before the CLI settles."""
    pty_terminal.attach(pane)
    _age(pane, 45)

    assert pty_terminal.pause(pane, min_idle_seconds=0.0) is False


def test_the_grace_period_does_end(pane) -> None:
    """It is a startup allowance, not a permanent exemption."""
    pty_terminal.attach(pane)          # past the never-shown guard below
    _age(pane, pty_terminal._PREWARM_SECONDS + 5)

    assert pty_terminal.pause(pane, min_idle_seconds=0.0) is True
    pty_terminal.resume(pane)


def test_the_window_covers_a_measured_agent_start() -> None:
    """12s of MCP startup plus one 30s timeout is the case that broke."""
    assert pty_terminal._PREWARM_SECONDS >= 42, (
        "too short to cover an agent start behind a slow MCP server"
    )


def test_an_explicit_protection_still_wins_immediately(pane) -> None:
    """Startup grace must not weaken the guards that already existed."""
    _age(pane, 3600)

    assert pty_terminal.pause(pane, protected=True, min_idle_seconds=0.0) is False


# --- No pane sleeps on sight -------------------------------------------------

def test_the_sweep_gives_every_pane_the_same_idle_requirement() -> None:
    """The linked sibling used to be frozen the instant focus left it.

    That is what stopped Codex mid-startup: a pane spawned as the background
    half of a linked pair was named standby before it had finished coming up,
    and zero idle tolerance meant the very next sweep stopped it.
    """
    source = (Path(__file__).resolve().parents[1] / "ui" / "web.py").read_text(encoding="utf-8")
    start = source.index("def api_terminal_runtime_sync(")
    body = source[start : source.index("@app.route", start + 10)]

    assert "min_idle_seconds=0.0" not in body, "a pane can still be frozen on sight"
    assert "min_idle_seconds=_RUNTIME_IDLE_SECONDS" in body
