"""Serena's own desktop: a headless X server with its own pointer, keyboard and focus.

XTest input sent to this display never reaches Raghav's cursor or keyboard focus,
so he keeps working while she drives her own browser and terminal. Nothing of it
is on his screen until he opens the viewer: a VNC window onto the display, served
by x11vnc on a private Unix socket. The server is view-only while she drives, so
no click or key of his can land there by accident; it accepts his input only
while she is paused or idle. Closing the viewer only stops watching.

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
# TigerVNC titles its window "<desktop name> - TigerVNC"; x11vnc names it TITLE.
VIEWER_TITLE = f"^{TITLE} - TigerVNC$"
VIEWERS = ("xtigervncviewer", "vncviewer")
SOCKET_NAME = "vnc.sock"
# sockaddr_un holds 108 bytes; a longer path makes x11vnc abort.
MAX_SOCKET_PATH = 100
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
        if not info or not _alive(info.get("server_pid"), "Xvfb"):
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

    def shell_env(self, info=None):
        """Her terminal's environment: her display, and URLs open in her browser."""
        from core.computer_shell import browser_opener

        env = self.env(info)
        binary = self.browser_binary()
        if binary:
            env["BROWSER"] = browser_opener(self.directory, self.profile, binary)
            # Desktop-specific openers would hand URLs to his session's browser.
            env.pop("XDG_CURRENT_DESKTOP", None)
        return env

    @staticmethod
    def browser_binary():
        return os.environ.get("SERENA_ISOLATED_BROWSER") or next(
            (item for item in BROWSERS if shutil.which(item)), None
        )

    def status(self):
        info = self.running()
        if not info:
            return {"running": False, "size": "{}x{}".format(*self.size)}
        return {
            "running": True,
            "display": info["display"],
            "size": info["size"],
            "started_at": info["started_at"],
            "viewer_window": self.viewer_window(),
            "viewer": "interactive" if info.get("interactive") else "view-only",
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
                if not _alive(info.get("vnc_pid"), "x11vnc"):
                    self._start_vnc(info)
                    self._write(info)
                return info
            self._retire(self._read())
            missing = [name for name in ("Xvfb", "x11vnc") if not shutil.which(name)]
            if missing:
                raise ComputerError(
                    "serena's desktop needs " + " and ".join(missing) + ": sudo apt install xvfb x11vnc"
                )
            number = self._free_display()
            display = f":{number}"
            cookie = secrets.token_hex(16)
            server_auth = self.directory / "server.auth"
            server_auth.unlink(missing_ok=True)
            self._run("xauth", "-f", str(server_auth), "add", display, "MIT-MAGIC-COOKIE-1", cookie)
            server_auth.chmod(0o600)
            self._run("xauth", "-f", self._xauthority(), "add", display, "MIT-MAGIC-COOKIE-1", cookie)
            width, height = self.size
            server = self._spawn(
                [
                    "Xvfb",
                    display,
                    "-auth",
                    str(server_auth),
                    "-screen",
                    "0",
                    f"{width}x{height}x24",
                    "-nolisten",
                    "tcp",
                    "-noreset",
                ],
                self.host_env,
            )
            info = {
                "display": display,
                "number": number,
                "server_pid": server.pid,
                "size": f"{width}x{height}",
                "started_at": time.time(),
                "interactive": False,
            }
            try:
                self._connect(display, 5).close()
                info["wm"], info["wm_pid"] = self._start_wm(self.env(info))
                self._start_vnc(info)
                self._write(info)
            except Exception:
                _terminate(info.get("vnc_pid"), "x11vnc")
                _terminate(server.pid, "Xvfb")
                self._forget(info)
                raise
        for app in ("terminal", "browser"):
            with contextlib.suppress(ComputerError, OSError, subprocess.SubprocessError):
                self.launch(app)
        return info

    def _start_wm(self, env):
        name = next((item for item in WINDOW_MANAGERS if shutil.which(item)), None)
        if not name:
            raise ComputerError("serena's desktop needs a window manager: sudo apt install metacity")
        args = [name, "--replace", "--sm-disable", "--compositor=none"] if name == "metacity" else [name]
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

    def _socket(self, info):
        path = self.directory / SOCKET_NAME
        if len(str(path)) > MAX_SOCKET_PATH:
            runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
            path = runtime / f"serena-desktop-{info['number']}.sock"
        return path

    def _start_vnc(self, info):
        """Serve the display on a private Unix socket, view-only until told otherwise.

        No TCP port and no password: the socket lives in her 0700 state directory
        (or his runtime directory), so only his user can connect. x11vnc's own
        clipboard sync is off because the clipboard bridge owns that job.
        """
        path = self._socket(info)
        path.unlink(missing_ok=True)
        process = self._spawn(
            [
                "x11vnc",
                "-display",
                info["display"],
                "-rfbport",
                "0",
                "-unixsock",
                str(path),
                "-forever",
                "-shared",
                "-viewonly",
                "-nosel",
                "-nopw",
                "-nobell",
                "-quiet",
                "-desktop",
                TITLE,
            ],
            self.env(info),
        )
        deadline = time.monotonic() + 5
        while not path.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                _terminate(process.pid, "x11vnc")
                raise ComputerError("serena's desktop viewer server did not start")
            time.sleep(0.05)
        info.update(vnc_pid=process.pid, socket=str(path), interactive=False)

    def set_interactive(self, interactive):
        """Accept his viewer input (True) or drop it at the server (False)."""
        info = self.running()
        if not info:
            return
        if not _alive(info.get("vnc_pid"), "x11vnc"):
            self._start_vnc(info)
        # Shutting him out waits (-sync, ~0.2 s) until the server has applied
        # it, so nothing of his lands after her first input. Letting him back
        # in can arrive a moment late.
        args = ["-sync", "-R", "viewonly"] if not interactive else ["-R", "noviewonly"]
        self._run("x11vnc", "-display", info["display"], *args, env=self.env(info), timeout=5)
        info["interactive"] = bool(interactive)
        self._write(info)

    def _retire(self, info):
        """Stop what a dead or older desktop left behind, then forget it."""
        if info:
            # Her desktop was a visible Xephyr window before it went headless.
            _terminate(info.get("xephyr_pid"), "Xephyr")
            _terminate(info.get("server_pid"), "Xvfb")
            _terminate(info.get("vnc_pid"), "x11vnc")
            _terminate(info.get("viewer_pid"), "vncviewer")
            _terminate(info.get("terminal_pid"), TERMINAL_APP_ID)
            _terminate(info.get("wm_pid"), info.get("wm", "") or "\0")
            # Her browser exits with its X server; a new one on the same profile
            # would otherwise open as a tab in the dying one.
            deadline = time.monotonic() + 5
            with contextlib.suppress(Exception):
                while self._browser_processes() and time.monotonic() < deadline:
                    time.sleep(0.1)
        self._forget(info)

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
        # Her apps exit when their X server goes away; the WM, terminal server,
        # VNC server and viewer are ours by recorded pid, so nothing else is
        # ever signalled.
        _terminate(info.get("viewer_pid"), "vncviewer")
        _terminate(info.get("vnc_pid"), "x11vnc")
        _terminate(info.get("terminal_pid"), TERMINAL_APP_ID)
        _terminate(info.get("server_pid") or info.get("xephyr_pid"), "Xvfb" if info.get("server_pid") else "Xephyr")
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
        env = self.shell_env(info)
        if app == "browser":
            self._spawn_browser(info, env, [url or "about:blank"])
        else:
            from core.computer_shell import SESSION, SOCKET

            self._ensure_terminal_server(info, env)
            # The window attaches to the tmux session her shell tool drives, so
            # he sees every command. The client reports a spurious registration
            # error for a custom app id, yet the window opens: ignore its code.
            self._spawn(
                [
                    "gnome-terminal",
                    "--app-id",
                    TERMINAL_APP_ID,
                    "--window",
                    f"--working-directory={Path.home()}",
                    "--",
                    "tmux",
                    "-L",
                    SOCKET,
                    "new-session",
                    "-A",
                    "-s",
                    SESSION,
                ],
                env,
            )
        return {"ok": True, "app": app}

    def _spawn_browser(self, info, env, extra):
        binary = self.browser_binary()
        if not binary:
            raise ComputerError("no Chromium-family browser is installed")
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        width, height = info["size"].split("x")
        self._spawn(
            [
                binary,
                f"--user-data-dir={self.profile}",
                # A loopback DevTools port, written to DevToolsActivePort in her
                # profile; attach checks that the listener is this profile's.
                "--remote-debugging-port=0",
                "--no-first-run",
                "--no-default-browser-check",
                "--start-maximized",
                f"--window-size={width},{height}",
                "--window-position=0,0",
                *extra,
            ],
            env,
        )

    def _browser_processes(self):
        import psutil

        profile = os.path.realpath(self.profile)
        found = []
        for process in psutil.process_iter(["pid", "cmdline"]):
            argv = process.info.get("cmdline") or []
            if any(
                arg.startswith("--user-data-dir=") and os.path.realpath(arg.split("=", 1)[1]) == profile
                for arg in argv
            ) and not any(arg.startswith("--type=") for arg in argv):
                found.append(process)
        return found

    def ensure_debuggable(self):
        """Her Edge running with its automation port; restart it once if not.

        An Edge started before the port existed keeps running without one, and
        a second launch only opens a tab in it. Restarting restores its tabs.
        """
        from core.computer_browser import BrowserError, _active_port, _verify_profile_listener

        info = self.running()
        if not info:
            raise ComputerError("serena's desktop is not running")
        port = _active_port(self.profile / "DevToolsActivePort")
        if port:
            try:
                _verify_profile_listener(port, self.profile)
                return port
            except BrowserError:
                pass
        for process in self._browser_processes():
            with contextlib.suppress(Exception):
                process.terminate()
        deadline = time.monotonic() + 8
        while self._browser_processes() and time.monotonic() < deadline:
            time.sleep(0.1)
        (self.profile / "DevToolsActivePort").unlink(missing_ok=True)
        self._spawn_browser(info, self.shell_env(info), ["--restore-last-session"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            port = _active_port(self.profile / "DevToolsActivePort")
            if port:
                with contextlib.suppress(BrowserError):
                    _verify_profile_listener(port, self.profile)
                    return port
            time.sleep(0.1)
        raise ComputerError("her browser did not open its automation port")

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

    def viewer_window(self):
        with contextlib.suppress(ComputerError, subprocess.SubprocessError):
            found = self._run("xdotool", "search", "--name", VIEWER_TITLE, timeout=2)
            return found.splitlines()[-1] if found else None
        return None

    def _viewer_position(self):
        """His rightmost secondary monitor when there is one, else none."""
        if not self.host_monitors:
            return None
        with contextlib.suppress(Exception):
            monitors = self.host_monitors()
            others = [m for m in monitors if not m.get("primary")] or monitors
            rect = max(others, key=lambda m: m["rect"]["x"])["rect"]
            width, height = self.size
            x = rect["x"] + max(0, (rect["width"] - width) // 2)
            y = rect["y"] + max(0, (rect["height"] - height) // 2)
            return x, y
        return None

    def _open_viewer(self, info):
        binary = next((item for item in VIEWERS if shutil.which(item)), None)
        if not binary:
            raise ComputerError("watching serena's desktop needs a VNC viewer: sudo apt install tigervnc-viewer")
        if not _alive(info.get("vnc_pid"), "x11vnc"):
            self._start_vnc(info)
        width, height = self.size
        position = self._viewer_position()
        geometry = f"{width}x{height}" + (f"+{position[0]}+{position[1]}" if position else "")
        process = self._spawn(
            [
                binary,
                "-Shared=1",
                "-RemoteResize=0",
                # The clipboard bridge carries text both ways; the viewer's own
                # sync would race it.
                "-AcceptClipboard=0",
                "-SendClipboard=0",
                "-SendPrimary=0",
                "-SetPrimary=0",
                "-AlertOnFatalError=0",
                "-ReconnectOnError=0",
                "-geometry",
                geometry,
                info["socket"],
            ],
            self.host_env,
        )
        info["viewer_pid"] = process.pid
        self._write(info)
        window = None
        deadline = time.monotonic() + 5
        while not window:
            if process.poll() is not None or time.monotonic() >= deadline:
                raise ComputerError("serena's desktop viewer did not open")
            time.sleep(0.05)
            window = self.viewer_window()
        return window

    def release_keys(self, names):
        """Release keys his viewer left held on her display.

        His viewer input reaches her display through x11vnc's XTest device, so
        an XTest key-up there releases it. Only called before her own input,
        and only while the server drops his, so nothing he holds is released.
        """
        info = self.running()
        if not info:
            return
        with contextlib.suppress(ComputerError, subprocess.SubprocessError):
            self._run("xdotool", "keyup", *names, env=self.env(info), timeout=2)

    def show(self):
        info = self.running()
        if not info:
            raise ComputerError("serena's desktop is not running")
        window = self.viewer_window() or self._open_viewer(info)
        self._run("xdotool", "windowactivate", window, timeout=2)
        return self.status()

    def hide(self):
        window = self.viewer_window()
        if window:
            self._run("xdotool", "windowminimize", window, timeout=2)
        return self.status()
