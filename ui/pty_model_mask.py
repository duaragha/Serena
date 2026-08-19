#!/usr/bin/env python3
"""PTY relay that fixes model labels for Codex-backed Claude workflows.

Claude's workflow UI reports the model of its relay agent, usually Sonnet,
instead of the Codex model that relay launches. Serena requires Codex workflow
labels to start with sol, terra, or luna, so this relay replaces only the
Claude model token on those rows. All other output and all input are untouched.
"""

from __future__ import annotations

import array
import errno
import fcntl
import os
import pty
import re
import select
import signal
import sys
import termios
import tty

_CSI = rb"\x1b\[[0-?]*[ -/]*[@-~]"
_OSC = rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
_CONTROL_STRING = rb"\x1b[P^_][\s\S]*?\x1b\\"
_SHORT_ESCAPE = rb"\x1b[@-_]"
_ESCAPE = rb"(?:" + b"|".join((_CSI, _OSC, _CONTROL_STRING, _SHORT_ESCAPE)) + rb")"

_FAMILY = rb"(?P<family>sol|terra|luna)"
_VERSION = rb"(?P<version>[0-9]+(?:\.[0-9]+)?)?"
_LABEL_CHAR = rb"(?:[A-Za-z0-9._-]|" + _ESCAPE + rb")"
_LABEL = _FAMILY + _VERSION + _LABEL_CHAR + rb"*:[^\s\x1b]+"
_GAP = rb"(?:[ \t]|" + _ESCAPE + rb")*"
_CLAUDE_MODEL = rb"(?:Sonnet|Opus|Haiku|Fable)[ \t]?[0-9](?:\.[0-9])?"
_PATTERN = re.compile(
    rb"(?P<prefix>" + _LABEL + _GAP + rb")(?P<model>" + _CLAUDE_MODEL + rb")",
    re.IGNORECASE,
)

_VISIBLE_COMPLETE = re.compile(
    rb"^(?:sol|terra|luna)(?:[0-9]+(?:\.[0-9]+)?)?[A-Za-z0-9._-]*:"
    rb"[^\s]+[ \t]*" + _CLAUDE_MODEL,
    re.IGNORECASE,
)
_FAMILIES = (b"sol", b"terra", b"luna")
_MODEL_NAMES = (b"sonnet", b"opus", b"haiku", b"fable")
_MAX_PARTIAL = 1024


def _model_display(match: re.Match[bytes]) -> bytes:
    family = match.group("family").decode("ascii").title()
    version = (match.group("version") or b"5.6").decode("ascii")
    return f"{family} {version}".encode("ascii")


def _rewrite(buf: bytes) -> bytes:
    return _PATTERN.sub(
        lambda match: match.group("prefix") + _model_display(match),
        buf,
    )


def _strip_escapes(buf: bytes) -> tuple[bytes, bool]:
    """Return visible bytes and whether the final escape is incomplete."""
    visible = bytearray()
    index = 0
    while index < len(buf):
        if buf[index] != 0x1B:
            visible.append(buf[index])
            index += 1
            continue

        if index + 1 >= len(buf):
            return bytes(visible), True
        kind = buf[index + 1]
        if kind == ord("["):
            end = index + 2
            while end < len(buf) and not 0x40 <= buf[end] <= 0x7E:
                end += 1
            if end >= len(buf):
                return bytes(visible), True
            index = end + 1
        elif kind == ord("]"):
            bell = buf.find(b"\x07", index + 2)
            string_term = buf.find(b"\x1b\\", index + 2)
            ends = [end for end in (bell, string_term) if end >= 0]
            if not ends:
                return bytes(visible), True
            end = min(ends)
            index = end + (1 if end == bell else 2)
        elif kind in (ord("P"), ord("^"), ord("_")):
            end = buf.find(b"\x1b\\", index + 2)
            if end < 0:
                return bytes(visible), True
            index = end + 2
        else:
            index += 2
    return bytes(visible), False


def _could_be_model_prefix(value: bytes) -> bool:
    value = value.lower()
    for name in _MODEL_NAMES:
        if name.startswith(value):
            return True
        if not value.startswith(name):
            continue
        suffix = value[len(name) :]
        if suffix in (b"", b" "):
            return True
        if suffix.startswith(b" "):
            suffix = suffix[1:]
        if len(suffix) == 1 and suffix.isdigit():
            return True
        if len(suffix) == 2 and suffix[0:1].isdigit() and suffix[1:] == b".":
            return True
    return False


