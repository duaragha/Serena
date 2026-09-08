"""Desktop I/O for the local computer service. No model or authority lives here."""

from __future__ import annotations

import contextlib
import ctypes
import os
import re
import select
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from functools import lru_cache


class ComputerError(RuntimeError):
    pass


class ComputerTransientError(ComputerError):
    """The foreground changed during capture; retry with a fresh screen check."""


@lru_cache(maxsize=1)
def legacy_unicode_keys():
    """Old X11 clients need legacy Greek/Cyrillic keysyms, not Uxxxx aliases."""
    result = {}
    try:
        xkb = ctypes.CDLL("libxkbcommon.so.0")
        to_unicode = xkb.xkb_keysym_to_utf32
        to_unicode.argtypes = [ctypes.c_uint32]
        to_unicode.restype = ctypes.c_uint32
        x11 = ctypes.CDLL("libX11.so.6")
        to_name = x11.XKeysymToString
        to_name.argtypes = [ctypes.c_ulong]
        to_name.restype = ctypes.c_char_p
        for keysym in range(0x100, 0x3000):
            codepoint = to_unicode(keysym)
            name = to_name(keysym) if codepoint > 255 else None
            if name:
                result.setdefault(chr(codepoint), name.decode("ascii"))
    except (OSError, AttributeError):
        pass
    return result


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def contains(self, x, y):
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height

    def box(self):
        return self.x, self.y, self.x + self.width, self.y + self.height

    def to_dict(self):
        return asdict(self)


