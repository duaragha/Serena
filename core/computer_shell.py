"""A real shell on Serena's own desktop, through the terminal he can watch.

Commands run in a tmux session on a private socket. Her desktop's terminal
window is attached to that session, so each command and its output shows in
the viewer, while the worker gets the output back as text instead of reading
pixels. `uname -r` through screenshots took four model calls; here it is one.

The session's environment points at her display, and $BROWSER opens her own
Edge profile, so a CLI that launches a sign-in page (gcloud, Shopify) opens it
on her desktop rather than in his browser.
"""

from __future__ import annotations

import re
import subprocess
import time
import uuid
from pathlib import Path

from core.computer_platform import ComputerError

SOCKET = "serena-desktop"
SESSION = "serena"
MAX_OUTPUT = 12000
MAX_COMMAND = 4000


class ShellError(ComputerError):
    pass


def browser_opener(directory, profile, binary):
    """A $BROWSER command that opens URLs in her profile, not his default browser."""
    path = Path(directory) / "open-in-her-browser"
    path.write_text(
        f'#!/bin/sh\nexec "{binary}" --user-data-dir="{profile}" "$@"\n', encoding="utf-8"
    )
    path.chmod(0o700)
    return str(path)


class HerShell:
    def __init__(self, env, *, show=None, browser=None):
        self.env = dict(env)
        if browser:
            self.env["BROWSER"] = browser
            # Desktop-specific openers would hand URLs to his session's browser.
            self.env.pop("XDG_CURRENT_DESKTOP", None)
        # Opens a terminal window on her desktop attached to the session.
        self.show = show

    def _tmux(self, *args, timeout=5, check=True):
        result = subprocess.run(
            ["tmux", "-L", SOCKET, *args],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if check and result.returncode:
            raise ShellError(f"tmux {args[0]} failed: {result.stderr.strip()[:200]}")
        return result.stdout

    def ensure(self):
        if not self._alive():
            self._tmux(
                "new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50", "-c", str(Path.home())
            )
            for key in ("DISPLAY", "XAUTHORITY", "BROWSER"):
                if self.env.get(key):
                    self._tmux("set-environment", "-t", SESSION, key, self.env[key])
        if self.show and not self._tmux("list-clients", "-t", SESSION, check=False).strip():
            # Nothing he can see is attached: open the window before running.
            self.show()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if self._tmux("list-clients", "-t", SESSION, check=False).strip():
                    break
                time.sleep(0.1)

    def _alive(self):
        return (
            subprocess.run(
                ["tmux", "-L", SOCKET, "has-session", "-t", SESSION],
                env=self.env,
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )

    def _line(self):
        history, cursor = self._tmux(
            "display-message", "-p", "-t", SESSION, "#{history_size} #{cursor_y}"
        ).split()
        return int(history) + int(cursor)

    def _lines(self, start=0):
        text = self._tmux("capture-pane", "-p", "-J", "-t", SESSION, "-S", "-", "-E", "-")
        return text.splitlines()[start:]

    def run(self, command, *, timeout=30.0, cancelled=lambda: False):
        if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND:
            raise ShellError(f"a command needs 1-{MAX_COMMAND} characters")
        if "\n" in command or command.rstrip().endswith("&"):
            raise ShellError("send one foreground command per call; chain with && or ;")
        self.ensure()
        token = uuid.uuid4().hex[:12]
        marker = f"__SERENA_DONE_{token}_"
        start = self._line()
        self._tmux("send-keys", "-t", SESSION, "-l", f"{command}; printf '\\n{marker}%s__\\n' \"$?\"")
        self._tmux("send-keys", "-t", SESSION, "Enter")
        done = re.compile(rf"^{marker}(\d+)__$")
        deadline = time.monotonic() + timeout
        while True:
            lines = self._lines(start)
            for index, line in enumerate(lines):
                match = done.match(line.strip())
                if match:
                    return {
                        "status": "done",
                        "exit_code": int(match.group(1)),
                        "output": self._clip(lines[1:index]),
                    }
            if cancelled():
                return {"status": "interrupted", "output": self._clip(lines[1:])}
            if time.monotonic() >= deadline:
                return {
                    "status": "running",
                    "output": self._clip(lines[1:]),
                    "note": "still running; read again later, or send input such as y then Enter",
                }
            time.sleep(0.1)

    def send(self, text, *, enter=True):
        """Answer a prompt the running command is waiting on."""
        if not isinstance(text, str) or len(text) > 500:
            raise ShellError("send at most 500 characters")
        self.ensure()
        if text:
            self._tmux("send-keys", "-t", SESSION, "-l", text)
        if enter:
            self._tmux("send-keys", "-t", SESSION, "Enter")
        time.sleep(0.5)
        return self.read()

    def read(self, lines=80):
        self.ensure()
        return {"output": self._clip(self._lines()[-int(lines):])}

    @staticmethod
    def _clip(lines):
        text = "\n".join(line.rstrip() for line in lines).strip("\n")
        if len(text) > MAX_OUTPUT:
            text = "… earlier output trimmed …\n" + text[-MAX_OUTPUT:]
        return text
