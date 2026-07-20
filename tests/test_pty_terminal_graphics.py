import os
import shutil
import subprocess
import time

import pytest

from ui import pty_terminal


@pytest.fixture(autouse=True)
def clean_terminal_registry():
    yield
    for tid in list(pty_terminal._terminals):
        pty_terminal.kill(tid)
    pty_terminal._session_tids.clear()


def test_sixel_environment_has_a_resolvable_terminfo_entry(tmp_path, monkeypatch):
    if not shutil.which("tic") or not shutil.which("infocmp"):
        pytest.skip("terminfo tools are not installed")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    env = dict(os.environ)

    assert pty_terminal._enable_sixel_environment(env) is True
    assert env["TERM"] == "xterm-sixel"

    result = subprocess.run(
        ["infocmp", env["TERM"]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "xterm-sixel" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY backend")
def test_sixel_protocol_reaches_the_child_process():
    tid = pty_terminal.spawn(
        ["/bin/sh", "-c", "printf '%s' \"$TERM\"; sleep 0.1"],
        cwd="/tmp",
        terminal_protocol="sixel",
    )
    output = bytearray()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        chunk = pty_terminal.read_available(tid, timeout=0.1)
        if chunk is None:
            break
        output.extend(chunk)

    terminal = pty_terminal.get(tid)
    assert terminal is not None
    assert terminal.graphics_protocol == "sixel"
    assert b"xterm-sixel" in output


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_runtime_busy_guard_and_hot_resume():
    tid = pty_terminal.spawn(
        ["/bin/sleep", "30"],
        cwd="/tmp",
        session_id="new-test",
        agent="codex",
    )
    term = pty_terminal.get(tid)
    assert term is not None

    pty_terminal.mark_turn_started(tid, (10, 100))
    assert pty_terminal.pause(tid, prewarm_seconds=0) is False
    assert pty_terminal.refresh_turn_state(tid, False, (10, 100)) is True
    assert pty_terminal.refresh_turn_state(tid, False, (20, 200)) is False

    assert pty_terminal.pause(tid, prewarm_seconds=0) is True
    assert pty_terminal.get_runtime_state(tid) == "paused"
    started = time.perf_counter()
    assert pty_terminal.resume(tid) is True
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert pty_terminal.get_runtime_state(tid) == "live"
    assert elapsed_ms < 100
    assert pty_terminal.is_alive(tid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY backend")
def test_pseudo_session_mapping_migrates_without_restarting_process():
    tid = pty_terminal.spawn(
        ["/bin/sleep", "30"],
        cwd="/tmp",
        session_id="new-pseudo",
        agent="claude",
    )
    pty_terminal.register_session("new-pseudo", tid)
    pid = pty_terminal.get(tid).proc.pid

    assert pty_terminal.migrate_session("new-pseudo", "real-session", tid)
    assert pty_terminal.tid_for_session("new-pseudo") is None
    assert pty_terminal.tid_for_session("real-session") == tid
    assert pty_terminal.get(tid).session_id == "real-session"
    assert pty_terminal.get(tid).proc.pid == pid
