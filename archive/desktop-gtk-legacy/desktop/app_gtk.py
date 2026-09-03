"""Native GTK shell for Linux.

The Code pane is a native Vte.Terminal positioned over the WebView via
GtkOverlay. The web UI still owns the rest of the application.

Each session gets its own VTE in a GtkStack, so switching chats hides the
current terminal but keeps the claude process running. Terminals are killed
only when the app closes or claude exits on its own.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

# Flask serves from a sibling thread and resolves the live GTK owner through
# this canonical module name. Keep the alias valid when launched as a script.
sys.modules.setdefault("desktop.app_gtk", sys.modules[__name__])


# New GTK processes on this machine can lose all keyboard input when routed
# through ibus: WebKit inputs focus, but text never lands, and GTK logs
# "Unable to connect to ibus" / event queue drops. Force the simpler X input
# path before gi/Gtk initialize.
if os.environ.get("SERENA_USE_IBUS", "").lower() not in ("1", "true", "yes"):
    if os.environ.get("GTK_IM_MODULE") in (None, "", "ibus"):
        os.environ["GTK_IM_MODULE"] = "xim"
    if os.environ.get("QT_IM_MODULE") in (None, "", "ibus"):
        os.environ["QT_IM_MODULE"] = "xim"
    if os.environ.get("XMODIFIERS") in (None, "", "@im=ibus"):
        os.environ["XMODIFIERS"] = "@im=none"


# === LOGGING === Tee stdout/stderr to a file so when Serena is launched via
# .desktop / Start Menu (stdout/stderr → /dev/null), we don't lose every
# print() + traceback. Mirrors the Windows logging we added earlier.
def _install_file_logging() -> Path:
    data_dir = Path(
        os.environ.get("CHATS_DATA_DIR", Path.home() / ".local" / "share" / "chats")
    ).expanduser()
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "serena.log"
    try:
        if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
            backup = log_dir / "serena.log.1"
            if backup.exists():
                backup.unlink()
            log_path.rename(backup)
    except OSError:
        pass
    try:
        has_console = bool(sys.stdout and hasattr(sys.stdout, "isatty") and sys.stdout.isatty())
    except (OSError, ValueError):
        has_console = False
    log_handle = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    log_handle.write(f"\n=== Serena launched {time.strftime('%Y-%m-%d %H:%M:%S')} (console={has_console}) ===\n")

    class _Tee:
        def __init__(self, *streams):
            self._streams = [s for s in streams if s is not None]
        def write(self, data):
            for s in self._streams:
                try:
                    s.write(data); s.flush()
                except Exception:
                    pass
        def flush(self):
            for s in self._streams:
                try:
                    s.flush()
                except Exception:
                    pass
        def isatty(self):
            return False

    if has_console:
        sys.stdout = _Tee(sys.__stdout__, log_handle)
        sys.stderr = _Tee(sys.__stderr__, log_handle)
    else:
        sys.stdout = log_handle
        sys.stderr = log_handle

    def _hook(exc_type, exc, tb):
        print("\n*** UNHANDLED EXCEPTION ***", file=sys.stderr, flush=True)
        traceback.print_exception(exc_type, exc, tb, file=sys.stderr)
        sys.stderr.flush()
    sys.excepthook = _hook
    return log_path

# Logging is installed from run(), NOT at import time: headless scripts and
# CLI helpers import this module too (e.g. core.linked_sessions resolving the
# visible split), and swapping their sys.stdout for the log file silently
# swallows their output.
_LOG_PATH: Path | None = None
# === LOGGING END ===

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
gi.require_version("WebKit2", "4.1")
gi.require_version("Gdk", "3.0")

from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte, WebKit2  # noqa: E402

# PCRE2 compile flags used by VTE.Regex
_PCRE2_MULTILINE = 0x00000400
_PCRE2_UTF = 0x00080000
_PCRE2_CASELESS = 0x00000008

# A resumed CLI paints its transcript over several frames. Follow that initial
# redraw to the newest line, then restore normal scrollback behavior.
_VTE_TAIL_FOLLOW_MS = 6000

# URL regex — simplified version of what gnome-terminal uses. Catches http(s),
# ftp, file, ssh, mailto, plus plain www.*.
_URL_REGEX = (
    r"(?:(?:(?:https?|ftp|file|ssh|telnet)://)"
    r"|(?:mailto:)"
    r"|(?:www\.))"
    r"[-[:alnum:]\\Q,?;.:/!*%$^&#~=+@|()\\E]*"
    r"[-[:alnum:]\\Q/!*%$^&#~=+@|()\\E]"
)

_EXTERNAL_URI_SCHEMES = frozenset({"http", "https"})


def _normalize_external_uri(value: object) -> str | None:
    """Return a browser-safe external URI accepted from the web renderer."""
    if not isinstance(value, str):
        return None
    uri = value.strip()
    if not uri or any(char.isspace() for char in uri):
        return None
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return None
    if parsed.scheme.casefold() not in _EXTERNAL_URI_SCHEMES or not parsed.netloc:
        return None
    return uri

from core.indexer import get_session, update_index, update_knowledge_index  # noqa: E402
from core.config import ensure_session_visible, resolve_session_cwd  # noqa: E402
from core.runtime_activity import TurnActivityReader  # noqa: E402
from core import metadata as meta_sync  # noqa: E402
from ui.web import app as flask_app  # noqa: E402


# ---------------------------------------------------------------------------
# Flask plumbing
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return True
        except Exception:
            time.sleep(0.1)
    return False


def _serve(host: str, port: int) -> None:
    flask_app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


# ---------------------------------------------------------------------------
# GTK window
# ---------------------------------------------------------------------------

def _terminal_backend(value: str | None = None) -> str:
    """Resolve the single terminal surface owned by this desktop process.

    The renderer is the product default. Native VTE remains an explicit
    recovery switch while the old overlay implementation is retired, never a
    second simultaneously active surface.
    """

    raw = str(
        value
        if value is not None
        else os.environ.get("SERENA_TERMINAL_BACKEND", "renderer")
    ).strip().casefold()
    aliases = {
        "renderer": "renderer",
        "xterm": "renderer",
        "web": "renderer",
        "vte": "vte",
        "native": "vte",
    }
    try:
        return aliases[raw]
    except KeyError as error:
        raise RuntimeError(
            "SERENA_TERMINAL_BACKEND must be renderer or vte"
        ) from error


_TERMINAL_BACKEND = _terminal_backend()
_USE_NATIVE_VTE = _TERMINAL_BACKEND == "vte"

BOOT_SCRIPT = (
    f"window.__terminalBackend = {json.dumps(_TERMINAL_BACKEND)};\n"
    f"window.__nativeTerminalBridge = {json.dumps(_USE_NATIVE_VTE)};\n"
) + r"""
window.__gtkBridge = true;
function markGtkShell() {
  if (document.documentElement) document.documentElement.classList.add('gtk-shell');
}
markGtkShell();
document.addEventListener('DOMContentLoaded', markGtkShell, { once: true });
window.gtkSend = function(obj) {
  try {
    window.webkit.messageHandlers.gtkbridge.postMessage(JSON.stringify(obj));
  } catch (e) { /* ignore */ }
};
"""

_FG = Gdk.RGBA()
_FG.parse("#c9d1d9")
_BG = Gdk.RGBA()
_BG.parse("#000000")
_CURSOR = Gdk.RGBA()
_CURSOR.parse("#3fb950")


# ---------------------------------------------------------------------------
# Keybindings — shared loader lives in core/keybindings.py so the JS frontend
# (Windows/macOS) can read the same ~/.config/serena/keybindings.json file.
# ---------------------------------------------------------------------------

from core.keybindings import load_combos as _load_combos  # noqa: E402

_MODIFIER_MAP = {
    "alt":   Gdk.ModifierType.MOD1_MASK,
    "ctrl":  Gdk.ModifierType.CONTROL_MASK,
    "shift": Gdk.ModifierType.SHIFT_MASK,
    "super": Gdk.ModifierType.SUPER_MASK,
    "meta":  Gdk.ModifierType.META_MASK,
}

_RELEVANT_MASKS = (
    Gdk.ModifierType.CONTROL_MASK
    | Gdk.ModifierType.SHIFT_MASK
    | Gdk.ModifierType.MOD1_MASK
    | Gdk.ModifierType.SUPER_MASK
    | Gdk.ModifierType.META_MASK
)


def _parse_shortcut(s: str) -> tuple[int, int] | None:
    """Parse 'Alt+d' / 'Ctrl+Shift+X' / 'Alt+Delete' into (keyval, modmask)."""
    if not s:
        return None
    parts = [p.strip() for p in re.split(r"\+", s) if p.strip()]
    if not parts:
        return None
    mods = 0
    for mod in parts[:-1]:
        m = _MODIFIER_MAP.get(mod.lower())
        if m is None:
            return None
        mods |= int(m)
    key_name = parts[-1]
    if len(key_name) == 1:
        key_name = key_name.lower()
    keyval = Gdk.keyval_from_name(key_name)
    if keyval == 0 or keyval is None:
        return None
    return keyval, mods


def _load_keybindings() -> dict[str, tuple[int, int]]:
    """Convert the shared combo strings into GTK (keyval, modmask) tuples."""
    parsed: dict[str, tuple[int, int]] = {}
    for action, combo in _load_combos().items():
        pk = _parse_shortcut(combo)
        if pk is not None:
            parsed[action] = pk
        elif combo:
            print(f"[keybindings] ignoring invalid binding for {action!r}: {combo!r}", flush=True)
    return parsed


class ChatsApp(Gtk.Window):
    INSTANCE: "ChatsApp | None" = None  # singleton handle for cross-thread bridges

    def __init__(self, url: str, width: int, height: int):
        super().__init__(title="Chats")
        ChatsApp.INSTANCE = self
        self._use_native_vte = _USE_NATIVE_VTE
        self.set_default_size(width, height)
        self.connect("destroy", self._on_destroy)
        self.connect("key-press-event", self._on_key_press)
        self.connect("focus-in-event", self._on_focus_in)

        # (keyval, modmask) → action. Customizable via ~/.config/serena/keybindings.json
        self._shortcut_map: dict[tuple[int, int], str] = {}
        for action, (keyval, mods) in _load_keybindings().items():
            self._shortcut_map[(keyval, mods)] = action

        # Per-session input draft buffer — tracks chars user typed since the last
        # Enter so Ctrl+A can copy the in-progress prompt to the clipboard
        # without forcing the user to mouse-select. Keeping this keyed by sid
        # preserves drafts across a cold runtime resume and VTE replacement.
        self._input_drafts: dict[str, str] = {}

        self.overlay = Gtk.Overlay()
        self.add(self.overlay)

        # Base: WebView
        self.web = WebKit2.WebView()
        settings = self.web.get_settings()
        settings.set_enable_developer_extras(True)
        settings.set_javascript_can_access_clipboard(True)
        settings.set_enable_write_console_messages_to_stdout(True)
        self.overlay.add(self.web)
        self.web.set_can_focus(True)
        self.web.set_hexpand(True)
        self.web.set_vexpand(True)


        # JS -> Python bridge
        cm = self.web.get_user_content_manager()
        cm.register_script_message_handler("gtkbridge")
        cm.connect("script-message-received::gtkbridge", self._on_js_message)
        cm.add_script(
            WebKit2.UserScript.new(
                BOOT_SCRIPT,
                WebKit2.UserContentInjectedFrames.TOP_FRAME,
                WebKit2.UserScriptInjectionTime.START,
                None,
                None,
            )
        )
        self.web.load_uri(url)

        # Overlay child: a Stack of per-session VTEs. Always mapped — we keep a
        # zero-sized placeholder as the "nothing showing" state so the stack never
        # goes through an unmap → map cycle on swap (that's the main source of
        # perceived slowness when switching between running sessions).
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self._stack.set_homogeneous(False)
        self._placeholder = Gtk.Box()
        self._stack.add_named(self._placeholder, "__blank__")
        self._stack.set_visible_child_name("__blank__")
        if self._use_native_vte:
            self.overlay.add_overlay(self._stack)
            self.overlay.set_overlay_pass_through(self._stack, False)
            self.overlay.connect("get-child-position", self._on_overlay_position)
        else:
            # Keep the recovery implementation constructible, but do not put
            # native widgets in the live scene graph. An empty Gtk overlay was
            # still capable of painting or intercepting above Fleet after a
            # WebKit navigation, which violated renderer ownership.
            self._stack.set_no_show_all(True)
        self._stack_hidden = True  # tracked logically via rect, not via widget visibility
        # Top-level web tabs can hide the Chats DOM while a native terminal is
        # still running. Keep that visibility separate from the terminal
        # lifecycle so leaving Chats unmaps the overlays without resizing VTEs.
        self._terminal_view_visible: bool = True

        # === SPLIT FEATURE ===
        # Second overlay child: a Paned that hosts two reparented VTEs side-by-side
        # for linked-thread split view. Active only when self._split_active is True;
        # otherwise it collapses to zero area like the stack does when hidden.
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.set_wide_handle(True)
        self._paned.get_style_context().add_class("serena-split-paned")
        if self._use_native_vte:
            self.overlay.add_overlay(self._paned)
            self.overlay.set_overlay_pass_through(self._paned, False)
        else:
            self._paned.set_no_show_all(True)
        # Re-apply ratio every time the paned re-lays-out — catches initial
        # mount allocation that lands a frame after we set position
        self._paned.connect("size-allocate", self._on_paned_allocate)
        self._split_active: bool = False
        self._split_sids: tuple[str | None, str | None] = (None, None)
        # Store the divider as a RATIO of total width (0..1) rather than fixed
        # pixels — when the outer rect resizes (chat-list divider, window edge,
        # files-pane toggle), the split should preserve its share of the conv
        # area instead of crushing one side.
        self._split_ratio: float = 0.5
        self._split_pos_settling: bool = False  # ignore notify::position spam during set
        self._split_spawn_pending: set[str] = set()
        # Style the paned handle to match the web pane-divider look:
        # 5px neutral by default, brighter on hover, green during drag.
        _split_css = (
            b"paned.serena-split-paned > separator {"
            b"  background-color: #30363d;"
            b"  background-image: none;"
            b"  min-width: 5px;"
            b"  min-height: 5px;"
            b"  border: none;"
            b"  transition: background-color 120ms ease;"
            b"}"
            b"paned.serena-split-paned > separator:hover {"
            b"  background-color: #6e7681;"
            b"}"
            b"paned.serena-split-paned > separator:active,"
            b"paned.serena-split-paned > separator:checked {"
            b"  background-color: #3fb950;"
            b"}"
            b".serena-runtime-asleep { opacity: 0.68; }"
            b".serena-runtime-paused { opacity: 0.76; }"
            b".serena-runtime-waking { opacity: 0.82; }"
        )
        try:
            _provider = Gtk.CssProvider()
            _provider.load_from_data(_split_css)
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(),
                _provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        except Exception as e:
            print(f"[split] css load failed: {e}", flush=True)
        # === SPLIT FEATURE END ===

        self._vte_rect: tuple[int, int, int, int] | None = None
        self._vtes: dict[str, Vte.Terminal] = {}
        self._vte_pids: dict[str, int] = {}
        self._sid_agent: dict[str, str] = {}
        self._runtime_cwd: dict[str, str] = {}
        self._runtime_state: dict[str, str] = {}
        self._runtime_last_activity: dict[str, float] = {}
        self._runtime_last_output: dict[str, float] = {}
        self._vte_tail_follow_until: dict[str, float] = {}
        self._vte_tail_scroll_pending: set[str] = set()
        self._runtime_file_paths: dict[str, str] = {}
        self._runtime_activity_reader = TurnActivityReader()
        self._runtime_started_at: dict[str, float] = {}
        self._runtime_busy: set[str] = set()
        self._runtime_turn_offsets: dict[str, int] = {}
        self._runtime_stop_reasons: dict[str, str] = {}
        self._runtime_sleep_after_wake: dict[str, str] = {}
        self._runtime_wake_after_stop: set[str] = set()
        self._runtime_reclaimed: set[str] = set()
        self._runtime_closed_vtes: set[int] = set()
        self._runtime_leases: dict[str, int] = {}
        self._runtime_work_reservations: dict[str, str] = {}
        self._runtime_lock = threading.Lock()
        self._runtime_focus_sid: str | None = None
        self._runtime_exclusive_generation = 0
        self._split_pinned = False
        try:
            self._runtime_idle_seconds = max(
                60, int(os.environ.get("SERENA_RUNTIME_IDLE_SECONDS", "600"))
            )
        except ValueError:
            self._runtime_idle_seconds = 600
        try:
            self._runtime_prewarm_seconds = max(
                0.5, float(os.environ.get("SERENA_RUNTIME_PREWARM_SECONDS", "5"))
            )
        except ValueError:
            self._runtime_prewarm_seconds = 5.0
        self._usage_popover: Gtk.Popover | None = None
        self._usage_popover_hide_source = 0
        self._font = self._pick_mono_font()
        self._runtime_sweep_source = (
            GLib.timeout_add_seconds(30, self._sweep_runtime_idle)
            if self._use_native_vte
            else 0
        )
        self._voice_inbox_store = None
        self._voice_inbox_source = 0
        self._voice_inbox_inflight: tuple[str, str] | None = None
        # The resident supervisor is the sole voice-work executor. A desktop
        # fallback used to claim jobs and spawn an unpinned Codex pane in the
        # currently visible directory, bypassing the accepted brief and Git
        # baseline. Keep these fields only for compatibility with old windows.

    # ------------------------------------------------------------------
    # Font picker
    # ------------------------------------------------------------------
    @staticmethod
    def _pick_mono_font():
        from gi.repository import Pango
        import subprocess
        try:
            out = subprocess.check_output(["fc-list", ":", "family"], text=True, timeout=2)
            installed = {line.strip().split(",")[0] for line in out.splitlines() if line.strip()}
        except Exception:
            installed = set()

        for fam in ("JetBrains Mono", "Fira Code", "Cascadia Code",
                    "Ubuntu Mono", "DejaVu Sans Mono", "Liberation Mono"):
            if fam in installed:
                return Pango.FontDescription.from_string(f"{fam} 11")
        return Pango.FontDescription.from_string("Monospace 11")

    # ------------------------------------------------------------------
    # Overlay positioning
    # ------------------------------------------------------------------
    def _on_overlay_position(self, overlay, child, allocation):
        # Stack: shows when not split-active and not stack-hidden.
        if child == self._stack:
            if (
                self._split_active
                or self._stack_hidden
                or self._vte_rect is None
            ):
                allocation.x = 0
                allocation.y = 0
                allocation.width = 0
                allocation.height = 0
                return True
            x, y, w, h = self._vte_rect
            width = max(int(w), 10)
            allocation.x = int(x) if self._terminal_view_visible else -(width + 32)
            allocation.y = int(y)
            allocation.width = width
            allocation.height = max(int(h), 10)
            return True
        # === SPLIT FEATURE === (paned: visible only when split is active)
        if child == self._paned:
            if (
                not self._split_active
                or self._vte_rect is None
            ):
                allocation.x = 0
                allocation.y = 0
                allocation.width = 0
                allocation.height = 0
                return True
            x, y, w, h = self._vte_rect
            width = max(int(w), 10)
            allocation.x = int(x) if self._terminal_view_visible else -(width + 32)
            allocation.y = int(y)
            allocation.width = width
            allocation.height = max(int(h), 10)
            return True
        # === SPLIT FEATURE END ===
        return False

    def _set_rect(self, rect: dict | None) -> bool:
        if not rect:
            return False
        try:
            new_rect = (
                int(rect.get("x", 0)),
                int(rect.get("y", 0)),
                int(rect.get("w", 800)),
                int(rect.get("h", 600)),
            )
        except (TypeError, ValueError):
            return False
        # A display:none DOM node reports 0x0. Never turn that transient web
        # geometry into a native VTE/Paned allocation: doing so sends a 1-column
        # WINCH to both agents and can corrupt the saved divider ratio.
        if new_rect[2] <= 10 or new_rect[3] <= 10:
            return False
        size_changed = (self._vte_rect is None) or (
            self._vte_rect[2] != new_rect[2] or self._vte_rect[3] != new_rect[3]
        )
        self._vte_rect = new_rect
        self.overlay.queue_resize()
        # === SPLIT FEATURE === Defer the divider correction to the Paned's real
        # size-allocate. Calling set_position with the *new* rect while GTK still
        # has the old/small allocation is what crushed the right-hand pane.
        if size_changed and self._split_active:
            self._split_pos_settling = True
        # === SPLIT FEATURE END ===
        return True

    def _redraw_terminal_underlay(self, rect: tuple[int, int, int, int] | None) -> None:
        """Damage the WebKit area exposed after parking a native terminal."""
        if rect is None:
            return
        x, y, width, height = rect
        width = max(int(width), 1)
        height = max(int(height), 1)
        for widget in (self.web, self.overlay):
            try:
                widget.queue_draw_area(int(x), int(y), width, height)
            except (AttributeError, RuntimeError):
                pass
        try:
            damage = Gdk.Rectangle()
            damage.x = int(x)
            damage.y = int(y)
            damage.width = width
            damage.height = height
            window = self.web.get_window()
            if window is not None:
                window.invalidate_rect(damage, True)
            Gdk.flush()
        except (AttributeError, RuntimeError):
            pass

    def _set_terminal_view_visible(
        self, visible: bool, rect: dict | None = None
    ) -> None:
        """Hide native overlays outside Chats without changing VTE geometry."""
        # Unmapping a VTE in place above accelerated WebKit can leave its last
        # black backing surface frozen over newly exposed web content. Explicit
        # underlay damage clears that surface. A hidden-tab allocation is also
        # parked offscreen, so any late lifecycle show() remains unable to cover
        # WebKit. Width and height stay cached for PTY and Paned restoration.
        if visible and rect is not None and not self._set_rect(rect):
            return

        old_rect = self._vte_rect
        visible = bool(visible)
        self._terminal_view_visible = visible
        for widget in (self._stack, self._paned):
            widget.set_opacity(1.0)
            widget.set_sensitive(visible)
            widget.set_child_visible(True)
            self.overlay.set_overlay_pass_through(widget, not visible)
            if visible:
                widget.show()
            else:
                widget.hide()

        self.overlay.queue_resize()
        self.overlay.queue_draw()

        if not visible:
            # Never let a hidden terminal keep keyboard focus over Fleet,
            # Memory, or Knowledge.
            try:
                self.web.grab_focus()
            except (AttributeError, RuntimeError):
                pass
            self._redraw_terminal_underlay(old_rect)

            def _redraw_after_parking() -> bool:
                if not self._terminal_view_visible:
                    self._redraw_terminal_underlay(old_rect)
                return False

            GLib.idle_add(_redraw_after_parking)
        else:
            # The cached allocation comes back at the current DOM rect. Repaint
            # the VTEs after the compositor has moved their native surfaces.
            for sid in self._split_sids if self._split_active else (
                self._stack.get_visible_child_name(),
            ):
                vte = self._vtes.get(sid) if sid else None
                if vte is not None:
                    vte.queue_draw()
            # Geometry never moved, but an explicit repaint/WINCH makes VTE
            # content return on the first frame instead of after new output.
            if self._split_active:
                self._apply_split_ratio()
                self._kick_split_winch()
            focus_sid = self._runtime_focus_sid
            focus_vte = self._vtes.get(focus_sid) if focus_sid else None
            if focus_vte is not None:
                self._queue_vte_focus(focus_vte)

        if self._split_active:
            children = self._paned.get_children()
            widths = ",".join(str(child.get_allocated_width()) for child in children)
            print(
                f"[split] tab {'visible' if visible else 'hidden'} "
                f"mapped={self._paned.get_mapped()} "
                f"x={self._paned.get_allocation().x} "
                f"alloc={self._paned.get_allocated_width()} "
                f"pos={self._paned.get_position()} children={widths} "
                f"ratio={self._split_ratio:.2f}",
                flush=True,
            )

    def _queue_vte_focus(self, vte: Vte.Terminal | None) -> None:
        """Focus a terminal only if Chats still owns the native overlay."""
        if vte is None:
            return

        def _focus_if_visible() -> bool:
            if self._terminal_view_visible and vte.get_mapped():
                vte.grab_focus()
            return False

        GLib.idle_add(_focus_if_visible)

    # ------------------------------------------------------------------
    # Usage popover
    # ------------------------------------------------------------------
    def _ensure_usage_popover(self) -> Gtk.Popover:
        if self._usage_popover is not None:
            return self._usage_popover

        css = b"""
        popover.serena-usage-popover,
        popover.serena-usage-popover > contents {
            background-color: #120d12;
            border: 1px solid rgba(224, 123, 168, 0.22);
            border-radius: 8px;
            box-shadow: 0 14px 40px rgba(0, 0, 0, 0.55);
        }
        .serena-usage-body {
            padding: 10px;
            font-family: "JetBrains Mono", "Fira Code", monospace;
            font-size: 11px;
            color: #f4eef5;
        }
        .serena-usage-card {
            background-color: #181219;
            border: 1px solid rgba(255, 255, 255, 0.13);
            border-radius: 6px;
        }
        .serena-usage-card.stale { opacity: 0.72; }
        .serena-usage-name { font-weight: 700; }
        .serena-usage-name.claude { color: #ff9f72; }
        .serena-usage-name.codex { color: #a7caff; }
        .serena-usage-meta,
        .serena-usage-reset { color: #b4a8b8; font-size: 9px; }
        .serena-usage-window-label { color: #b9acba; font-size: 10px; }
        .serena-usage-pct { color: #f4eef5; font-weight: 700; }
        .serena-usage-age { color: #a99bad; font-size: 9px; }
        .serena-usage-age.stale { color: #f2c66d; }
        .serena-usage-waiting { color: #817583; }
        progressbar.serena-usage-meter trough {
            min-height: 6px;
            background-color: rgba(255, 255, 255, 0.12);
            border: 1px solid rgba(255, 255, 255, 0.13);
            border-radius: 999px;
        }
        progressbar.serena-usage-meter progress {
            min-height: 6px;
            background-color: #78d88f;
            background-image: none;
            border: 0;
            border-radius: 999px;
        }
        progressbar.serena-usage-meter.tone-warn progress { background-color: #f2c66d; }
        progressbar.serena-usage-meter.tone-danger progress { background-color: #ff6f7a; }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self._usage_popover_css = provider

        popover = Gtk.Popover.new(self.web)
        popover.get_style_context().add_class("serena-usage-popover")
        popover.set_position(Gtk.PositionType.BOTTOM)
        popover.set_constrain_to(Gtk.PopoverConstraint.WINDOW)
        popover.set_modal(False)
        popover.set_transitions_enabled(False)
        popover.set_can_focus(False)
        popover.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        popover.connect("enter-notify-event", self._on_usage_popover_enter)
        popover.connect("leave-notify-event", self._on_usage_popover_leave)
        self._usage_popover = popover
        return popover

    @staticmethod
    def _usage_label(text: object, style_class: str, xalign: float = 0.0) -> Gtk.Label:
        label = Gtk.Label(label=str(text or ""))
        label.set_xalign(xalign)
        label.set_yalign(0.5)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.get_style_context().add_class(style_class)
        return label

    def _build_usage_window(self, data: dict) -> Gtk.Grid:
        row = Gtk.Grid(column_spacing=7)
        row.set_hexpand(True)

        label = self._usage_label(data.get("label", ""), "serena-usage-window-label")
        label.set_size_request(32, -1)

        raw_pct = data.get("pct")
        try:
            pct = max(0.0, float(raw_pct)) if raw_pct is not None else None
        except (TypeError, ValueError):
            pct = None
        meter = Gtk.ProgressBar()
        meter.set_fraction(min(1.0, (pct or 0.0) / 100.0))
        meter.set_hexpand(True)
        meter.set_valign(Gtk.Align.CENTER)
        meter.set_size_request(150, 6)
        meter.get_style_context().add_class("serena-usage-meter")
        if pct is not None and pct >= 90:
            meter.get_style_context().add_class("tone-danger")
        elif pct is not None and pct >= 70:
            meter.get_style_context().add_class("tone-warn")

        pct_text = "--" if pct is None else f"{int(round(pct))}%"
        pct_label = self._usage_label(pct_text, "serena-usage-pct", 1.0)
        pct_label.set_size_request(42, -1)
        reset = self._usage_label(data.get("reset", ""), "serena-usage-reset", 1.0)
        reset.set_size_request(62, -1)

        row.attach(label, 0, 0, 1, 1)
        row.attach(meter, 1, 0, 1, 1)
        row.attach(pct_label, 2, 0, 1, 1)
        row.attach(reset, 3, 0, 1, 1)
        return row

    def _build_usage_card(self, service: dict) -> Gtk.EventBox:
        card = Gtk.EventBox()
        card.get_style_context().add_class("serena-usage-card")
        if service.get("stale"):
            card.get_style_context().add_class("stale")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_margin_start(9)
        content.set_margin_end(9)
        content.set_margin_top(9)
        content.set_margin_bottom(9)
        card.add(content)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        name_class = "codex" if service.get("name") == "codex" else "claude"
        name = self._usage_label(service.get("name", ""), "serena-usage-name")
        name.get_style_context().add_class(name_class)
        header.pack_start(name, False, False, 0)

        if not service.get("available"):
            waiting = self._usage_label("waiting", "serena-usage-waiting", 1.0)
            waiting.set_hexpand(True)
            header.pack_start(waiting, True, True, 0)
            content.pack_start(header, False, False, 0)
            return card

        meta = self._usage_label(service.get("meta", ""), "serena-usage-meta", 1.0)
        meta.set_hexpand(True)
        meta.set_max_width_chars(34)
        header.pack_start(meta, True, True, 0)
        content.pack_start(header, False, False, 0)

        for window in service.get("windows") or []:
            if isinstance(window, dict):
                content.pack_start(self._build_usage_window(window), False, False, 0)

        age_text = service.get("age") or ""
        if age_text:
            age = self._usage_label(age_text, "serena-usage-age", 1.0)
            if service.get("stale"):
                age.get_style_context().add_class("stale")
            content.pack_start(age, False, False, 0)
        return card

    def _show_usage_popover(self, payload: dict) -> None:
        self._cancel_usage_popover_hide()
        popover = self._ensure_usage_popover()
        rect_data = payload.get("rect") or {}
        rect = Gdk.Rectangle()
        rect.x = max(0, int(round(float(rect_data.get("x", 0)))))
        rect.y = max(0, int(round(float(rect_data.get("y", 0)))))
        rect.width = max(1, int(round(float(rect_data.get("w", 1)))))
        rect.height = max(1, int(round(float(rect_data.get("h", 1)))))
        popover.set_pointing_to(rect)

        old_child = popover.get_child()
        if old_child is not None:
            popover.remove(old_child)
            old_child.destroy()

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_size_request(340, -1)
        body.get_style_context().add_class("serena-usage-body")
        for service in payload.get("services") or []:
            if isinstance(service, dict):
                body.pack_start(self._build_usage_card(service), False, False, 0)
        popover.add(body)
        body.show_all()
        popover.popup()

    def _cancel_usage_popover_hide(self) -> None:
        if not self._usage_popover_hide_source:
            return
        try:
            GLib.source_remove(self._usage_popover_hide_source)
        except Exception:
            pass
        self._usage_popover_hide_source = 0

    def _schedule_usage_popover_hide(self, delay_ms: int = 160) -> None:
        self._cancel_usage_popover_hide()
        if delay_ms <= 0:
            self._hide_usage_popover()
            return
        self._usage_popover_hide_source = GLib.timeout_add(delay_ms, self._hide_usage_popover)

    def _hide_usage_popover(self) -> bool:
        self._usage_popover_hide_source = 0
        if self._usage_popover is not None:
            self._usage_popover.popdown()
        return False

    def _on_usage_popover_enter(self, *_args):
        self._cancel_usage_popover_hide()
        return False

    def _on_usage_popover_leave(self, *_args):
        self._schedule_usage_popover_hide(120)
        return False

    # === SPLIT FEATURE === (debounced SIGWINCH + paint nudge after split
    # reparent/resize. Do not feed terminal control bytes here: Codex treats
    # Ctrl+L as a screen clear, which makes resumed content appear to vanish.)
    def _kick_split_winch(self) -> None:
        if not hasattr(self, "_paned_winch_token"):
            self._paned_winch_token = 0
        self._paned_winch_token += 1
        tok = self._paned_winch_token
        def _send_winch_burst():
            if tok != self._paned_winch_token or not self._split_active:
                return False
            for sid in self._split_sids:
                pid = self._vte_pids.get(sid) if sid else None
                if pid:
                    try:
                        pgid = os.getpgid(pid)
                        if pgid == pid and pgid != os.getpgrp():
                            os.killpg(pgid, signal.SIGWINCH)
                        else:
                            os.kill(pid, signal.SIGWINCH)
                    except (ProcessLookupError, PermissionError):
                        pass
                    except Exception as e:
                        print(f"[split] SIGWINCH failed for {sid}: {e}", flush=True)
            return False
        def _queue_codex_paint():
            if tok != self._paned_winch_token or not self._split_active:
                return False
            sid_agent = getattr(self, "_sid_agent", {})
            for sid in self._split_sids:
                if not sid:
                    continue
                if (sid_agent.get(sid) or "").lower() != "codex":
                    continue
                vte = self._vtes.get(sid)
                if vte is None:
                    continue
                try:
                    vte.queue_draw()
                except Exception as e:
                    print(f"[split] codex paint nudge failed: {e}", flush=True)
            return False
        # Staggered: TIOCSWINSZ hits first, then SIGWINCH again as a backup,
        # then queue a VTE paint without sending input to the child process.
        GLib.timeout_add(150, _send_winch_burst)
        GLib.timeout_add(380, _send_winch_burst)
        GLib.timeout_add(550, _queue_codex_paint)
        GLib.timeout_add(900, _queue_codex_paint)
    # === SPLIT FEATURE END ===

    # ------------------------------------------------------------------
    # Runtime lifecycle
    # ------------------------------------------------------------------
    def _sid_for_vte(self, vte: Vte.Terminal | None) -> str | None:
        if vte is None:
            return None
        for sid, candidate in self._vtes.items():
            if candidate is vte:
                return sid
        return None

    def _draft_get(self, vte: Vte.Terminal | None) -> str:
        sid = self._sid_for_vte(vte)
        return self._input_drafts.get(sid, "") if sid else ""

    def _draft_set(self, vte: Vte.Terminal | None, value: str) -> None:
        sid = self._sid_for_vte(vte)
        if sid:
            self._input_drafts[sid] = value
            self._runtime_last_activity[sid] = time.monotonic()

    def _draft_append(self, vte: Vte.Terminal | None, value: str) -> None:
        sid = self._sid_for_vte(vte)
        if sid:
            self._input_drafts[sid] = self._input_drafts.get(sid, "") + value
            self._runtime_last_activity[sid] = time.monotonic()

    @staticmethod
    def _runtime_pid_alive(pid: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, OSError):
            return False

    @staticmethod
    def _terminate_runtime_pid(pid: int, sig: int) -> None:
        """Signal the complete VTE process group when it is safe to do so."""
        try:
            pgid = os.getpgid(pid)
            if pgid == pid and pgid != os.getpgrp():
                os.killpg(pgid, sig)
            else:
                os.kill(pid, sig)
        except ProcessLookupError:
            pass

    @staticmethod
    def _runtime_cgroup_dir(pid: int) -> str | None:
        try:
            with open(f"/proc/{pid}/cgroup", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("0::"):
                        rel = line.split("::", 1)[1].strip()
                        if rel and rel != "/":
                            return f"/sys/fs/cgroup{rel}"
        except OSError:
            pass
        return None

    def _reclaim_runtime_memory(self, sid: str, pid: int) -> None:
        """Push a frozen pane's pages to swap via its vte-spawn cgroup.

        Wake stays a millisecond SIGCONT; pages fault back from swap on
        demand, so this trades idle RSS for refault I/O instead of latency.
        """
        cgroup = self._runtime_cgroup_dir(pid)
        if not cgroup:
            return
        before = self._runtime_rss_mb(pid)
        try:
            fh = open(os.path.join(cgroup, "memory.reclaim"), "w", encoding="utf-8")
        except OSError:
            return
        with fh:
            try:
                fh.write("4G")
            except OSError:
                # EAGAIN after a partial pass: whatever was cold is already out.
                pass
        self._runtime_reclaimed.add(sid)
        after = self._runtime_rss_mb(pid)
        print(
            f"[runtime] reclaimed {sid[:8]} pid={pid} rss {before}MB -> {after}MB",
            flush=True,
        )

    @staticmethod
    def _runtime_rss_mb(pid: int) -> int:
        try:
            with open(f"/proc/{pid}/statm", "r", encoding="utf-8") as fh:
                pages = int(fh.read().split()[1])
            return pages * os.sysconf("SC_PAGE_SIZE") // (1024 * 1024)
        except (OSError, ValueError, IndexError):
            return 0

    def _remember_runtime(self, sid: str, cwd: str, agent: str) -> None:
        if not sid:
            return
        if cwd:
            self._runtime_cwd[sid] = cwd
        if agent:
            self._sid_agent[sid] = agent.lower()
        self._runtime_last_activity.setdefault(sid, time.monotonic())

    def _set_runtime_state(self, sid: str, state: str, reason: str = "") -> None:
        if not sid:
            return
        self._runtime_state[sid] = state
        if state != "paused":
            self._runtime_reclaimed.discard(sid)
        vte = self._vtes.get(sid)
        if vte is not None:
            style = vte.get_style_context()
            style.remove_class("serena-runtime-asleep")
            style.remove_class("serena-runtime-paused")
            style.remove_class("serena-runtime-waking")
            if state == "asleep":
                style.add_class("serena-runtime-asleep")
            elif state == "paused":
                style.add_class("serena-runtime-paused")
            elif state in ("sleeping", "waking"):
                style.add_class("serena-runtime-waking")
        self._eval_js(
            "window.onGtkRuntimeState && window.onGtkRuntimeState("
            f"{json.dumps(sid)}, {json.dumps(state)}, {json.dumps(reason)});"
        )

    def _paint_runtime_asleep(self, sid: str) -> None:
        vte = self._vtes.get(sid)
        if vte is None:
            return
        agent = self._sid_agent.get(sid, "session")
        try:
            vte.feed(
                f"\r\n\x1b[38;5;244m  {agent} asleep\x1b[0m\r\n".encode("utf-8")
            )
        except Exception as error:
            print(f"[runtime] asleep marker failed for {sid[:8]}: {error}", flush=True)

    def _runtime_transcript_path(self, sid: str) -> str | None:
        cached = self._runtime_file_paths.get(sid)
        if cached:
            return cached
        try:
            session = get_session(sid) or {}
        except Exception:
            session = {}
        path = session.get("file_path")
        if path:
            self._runtime_file_paths[sid] = path
        return path

    def _runtime_has_active_turn(self, sid: str) -> bool:
        state = self._runtime_activity_reader.read(
            self._runtime_transcript_path(sid),
            self._sid_agent.get(sid, ""),
        )
        if state is not None:
            return state
        last_output = self._runtime_last_output.get(sid, 0.0)
        return bool(
            self._runtime_state.get(sid) in ("live", "waking")
            and time.monotonic() - last_output < 2.0
        )

    def _runtime_transcript_size(self, sid: str) -> int | None:
        path = self._runtime_transcript_path(sid)
        if not path:
            return None
        try:
            return Path(path).stat().st_size
        except OSError:
            return None

    def _runtime_is_working(self, sid: str) -> bool:
        with self._runtime_lock:
            leased = self._runtime_leases.get(sid, 0) > 0
            busy = sid in self._runtime_busy
            reserved = sid in self._runtime_work_reservations
            turn_offset = self._runtime_turn_offsets.get(sid)
        if busy and turn_offset is not None:
            completed = self._runtime_activity_reader.completed_since(
                self._runtime_transcript_path(sid),
                self._sid_agent.get(sid, ""),
                turn_offset,
            )
            if completed:
                with self._runtime_lock:
                    if self._runtime_turn_offsets.get(sid) == turn_offset:
                        self._runtime_busy.discard(sid)
                        self._runtime_turn_offsets.pop(sid, None)
                        busy = False
                if not busy:
                    print(
                        f"[runtime] reconciled completed turn {sid[:8]}",
                        flush=True,
                    )
        return leased or reserved or busy or self._runtime_has_active_turn(sid)

    def _runtime_is_protected(self, sid: str) -> bool:
        return self._runtime_is_working(sid) or bool(self._input_drafts.get(sid))

    def runtime_acquire_lease(self, sid: str) -> None:
        with self._runtime_lock:
            self._runtime_leases[sid] = self._runtime_leases.get(sid, 0) + 1
        self._runtime_last_activity[sid] = time.monotonic()

    def runtime_release_lease(self, sid: str) -> None:
        with self._runtime_lock:
            count = self._runtime_leases.get(sid, 0)
            if count <= 1:
                self._runtime_leases.pop(sid, None)
            else:
                self._runtime_leases[sid] = count - 1
        GLib.idle_add(self._runtime_after_lease, sid)

    def runtime_work_reservation(self, sid: str) -> str | None:
        with self._runtime_lock:
            return self._runtime_work_reservations.get(sid)

    def runtime_reserve_work(self, sid: str, item_id: str) -> tuple[bool, str]:
        """Reserve one idle Codex VTE for an accepted voice coding turn."""
        if not sid or not item_id:
            return False, "sid and item_id are required"
        vte = self._vtes.get(sid)
        pid = self._vte_pids.get(sid)
        if vte is None or not self._runtime_pid_alive(pid):
            return False, "runtime is not alive"
        if self._sid_agent.get(sid) != "codex":
            return False, "target is not a codex runtime"
        if meta_sync.external_runtime_active(sid):
            return False, "runtime has an external owner"
        meta = meta_sync.get_meta(sid)
        if meta.get("done"):
            return False, "runtime is marked done"
        marker = meta.get("fleet_worker")
        if isinstance(marker, dict) and marker.get("run_id"):
            return False, "fleet worker runtimes are read-only"
        if self._runtime_has_active_turn(sid):
            return False, "runtime has an active turn"
        with self._runtime_lock:
            current = self._runtime_work_reservations.get(sid)
            if current:
                return False, f"runtime reserved by {current}"
            if self._runtime_leases.get(sid, 0) > 0 or sid in self._runtime_busy:
                return False, "runtime has an active turn"
            if self._input_drafts.get(sid, "").strip():
                return False, "runtime has an unsent draft"
            if self._runtime_state.get(sid) not in {"live", "paused"}:
                return False, f"runtime state is {self._runtime_state.get(sid, 'unknown')}"
            self._runtime_work_reservations[sid] = item_id
        try:
            vte.set_input_enabled(False)
        except (AttributeError, RuntimeError):
            pass
        self._runtime_last_activity[sid] = time.monotonic()
        return True, "reserved"

    def runtime_release_work(self, sid: str, item_id: str) -> bool:
        with self._runtime_lock:
            if self._runtime_work_reservations.get(sid) != item_id:
                return False
            self._runtime_work_reservations.pop(sid, None)
        vte = self._vtes.get(sid)
        if vte is not None:
            try:
                vte.set_input_enabled(True)
            except (AttributeError, RuntimeError):
                pass
        self._runtime_last_activity[sid] = time.monotonic()
        return True

    def runtime_interrupt_work(self, sid: str, item_id: str) -> bool:
        """Interrupt only the item holding this VTE, never the desktop process."""
        if self.runtime_work_reservation(sid) != item_id:
            return False
        vte = self._vtes.get(sid)
        if vte is None or not self._runtime_pid_alive(self._vte_pids.get(sid)):
            return False
        try:
            vte.feed_child(b"\x03")
        except Exception:
            return False
        return True

    def runtime_context_snapshot(self) -> dict:
        """Return exact native owner state without exposing draft contents."""
        with self._runtime_lock:
            busy_sids = set(self._runtime_busy)
            reservations = dict(self._runtime_work_reservations)
        sids = set(self._vtes) | set(self._vte_pids) | set(self._runtime_state)
        entries = []
        for sid in sorted(sids):
            pid = self._vte_pids.get(sid)
            entries.append(
                {
                    "sid": sid,
                    "agent": self._sid_agent.get(sid, ""),
                    "cwd": self._runtime_cwd.get(sid, ""),
                    "alive": self._runtime_pid_alive(pid),
                    "state": self._runtime_state.get(sid, "closed"),
                    "busy": sid in busy_sids or self._runtime_has_active_turn(sid),
                    "draft": bool(self._input_drafts.get(sid, "").strip()),
                    "reserved": sid in reservations,
                    "reservation_item_id": reservations.get(sid),
                    "owner": "gtk",
                    "pid": pid,
                }
            )
        split_pair = [
            sid for sid in self._split_sids if sid
        ] if self._split_active else []
        focused_at = self._runtime_last_activity.get(self._runtime_focus_sid or "", 0.0)
        try:
            window_active = bool(self.is_active())
        except (AttributeError, RuntimeError):
            window_active = False
        return {
            "focused_sid": self._runtime_focus_sid,
            "split_pair": split_pair,
            "runtimes": entries,
            "focused_at": focused_at,
            "window_active": window_active,
        }

    def _runtime_after_lease(self, sid: str) -> bool:
        if self._runtime_is_background_split_sid(sid):
            self._pause_runtime(sid, "bridge-idle")
        return False

    def _runtime_is_background_split_sid(self, sid: str) -> bool:
        return bool(
            self._split_active
            and not self._split_pinned
            and sid in self._split_sids
            and sid != self._runtime_focus_sid
        )

    def runtime_begin_turn(self, sid: str) -> None:
        if not sid:
            return
        turn_offset = self._runtime_transcript_size(sid)
        with self._runtime_lock:
            self._runtime_busy.add(sid)
            if turn_offset is not None:
                self._runtime_turn_offsets[sid] = turn_offset
            else:
                self._runtime_turn_offsets.pop(sid, None)
        self._runtime_last_activity[sid] = time.monotonic()
        if self._sid_agent.get(sid) == "codex" and not sid.startswith("new-"):
            try:
                from core import codex_attention_watcher

                session = get_session(sid) or {}
                codex_attention_watcher.watch(sid, session.get("file_path"))
            except Exception as error:
                print(
                    f"[runtime] codex completion watch failed for {sid[:8]}: {error}",
                    flush=True,
                )

    def _poll_voice_inbox(self) -> bool:
        """Retired unsafe executor. The resident supervisor owns the queue."""

        return False

    def notify_runtime_turn_finished(self, sid: str) -> None:
        if sid:
            GLib.idle_add(self._runtime_turn_finished, sid)

    def _runtime_turn_finished(self, sid: str) -> bool:
        with self._runtime_lock:
            self._runtime_busy.discard(sid)
            self._runtime_turn_offsets.pop(sid, None)
        self._runtime_last_activity[sid] = time.monotonic()
        if self._runtime_is_background_split_sid(sid):
            def _pause_after_turn() -> bool:
                if self._runtime_is_background_split_sid(sid):
                    paused = self._pause_runtime(sid, "turn-finished")
                    if not paused and self._runtime_focus_sid:
                        self._schedule_exclusive_runtime(self._runtime_focus_sid)
                return False

            GLib.timeout_add(150, _pause_after_turn)
        return False

    def _pause_runtime(self, sid: str | None, reason: str = "standby") -> bool:
        """Freeze an idle runtime in-place for a millisecond-scale wake."""
        if not sid or sid.startswith("new-"):
            return False
        state = self._runtime_state.get(sid)
        protected = self._runtime_is_protected(sid)
        if state == "paused":
            if protected:
                self._wake_runtime(sid, "working-detected", focus=False)
                print(
                    f"[runtime] woke protected {sid[:8]} ({reason})",
                    flush=True,
                )
                return False
            return True
        if state in ("asleep", "sleeping"):
            return True
        if protected:
            print(f"[runtime] keep {sid[:8]} live, protected ({reason})", flush=True)
            return False
        if self._split_pinned and self._split_active and sid in self._split_sids:
            return False

        pid = self._vte_pids.get(sid)
        if not self._runtime_pid_alive(pid):
            return False
        try:
            self._terminate_runtime_pid(pid, signal.SIGSTOP)
        except Exception as error:
            print(f"[runtime] standby failed for {sid[:8]}: {error}", flush=True)
            return False
        self._set_runtime_state(sid, "paused", reason)
        print(f"[runtime] standby {sid[:8]} pid={pid} reason={reason}", flush=True)
        return True

    def _sleep_runtime(self, sid: str | None, reason: str = "idle") -> bool:
        if not sid or sid.startswith("new-"):
            return False
        state = self._runtime_state.get(sid)
        if state in ("asleep", "sleeping"):
            return True
        if self._runtime_is_protected(sid):
            print(f"[runtime] keep {sid[:8]} live, protected ({reason})", flush=True)
            return False
        if self._split_pinned and self._split_active and sid in self._split_sids:
            return False

        pid = self._vte_pids.get(sid)
        if state == "waking" and not self._runtime_pid_alive(pid):
            self._runtime_sleep_after_wake[sid] = reason
            self._set_runtime_state(sid, "sleeping", reason)
            return True
        if not self._runtime_pid_alive(pid):
            self._vte_pids.pop(sid, None)
            self._set_runtime_state(sid, "asleep", reason)
            self._paint_runtime_asleep(sid)
            return True

        self._runtime_stop_reasons[sid] = reason
        self._set_runtime_state(sid, "sleeping", reason)
        print(f"[runtime] sleeping {sid[:8]} pid={pid} reason={reason}", flush=True)
        try:
            self._terminate_runtime_pid(pid, signal.SIGTERM)
        except Exception as error:
            print(f"[runtime] stop failed for {sid[:8]}: {error}", flush=True)
            self._runtime_stop_reasons.pop(sid, None)
            self._set_runtime_state(sid, "live", "stop-failed")
            return False

        def _force_stop(expected_pid=pid):
            if (
                self._runtime_state.get(sid) == "sleeping"
                and self._vte_pids.get(sid) == expected_pid
                and self._runtime_pid_alive(expected_pid)
            ):
                print(f"[runtime] force stopping {sid[:8]} pid={expected_pid}", flush=True)
                try:
                    self._terminate_runtime_pid(expected_pid, signal.SIGKILL)
                except Exception as error:
                    print(f"[runtime] force stop failed for {sid[:8]}: {error}", flush=True)
            return False

        GLib.timeout_add(2000, _force_stop)
        return True

    def _wake_runtime(self, sid: str | None, reason: str = "focus", focus: bool = True) -> bool:
        if not sid:
            return False
        state = self._runtime_state.get(sid)
        pid = self._vte_pids.get(sid)
        if state == "sleeping":
            self._runtime_wake_after_stop.add(sid)
            return True
        if state == "paused" and self._runtime_pid_alive(pid):
            started = time.perf_counter()
            try:
                self._terminate_runtime_pid(pid, signal.SIGCONT)
            except Exception as error:
                print(f"[runtime] wake failed for {sid[:8]}: {error}", flush=True)
                return False
            self._set_runtime_state(sid, "live", reason)
            self._runtime_last_activity[sid] = time.monotonic()
            elapsed_ms = (time.perf_counter() - started) * 1000
            print(f"[runtime] woke {sid[:8]} in {elapsed_ms:.2f}ms", flush=True)
            if focus and sid in self._vtes:
                self._queue_vte_focus(self._vtes[sid])
            return True
        if self._runtime_pid_alive(pid):
            self._set_runtime_state(sid, "live", reason)
            self._runtime_last_activity[sid] = time.monotonic()
            if focus and sid in self._vtes:
                self._queue_vte_focus(self._vtes[sid])
            return True
        if state == "waking":
            return True

        vte = self._vtes.get(sid)
        if vte is None:
            return False
        cwd = self._runtime_cwd.get(sid, "")
        agent = self._sid_agent.get(sid, "")
        if not cwd or not agent:
            try:
                session = get_session(sid) or {}
            except Exception:
                session = {}
            raw_cwd = session.get("cwd") or session.get("project_dir") or cwd
            cwd = resolve_session_cwd(raw_cwd)
            agent = (session.get("agent") or agent or "claude").lower()
            self._remember_runtime(sid, cwd, agent)
        self._set_runtime_state(sid, "waking", reason)
        self._spawn_claude(vte, sid, cwd, resume=True, agent=agent, focus=focus)
        return True

    def _schedule_exclusive_runtime(self, active_sid: str) -> None:
        self._runtime_exclusive_generation += 1
        generation = self._runtime_exclusive_generation

        def _try() -> bool:
            if (
                generation != self._runtime_exclusive_generation
                or not active_sid
                or not self._split_active
                or self._split_pinned
                or self._runtime_focus_sid != active_sid
                or active_sid not in self._split_sids
            ):
                return False
            if not self._runtime_pid_alive(self._vte_pids.get(active_sid)):
                GLib.timeout_add(150, _try)
                return False
            retry = False
            retry_delay = 150
            now = time.monotonic()
            for sid in self._split_sids:
                if sid and sid != active_sid:
                    if self._runtime_is_protected(sid):
                        if self._runtime_state.get(sid) != "live":
                            self._wake_runtime(sid, "working-detected", focus=False)
                        retry = True
                        retry_delay = 1000
                        continue
                    pid = self._vte_pids.get(sid)
                    if (
                        self._runtime_state.get(sid) == "waking"
                        and not self._runtime_pid_alive(pid)
                    ):
                        retry = True
                        continue
                    started_at = self._runtime_started_at.get(sid, 0.0)
                    if (
                        self._runtime_pid_alive(pid)
                        and started_at
                        and now - started_at < self._runtime_prewarm_seconds
                    ):
                        retry = True
                        continue
                    if not self._pause_runtime(sid, "sibling-focused"):
                        retry = True
                        retry_delay = 1000
            if retry:
                GLib.timeout_add(retry_delay, _try)
            return False

        GLib.timeout_add(50, _try)

    @staticmethod
    def _select_split_focus(
        requested_sid: str | None,
        split_sids: tuple[str, str],
        working_sids: set[str],
    ) -> str:
        requested = requested_sid if requested_sid in split_sids else split_sids[0]
        working = [sid for sid in split_sids if sid in working_sids]
        return working[0] if len(working) == 1 else requested

    def _activate_runtime(self, sid: str | None, reason: str = "focus") -> None:
        if not sid:
            return
        self._runtime_focus_sid = sid
        self._runtime_last_activity[sid] = time.monotonic()
        self._wake_runtime(sid, reason, focus=False)
        if self._split_active and not self._split_pinned:
            self._schedule_exclusive_runtime(sid)

    def _evict_runtime_sids(self, sids, keep: set[str] | None = None) -> None:
        """Navigation never stops a runtime; it only starts its idle grace."""
        keep = keep or set()
        now = time.monotonic()
        for sid in sids:
            if sid and sid not in keep:
                self._runtime_last_activity[sid] = now

    def _sweep_runtime_idle(self) -> bool:
        """Idle every pane to frozen, swap-reclaimed standby.

        Covers stack panes as well as split ones. The focused pane is exempt
        here; protected and pinned-split runtimes are exempt inside
        _pause_runtime. Reclaim fires once per standby stretch and the flag
        clears whenever the runtime leaves the paused state.
        """
        now = time.monotonic()
        for sid, pid in list(self._vte_pids.items()):
            if not sid or sid.startswith("new-") or sid == self._runtime_focus_sid:
                continue
            if not self._runtime_pid_alive(pid):
                continue
            last = self._runtime_last_activity.get(sid, 0.0)
            waiting_for_reset = self._runtime_activity_reader.waiting_for_usage_reset(
                self._runtime_transcript_path(sid),
                self._sid_agent.get(sid, ""),
            )
            if not waiting_for_reset and now - last < self._runtime_idle_seconds:
                continue
            if self._runtime_state.get(sid) != "paused":
                reason = "usage-reset-wait" if waiting_for_reset else "idle-standby"
                if not self._pause_runtime(sid, reason):
                    continue
            if sid not in self._runtime_reclaimed:
                self._reclaim_runtime_memory(sid, pid)
        return True

    def _set_split_pinned(self, pinned: bool, focus_sid: str | None = None) -> None:
        self._split_pinned = bool(pinned)
        if not self._split_active:
            return
        if self._split_pinned:
            for sid in self._split_sids:
                self._wake_runtime(sid, "pinned", focus=False)
        else:
            active = self._runtime_focus_sid or focus_sid or self._split_sids[0]
            self._activate_runtime(active, "unpinned")

    def _migrate_runtime_sid(self, old: str, new: str) -> None:
        for mapping in (
            self._input_drafts,
            self._runtime_cwd,
            self._runtime_state,
            self._runtime_last_activity,
            self._runtime_last_output,
            self._runtime_file_paths,
            self._runtime_started_at,
            self._runtime_turn_offsets,
            self._runtime_stop_reasons,
            self._runtime_sleep_after_wake,
        ):
            if old in mapping:
                mapping[new] = mapping.pop(old)
        if old in self._runtime_busy:
            self._runtime_busy.discard(old)
            self._runtime_busy.add(new)
        if old in self._runtime_wake_after_stop:
            self._runtime_wake_after_stop.discard(old)
            self._runtime_wake_after_stop.add(new)
        tail_follow = getattr(self, "_vte_tail_follow_until", None)
        if tail_follow is not None:
            tail_follow.pop(old, None)
        pending_scrolls = getattr(self, "_vte_tail_scroll_pending", None)
        if pending_scrolls is not None:
            pending_scrolls.discard(old)
        migrated_vte = self._vtes.get(new)
        if migrated_vte is not None:
            with suppress(AttributeError, RuntimeError):
                migrated_vte.set_scroll_on_output(False)
        with self._runtime_lock:
            if old in self._runtime_leases:
                self._runtime_leases[new] = self._runtime_leases.pop(old)
            if old in self._runtime_work_reservations:
                self._runtime_work_reservations[new] = (
                    self._runtime_work_reservations.pop(old)
                )
        if self._runtime_focus_sid == old:
            self._runtime_focus_sid = new
        state = self._runtime_state.get(new)
        if state:
            self._set_runtime_state(new, state, "sid-migrated")

    def _open_external_uri(self, value: object) -> bool:
        uri = _normalize_external_uri(value)
        if not uri:
            print(f"[external-link] rejected URI: {str(value)[:120]}", flush=True)
            return False
        try:
            if Gtk.show_uri_on_window(self, uri, Gdk.CURRENT_TIME) is not False:
                print(f"[external-link] opened {uri[:120]}", flush=True)
                return True
        except Exception as error:
            print(f"[external-link] GTK launch failed: {error}", flush=True)
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
            print(f"[external-link] opened via Gio {uri[:120]}", flush=True)
            return True
        except Exception as error:
            print(f"[external-link] launch failed for {uri[:120]}: {error}", flush=True)
            return False

    # ------------------------------------------------------------------
    # JS bridge
    # ------------------------------------------------------------------
    def _on_js_message(self, cm, result):
        try:
            payload = json.loads(result.get_js_value().to_string())
        except Exception as e:
            print(f"[gtkbridge] bad payload: {e}", flush=True)
            return

        kind = payload.get("type")
        if kind == "code-on":
            self._show_session(payload)
        elif kind == "code-off":
            self._hide_stack()
        elif kind == "usage-popover-show":
            self._show_usage_popover(payload)
        elif kind == "usage-popover-hide":
            self._schedule_usage_popover_hide(0 if payload.get("immediate") else 160)
        elif kind == "open-external-uri":
            self._open_external_uri(payload.get("uri"))
        elif kind == "code-rect":
            self._set_rect(payload.get("rect"))
        elif kind == "code-tab-visible":
            self._set_terminal_view_visible(
                bool(payload.get("visible")), payload.get("rect")
            )
        elif kind == "code-close":
            self._kill_session(payload.get("sid"))
        # === SPLIT FEATURE ===
        elif kind == "code-split-on":
            self._enter_split(payload)
        elif kind == "code-split-off":
            self._exit_split(payload.get("focus_sid"))
        elif kind == "code-focus":
            self._set_rect(payload.get("rect"))
            self._focus_session(payload.get("sid"))
        elif kind == "code-pin-both":
            self._set_split_pinned(
                bool(payload.get("pinned")), payload.get("focus_sid")
            )
        # === SPLIT FEATURE END ===
        elif kind == "feed-text":
            # Click-to-insert from the files pane → type into the visible VTE
            sid = payload.get("sid")
            text = payload.get("text", "")
            submit = bool(payload.get("submit"))
            vte = self._vtes.get(sid) if sid else self._current_vte()
            sid = sid or self._sid_for_vte(vte)
            if sid and self.runtime_work_reservation(sid):
                print(
                    f"[bridge] blocked manual feed for reserved runtime {sid[:8]}",
                    flush=True,
                )
                return
            if vte is None and text:
                print(f"[bridge] feed-text no VTE for {(sid or '')[:8]}", flush=True)
            if vte is not None and text:
                if sid and not self._runtime_pid_alive(self._vte_pids.get(sid)):
                    self._wake_runtime(sid, "feed-text", focus=True)

                def _deliver(attempt=0):
                    if sid and not self._runtime_pid_alive(self._vte_pids.get(sid)):
                        if attempt < 100:
                            GLib.timeout_add(100, _deliver, attempt + 1)
                        return False
                    try:
                        if submit:
                            body = b"\x1b[200~" + text.encode("utf-8") + b"\x1b[201~"
                            vte.feed_child(body)
                            self._draft_set(vte, "")
                            if sid:
                                self.runtime_begin_turn(sid)

                            def _submit():
                                try:
                                    vte.feed_child(b"\r")
                                except Exception as e:
                                    print(f"[bridge] feed-text submit failed: {e}", flush=True)
                                return False

                            GLib.timeout_add(250, _submit)
                        else:
                            vte.feed_child(text.encode("utf-8"))
                            self._draft_append(vte, text)
                        self._queue_vte_focus(vte)
                    except Exception as e:
                        print(f"[bridge] feed-text failed: {e}", flush=True)
                    return False

                _deliver()
        elif kind == "pick-folder":
            # === FOLDER PICKER === (Alt+Shift+N → JS calls this → we open
            # a Gtk.FileChooserNative for folders, then resolve the JS-side
            # promise via window.__resolveFolderPick(token, path).)
            self._open_folder_picker(payload)
        # === FOLDER PICKER END ===
        elif kind == "attention-state":
            # === ATTENTION === JS sends the current set of sids needing
            # attention. Apply / remove the `needs-attention` CSS class on
            # the matching VTE widgets so split-view panes glow.
            self._apply_attention_state(payload.get("sids") or [])
        # === ATTENTION END ===
        elif kind == "code-migrate-sid":
            # /clear pivoted the claude CLI inside this PTY to a new session id.
            # Rename the stack child + rebind its child-exit handler.
            old = payload.get("old")
            new = payload.get("new")
            if not old or not new or old == new:
                return
            vte = self._vtes.get(old)
            if vte is None:
                return
            # === SPLIT FEATURE === (if the VTE is currently in the paned, the
            # stack rename is a no-op for it. Just swap our internal maps.)
            if vte.get_parent() is self._stack:
                try:
                    self._stack.child_set_property(vte, "name", new)
                except Exception as e:
                    print(f"[migrate] stack rename failed: {e}", flush=True)
                    return
            # === SPLIT FEATURE END ===
            self._vtes[new] = vte
            self._vtes.pop(old, None)
            if old in self._vte_pids:
                self._vte_pids[new] = self._vte_pids.pop(old)
            if old in self._sid_agent:
                self._sid_agent[new] = self._sid_agent.pop(old)
            self._migrate_runtime_sid(old, new)
            try:
                from core.voice_inbox import get_default_voice_inbox

                get_default_voice_inbox().migrate_work_target(old, new)
            except Exception as error:
                print(
                    f"[voice-inbox] work target migration failed: {error}",
                    flush=True,
                )
            # Migrate the split-sids tuple if applicable
            if self._split_active and old in self._split_sids:
                a, b = self._split_sids
                self._split_sids = (new if a == old else a, new if b == old else b)
            print(f"[migrate] {old[:8]} → {new[:8]}", flush=True)
    # ------------------------------------------------------------------
    # Session lifecycle — per-session VTE in the stack
    # ------------------------------------------------------------------
    def _show_session(self, payload: dict):
        # xterm owns terminal processes when the native backend is disabled.
        # Refuse accidental server-side calls so they cannot create a second,
        # invisible CLI for a session already running in the web terminal.
        if not self._use_native_vte:
            print("[vte] ignored native show while xterm backend is active", flush=True)
            return
        sid = payload.get("sid")
        if not sid:
            return
        if not payload.get("isNew") and meta_sync.external_runtime_active(sid):
            print(f"[vte] refusing duplicate resume for external runtime {sid[:8]}", flush=True)
            self._eval_js(
                "window.onGtkExternalRuntime && "
                f"window.onGtkExternalRuntime({json.dumps(sid)});"
            )
            return
        t0 = time.monotonic()
        previous_visible = [
            item for item in (
                self._split_sids if self._split_active else (
                    self._stack.get_visible_child_name(),
                )
            ) if item and item != "__blank__"
        ]

        # === SPLIT FEATURE === (if a non-split chat is being shown, dismantle
        # any active split first so VTEs are back in the stack and reachable)
        if self._split_active and sid not in self._split_sids:
            self._exit_split(None, queue_resize=False)
        # === SPLIT FEATURE END ===

        # Pre-warm the rect BEFORE swapping the visible child so first paint
        # happens at the correct allocation (no size-jump on first frame).
        self._stack_hidden = False
        self._set_rect(payload.get("rect"))

        if sid in self._vtes:
            # Existing session — but verify the child process is still alive
            # before reusing. Stale VTEs (codex exited / crashed) would just
            # show a dead pane with no input or output. Tear them down and
            # respawn fresh instead of swapping to a corpse.
            existing_vte = self._vtes[sid]
            stale = False
            pid = self._vte_pids.get(sid)
            if pid:
                try:
                    os.kill(pid, 0)  # signal 0 = existence check, no actual signal
                except (ProcessLookupError, OSError):
                    stale = True
            else:
                stale = True  # no pid recorded → can't verify, treat as stale
            if not stale:
                if self._stack.get_visible_child_name() != sid:
                    self._stack.set_visible_child_name(sid)
                self._activate_runtime(sid, "shown")
                self._arm_vte_tail_follow(sid, existing_vte)
                self._queue_vte_focus(existing_vte)
                self._evict_runtime_sids(previous_visible, {sid})
                print(f"[swap] {sid[:8]} swap took {(time.monotonic()-t0)*1000:.1f}ms", flush=True)
                return
            if self._runtime_state.get(sid) in ("asleep", "sleeping", "waking"):
                if self._stack.get_visible_child_name() != sid:
                    self._stack.set_visible_child_name(sid)
                self._activate_runtime(sid, "shown")
                self._arm_vte_tail_follow(sid, existing_vte)
                self._queue_vte_focus(existing_vte)
                self._evict_runtime_sids(previous_visible, {sid})
                return
            # Stale VTE — destroy it so the spawn path below builds a fresh one
            print(f"[swap] {sid[:8]} stale VTE (pid={pid} gone), respawning", flush=True)
            try:
                self._vte_pids.pop(sid, None)
                self._vtes.pop(sid, None)
                self._detach_vte(existing_vte)
                existing_vte.destroy()
            except Exception as e:
                print(f"[swap] stale teardown failed: {e}", flush=True)

        # First time seeing this session — spawn a new VTE + claude.
        is_new = bool(payload.get("isNew"))
        # Front-door seed: lands as the pane's first prompt (positional argv).
        if is_new and payload.get("seed"):
            self._pending_seeds = getattr(self, "_pending_seeds", {})
            self._pending_seeds[sid] = str(payload["seed"])
        cwd = payload.get("cwd") or ""
        session = None
        if not cwd or not os.path.isdir(cwd):
            if is_new:
                cwd = resolve_session_cwd(cwd)
            else:
                try:
                    session = get_session(sid) if sid else None
                    raw = (session or {}).get("cwd") or (session or {}).get("project_dir") or ""
                    cwd = resolve_session_cwd(raw)
                except Exception:
                    cwd = str(Path.home())
        if not is_new:
            session = session or (get_session(sid) if sid else None)
            ensure_session_visible(sid, (session or {}).get("project_dir", ""), cwd)
        vte = self._build_vte(sid)
        self._vtes[sid] = vte
        self._stack.add_named(vte, sid)
        vte.show()
        self._stack.set_visible_child_name(sid)
        self._arm_vte_tail_follow(sid, vte)

        # Resolve agent: codex sessions resume via codex CLI, otherwise claude.
        agent = (session or {}).get("agent") or payload.get("agent") or "claude"
        self._remember_runtime(sid, cwd, agent)
        self._runtime_focus_sid = sid

        # For brand-new chats we don't pass -r <tempId> to claude.
        self._spawn_claude(vte, sid, cwd, resume=not is_new, agent=agent)
        self._eval_js(f"window.onGtkCodeStart && window.onGtkCodeStart({json.dumps(sid)});")
        # Tell JS the RESOLVED cwd so the pseudo reconciler can match on cwd
        # when the real session file appears on disk.
        self._eval_js(
            f"window.onGtkCodeStarted && window.onGtkCodeStarted({json.dumps(sid)}, {json.dumps(cwd)});"
        )
        self._evict_runtime_sids(previous_visible, {sid})

    def _build_vte(self, sid: str) -> Vte.Terminal:
        vte = Vte.Terminal()
        vte.set_scrollback_lines(5000)
        vte.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
        vte.set_mouse_autohide(True)
        vte.set_colors(_FG, _BG, None)
        vte.set_color_cursor(_CURSOR)
        if self._font:
            vte.set_font(self._font)
        try:
            vte.set_rewrap_on_resize(True)
            vte.set_redraw_on_allocate(True)
        except Exception:
            pass
        # Turn on OSC-8 hyperlink parsing (terminals that emit it will show clickable links)
        try:
            vte.set_allow_hyperlink(True)
        except Exception:
            pass

        # Register URL regex matching so plain http://... gets detected and clickable.
        self._register_url_match(vte)
        vte.connect("button-press-event", self._on_vte_button_press)
        vte.connect("focus-in-event", self._on_vte_focus_in)
        vte.connect("contents-changed", self._on_vte_contents_changed)

        # Claude Code enables xterm mouse reporting (?1000h/?1006h), which makes
        # VTE forward wheel events to the app instead of scrolling scrollback —
        # and claude ignores them, so the wheel feels dead. Intercept scroll and
        # move the scrollback ourselves whenever there IS scrollback (main
        # screen). Alt-screen TUIs (codex) have upper == page_size, so we fall
        # through and their own mouse handling still works.
        vte.connect("scroll-event", self._on_vte_scroll)

        # Native file drop target — the reason we're in GTK and not pywebview
        target = Gtk.TargetEntry.new("text/uri-list", 0, 0)
        vte.drag_dest_set(Gtk.DestDefaults.ALL, [target], Gdk.DragAction.COPY)
        vte.connect("drag-data-received", self._on_drag_data_received)

        vte.connect("child-exited", self._make_child_exit_handler(sid))
        return vte

    def _register_url_match(self, vte: Vte.Terminal):
        try:
            regex = Vte.Regex.new_for_match(
                _URL_REGEX, -1, _PCRE2_MULTILINE | _PCRE2_UTF | _PCRE2_CASELESS
            )
            tag = vte.match_add_regex(regex, 0)
            try:
                vte.match_set_cursor_name(tag, "pointer")
            except Exception:
                pass
        except Exception as e:
            print(f"[vte] URL match register failed: {e}", flush=True)

    def _queue_vte_tail_scroll(self, sid: str, vte: Vte.Terminal) -> None:
        if sid in self._vte_tail_scroll_pending:
            return
        self._vte_tail_scroll_pending.add(sid)

        def _scroll() -> bool:
            self._vte_tail_scroll_pending.discard(sid)
            if self._vtes.get(sid) is not vte:
                return False
            if self._vte_tail_follow_until.get(sid, 0.0) < time.monotonic():
                return False
            try:
                adjustment = vte.get_vadjustment()
                target = max(
                    adjustment.get_lower(),
                    adjustment.get_upper() - adjustment.get_page_size(),
                )
                adjustment.set_value(target)
            except (AttributeError, RuntimeError):
                pass
            return False

        GLib.idle_add(_scroll)

    def _stop_vte_tail_follow(
        self, sid: str, vte: Vte.Terminal | None = None
    ) -> None:
        current = self._vtes.get(sid)
        if vte is not None and current is not vte:
            return
        self._vte_tail_follow_until.pop(sid, None)
        if current is not None:
            with suppress(AttributeError, RuntimeError):
                current.set_scroll_on_output(False)

    def _arm_vte_tail_follow(self, sid: str, vte: Vte.Terminal) -> None:
        """Keep a newly opened transcript at its tail during initial redraw."""
        deadline = time.monotonic() + (_VTE_TAIL_FOLLOW_MS / 1000.0)
        self._vte_tail_follow_until[sid] = deadline
        with suppress(AttributeError, RuntimeError):
            vte.set_scroll_on_output(True)
        self._queue_vte_tail_scroll(sid, vte)

        def _release() -> bool:
            if self._vte_tail_follow_until.get(sid) == deadline:
                self._stop_vte_tail_follow(sid, vte)
            return False

        GLib.timeout_add(_VTE_TAIL_FOLLOW_MS, _release)

    def _on_vte_scroll(self, vte: Vte.Terminal, event):
        # Scroll the VTE scrollback directly, bypassing app mouse reporting.
        try:
            lines = 3.0
            if event.direction == Gdk.ScrollDirection.UP:
                delta = -lines
            elif event.direction == Gdk.ScrollDirection.DOWN:
                delta = lines
            elif event.direction == Gdk.ScrollDirection.SMOOTH:
                delta = event.delta_y * lines
                if delta == 0:
                    return False
            else:
                return False
            if delta < 0:
                sid = self._sid_for_vte(vte)
                if sid:
                    self._stop_vte_tail_follow(sid, vte)
            adj = vte.get_vadjustment()
            upper, page, val = adj.get_upper(), adj.get_page_size(), adj.get_value()
            if upper <= page:
                return False  # no scrollback (alt screen) → let the app have it
            adj.set_value(max(adj.get_lower(), min(val + delta, upper - page)))
            return True
        except Exception:
            return False

    def _on_vte_button_press(self, vte: Vte.Terminal, event):
        sid = self._sid_for_vte(vte)
        # Left-click with Ctrl → open URL in the user's default browser.
        # Plain left-click falls through to VTE for selection.
        if event.button == 1 and event.state & Gdk.ModifierType.CONTROL_MASK:
            uri = None
            # OSC-8 hyperlink takes priority (apps like newer claude may emit them)
            try:
                uri = vte.hyperlink_check_event(event)
            except Exception as e:
                print(f"[vte-link] hyperlink_check_event failed: {e}", flush=True)
            if not uri:
                try:
                    match_text, _tag = vte.match_check_event(event)
                    if match_text:
                        uri = match_text
                        if uri.startswith("www."):
                            uri = "http://" + uri
                except Exception as e:
                    print(f"[vte-link] match_check_event failed: {e}", flush=True)
            if uri and self._open_external_uri(uri):
                return True
            if not uri:
                print(
                    "[vte-link] ctrl+click produced no URI "
                    f"(x={int(event.x)} y={int(event.y)} sid={sid[:8] if sid else '?'})",
                    flush=True,
                )

        # Resolve a link before waking a sleeping runtime. Waking redraws the
        # terminal and can invalidate the event's row before VTE checks it.
        if sid:
            self._activate_runtime(sid, "clicked")
        return False

    def _on_vte_focus_in(self, vte: Vte.Terminal, _event):
        sid = self._sid_for_vte(vte)
        if sid:
            self._activate_runtime(sid, "focused")
        return False

    def _on_vte_contents_changed(self, vte: Vte.Terminal):
        sid = self._sid_for_vte(vte)
        if sid and self._runtime_state.get(sid) in ("live", "waking"):
            now = time.monotonic()
            self._runtime_last_activity[sid] = now
            self._runtime_last_output[sid] = now
            if self._vte_tail_follow_until.get(sid, 0.0) >= now:
                self._queue_vte_tail_scroll(sid, vte)

    def _vte_cell_geometry(self, vte: Vte.Terminal) -> tuple[int, int]:
        width = max(0, vte.get_allocated_width())
        height = max(0, vte.get_allocated_height())
        try:
            char_w = max(1, vte.get_char_width())
            char_h = max(1, vte.get_char_height())
        except Exception:
            char_w, char_h = 8, 18
        cols = max(20, width // char_w) if width else 100
        rows = max(5, height // char_h) if height else 30
        return int(cols), int(rows)

    def _sync_vte_size(self, vte: Vte.Terminal, sid: str | None = None) -> tuple[int, int]:
        cols, rows = self._vte_cell_geometry(vte)
        try:
            vte.set_size(cols, rows)
        except Exception as e:
            if sid:
                print(f"[vte] set_size failed for {sid[:8]}: {e}", flush=True)
        return cols, rows

    def _schedule_codex_resume_stabilizers(self, sid: str, vte: Vte.Terminal) -> None:
        """Codex resume can redraw after it loads the transcript; keep the PTY
        size and terminal frame in sync through that startup window."""
        for delay_ms in (120, 450, 900, 1600, 2800):
            def _stabilize(delay=delay_ms):
                if sid not in self._vtes or self._vtes.get(sid) is not vte:
                    return False
                cols, rows = self._sync_vte_size(vte, sid)
                pid = self._vte_pids.get(sid)
                if pid:
                    try:
                        os.kill(pid, signal.SIGWINCH)
                    except (ProcessLookupError, PermissionError):
                        pass
                    except Exception as e:
                        print(f"[vte] codex resize signal failed for {sid[:8]}: {e}", flush=True)
                vte.queue_draw()
                print(f"[vte] codex stabilize {sid[:8]} {cols}x{rows} delay={delay}ms", flush=True)
                return False
            GLib.timeout_add(delay_ms, _stabilize)

    def _spawn_claude(self, vte: Vte.Terminal, sid: str, cwd: str, resume: bool = True,
                      agent: str = "claude", focus: bool = True):
        if resume and sid and meta_sync.external_runtime_active(sid):
            print(f"[vte] blocked external runtime resume for {sid[:8]}", flush=True)
            self._eval_js(
                "window.onGtkExternalRuntime && "
                f"window.onGtkExternalRuntime({json.dumps(sid)});"
            )
            return
        # Remember per-sid agent so split-resize logic can target codex specifically.
        if not hasattr(self, "_sid_agent"):
            self._sid_agent = {}
        self._sid_agent[sid] = agent
        self._remember_runtime(sid, cwd, agent)
        # Dispatch on agent — codex sessions go through a different CLI with
        # different flags. Default is claude.
        # Front-door seed: consume if one was staged for this sid. Both CLIs
        # accept a positional initial prompt; only valid for brand-new panes.
        seed = ""
        if not resume:
            seed = getattr(self, "_pending_seeds", {}).pop(sid, "")
        if agent == "codex":
            codex_bin = shutil.which("codex") or "codex"
            argv = [codex_bin]
            if resume and sid and not sid.startswith("new-"):
                argv += ["resume", sid]
            elif seed:
                argv.append(seed)
        else:
            claude_bin = shutil.which("claude") or "claude"
            argv = [claude_bin, "--dangerously-skip-permissions"]

            # === PERSONA === Bake the Serena persona into every claude spawn
            # so all chats (new + old) always have it, independent of the
            # external SessionStart hook (which can desync / get reset). Read
            # fresh each spawn so edits to Persona.md take effect on next chat.
            try:
                from core.config import read_agent_context
                ctx = read_agent_context()  # Persona.md + Tooling.md
                if ctx.strip():
                    argv += ["--append-system-prompt", ctx]
            except Exception as e:
                print(f"[spawn] persona inject failed: {e}", flush=True)
            # === PERSONA END ===

            if resume and sid:
                argv += ["-r", sid]
            elif seed:
                argv.append(seed)

            # === MODEL MASK === Cosmetic PTY relay: shows the assigned Sol,
            # Terra, or Luna model on matching Codex-relay /workflows rows.
            # DEFAULT OFF — wrapping claude in this relay adds keystroke latency
            # (vte→relay→claude round-trips every byte), and Fleet's own panel now
            # shows real Sol/Terra/Luna identity, so the mask is obsolete. Strict
            # opt-in only: SERENA_MODEL_MASK=on to re-enable.
            if os.environ.get("SERENA_MODEL_MASK", "off").lower() in ("on", "1", "true"):
                _mask = str(Path(__file__).resolve().parent.parent / "ui" / "pty_model_mask.py")
                if os.path.exists(_mask):
                    argv = [sys.executable, _mask, "--"] + argv
            # === MODEL MASK END ===
        cols, rows = self._sync_vte_size(vte, sid)
        from core.billing import strip_metered_auth_env

        env = strip_metered_auth_env(os.environ)
        env["COLUMNS"] = str(cols)
        env["LINES"] = str(rows)
        envv = [f"{k}={v}" for k, v in env.items()]

        t0 = time.monotonic()
        self._set_runtime_state(sid, "waking", "spawn")
        if seed:
            self.runtime_begin_turn(sid)
        print(
            f"[vte] spawning agent={agent} resume={resume} size={cols}x{rows} cwd={cwd} sid={sid[:8]}",
            flush=True,
        )

        def on_spawn(term, pid, error, _user):
            dt = time.monotonic() - t0
            pending_sleep = self._runtime_sleep_after_wake.pop(sid, None)
            if error is not None:
                print(f"[vte] spawn error after {dt:.2f}s: {error.message}", flush=True)
                if pending_sleep:
                    self._set_runtime_state(sid, "asleep", pending_sleep)
                    self._paint_runtime_asleep(sid)
                else:
                    self._set_runtime_state(sid, "crashed", "spawn-error")
                return
            self._vte_pids[sid] = pid
            started_at = time.monotonic()
            self._runtime_started_at[sid] = started_at
            self._runtime_last_activity[sid] = started_at
            self._set_runtime_state(sid, "live", "resumed" if resume else "started")
            print(f"[vte] {sid[:8]} pid={pid} after {dt:.2f}s", flush=True)
            if pending_sleep:
                GLib.idle_add(
                    lambda: (self._sleep_runtime(sid, pending_sleep), False)[1]
                )
                return
            if focus:
                self._queue_vte_focus(vte)
            if agent == "codex" and resume:
                self._schedule_codex_resume_stabilizers(sid, vte)

        try:
            vte.spawn_async(
                Vte.PtyFlags.DEFAULT,
                cwd,
                argv,
                envv,
                GLib.SpawnFlags.DEFAULT,
                None, None, None,
                -1,
                None,
                on_spawn,
                None,
            )
        except TypeError:
            ok, pid = vte.spawn_sync(
                Vte.PtyFlags.DEFAULT, cwd, argv, envv,
                GLib.SpawnFlags.DEFAULT, None, None, None,
            )
            if ok:
                self._vte_pids[sid] = pid
                started_at = time.monotonic()
                self._runtime_started_at[sid] = started_at
                self._runtime_last_activity[sid] = started_at
                self._set_runtime_state(sid, "live", "resumed" if resume else "started")
                print(f"[vte] {sid[:8]} pid={pid} (sync)", flush=True)
                pending_sleep = self._runtime_sleep_after_wake.pop(sid, None)
                if pending_sleep:
                    self._sleep_runtime(sid, pending_sleep)
                    return
                if focus:
                    self._queue_vte_focus(vte)
                if agent == "codex" and resume:
                    self._schedule_codex_resume_stabilizers(sid, vte)
            else:
                pending_sleep = self._runtime_sleep_after_wake.pop(sid, None)
                if pending_sleep:
                    self._set_runtime_state(sid, "asleep", pending_sleep)
                    self._paint_runtime_asleep(sid)
                else:
                    self._set_runtime_state(sid, "crashed", "spawn-error")

    def _hide_stack(self):
        """Collapse the terminal overlay to zero area, keeping every VTE mapped.

        We never actually unmap the stack — that's what makes re-showing fast.
        """
        visible = [
            sid for sid in (
                self._split_sids if self._split_active else (
                    self._stack.get_visible_child_name(),
                )
            ) if sid and sid != "__blank__"
        ]
        self._stack_hidden = True
        self._stack.set_visible_child_name("__blank__")
        # === SPLIT FEATURE === (a code-off should also dismantle any active split)
        if self._split_active:
            self._exit_split(None, queue_resize=False)
        # === SPLIT FEATURE END ===
        self._runtime_focus_sid = None
        self._split_pinned = False
        self._evict_runtime_sids(visible)
        self.overlay.queue_resize()

    # ------------------------------------------------------------------
    # === SPLIT FEATURE === (split-pane mount/dismount)
    # ------------------------------------------------------------------
    def _ensure_vte(self, sid: str, cwd: str, agent: str, is_new: bool = False,
                    deferred_spawn: bool = False) -> tuple[Vte.Terminal, bool, str]:
        """Return (vte, is_new_vte, resolved_cwd). If deferred_spawn is True
        the VTE is built but the agent process is NOT spawned yet — caller
        is responsible for calling _finish_vte_spawn(vte, sid, cwd, agent)
        once the VTE has a real allocation."""
        if sid in self._vtes:
            # Reuse only if the agent process is still alive. A dead VTE
            # (codex exited/hung/killed) would otherwise be re-shown as a
            # blank pane in split view. Tear it down so we respawn fresh.
            pid = self._vte_pids.get(sid)
            alive = False
            if pid:
                try:
                    os.kill(pid, 0)
                    alive = True
                except (ProcessLookupError, OSError):
                    alive = False
            if alive:
                return self._vtes[sid], False, cwd
            if self._runtime_state.get(sid) in ("asleep", "sleeping", "waking"):
                return self._vtes[sid], False, cwd
            print(f"[ensure_vte] {sid[:8]} stale (pid={pid} gone), respawning", flush=True)
            stale_vte = self._vtes.pop(sid, None)
            self._vte_pids.pop(sid, None)
            if stale_vte is not None:
                try:
                    self._detach_vte(stale_vte)
                    stale_vte.destroy()
                except Exception as e:
                    print(f"[ensure_vte] stale teardown failed: {e}", flush=True)
        if not cwd or not os.path.isdir(cwd):
            try:
                session = get_session(sid) if sid else None
                raw = (session or {}).get("cwd") or (session or {}).get("project_dir") or ""
                cwd = resolve_session_cwd(raw)
            except Exception:
                cwd = str(Path.home())
        session_obj = None
        try:
            session_obj = get_session(sid) if sid else None
        except Exception:
            pass
        if not is_new and session_obj:
            ensure_session_visible(sid, session_obj.get("project_dir", ""), cwd)
        agent = agent or (session_obj or {}).get("agent") or "claude"
        self._remember_runtime(sid, cwd, agent)
        vte = self._build_vte(sid)
        self._vtes[sid] = vte
        if deferred_spawn:
            return vte, True, cwd
        # Default path: park in stack and spawn immediately (single-view)
        self._stack.add_named(vte, sid)
        vte.show()
        self._spawn_claude(vte, sid, cwd, resume=not is_new, agent=agent)
        self._eval_js(f"window.onGtkCodeStart && window.onGtkCodeStart({json.dumps(sid)});")
        self._eval_js(
            f"window.onGtkCodeStarted && window.onGtkCodeStarted({json.dumps(sid)}, {json.dumps(cwd)});"
        )
        return vte, True, cwd

    def _finish_vte_spawn(self, vte: Vte.Terminal, sid: str, cwd: str,
                          agent: str, is_new: bool, focus: bool = True) -> None:
        """Spawn the agent into a VTE that's already parented and allocated."""
        self._spawn_claude(
            vte, sid, cwd, resume=not is_new, agent=agent, focus=focus
        )
        self._eval_js(f"window.onGtkCodeStart && window.onGtkCodeStart({json.dumps(sid)});")
        self._eval_js(
            f"window.onGtkCodeStarted && window.onGtkCodeStarted({json.dumps(sid)}, {json.dumps(cwd)});"
        )

    def _spawn_when_split_allocated(self, vte: Vte.Terminal, sid: str, cwd: str,
                                    agent: str, is_new: bool, focus: bool = True) -> None:
        """Wait until a split VTE has a real size before spawning the CLI.

        Codex snapshots the PTY size at startup. An idle callback can still run
        before GtkPaned has allocated its children, which leaves Codex drawing
        into a narrow/default terminal inside a full-width black VTE.
        """
        if sid in self._split_spawn_pending:
            return
        self._split_spawn_pending.add(sid)
        attempts = {"n": 0}

        def _try_spawn():
            if not self._split_active or sid not in self._split_sids:
                self._split_spawn_pending.discard(sid)
                return False
            attempts["n"] += 1
            width = vte.get_allocated_width()
            height = vte.get_allocated_height()
            ready = width > 80 and height > 80
            if ready or attempts["n"] >= 12:
                self._split_spawn_pending.discard(sid)
                print(
                    f"[split] spawning {sid[:8]} after alloc={width}x{height} attempts={attempts['n']}",
                    flush=True,
                )
                self._finish_vte_spawn(vte, sid, cwd, agent, is_new, focus)
                self._kick_split_winch()
                return False
            return True

        GLib.timeout_add(50, _try_spawn)

    def _detach_vte(self, vte: Vte.Terminal) -> None:
        """Remove a VTE from whatever parent currently holds it (stack or paned),
        WITHOUT destroying it. PyGObject keeps the python ref alive; the widget
        just becomes orphaned and ready to be reparented."""
        parent = vte.get_parent()
        if parent is None:
            return
        try:
            parent.remove(vte)
        except Exception as e:
            print(f"[split] detach failed: {e}", flush=True)

    def _enter_split(self, payload: dict) -> None:
        sids = payload.get("sids") or []
        if not isinstance(sids, list) or len(sids) != 2:
            print("[split] need exactly 2 sids", flush=True)
            return
        left_sid, right_sid = sids[0], sids[1]
        if not left_sid or not right_sid or left_sid == right_sid:
            return
        spawn_meta = payload.get("spawn_meta") or {}
        for candidate_sid in (left_sid, right_sid):
            candidate = spawn_meta.get(candidate_sid) or {}
            if not candidate.get("isNew") and meta_sync.external_runtime_active(candidate_sid):
                print(
                    f"[split] refusing external runtime {candidate_sid[:8]}",
                    flush=True,
                )
                self._eval_js(
                    "window.onGtkExternalRuntime && "
                    f"window.onGtkExternalRuntime({json.dumps(candidate_sid)});"
                )
                return
        previous_visible = [
            sid for sid in (
                self._split_sids if self._split_active else (
                    self._stack.get_visible_child_name(),
                )
            ) if sid and sid != "__blank__"
        ]
        meta = spawn_meta
        # Front-door seeds for split spawns — staged per-sid, consumed in
        # _spawn_claude when the deferred spawn actually fires.
        for _sid, _m in meta.items():
            if isinstance(_m, dict) and _m.get("isNew") and _m.get("seed"):
                self._pending_seeds = getattr(self, "_pending_seeds", {})
                self._pending_seeds[_sid] = str(_m["seed"])

        def _meta_for(sid: str) -> tuple[str, str, bool]:
            m = meta.get(sid) or {}
            return (m.get("cwd") or "", m.get("agent") or "claude", bool(m.get("isNew")))

        # Pre-warm rect so first paint is correct
        self._set_rect(payload.get("rect"))

        left_cwd, left_agent, left_new = _meta_for(left_sid)
        right_cwd, right_agent, right_new = _meta_for(right_sid)
        # Defer spawn for VTEs we have to build fresh — they should spawn
        # AFTER they're packed into the paned so codex's PTY sees the real
        # allocated size at startup, not the stack's zero size.
        left_vte, left_created, left_cwd = self._ensure_vte(
            left_sid, left_cwd, left_agent, left_new, deferred_spawn=True
        )
        right_vte, right_created, right_cwd = self._ensure_vte(
            right_sid, right_cwd, right_agent, right_new, deferred_spawn=True
        )
        self._remember_runtime(left_sid, left_cwd, left_agent)
        self._remember_runtime(right_sid, right_cwd, right_agent)

        if self._split_active:
            self._tear_down_paned()
        for ch in list(self._paned.get_children()):
            self._paned.remove(ch)

        self._detach_vte(left_vte)
        self._detach_vte(right_vte)
        self._paned.pack1(left_vte, True, True)
        self._paned.pack2(right_vte, True, True)
        left_vte.show()
        right_vte.show()

        # Initial ratio from payload, sanity-clamped to [0.15, 0.85] so a bad
        # persisted value can't crush either pane on first mount.
        ratio_hint = payload.get("ratio")
        if isinstance(ratio_hint, (int, float)) and 0.15 <= ratio_hint <= 0.85:
            self._split_ratio = float(ratio_hint)
        else:
            self._split_ratio = 0.5
        print(f"[split] enter ratio={self._split_ratio:.2f}", flush=True)

        # Hide the stack and mark split active before applying the ratio. The
        # overlay position callback collapses the paned to 0x0 while inactive.
        self._stack.set_visible_child_name("__blank__")
        self._stack_hidden = True
        self._split_active = True
        self._split_sids = (left_sid, right_sid)
        self._split_pinned = bool(payload.get("pin_both"))
        working_sids = {
            sid for sid in self._split_sids if self._runtime_is_working(sid)
        }
        focus_sid = self._select_split_focus(
            payload.get("focus_sid"), self._split_sids, working_sids
        )
        self._runtime_focus_sid = focus_sid
        self._paned.show()
        self.overlay.queue_resize()
        self._split_pos_settling = True
        self._apply_split_ratio()

        runtime_specs = (
            (left_sid, left_vte, left_cwd, left_agent, left_new, left_created),
            (right_sid, right_vte, right_cwd, right_agent, right_new, right_created),
        )
        for runtime_sid, vte, cwd, agent, is_new, created in runtime_specs:
            self._arm_vte_tail_follow(runtime_sid, vte)
            must_live = bool(
                self._split_pinned
                or runtime_sid == focus_sid
                or runtime_sid in working_sids
                or is_new
            )
            if created:
                self._spawn_when_split_allocated(
                    vte, runtime_sid, cwd, agent, is_new,
                    focus=runtime_sid == focus_sid,
                )
            elif must_live:
                self._wake_runtime(
                    runtime_sid,
                    "split-opened",
                    focus=runtime_sid == focus_sid,
                )
            else:
                current_state = self._runtime_state.get(runtime_sid)
                if current_state in ("asleep", "sleeping"):
                    self._wake_runtime(
                        runtime_sid, "split-prewarm", focus=False
                    )
                elif current_state:
                    self._set_runtime_state(
                        runtime_sid, current_state, "split-opened"
                    )

        focus_vte = self._vtes.get(focus_sid)
        if focus_vte is not None:
            self._queue_vte_focus(focus_vte)
        if not self._split_pinned:
            self._schedule_exclusive_runtime(focus_sid)
        self._evict_runtime_sids(previous_visible, {left_sid, right_sid})

        # Emit the divider position back to JS whenever it changes so we can
        # persist it across launches.
        self._wire_paned_persist()

        # Initial WINCH kick — both VTEs were just reparented from a wider stack
        # into a narrow paned half. Codex won't redraw without the signal.
        GLib.timeout_add(220, lambda: (self._kick_split_winch(), False)[1])

    def _apply_split_ratio(self) -> bool:
        """Initial position from the ratio. The size-allocate handler refines
        this every layout cycle, so we just need a reasonable first guess."""
        alloc = self._paned.get_allocated_width()
        if alloc <= 10:
            return False
        min_each = 200
        if alloc < 2 * min_each:
            target_pos = alloc // 2
        else:
            target_pos = max(min_each, min(int(alloc * self._split_ratio), alloc - min_each))
        self._last_programmatic_pos = target_pos
        self._paned.set_position(target_pos)
        self._paned.queue_resize()
        return True

    def _on_paned_allocate(self, widget, allocation):
        if not self._split_active or not self._terminal_view_visible:
            return
        alloc_w = allocation.width
        if alloc_w <= 10:
            return
        min_each = 200
        if alloc_w < 2 * min_each:
            target_pos = alloc_w // 2
        else:
            target_pos = max(min_each, min(int(alloc_w * self._split_ratio), alloc_w - min_each))
        cur = self._paned.get_position()
        # Wider drift threshold (30px) — GtkPaned snaps near children's preferred
        # widths, so a 6px threshold caused a feedback loop with the notify::
        # position handler recapturing slightly-off ratios.
        settling = self._split_pos_settling
        if settling or abs(cur - target_pos) > 30:
            self._last_programmatic_pos = target_pos
            self._paned.set_position(target_pos)
            # set_position inside size-allocate changes the property after the
            # current child allocation. Queue one more pass so the children,
            # not just get_position(), receive the restored ratio.
            self._paned.queue_resize()
            self._kick_split_winch()
        if settling:
            # notify::position from set_position is synchronous, so it was
            # safely ignored while this flag was still armed.
            self._split_pos_settling = False

    def _wire_paned_persist(self) -> None:
        if getattr(self, "_paned_pos_handler", None):
            try:
                self._paned.disconnect(self._paned_pos_handler)
            except Exception:
                pass
            self._paned_pos_handler = None
        if not hasattr(self, "_paned_winch_token"):
            self._paned_winch_token = 0
        def _on_pos(_paned, _gp):
            try:
                if self._split_pos_settling or not self._terminal_view_visible:
                    return
                pos = self._paned.get_position()
                alloc_w = self._paned.get_allocated_width()
                if alloc_w <= 10:
                    return
                # Distinguish programmatic from user drag using two signals:
                # 1. Was this position close to the LAST one we set programmatically?
                #    (GtkPaned snaps to child preferred widths near our target —
                #    those tiny adjustments shouldn't recapture as user intent.)
                # 2. Is this position close to our current ratio's expected value?
                last_set = getattr(self, "_last_programmatic_pos", None)
                if last_set is not None and abs(pos - last_set) <= 40:
                    return
                expected = int(alloc_w * self._split_ratio)
                if abs(pos - expected) <= 40:
                    return
                # Real user drag — recapture ratio for future resizes
                self._split_ratio = max(0.05, min(0.95, pos / float(alloc_w)))
                self._eval_js(
                    f"window.onGtkPanedPos && window.onGtkPanedPos({pos}, {self._split_ratio});"
                )
            except Exception:
                pass
            self._kick_split_winch()
        self._paned_pos_handler = self._paned.connect("notify::position", _on_pos)

    def _tear_down_paned(self) -> None:
        """Reparent VTEs out of the paned back into the stack, leaving the paned empty."""
        for ch in list(self._paned.get_children()):
            sid = None
            for s, v in self._vtes.items():
                if v is ch:
                    sid = s
                    break
            self._detach_vte(ch)
            if sid:
                # Re-add to the stack under its original name
                if self._stack.get_child_by_name(sid) is None:
                    self._stack.add_named(ch, sid)
                ch.show()

    def _exit_split(self, focus_sid: str | None = None, queue_resize: bool = True) -> None:
        if not self._split_active:
            return
        old_sids = tuple(sid for sid in self._split_sids if sid)
        self._tear_down_paned()
        self._split_active = False
        self._split_pos_settling = False
        self._split_sids = (None, None)
        self._split_pinned = False
        if focus_sid and focus_sid in self._vtes:
            self._stack.set_visible_child_name(focus_sid)
            self._stack_hidden = False
            self._runtime_focus_sid = focus_sid
            self._wake_runtime(focus_sid, "split-exit", focus=False)
            self._queue_vte_focus(self._vtes[focus_sid])
            self._evict_runtime_sids(old_sids, {focus_sid})
        else:
            self._stack.set_visible_child_name("__blank__")
            self._stack_hidden = True
            self._runtime_focus_sid = None
            self._evict_runtime_sids(old_sids)
        if queue_resize:
            self.overlay.queue_resize()
    # === SPLIT FEATURE END ===

    def _kill_session(self, sid: str | None):
        if not sid:
            return
        try:
            from core.voice_inbox import get_default_voice_inbox

            get_default_voice_inbox().finish_work_target(
                sid, error="working pane was closed"
            )
        except Exception:
            pass
        pid = self._vte_pids.pop(sid, None)
        print(f"[close] _kill_session sid={(sid or '')[:8]} pid={pid} "
              f"in_vtes={sid in self._vtes} agent={getattr(self, '_sid_agent', {}).get(sid)}", flush=True)
        if pid is not None:
            try:
                if self._runtime_state.get(sid) == "paused":
                    self._terminate_runtime_pid(pid, signal.SIGCONT)
                self._terminate_runtime_pid(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception as e:
                print(f"[vte] kill error: {e}", flush=True)
        vte = self._vtes.get(sid)
        self._stop_vte_tail_follow(sid, vte)
        self._vte_tail_scroll_pending.discard(sid)
        vte = self._vtes.pop(sid, None)
        if vte is not None:
            self._runtime_closed_vtes.add(id(vte))
            # === SPLIT FEATURE === (if killed sid is half of an active split, dismantle)
            if self._split_active and sid in self._split_sids:
                self._exit_split(None)
            # === SPLIT FEATURE END ===
            # If the user was looking at this one, fall back to the blank placeholder
            if self._stack.get_visible_child_name() == sid:
                self._stack.set_visible_child_name("__blank__")
                self._stack_hidden = True
                self.overlay.queue_resize()
            self._detach_vte(vte)
            vte.destroy()
        self._sid_agent.pop(sid, None)
        self._runtime_cwd.pop(sid, None)
        self._runtime_state.pop(sid, None)
        self._runtime_last_activity.pop(sid, None)
        self._runtime_last_output.pop(sid, None)
        self._runtime_file_paths.pop(sid, None)
        self._runtime_started_at.pop(sid, None)
        self._runtime_turn_offsets.pop(sid, None)
        self._runtime_stop_reasons.pop(sid, None)
        self._runtime_sleep_after_wake.pop(sid, None)
        self._runtime_wake_after_stop.discard(sid)
        self._input_drafts.pop(sid, None)
        with self._runtime_lock:
            self._runtime_busy.discard(sid)
            self._runtime_leases.pop(sid, None)
            self._runtime_work_reservations.pop(sid, None)
        # Notify JS so the sidebar marker clears immediately
        self._eval_js(f"window.onGtkCodeExit && window.onGtkCodeExit({json.dumps(sid)});")

    # === ATTENTION === (apply glow CSS to VTE panes whose chat finished)
    def _apply_attention_state(self, sids: list[str]) -> None:
        if not isinstance(sids, list):
            return
        self._ensure_attention_css_loaded()
        flagged = set(s for s in sids if isinstance(s, str))
        for sid, vte in list(self._vtes.items()):
            sc = vte.get_style_context()
            if sid in flagged:
                sc.add_class("needs-attention")
            else:
                sc.remove_class("needs-attention")

    def _ensure_attention_css_loaded(self) -> None:
        if getattr(self, "_attention_css_loaded", False):
            return
        try:
            from gi.repository import Gdk
            provider = Gtk.CssProvider()
            css = b"""
            terminal.needs-attention {
                box-shadow: inset 0 0 0 2px rgba(245, 166, 35, 0.85),
                            0 0 12px rgba(245, 166, 35, 0.45);
            }
            .needs-attention {
                border: 2px solid rgba(245, 166, 35, 0.85);
            }
            """
            provider.load_from_data(css)
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(),
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
            self._attention_css_loaded = True
        except Exception as e:
            print(f"[attention] css load failed: {e}", flush=True)
    # === ATTENTION END ===

    # === FOLDER PICKER ===
    def _open_folder_picker(self, payload: dict):
        """Pop a Gtk.FileChooserNative for folder selection, then call back
        into JS with the picked path (or null on cancel)."""
        token = payload.get("token") or ""
        title = payload.get("title") or "Choose a folder"
        start_dir = payload.get("startDir") or os.path.expanduser("~")
        try:
            chooser = Gtk.FileChooserNative.new(
                title,
                self,
                Gtk.FileChooserAction.SELECT_FOLDER,
                "Open",
                "Cancel",
            )
            try:
                if start_dir and os.path.isdir(start_dir):
                    chooser.set_current_folder(start_dir)
            except Exception:
                pass
            response = chooser.run()
            picked = None
            if response == Gtk.ResponseType.ACCEPT:
                picked = chooser.get_filename()
            chooser.destroy()
        except Exception as e:
            print(f"[picker] folder chooser failed: {e}", flush=True)
            picked = None
        # Always resolve — JS waits on this token even if picker errored.
        path_js = json.dumps(picked) if picked else "null"
        self._eval_js(
            f"window.__resolveFolderPick && window.__resolveFolderPick({json.dumps(token)}, {path_js});"
        )
    # === FOLDER PICKER END ===

    def _make_child_exit_handler(self, sid: str):
        def _on_child_exit(term, status):
            actual_sid = self._sid_for_vte(term) or sid
            try:
                if os.WIFSIGNALED(status):
                    why = f"killed by signal {os.WTERMSIG(status)}"
                elif os.WIFEXITED(status):
                    why = f"exit code {os.WEXITSTATUS(status)}"
                else:
                    why = f"raw status {status}"
            except Exception:
                why = f"raw status {status}"
            print(f"[vte] child-exited sid={(actual_sid or '')[:8]} "
                  f"agent={self._sid_agent.get(actual_sid)} ({why})", flush=True)
            if id(term) in self._runtime_closed_vtes:
                self._runtime_closed_vtes.discard(id(term))
                return

            self._vte_pids.pop(actual_sid, None)
            tail_follow = getattr(self, "_vte_tail_follow_until", None)
            if tail_follow is not None:
                tail_follow.pop(actual_sid, None)
            pending_scrolls = getattr(self, "_vte_tail_scroll_pending", None)
            if pending_scrolls is not None:
                pending_scrolls.discard(actual_sid)
            with suppress(AttributeError, RuntimeError):
                term.set_scroll_on_output(False)
            sleep_reason = self._runtime_stop_reasons.pop(actual_sid, None)
            if sleep_reason is not None:
                self._set_runtime_state(actual_sid, "asleep", sleep_reason)
                self._paint_runtime_asleep(actual_sid)
                if actual_sid in self._runtime_wake_after_stop:
                    self._runtime_wake_after_stop.discard(actual_sid)
                    GLib.idle_add(
                        lambda: (
                            self._wake_runtime(actual_sid, "wake-after-stop", True),
                            False,
                        )[1]
                    )
                return

            with self._runtime_lock:
                self._runtime_work_reservations.pop(actual_sid, None)
            self._set_runtime_state(actual_sid, "crashed", why)
            try:
                from core.voice_inbox import get_default_voice_inbox

                get_default_voice_inbox().finish_work_target(
                    actual_sid, error=f"working pane {why}"
                )
            except Exception:
                pass
            vte = self._vtes.pop(actual_sid, None)
            if vte is not None:
                # === SPLIT FEATURE === (one half died → leave split, keep survivor alive)
                if self._split_active and actual_sid in self._split_sids:
                    self._exit_split(None)
                # === SPLIT FEATURE END ===
                self._detach_vte(vte)
                vte.destroy()
            self._eval_js(
                f"window.onGtkCodeExit && window.onGtkCodeExit({json.dumps(actual_sid)});"
            )
        return _on_child_exit

    # ------------------------------------------------------------------
    # Drag-drop on VTE — same handler shared across all per-session VTEs
    # ------------------------------------------------------------------
    def _on_drag_data_received(self, widget, context, x, y, data, info, time_):
        from urllib.parse import unquote, urlparse

        sid = self._sid_for_vte(widget)
        reservation_reader = getattr(self, "runtime_work_reservation", None)
        if sid and callable(reservation_reader) and reservation_reader(sid):
            context.finish(False, False, time_)
            return

        if not data:
            context.finish(False, False, time_)
            return

        uris = data.get_uris() or []
        if not uris:
            text = data.get_text()
            if text:
                uris = [line.strip() for line in text.splitlines() if line.strip()]

        paths = []
        for uri in uris:
            if uri.startswith("file://"):
                paths.append(unquote(urlparse(uri).path))
            elif uri.startswith("/"):
                paths.append(uri)

        print(f"[drop] VTE received {len(paths)} path(s): {paths}", flush=True)

        for p in paths:
            quoted = "'" + p.replace("'", "'\\''") + "' "
            widget.feed_child(quoted.encode("utf-8"))
            self._draft_append(widget, p + " ")

        context.finish(True, False, time_)

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    def _eval_js(self, script: str):
        try:
            self.web.run_javascript(script, None, None, None)
        except Exception as e:
            print(f"[gtkbridge] run_javascript failed: {e}", flush=True)

    def _current_vte(self) -> Vte.Terminal | None:
        # Prefer the window's actual focus widget — this works for VTEs in
        # either the Stack OR the split Paned without case-by-case logic.
        focused = self.get_focus()
        if isinstance(focused, Vte.Terminal):
            return focused
        # === SPLIT FEATURE === (focus might be on the Paned's separator or
        # the window itself; fall back to the left split VTE so click-to-insert
        # and feed-text bridges still have a target)
        if self._split_active:
            for sid in self._split_sids:
                v = self._vtes.get(sid) if sid else None
                if v is not None:
                    return v
            return None
        # === SPLIT FEATURE END ===
        name = self._stack.get_visible_child_name()
        return self._vtes.get(name)

    def _focused_vte(self) -> Vte.Terminal | None:
        """Strict variant: only returns the VTE that actually has keyboard
        focus right now (or None). Used by key-press intercepts where firing
        on a non-focused VTE would route the action to the wrong terminal."""
        focused = self.get_focus()
        return focused if isinstance(focused, Vte.Terminal) else None

    def _focus_preferred_input(self) -> None:
        # When the window is focused, prefer an active VTE if one is visible.
        # Otherwise ensure the embedded WebView has focus so list/search/search-bar
        # keyboard handling works on startup / after tab restores.
        if not self._terminal_view_visible:
            self.web.grab_focus()
            return
        if self._split_active:
            for sid in self._split_sids:
                if sid and sid in self._vtes:
                    self._vtes[sid].grab_focus()
                    return
        else:
            sid = self._stack.get_visible_child_name()
            if sid and sid != "__blank__":
                vte = self._vtes.get(sid)
                if vte is not None:
                    vte.grab_focus()
                    return
        self.web.grab_focus()

    def _focus_preferred_input_if_unset(self) -> None:
        focused = self.get_focus()
        if focused is self.web or isinstance(focused, Vte.Terminal):
            return
        self._focus_preferred_input()

    def _focus_session(self, sid: str | None) -> None:
        if not sid:
            return
        vte = self._vtes.get(sid)
        if vte is None:
            return
        if self._split_active and sid not in self._split_sids:
            return
        if not self._split_active and self._stack.get_visible_child_name() != sid:
            return
        self._activate_runtime(sid, "code-focus")
        self._arm_vte_tail_follow(sid, vte)
        self._queue_vte_focus(vte)

    def _on_focus_in(self, *_):
        # Keep keyboard input flowing after window activation without stealing
        # focus back from a clicked WebKit input or VTE.
        GLib.idle_add(self._focus_preferred_input_if_unset)
        return False

    def _on_key_press(self, widget, event):
        state = event.state
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(state & Gdk.ModifierType.MOD1_MASK)
        key = Gdk.keyval_to_lower(event.keyval)

        focused_vte = self._focused_vte()
        focused_sid = self._sid_for_vte(focused_vte)
        if focused_sid and self.runtime_work_reservation(focused_sid):
            # The accepted work bridge feeds this VTE directly. Swallow every
            # manual keystroke until that exact item releases its reservation.
            return True

        # Copy/paste bindings:
        #   Ctrl+A  → copy current input draft to clipboard (typed-since-last-Enter)
        #   Ctrl+C  → copy IF there's a selection, else falls through to SIGINT
        #   Ctrl+V  → paste (also append clipboard text to draft)
        #   Ctrl+Shift+C/V → same (kept as fallbacks, standard terminal muscle memory)
        if ctrl and not alt:
            vte = self._focused_vte()
            if vte is not None:
                if key == Gdk.KEY_a and not shift:
                    # Copy in-progress input draft to clipboard. Doesn't disturb
                    # the running CLI — Ctrl+A is swallowed here and never
                    # reaches claude/codex's readline (which would treat it as
                    # "go to start of line").
                    draft = self._draft_get(vte)
                    if draft:
                        cb = Gtk.Clipboard.get_default(self.get_display())
                        cb.set_text(draft, -1)
                        print(f"[input] copied {len(draft)} chars to clipboard", flush=True)
                    return True
                if key == Gdk.KEY_c:
                    if vte.get_has_selection():
                        vte.copy_clipboard_format(Vte.Format.TEXT)
                        return True
                    # No selection — Ctrl+C is SIGINT to claude, which clears
                    # whatever was being typed. Reset our shadow draft too.
                    self._draft_set(vte, "")
                    sid = self._sid_for_vte(vte)
                    if sid:
                        with self._runtime_lock:
                            self._runtime_busy.discard(sid)
                            self._runtime_turn_offsets.pop(sid, None)
                    return False
                if key == Gdk.KEY_v:
                    cb = Gtk.Clipboard.get_default(self.get_display())
                    # 1) Image on clipboard (Ctrl+C in a screenshot tool, Ctrl+V
                    #    here). VTE's paste_clipboard only handles text — for
                    #    images we save to a temp file and feed the path the
                    #    same way drag-drop does, which is what claude expects.
                    pixbuf = cb.wait_for_image()
                    if pixbuf is not None:
                        try:
                            import tempfile, time
                            from pathlib import Path
                            from uuid import uuid4
                            upload_dir = Path(tempfile.gettempdir()) / "serena-chats-uploads"
                            upload_dir.mkdir(parents=True, exist_ok=True)
                            dest = upload_dir / f"{uuid4().hex}.png"
                            pixbuf.savev(str(dest), "png", [], [])
                            quoted = "'" + str(dest).replace("'", "'\\''") + "' "
                            vte.feed_child(quoted.encode("utf-8"))
                            self._draft_append(vte, str(dest) + " ")
                            print(f"[paste-image] saved {dest}", flush=True)
                        except Exception as e:
                            print(f"[paste-image] failed: {e}", flush=True)
                        return True
                    # 2) File URIs on clipboard (Ctrl+C on a file in Nautilus,
                    #    Ctrl+V here). Same flow as drop — quote + feed each path.
                    uris = cb.wait_for_uris() or []
                    if uris:
                        from urllib.parse import unquote, urlparse
                        any_fed = False
                        for uri in uris:
                            if uri.startswith("file://"):
                                p = unquote(urlparse(uri).path)
                            elif uri.startswith("/"):
                                p = uri
                            else:
                                continue
                            quoted = "'" + p.replace("'", "'\\''") + "' "
                            vte.feed_child(quoted.encode("utf-8"))
                            self._draft_append(vte, p + " ")
                            any_fed = True
                        if any_fed:
                            return True
                    # 3) Plain text — existing flow
                    pasted = cb.wait_for_text() or ""
                    if pasted:
                        self._draft_append(vte, pasted)
                    vte.paste_clipboard()
                    return True

        # Terminal input remaps — translate keyboard events that the kernel/xterm
        # spec can't distinguish into the actual control bytes claude expects.
        # These only fire when VTE has keyboard focus.
        vte_focused = self._focused_vte()
        if vte_focused is not None:
            # Ctrl+Backspace → Ctrl+W (0x17): readline "delete previous word".
            # Pop last word from our shadow draft as well.
            if ctrl and not shift and not alt and event.keyval == Gdk.KEY_BackSpace:
                draft = self._draft_get(vte_focused)
                if draft:
                    rstripped = draft.rstrip()
                    last_space = max(rstripped.rfind(" "), rstripped.rfind("\n"))
                    self._draft_set(
                        vte_focused,
                        rstripped[: last_space + 1] if last_space >= 0 else "",
                    )
                vte_focused.feed_child(b"\x17")
                return True
            # Shift+Enter → Ctrl+J (0x0a): claude CLI insert-newline in multiline input.
            if shift and not ctrl and not alt and event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                self._draft_append(vte_focused, "\n")
                vte_focused.feed_child(b"\x0a")
                return True
            # Plain Enter → submit, clear our shadow draft.
            if not ctrl and not shift and not alt and event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                submitted = self._draft_get(vte_focused)
                self._draft_set(vte_focused, "")
                sid = self._sid_for_vte(vte_focused)
                if sid and submitted.strip():
                    self.runtime_begin_turn(sid)
                # fall through — Enter passes to VTE normally
            # Backspace → pop last char from draft.
            elif not ctrl and not shift and not alt and event.keyval == Gdk.KEY_BackSpace:
                draft = self._draft_get(vte_focused)
                if draft:
                    self._draft_set(vte_focused, draft[:-1])
                # fall through
            else:
                # Printable char → append to shadow draft. Skip when ctrl/alt
                # are held (those are control sequences, not input text).
                if not ctrl and not alt:
                    cp = Gdk.keyval_to_unicode(event.keyval)
                    if cp:
                        ch = chr(cp)
                        if ch.isprintable() and ch != "\x7f":
                            self._draft_append(vte_focused, ch)

        # User-configurable shortcuts (default: Alt+<letter>). Match the event
        # against the exact (keyval, modmask) entries from the config.
        mods_only = int(state) & int(_RELEVANT_MASKS)
        action = self._shortcut_map.get((event.keyval, mods_only)) \
            or self._shortcut_map.get((key, mods_only))
        if action is None:
            return False
        # With the xterm backend, terminal-specific actions must continue into
        # WebKit. JS owns the PTY reference and translates them there.
        if action.startswith("term-") and self._focused_vte() is None:
            return False
        # A malformed no-modifier binding would swallow ordinary typing before
        # WebKit or VTE sees it. App shortcuts must be modified keys.
        if mods_only == 0:
            print(f"[keybindings] ignored unmodified shortcut action={action}", flush=True)
            return False
        if self._focused_vte() is None:
            self.web.grab_focus()
        self._eval_js(f"window.__gtkShortcut && window.__gtkShortcut({json.dumps(action)})")
        return True

    def _on_destroy(self, *_):
        for source in (
            getattr(self, "_runtime_sweep_source", 0),
            getattr(self, "_voice_inbox_source", 0),
        ):
            if source:
                try:
                    GLib.source_remove(source)
                except Exception:
                    pass
        for sid in list(self._vte_pids):
            self._kill_session(sid)
        if not self._use_native_vte:
            # WebSocket teardown normally closes renderer-owned PTYs. Kill the
            # local registry too so a hard window close cannot orphan a CLI.
            from ui import pty_terminal

            pty_terminal.kill_all()
        Gtk.main_quit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(width: int = 1400, height: int = 900) -> None:
    global _LOG_PATH
    if _LOG_PATH is None:
        _LOG_PATH = _install_file_logging()
        print(f"[boot] logging to {_LOG_PATH}", flush=True)
    # === STARTUP === Fast path: get Flask + the GTK window up FIRST, then do
    # the heavy work (index scans, MCP sync, MCP multiplex spawn) in
    # background threads. Previously these ran serially before Flask started,
    # adding 8-12s of perceived load time on every boot. Now the UI appears
    # in ~1s; backend work catches up while Raghav is reading the sidebar.

    # MCP sync is fast IF the master + agent configs haven't changed (a hash
    # check short-circuits the subprocess loop). Run it inline only if it's
    # going to skip; otherwise defer to background.
    # Always capture diagnostics to a file so chat-death / focus issues are
    # debuggable no matter how the app was launched — detached launchers send
    # stdout to /dev/null otherwise. Fresh file each launch.
    try:
        import sys as _sys
        _logp = Path.home() / ".config" / "serena" / "desktop.log"
        _logp.parent.mkdir(parents=True, exist_ok=True)
        _logf = open(_logp, "w", buffering=1)
        _sys.stdout = _logf
        _sys.stderr = _logf
    except Exception:
        pass

    boot_t0 = time.monotonic()
    print(f"[boot] launching @ {time.strftime('%H:%M:%S')}", flush=True)

    def _bg_indexing():
        t = time.monotonic()
        print("[bg] indexing sessions...", flush=True)
        try:
            update_index()
        except Exception as e:
            print(f"[bg] update_index failed: {e}", flush=True)
        print(f"[bg] sessions indexed in {time.monotonic()-t:.1f}s", flush=True)
        t = time.monotonic()
        try:
            update_knowledge_index()
        except Exception as e:
            print(f"[bg] update_knowledge_index failed: {e}", flush=True)
        print(f"[bg] knowledge indexed in {time.monotonic()-t:.1f}s", flush=True)

    def _bg_mcp():
        # === RETIRED (2026-06-04) === The multiplexer + config sync are dead:
        # MCP servers now run as Docker containers on the PC tailnet
        # (Projects/mcp_servers/gateway, ports 8801+), registered directly in
        # the Syncthing-shared ~/.claude.json with tailnet URLs that work from
        # every machine. The old sync kept re-injecting 127.0.0.1:17541
        # multiplexer URLs into the shared config — dead on any machine not
        # running the multiplexer — causing claude's per-turn "retrying…" spam.
        print("[bg] mcp multiplex/sync retired — servers live on the tailnet gateway", flush=True)

    def _bg_attention():
        try:
            from core import codex_attention_watcher
            codex_attention_watcher.start()
        except Exception as e:
            print(f"[bg] attention watcher failed: {e}", flush=True)

    def _bg_frontdoor():
        # The front-door banner greeting is local JS now (instant, no model
        # call), so there's nothing to pre-warm. Kept as a hook point.
        pass

    def _bg_archive():
        try:
            from core.locket_archive_sync import start_auto_sync

            start_auto_sync()
        except Exception as e:
            print(f"[bg] archive sync failed: {e}", flush=True)

    # Kick off background work in parallel — daemon threads so they die on exit
    for fn in (_bg_indexing, _bg_mcp, _bg_attention, _bg_archive, _bg_frontdoor):
        threading.Thread(target=fn, daemon=True).start()

    host = "127.0.0.1"
    port = _find_free_port()
    url = f"http://{host}:{port}"

    threading.Thread(target=_serve, args=(host, port), daemon=True).start()
    if not _wait_for_server(url):
        print(f"Flask didn't start at {url}", flush=True)
        return

    print(f"[boot] window opening @ {time.monotonic()-boot_t0:.2f}s", flush=True)
    win = ChatsApp(url, width, height)
    win.maximize()
    win.show_all()
    GLib.idle_add(win._focus_preferred_input)
    Gtk.main()


if __name__ == "__main__":
    run()
