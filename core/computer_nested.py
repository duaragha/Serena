"""Serena's own desktop: a nested X server with its own pointer, keyboard and focus.

XTest input sent to this display never reaches Raghav's cursor or keyboard focus,
so he keeps working while she drives her own browser and terminal. The Xephyr
window on his screen is the live viewer. Clicking or typing inside it is an
explicit takeover; closing it closes her desktop.

X clients authenticate with a per-start cookie. The server reads it from a
private file and clients find it in his normal Xauthority file, keyed by display
number, so Xlib, PIL and the xdotool/xinput subprocesses all connect without
swapping process-wide environment variables.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
from pathlib import Path

from core.computer_client import state_dir
from core.computer_platform import ComputerError, desktop_environment

TITLE = "Serena's desktop"
TERMINAL_APP_ID = "org.serena.IsolatedDesktop"
FIRST_DISPLAY = 70
LAST_DISPLAY = 99
DEFAULT_SIZE = (1920, 1080)
WINDOW_MANAGERS = ("metacity", "openbox", "xfwm4", "fluxbox", "icewm")
BROWSERS = (
    "microsoft-edge-stable",
    "microsoft-edge",
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
)
APPS = frozenset({"browser", "terminal"})


def _size():
    match = re.fullmatch(r"(\d{3,4})x(\d{3,4})", os.environ.get("SERENA_ISOLATED_SIZE", ""))
    if not match:
        return DEFAULT_SIZE
    width, height = int(match.group(1)), int(match.group(2))
    return (width, height) if 800 <= width <= 3840 and 600 <= height <= 2160 else DEFAULT_SIZE


def _alive(pid, needle=""):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        command = Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ")
    except (OSError, ValueError):
        return False
    return needle.encode() in command


def _terminate(pid, needle):
    """Signal only a process that is still the one recorded, never a recycled pid."""
    if not _alive(pid, needle):
        return
    with contextlib.suppress(OSError):
        os.kill(int(pid), signal.SIGTERM)
    deadline = time.monotonic() + 2
    while _alive(pid, needle) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _alive(pid, needle):
        with contextlib.suppress(OSError):
            os.kill(int(pid), signal.SIGKILL)


class IsolatedDesktop:
    """Start, adopt, and close the nested desktop. Holds no model or authority."""

    def __init__(self, directory=None, *, host_env=None, host_monitors=None, size=None):
        self.directory = Path(directory or state_dir() / "isolated")
        self.host_env = dict(host_env or desktop_environment())
        self.host_monitors = host_monitors
        self.size = size or _size()

    # -- state -----------------------------------------------------------
    @property
    def runtime_path(self):
        return self.directory / "runtime.json"

    @property
    def profile(self):
        return self.directory / "browser-profile"

    def _prepare(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)

    def _read(self):
        try:
            return json.loads(self.runtime_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, info):
        temporary = self.runtime_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(info), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.runtime_path)

    def running(self):
        info = self._read()
        if not info or not _alive(info.get("xephyr_pid"), "Xephyr"):
            return None
        if not Path(f"/tmp/.X11-unix/X{info['number']}").exists():
            return None
        return info

    def env(self, info=None):
        info = info or self.running()
        if not info:
            raise ComputerError("serena's desktop is not running")
        env = dict(self.host_env)
        env["DISPLAY"] = info["display"]
        env["XAUTHORITY"] = self._xauthority()
        return env

    def status(self):
        info = self.running()
        if not info:
            return {"running": False, "size": "{}x{}".format(*self.size)}
        return {
            "running": True,
            "display": info["display"],
            "size": info["size"],
            "started_at": info["started_at"],
            "viewer_window": self._viewer(),
            "browser_profile": str(self.profile),
        }

    # -- lifecycle -------------------------------------------------------
    def _xauthority(self):
        return self.host_env.get("XAUTHORITY") or str(Path.home() / ".Xauthority")

    def _run(self, *args, env=None, timeout=5):
        result = subprocess.run(
            args,
            env=env or self.host_env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode:
            raise ComputerError(f"{args[0]} failed: {result.stderr.strip()[:180]}")
        return result.stdout.strip()

    def _spawn(self, args, env):
        with (self.directory / "desktop.log").open("ab") as log:
            (self.directory / "desktop.log").chmod(0o600)
            return subprocess.Popen(
                args,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )

    @staticmethod
    def _free_display():
        for number in range(FIRST_DISPLAY, LAST_DISPLAY + 1):
            if not (
                Path(f"/tmp/.X11-unix/X{number}").exists() or Path(f"/tmp/.X{number}-lock").exists()
            ):
                return number
        raise ComputerError("no free X display number for serena's desktop")

    def _connect(self, display, timeout):
        from Xlib import display as xdisplay
        from Xlib.error import DisplayError

        deadline = time.monotonic() + timeout
        while True:
            try:
                return xdisplay.Display(display)
            except (DisplayError, OSError):
                if time.monotonic() >= deadline:
                    raise ComputerError("serena's desktop did not accept connections") from None
                time.sleep(0.05)

    def start(self):
        """Adopt the running desktop or start a fresh one; idempotent."""

        import fcntl

        self._prepare()
        with (self.directory / "desktop.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            info = self.running()
            if info:
                if not _alive(info.get("wm_pid"), info.get("wm", "")):
                    info["wm"], info["wm_pid"] = self._start_wm(self.env(info))
                    self._write(info)
                return info
            self._forget(self._read())
            if not shutil.which("Xephyr"):
                raise ComputerError("serena's desktop needs Xephyr: sudo apt install xserver-xephyr")
            number = self._free_display()
            display = f":{number}"
            cookie = secrets.token_hex(16)
            server_auth = self.directory / "server.auth"
            server_auth.unlink(missing_ok=True)
            self._run("xauth", "-f", str(server_auth), "add", display, "MIT-MAGIC-COOKIE-1", cookie)
            server_auth.chmod(0o600)
            self._run("xauth", "-f", self._xauthority(), "add", display, "MIT-MAGIC-COOKIE-1", cookie)
            width, height = self.size
            xephyr = self._spawn(
                [
                    "Xephyr",
                    display,
                    "-auth",
                    str(server_auth),
                    "-screen",
                    f"{width}x{height}",
                    "-no-host-grab",
                    "-title",
                    TITLE,
                    "-nolisten",
                    "tcp",
                    "-br",
                    "-noreset",
                ],
                self.host_env,
            )
            info = {
                "display": display,
                "number": number,
                "xephyr_pid": xephyr.pid,
                "size": f"{width}x{height}",
                "started_at": time.time(),
            }
            try:
                self._connect(display, 5).close()
                info["wm"], info["wm_pid"] = self._start_wm(self.env(info))
                self._write(info)
            except Exception:
                _terminate(xephyr.pid, "Xephyr")
                self._forget(info)
                raise
        self._place_viewer()
        for app in ("terminal", "browser"):
            with contextlib.suppress(ComputerError, OSError, subprocess.SubprocessError):
                self.launch(app)
        return info

    def _start_wm(self, env):
        name = next((item for item in WINDOW_MANAGERS if shutil.which(item)), None)
        if not name:
            raise ComputerError("serena's desktop needs a window manager: sudo apt install metacity")
        args = [name, "--replace", "--sm-disable", "--no-composite"] if name == "metacity" else [name]
        process = self._spawn(args, env)
        connection = self._connect(env["DISPLAY"], 3)
        try:
            atom = connection.intern_atom("_NET_SUPPORTING_WM_CHECK")
            deadline = time.monotonic() + 5
            while connection.screen().root.get_full_property(atom, 0) is None:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise ComputerError(f"{name} did not start on serena's desktop")
                time.sleep(0.05)
        finally:
            connection.close()
        return name, process.pid

    def _forget(self, info):
        """Drop a dead desktop's cookie and runtime record."""
        if info and info.get("display"):
            with contextlib.suppress(ComputerError, OSError, subprocess.SubprocessError):
                self._run("xauth", "-f", self._xauthority(), "remove", info["display"])
        self.runtime_path.unlink(missing_ok=True)

    def stop(self):
        info = self._read()
        if not info:
            return self.status()
        # Her apps exit when their X server goes away; the WM and terminal
        # server are ours by recorded pid, so nothing else is ever signalled.
        _terminate(info.get("terminal_pid"), TERMINAL_APP_ID)
        _terminate(info.get("xephyr_pid"), "Xephyr")
        _terminate(info.get("wm_pid"), info.get("wm", "") or "\0")
        self._forget(info)
        return self.status()

    # -- apps and viewer ---------------------------------------------------
    def launch(self, app, url=None):
        if app not in APPS:
            raise ComputerError("launch supports browser or terminal")
        info = self.running()
        if not info:
            raise ComputerError("serena's desktop is not running")
        env = self.env(info)
        if app == "browser":
            binary = os.environ.get("SERENA_ISOLATED_BROWSER") or next(
                (item for item in BROWSERS if shutil.which(item)), None
            )
            if not binary:
                raise ComputerError("no Chromium-family browser is installed")
            self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            width, height = info["size"].split("x")
            self._spawn(
                [
                    binary,
                    f"--user-data-dir={self.profile}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--start-maximized",
                    f"--window-size={width},{height}",
                    "--window-position=0,0",
                    url or "about:blank",
                ],
                env,
            )
        else:
            self._ensure_terminal_server(info, env)
            # The client reports a spurious registration error for a custom
            # app id, yet the server still opens the window: ignore its code.
            self._spawn(
                [
                    "gnome-terminal",
                    "--app-id",
                    TERMINAL_APP_ID,
                    "--window",
                    f"--working-directory={Path.home()}",
                ],
                env,
            )
        return {"ok": True, "app": app}

    def _ensure_terminal_server(self, info, env):
        if _alive(info.get("terminal_pid"), TERMINAL_APP_ID):
            return
        binary = next(
            (
                path
                for path in ("/usr/libexec/gnome-terminal-server", "/usr/lib/gnome-terminal/gnome-terminal-server")
                if Path(path).exists()
            ),
            None,
        )
        if not binary or not shutil.which("gnome-terminal"):
            raise ComputerError("serena's desktop terminal needs gnome-terminal")
        # A separate server app id is what puts its windows on this display
        # instead of the one already serving his own terminals.
        process = self._spawn([binary, "--app-id", TERMINAL_APP_ID], env)
        info["terminal_pid"] = process.pid
        self._write(info)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with contextlib.suppress(ComputerError, subprocess.SubprocessError):
                owned = self._run(
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.freedesktop.DBus",
                    "--object-path",
                    "/org/freedesktop/DBus",
                    "--method",
                    "org.freedesktop.DBus.NameHasOwner",
                    TERMINAL_APP_ID,
                    env=env,
                    timeout=1,
                )
                if "true" in owned:
                    return
            time.sleep(0.1)

    def _viewer(self):
        with contextlib.suppress(ComputerError, subprocess.SubprocessError):
            found = self._run("xdotool", "search", "--name", f"^{TITLE}$", timeout=2)
            return found.splitlines()[-1] if found else None
        return None

    def _place_viewer(self):
        """Put the viewer on his rightmost secondary monitor when there is one."""
        if not self.host_monitors:
            return
        with contextlib.suppress(Exception):
            monitors = self.host_monitors()
            others = [m for m in monitors if not m.get("primary")] or monitors
            rect = max(others, key=lambda m: m["rect"]["x"])["rect"]
            width, height = self.size
            window = None
            deadline = time.monotonic() + 2
            while not window and time.monotonic() < deadline:
                window = self._viewer()
                time.sleep(0.05)
            if window:
                x = rect["x"] + max(0, (rect["width"] - width) // 2)
                y = rect["y"] + max(0, (rect["height"] - height) // 2)
                self._run("xdotool", "windowmove", window, str(x), str(y), timeout=2)

    def show(self):
        window = self._viewer()
        if not window:
            raise ComputerError("serena's desktop viewer is not open")
        self._run("xdotool", "windowactivate", window, timeout=2)
        return self.status()

    def hide(self):
        window = self._viewer()
        if not window:
            raise ComputerError("serena's desktop viewer is not open")
        self._run("xdotool", "windowminimize", window, timeout=2)
        return self.status()