def desktop_environment():
    env = os.environ.copy()
    if os.name != "nt" and not env.get("DISPLAY"):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            result = subprocess.run(
                ["systemctl", "--user", "show-environment"],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
            for line in result.stdout.splitlines():
                key, _, value = line.partition("=")
                if key in {
                    "DISPLAY",
                    "XAUTHORITY",
                    "XDG_SESSION_TYPE",
                    "WAYLAND_DISPLAY",
                    "DBUS_SESSION_BUS_ADDRESS",
                }:
                    env[key] = value
    return env


class X11Desktop:
    name = "x11"

    def __init__(self):
        from Xlib import display

        self.env = desktop_environment()
        if self.env.get("XDG_SESSION_TYPE") == "wayland":
            raise ComputerError(
                "Wayland requires a portal adapter; XWayland is not full desktop access"
            )
        if not self.env.get("DISPLAY"):
            raise ComputerError("no graphical X11 session; start the service in your desktop login")
        if not shutil.which("xdotool") or not shutil.which("xinput"):
            raise ComputerError("computer use needs xdotool and xinput installed")
        # Xlib reads XAUTHORITY itself, once at connection creation.
        for key in ("DISPLAY", "XAUTHORITY"):
            if self.env.get(key):
                os.environ[key] = self.env[key]
        self.display = display.Display(self.env["DISPLAY"])
        if not self.display.has_extension("XTEST"):
            raise ComputerError("this X server does not support XTEST input")
        self.root = self.display.screen().root
        self.lock = threading.RLock()
        self.held_keys = set()
        self.held_buttons = set()
        self.monitor_process = None
        self.monitor_stop = threading.Event()
        self.monitor_ready = threading.Event()
        self.monitor_error = ""
        self.shortcut_ready = threading.Event()
        self.shortcut_error = ""

    def _run(self, *args, timeout=3, input=None):
        result = subprocess.run(
            args, env=self.env, text=True, input=input, capture_output=True, timeout=timeout
        )
        if result.returncode:
            raise ComputerError(f"{args[0]} failed: {result.stderr.strip()[:180]}")
        return result.stdout.strip()

    def monitors(self):
        result = []
        for line in self._run("xrandr", "--listmonitors").splitlines()[1:]:
            match = re.search(r"(\d+)/\d+x(\d+)/\d+([+-]\d+)([+-]\d+)\s+(\S+)", line)
            if match:
                w, h, x, y, name = match.groups()
                result.append(
                    {
                        "name": name,
                        "primary": "*" in line,
                        "rect": Rect(int(x), int(y), int(w), int(h)).to_dict(),
                    }
                )
        if not result:
            raise ComputerError("no active monitors")
        return result

    def active_window(self):
        with self.lock:
            prop = self.root.get_full_property(self.display.intern_atom("_NET_ACTIVE_WINDOW"), 0)
            return str(int(prop.value[0])) if prop is not None and len(prop.value) else ""

    def window_info(self, window_id):
        with self.lock:
            try:
                window = self.display.create_resource_object("window", int(window_id))
                geometry = window.get_geometry()
                origin = self.root.translate_coords(window, 0, 0)
                attrs = window.get_attributes()
                prop = window.get_full_property(self.display.intern_atom("_NET_WM_NAME"), 0)
                title = (
                    prop.value.decode("utf-8", errors="replace")
                    if prop is not None
                    else window.get_wm_name() or ""
                )
                app = " ".join(window.get_wm_class() or ())
                return {
                    "id": str(window_id),
                    "title": str(title)[:300],
                    "app": app[:200],
                    "visible": attrs.map_state == 2,
                    "rect": Rect(origin.x, origin.y, geometry.width, geometry.height).to_dict(),
                }
            except Exception as exc:
                raise ComputerError("target window is no longer available") from exc

    def context(self):
        window = self.active_window()
        if not window:
            return {"id": "", "title": "", "app": "", "visible": True}
        try:
            return self.window_info(window)
        except ComputerError as exc:
            raise ComputerTransientError("foreground window changed while inspecting it") from exc

    def visible_windows(self):
        with self.lock:
            prop = self.root.get_full_property(
                self.display.intern_atom("_NET_CLIENT_LIST_STACKING"), 0
            )
            if prop is None:
                raise ComputerError("cannot inspect visible windows")
            result = []
            for identifier in prop.value:
                with contextlib.suppress(ComputerError):
                    info = self.window_info(str(identifier))
                    if info["visible"]:
                        result.append(info)
            return result

    def window_in_scope(self, window_id, target_id):
        """A modal dialog belongs to its selected parent, not every app window."""
        with self.lock:
            for _ in range(8):
                if str(window_id) == str(target_id):
                    return True
                try:
                    window = self.display.create_resource_object("window", int(window_id))
                    parent = window.get_wm_transient_for()
                    if not parent:
                        return False
                    window_id = parent.id
                except Exception:
                    return False
        return False

    def locked(self):
        # Fail closed if the current desktop lock cannot be queried.
        for name, path, interface in [
            (
                "org.cinnamon.ScreenSaver",
                "/org/cinnamon/ScreenSaver",
                "org.cinnamon.ScreenSaver.GetActive",
            ),
            (
                "org.freedesktop.ScreenSaver",
                "/ScreenSaver",
                "org.freedesktop.ScreenSaver.GetActive",
            ),
        ]:
            try:
                answer = self._run(
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    name,
                    "--object-path",
                    path,
                    "--method",
                    interface,
                    timeout=1,
                )
                if "true" in answer:
                    return True
                if "false" in answer:
                    return False
            except (OSError, subprocess.SubprocessError, ComputerError):
                continue
        raise ComputerError("cannot verify desktop lock state")

    def capture(self, rect):
        from PIL import ImageGrab

        return ImageGrab.grab(bbox=rect.box(), xdisplay=self.env["DISPLAY"])

    def move(self, x, y):
        from Xlib import X
        from Xlib.ext import xtest

        with self.lock:
            xtest.fake_input(self.display, X.MotionNotify, x=int(x), y=int(y))
            self.display.sync()

    def button(self, button, down):
        from Xlib import X
        from Xlib.ext import xtest

        with self.lock:
            xtest.fake_input(
                self.display, X.ButtonPress if down else X.ButtonRelease, detail=button
            )
            (self.held_buttons.add if down else self.held_buttons.discard)(button)
            self.display.sync()

    def key(self, name, down):
        from Xlib import XK, X
        from Xlib.ext import xtest

        aliases = {
            "CTRL": "Control_L",
            "CONTROL": "Control_L",
            "ALT": "Alt_L",
            "SHIFT": "Shift_L",
            "SUPER": "Super_L",
            "META": "Super_L",
            "WIN": "Super_L",
            "ENTER": "Return",
            "ESC": "Escape",
            "ESCAPE": "Escape",
            "BACKSPACE": "BackSpace",
            "TAB": "Tab",
            "DELETE": "Delete",
            "SPACE": "space",
            "UP": "Up",
            "DOWN": "Down",
            "LEFT": "Left",
            "RIGHT": "Right",
            "HOME": "Home",
            "END": "End",
            "PAGEUP": "Prior",
            "PAGEDOWN": "Next",
        }
        keysym = XK.string_to_keysym(aliases.get(name.upper(), name))
        with self.lock:
            code = self.display.keysym_to_keycode(keysym)
            if not code:
                raise ComputerError(f"unsupported key: {name}")
            xtest.fake_input(self.display, X.KeyPress if down else X.KeyRelease, detail=code)
            (self.held_keys.add if down else self.held_keys.discard)(code)
            self.display.sync()

    def type_text(self, text, cancelled):
        # Short complete chunks bound takeover latency without killing xdotool
        # between a synthetic key-down and its key-up. stdin hides text from ps.
        legacy = legacy_unicode_keys() if any(ord(char) > 255 for char in text) else {}
        chunks = re.findall(r"[\x00-\x7f]{1,8}|[^\x00-\x7f]", text)
        for chunk in chunks:
            if cancelled():
                raise ComputerError("text entry interrupted; some characters may have been typed")
            if chunk in legacy:
                name = legacy[chunk]
                if chunk.isupper() and chunk.lower() in legacy:
                    name = "shift+" + legacy[chunk.lower()]
                self._run("xdotool", "key", "--delay", "12", name, timeout=2)
            else:
                delay = "1" if chunk.isascii() else "12"
                self._run(
                    "xdotool", "type", "--delay", delay, "--file", "-", input=chunk, timeout=2
                )
            # Give clients time to consume MappingNotify before xdotool reuses
            # its temporary Unicode keymap for the following chunk.
            if any(ord(char) > 127 for char in chunk):
                time.sleep(0.025)

    def release(self):
        from Xlib import X
        from Xlib.ext import xtest

        with self.lock:
            for code in tuple(self.held_keys):
                xtest.fake_input(self.display, X.KeyRelease, detail=code)
            for button in tuple(self.held_buttons):
                xtest.fake_input(self.display, X.ButtonRelease, detail=button)
            self.held_keys.clear()
            self.held_buttons.clear()
            self.display.sync()

    def start_input_monitor(self, on_input, on_stop):
        """XI2 identifies XTEST separately, so our input never counts as takeover."""

        def watch():
            try:
                text = self._run("xinput", "--list", "--short")
                synthetic = {
                    int(m.group(1))
                    for line in text.splitlines()
                    if "XTEST" in line
                    for m in [re.search(r"id=(\d+)", line)]
                    if m
                }
                if len(synthetic) < 2:
                    raise ComputerError("cannot identify synthetic input devices")
                self.monitor_process = subprocess.Popen(
                    ["stdbuf", "-oL", "xinput", "test-xi2", "--root"],
                    env=self.env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                kind = ""
                self.monitor_ready.set()
                for line in self.monitor_process.stdout:
                    if self.monitor_stop.is_set():
                        break
                    if line.startswith("EVENT"):
                        kind = line
                        if "HierarchyChanged" in kind:
                            on_stop("input devices changed; start a new session")
                    elif "device:" in line and any(
                        name in kind for name in ("RawMotion", "RawKeyPress", "RawButtonPress")
                    ):
                        match = re.search(r"\((\d+)\)", line)
                        if match and int(match.group(1)) not in synthetic:
                            on_input()
                if not self.monitor_stop.is_set():
                    raise ComputerError("physical input monitor disconnected")
            except Exception as exc:
                self.monitor_error = str(exc)
                self.monitor_ready.set()
                on_stop("input monitor unavailable")

        threading.Thread(target=watch, name="computer-physical-input", daemon=True).start()
        self.monitor_ready.wait(3)
        if self.monitor_error or not self.monitor_ready.is_set():
            raise ComputerError(self.monitor_error or "input monitor did not start")
        threading.Thread(
            target=self._stop_shortcut, args=(on_stop,), name="computer-stop-key", daemon=True
        ).start()
        if not self.shortcut_ready.wait(3) or self.shortcut_error:
            raise ComputerError(self.shortcut_error or "stop shortcut did not register")

    def _stop_shortcut(self, on_stop):
        from Xlib import XK, X, display
        from Xlib.error import CatchError

        connection = display.Display(self.env["DISPLAY"])
        root = connection.screen().root
        key = connection.keysym_to_keycode(XK.string_to_keysym("Escape"))
        for extra in (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask):
            error = CatchError()
            root.grab_key(
                key,
                X.ControlMask | X.Mod1Mask | X.ShiftMask | extra,
                False,
                X.GrabModeAsync,
                X.GrabModeAsync,
                onerror=error,
            )
            connection.sync()
            if error.get_error():
                self.shortcut_error = "Ctrl+Alt+Shift+Escape is already in use"
                self.shortcut_ready.set()
                connection.close()
                return
        self.shortcut_ready.set()
        try:
            while not self.monitor_stop.is_set():
                if (
                    connection.pending_events()
                    or select.select([connection.fileno()], [], [], 0.05)[0]
                ):
                    event = connection.next_event()
                    if event.type == X.KeyPress:
                        on_stop("Ctrl+Alt+Shift+Escape")
        finally:
            connection.close()

    def close(self):
        self.monitor_stop.set()
        if self.monitor_process:
            self.monitor_process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.monitor_process.wait(timeout=2)
        self.release()
        self.display.close()


def create_desktop():
    if os.name == "nt":
        raise ComputerError(
            "native Windows computer service is not installed; use the laptop device"
        )
    return X11Desktop()