def _candidate_is_incomplete(buf: bytes) -> bool:
    if len(buf) > _MAX_PARTIAL:
        return False
    visible, partial_escape = _strip_escapes(buf)
    lower = visible.lower()
    if partial_escape and not lower:
        return True

    family = next(
        (name for name in _FAMILIES if name.startswith(lower) or lower.startswith(name)),
        None,
    )
    if family is None:
        return False
    if len(lower) < len(family):
        return True
    if _VISIBLE_COMPLETE.match(visible):
        return False

    remainder = visible[len(family) :]
    if b"\r" in remainder or b"\n" in remainder:
        return False
    colon = remainder.find(b":")
    if colon < 0:
        return all(chr(byte).isalnum() or byte in b"._-" for byte in remainder)
    if not all(chr(byte).isalnum() or byte in b"._-" for byte in remainder[:colon]):
        return False

    after_colon = remainder[colon + 1 :]
    if not after_colon:
        return True
    token_end = 0
    while token_end < len(after_colon) and after_colon[token_end] not in b" \t":
        token_end += 1
    if token_end == 0:
        return False
    if token_end == len(after_colon):
        return True
    model = after_colon[token_end:].lstrip(b" \t")
    return not model or _could_be_model_prefix(model)


def _incomplete_candidate_start(buf: bytes) -> int | None:
    start_at = max(0, len(buf) - _MAX_PARTIAL)
    lower = buf.lower()
    for index in range(start_at, len(buf)):
        if lower[index] not in b"stl":
            continue
        if _candidate_is_incomplete(buf[index:]):
            return index
    return None


class ModelMaskStream:
    """Rewriter that preserves matches split across arbitrary PTY reads."""

    def __init__(self) -> None:
        self._pending = b""

    def feed(self, data: bytes) -> bytes:
        buf = self._pending + data
        hold_at = _incomplete_candidate_start(buf)
        if hold_at is None:
            self._pending = b""
            return _rewrite(buf)
        self._pending = buf[hold_at:]
        return _rewrite(buf[:hold_at])

    def finish(self) -> bytes:
        output = _rewrite(self._pending)
        self._pending = b""
        return output


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print("pty_model_mask: no command", file=sys.stderr)
        return 2

    # Default OFF (passthrough): the launchers only invoke this relay when
    # SERENA_MODEL_MASK=on is explicitly set. Keeping the standalone default off
    # too means a stray direct invocation never silently adds keystroke latency.
    if os.environ.get("SERENA_MODEL_MASK", "off").lower() in ("off", "0", "false"):
        os.execvp(argv[0], argv)

    pid, master = pty.fork()
    if pid == 0:
        os.execvp(argv[0], argv)

    def _sync_winsize(*_args: object) -> None:
        try:
            winsize = array.array("h", [0, 0, 0, 0])
            fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, winsize, True)
            fcntl.ioctl(master, termios.TIOCSWINSZ, winsize)
            os.kill(pid, signal.SIGWINCH)
        except OSError:
            pass

    signal.signal(signal.SIGWINCH, _sync_winsize)
    _sync_winsize()

    stdin_fd = sys.stdin.fileno()
    try:
        old_attrs = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)
    except termios.error:
        old_attrs = None

    stream = ModelMaskStream()
    exit_code = 1
    try:
        while True:
            try:
                readable, _, _ = select.select([master, stdin_fd], [], [])
            except InterruptedError:
                continue
            if stdin_fd in readable:
                try:
                    data = os.read(stdin_fd, 65536)
                except OSError:
                    data = b""
                if data:
                    os.write(master, data)
            if master in readable:
                try:
                    data = os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not data:
                    break
                output = stream.feed(data)
                if output:
                    os.write(sys.stdout.fileno(), output)
        output = stream.finish()
        if output:
            os.write(sys.stdout.fileno(), output)
        _, status = os.waitpid(pid, 0)
        exit_code = os.waitstatus_to_exitcode(status)
        if exit_code < 0:
            exit_code = 128 - exit_code
    finally:
        if old_attrs is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
