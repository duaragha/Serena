"""Reattaching or thawing a pane makes its TUI redraw.

After a renderer reload -- a desktop update, a window reload -- every pane
reattaches, and the renderer draws from a blank screen plus the tail of bytes
emitted since it detached. Claude's Ink UI re-renders every frame and heals
itself; codex (ratatui) and agy (bubbletea) redraw only on an event, and until
one arrives they sit there garbled. The reconnect path already "forced" a
resize for exactly this reason. It never worked, because TIOCSWINSZ with
unchanged dimensions delivers no SIGWINCH, and after a reload the dimensions
are unchanged.

A pane thawed from SIGSTOP has the same problem from the other side: it was
frozen mid-frame and wakes mid-frame.

The fix nudges the width one column off and back. Two real changes, two
SIGWINCH, a full redraw, and the pane ends at the size it started.
"""

from __future__ import annotations

import fcntl
import os
import pty
import signal
import struct
import sys
import termios
import time

import pytest

from ui import pty_terminal

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="pty + SIGWINCH")


# ── the premise, at the kernel ────────────────────────────────────────────────

class _WinchCounter:
    """A real child on a real pty that reports every SIGWINCH it receives."""

    def __init__(self):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child
            # fd 1 is the pty slave. sys.stdout is NOT: under pytest it is the
            # capture object, and writes through it never reach the pty.
            count = 0

            def on_winch(*_):
                nonlocal count
                count += 1
                os.write(1, f"WINCH{count}\n".encode())

            signal.signal(signal.SIGWINCH, on_winch)
            os.write(1, b"ready\n")
            time.sleep(10)
            os._exit(0)
        os.set_blocking(self.fd, False)
        self._read()

    def setwinsize(self, rows, cols):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _read(self) -> str:
        time.sleep(0.25)
        out = b""
        try:
            while True:
                out += os.read(self.fd, 4096)
        except (BlockingIOError, OSError):
            pass
        return out.decode(errors="replace")

    def winches(self) -> int:
        return self._read().count("WINCH")

    def close(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except OSError:
            pass
        os.close(self.fd)


@pytest.fixture
def child():
    counter = _WinchCounter()
    yield counter
    counter.close()


def test_the_premise_a_same_size_resize_delivers_no_sigwinch(child) -> None:
    """If this ever starts passing a signal through, the nudge is redundant
    and this file should say so rather than quietly keep working."""
    child.setwinsize(40, 120)
    assert child.winches() == 1

    child.setwinsize(40, 120)
    child.setwinsize(40, 120)
    assert child.winches() == 0, "the kernel now signals on a same-size resize"


def test_the_nudge_delivers_a_sigwinch_and_lands_on_the_original_size(child) -> None:
    child.setwinsize(40, 120)
    child.winches()

    term = pty_terminal.Terminal(id="t", proc=child, cols=120, rows=40)
    assert pty_terminal._repaint(term) is True

    assert child.winches() >= 1
    rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(child.fd, termios.TIOCGWINSZ, b"\0" * 8))
    assert (rows, cols) == (40, 120), "the pane did not end at the size it started"


# ── where it is wired in ──────────────────────────────────────────────────────

class _RecordingProc:
    def __init__(self):
        self.pid = os.getpid()
        self.calls: list[tuple] = []

    def setwinsize(self, rows, cols):
        self.calls.append(("setwinsize", rows, cols))


@pytest.fixture
def registered(monkeypatch):
    """A Terminal in the registry, with SIGCONT recorded instead of sent."""
    proc = _RecordingProc()
    term = pty_terminal.Terminal(id="test-tid", proc=proc, cols=100, rows=30)
    signals: list[str] = []

    def fake_killpg(pgid, sig):
        proc.calls.append(("signal", sig))
        signals.append(sig)

    monkeypatch.setattr(pty_terminal.os, "killpg", fake_killpg)
    monkeypatch.setattr(pty_terminal, "_IS_WINDOWS", False)
    with pty_terminal._registry_lock:
        pty_terminal._terminals[term.id] = term
    try:
        yield term, proc
    finally:
        with pty_terminal._registry_lock:
            pty_terminal._terminals.pop(term.id, None)


def test_attach_nudges_the_width_after_waking(registered) -> None:
    term, proc = registered
    term.runtime_state = "paused"

    result = pty_terminal.attach(term.id)

    assert result is not None
    # Woken first, then nudged: a stopped process receives no SIGWINCH.
    assert proc.calls[0] == ("signal", signal.SIGCONT)
    resizes = [c for c in proc.calls if c[0] == "setwinsize"]
    assert resizes[:2] == [("setwinsize", 30, 99), ("setwinsize", 30, 100)]


def test_attach_nudges_a_pane_that_was_never_asleep(registered) -> None:
    """A live pane behind a freshly reloaded renderer is just as blank."""
    term, proc = registered
    term.runtime_state = "live"

    pty_terminal.attach(term.id)

    assert ("signal", signal.SIGCONT) not in proc.calls
    assert [c for c in proc.calls if c[0] == "setwinsize"] == [
        ("setwinsize", 30, 99), ("setwinsize", 30, 100),
    ]


def test_thaw_nudges_after_sigcont(registered) -> None:
    term, proc = registered
    term.runtime_state = "paused"

    assert pty_terminal.resume(term.id) is True

    assert proc.calls == [
        ("signal", signal.SIGCONT), ("setwinsize", 30, 99), ("setwinsize", 30, 100),
    ]
    assert term.runtime_state == "live"


def test_resuming_a_live_pane_does_nothing(registered) -> None:
    """The focus sweep calls resume every two seconds; it must stay free."""
    term, proc = registered
    term.runtime_state = "live"

    assert pty_terminal.resume(term.id) is True

    assert proc.calls == []


def test_a_pane_at_the_minimum_width_nudges_outward() -> None:
    proc = _RecordingProc()
    term = pty_terminal.Terminal(id="narrow", proc=proc, cols=pty_terminal.MIN_COLS, rows=30)

    pty_terminal._repaint(term)

    floor = pty_terminal.MIN_COLS
    assert proc.calls == [("setwinsize", 30, floor + 1), ("setwinsize", 30, floor)]


def test_windows_is_left_alone(monkeypatch) -> None:
    """ConPTY repaints on its own and each resize is a slow call."""
    monkeypatch.setattr(pty_terminal, "_IS_WINDOWS", True)
    proc = _RecordingProc()
    term = pty_terminal.Terminal(id="win", proc=proc, cols=100, rows=30)

    assert pty_terminal._repaint(term) is False
    assert proc.calls == []


def test_a_dead_pty_does_not_take_the_attach_down_with_it(registered) -> None:
    term, proc = registered

    def broken(rows, cols):
        raise OSError("Input/output error")

    proc.setwinsize = broken

    assert pty_terminal.attach(term.id) is not None


# ── end to end: a real frozen child on a real pty ─────────────────────────────

def test_a_real_child_frozen_then_thawed_gets_a_redraw_signal(child, monkeypatch) -> None:
    monkeypatch.setattr(pty_terminal, "_IS_WINDOWS", False)
    child.setwinsize(40, 120)
    child.winches()
    term = pty_terminal.Terminal(id="real", proc=child, cols=120, rows=40)
    term.runtime_state = "paused"
    os.kill(child.pid, signal.SIGSTOP)
    time.sleep(0.2)

    with term.state_lock:
        assert pty_terminal._resume_locked(term) is True

    assert child.winches() >= 1, "the thawed child never got a SIGWINCH"
