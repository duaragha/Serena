"""Shift+Enter has to insert a newline on Windows, not submit the prompt.

A browser terminal cannot deliver Shift+Enter at all, so Serena synthesises
something and writes it to the PTY. A bare LF is what the Linux side has always
sent and it works there.

It does not on Windows, and the documented remedy makes it worse. Claude Code
recognises ESC followed by CR as "insert a newline", which is what every
terminal-setup guide tells people to bind. ConPTY eats the ESC: a real child
receives a lone carriage return, which is Enter, so the prompt submits. Measured
through ui.pty_terminal against a live child:

    LF       arrives intact
    ESC+CR   arrives as CR      <- submits
    ESC+LF   arrives as LF
    CSI-u    arrives intact     <- the one that works

CSI-u is the modern encoding for a modified key, and Claude Code requests that
protocol at startup, so it is what Windows sends.
"""

from __future__ import annotations

import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "ui" / "web.py"

ESC = "\x1b"
CSI_U_SHIFT_ENTER = ESC + "[13;2u"


def _page() -> str:
    """Read the page source directly.

    Importing ui.web would drag in the whole app, and these assertions are
    about what the page sends, not about the server booting.
    """
    return SOURCE.read_text(encoding="utf-8")


def test_windows_sends_the_sequence_that_survives_conpty() -> None:
    page = _page()

    assert "_TERM_NEWLINE" in page, "the newline sequence is no longer chosen in one place"
    match = re.search(
        r"const _TERM_NEWLINE = .*?includes\('Windows'\)\s*\?\s*'([^']*)'\s*:\s*'([^']*)'",
        page,
        re.S,
    )
    assert match, "could not find the platform choice for the synthesised newline"

    windows, posix = match.group(1), match.group(2)

    assert windows == "\\x1b[13;2u", f"Windows must send CSI-u, got {windows!r}"
    assert posix == "\\n", f"POSIX should keep the LF that works there, got {posix!r}"


def test_the_escape_prefixed_forms_are_not_used() -> None:
    """Both lose their ESC through ConPTY and degrade to a plain Enter."""
    match = re.search(
        r"const _TERM_NEWLINE = .*?includes\('Windows'\)\s*\?\s*'([^']*)'",
        _page(),
        re.S,
    )
    windows = match.group(1)

    assert windows != "\\x1b\\r", "ESC+CR arrives as a bare CR and submits the prompt"
    assert windows != "\\x1b\\n", "ESC+LF arrives as a bare LF"


def test_every_newline_path_uses_the_shared_sequence() -> None:
    """Two places synthesise it: the capture handler and the keybinding action.

    They were both hard-coded to LF. If one drifts back, Shift+Enter works from
    one code path and submits from the other, which is worse than either.
    """
    page = _page()

    assert page.count("ws.send(_TERM_NEWLINE)") == 2
    assert "ws.send('\\n')" not in page, "a hard-coded LF newline is back"
