"""Native GTK shell for Linux.

Same Flask backend, same web UI, but the "Code" pane is a real Vte.Terminal
positioned over the WebView via GtkOverlay. Gives us native drag-drop (VTE is
the widget gnome-terminal is built from) without giving up the web UI for
everything else.

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
import threading
import time
import urllib.request
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
gi.require_version("WebKit2", "4.1")
gi.require_version("Gdk", "3.0")

from gi.repository import Gdk, Gio, GLib, Gtk, Vte, WebKit2  # noqa: E402

# PCRE2 compile flags used by VTE.Regex
_PCRE2_MULTILINE = 0x00000400
_PCRE2_UTF = 0x00080000
_PCRE2_CASELESS = 0x00000008

# URL regex — simplified version of what gnome-terminal uses. Catches http(s),
# ftp, file, ssh, mailto, plus plain www.*.
_URL_REGEX = (
    r"(?:(?:(?:https?|ftp|file|ssh|telnet)://)"
    r"|(?:mailto:)"
    r"|(?:www\.))"
    r"[-[:alnum:]\\Q,?;.:/!*%$^&#~=+@|()\\E]*"
    r"[-[:alnum:]\\Q/!*%$^&#~=+@|()\\E]"
)

from core.indexer import get_session, update_index, update_knowledge_index  # noqa: E402
from core.config import ensure_session_visible, resolve_session_cwd  # noqa: E402
from core import metadata as meta_sync  # noqa: E402
from ui.web import app as flask_app  # noqa: E402


def _snapshot_default_model() -> str | None:
    """Read the current global claude model once at app start, so later /model
    writes (ours or the user's) don't leak into unpinned sessions."""
    try:
        import json as _json
        p = Path.home() / ".claude" / "settings.json"
        if not p.exists():
            return None
        data = _json.loads(p.read_text(encoding="utf-8"))
        return data.get("model") or None
    except Exception:
        return None


_DEFAULT_MODEL_SNAPSHOT = _snapshot_default_model()


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

BOOT_SCRIPT = r"""
window.__gtkBridge = true;
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
# Keybindings — user-customizable via ~/.config/serena/keybindings.json
# ---------------------------------------------------------------------------

_KEYBINDINGS_PATH = Path.home() / ".config" / "serena" / "keybindings.json"

_DEFAULT_BINDINGS: dict[str, str] = {
    "toggle-done":        "Alt+d",
    "close-terminal":     "Alt+w",
    "next":               "Alt+j",
    "prev":               "Alt+k",
    "delete":             "Alt+Delete",
    "rename":             "Alt+r",
    "retitle":            "Alt+t",
    "star":               "Alt+s",
    "resume-ext":         "Alt+o",
    "new-chat-external":  "Alt+n",
    "focus-search":       "Alt+slash",
    "view-chats":         "Alt+1",
    "view-memory":        "Alt+2",
    "view-knowledge":     "Alt+3",
    "view-usage":         "Alt+4",
    "toggle-files":       "Alt+b",
}

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
    # Accept lowercase letters, digits, and named keys ("Delete", "slash", "BackSpace")
    if len(key_name) == 1:
        key_name = key_name.lower()
    keyval = Gdk.keyval_from_name(key_name)
    if keyval == 0 or keyval is None:
        return None
    return keyval, mods


def _load_keybindings() -> dict[str, tuple[int, int]]:
    """Load user-overridden bindings, merged over defaults. Creates the config file
    with defaults on first run so it's discoverable."""
    merged = dict(_DEFAULT_BINDINGS)
    try:
        if _KEYBINDINGS_PATH.exists():
            user = json.loads(_KEYBINDINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                for action, combo in user.items():
                    if isinstance(combo, str):
                        merged[action] = combo
        else:
            _KEYBINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            _KEYBINDINGS_PATH.write_text(
                json.dumps(_DEFAULT_BINDINGS, indent=2) + "\n", encoding="utf-8"
            )
            print(f"[keybindings] wrote defaults to {_KEYBINDINGS_PATH}", flush=True)
    except Exception as e:
        print(f"[keybindings] load failed: {e}", flush=True)

    parsed: dict[str, tuple[int, int]] = {}
    for action, combo in merged.items():
        pk = _parse_shortcut(combo)
        if pk is not None:
            parsed[action] = pk
        elif combo:
            print(f"[keybindings] ignoring invalid binding for {action!r}: {combo!r}", flush=True)
    return parsed


class ChatsApp(Gtk.Window):
    def __init__(self, url: str, width: int, height: int):
        super().__init__(title="Chats")
        self.set_default_size(width, height)
        self.connect("destroy", self._on_destroy)
        self.connect("key-press-event", self._on_key_press)

        # (keyval, modmask) → action. Customizable via ~/.config/serena/keybindings.json
        self._shortcut_map: dict[tuple[int, int], str] = {}
        for action, (keyval, mods) in _load_keybindings().items():
            self._shortcut_map[(keyval, mods)] = action

        self.overlay = Gtk.Overlay()
        self.add(self.overlay)

        # Base: WebView
        self.web = WebKit2.WebView()
        settings = self.web.get_settings()
        settings.set_enable_developer_extras(True)
        settings.set_javascript_can_access_clipboard(True)
        settings.set_enable_write_console_messages_to_stdout(True)
        self.overlay.add(self.web)

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
        self.overlay.add_overlay(self._stack)
        self.overlay.set_overlay_pass_through(self._stack, False)
        self.overlay.connect("get-child-position", self._on_overlay_position)
        self._stack_hidden = True  # tracked logically via rect, not via widget visibility

        self._vte_rect: tuple[int, int, int, int] | None = None
        self._vtes: dict[str, Vte.Terminal] = {}
        self._vte_pids: dict[str, int] = {}
        self._font = self._pick_mono_font()

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
        if child is not self._stack:
            return False
        if self._stack_hidden or self._vte_rect is None:
            # Collapse to zero-area so the overlay doesn't paint over the webview.
            allocation.x = 0
            allocation.y = 0
            allocation.width = 0
            allocation.height = 0
            return True
        x, y, w, h = self._vte_rect
        allocation.x = int(x)
        allocation.y = int(y)
        allocation.width = max(int(w), 10)
        allocation.height = max(int(h), 10)
        return True

    def _set_rect(self, rect: dict | None):
        if not rect:
            return
        self._vte_rect = (
            rect.get("x", 0),
            rect.get("y", 0),
            rect.get("w", 800),
            rect.get("h", 600),
        )
        self.overlay.queue_resize()

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
        elif kind == "code-rect":
            self._set_rect(payload.get("rect"))
        elif kind == "code-close":
            self._kill_session(payload.get("sid"))
        elif kind == "feed-text":
            # Click-to-insert from the files pane → type into the visible VTE
            sid = payload.get("sid")
            text = payload.get("text", "")
            vte = self._vtes.get(sid) if sid else self._current_vte()
            if vte is not None and text:
                try:
                    vte.feed_child(text.encode("utf-8"))
                    GLib.idle_add(vte.grab_focus)
                except Exception as e:
                    print(f"[bridge] feed-text failed: {e}", flush=True)
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
            try:
                self._stack.child_set_property(vte, "name", new)
            except Exception as e:
                print(f"[migrate] stack rename failed: {e}", flush=True)
                return
            self._vtes[new] = vte
            self._vtes.pop(old, None)
            if old in self._vte_pids:
                self._vte_pids[new] = self._vte_pids.pop(old)
            print(f"[migrate] {old[:8]} → {new[:8]}", flush=True)

    # ------------------------------------------------------------------
    # Session lifecycle — per-session VTE in the stack
    # ------------------------------------------------------------------
    def _show_session(self, payload: dict):
        sid = payload.get("sid")
        if not sid:
            return
        t0 = time.monotonic()

        # Pre-warm the rect BEFORE swapping the visible child so first paint
        # happens at the correct allocation (no size-jump on first frame).
        self._stack_hidden = False
        self._set_rect(payload.get("rect"))

        if sid in self._vtes:
            # Existing session: O(1) child swap. Claude stays alive.
            if self._stack.get_visible_child_name() != sid:
                self._stack.set_visible_child_name(sid)
            GLib.idle_add(self._vtes[sid].grab_focus)
            print(f"[swap] {sid[:8]} swap took {(time.monotonic()-t0)*1000:.1f}ms", flush=True)
            return

        # First time seeing this session — spawn a new VTE + claude.
        is_new = bool(payload.get("isNew"))
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

        # For brand-new chats we don't pass -r <tempId> to claude.
        self._spawn_claude(vte, sid, cwd, resume=not is_new)
        self._eval_js(f"window.onGtkCodeStart && window.onGtkCodeStart({json.dumps(sid)});")
        # Tell JS the RESOLVED cwd so the pseudo reconciler can match on cwd
        # when the real session file appears on disk.
        self._eval_js(
            f"window.onGtkCodeStarted && window.onGtkCodeStarted({json.dumps(sid)}, {json.dumps(cwd)});"
        )

    def _build_vte(self, sid: str) -> Vte.Terminal:
        vte = Vte.Terminal()
        vte.set_scrollback_lines(5000)
        vte.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
        vte.set_mouse_autohide(True)
        vte.set_colors(_FG, _BG, None)
        vte.set_color_cursor(_CURSOR)
        if self._font:
            vte.set_font(self._font)
        # Turn on OSC-8 hyperlink parsing (terminals that emit it will show clickable links)
        try:
            vte.set_allow_hyperlink(True)
        except Exception:
            pass

        # Register URL regex matching so plain http://... gets detected and clickable.
        self._register_url_match(vte)
        vte.connect("button-press-event", self._on_vte_button_press)

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

    def _on_vte_button_press(self, vte: Vte.Terminal, event):
        # Left-click with Ctrl → open URL in the user's default browser.
        # Plain left-click falls through to VTE for selection.
        if event.button != 1:
            return False
        if not (event.state & Gdk.ModifierType.CONTROL_MASK):
            return False

        uri = None
        # OSC-8 hyperlink takes priority (apps like newer claude may emit them)
        try:
            uri = vte.hyperlink_check_event(event)
        except Exception:
            uri = None
        if not uri:
            try:
                match_text, _tag = vte.match_check_event(event)
                if match_text:
                    uri = match_text
                    if uri.startswith("www."):
                        uri = "http://" + uri
            except Exception:
                pass

        if not uri:
            return False

        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
            return True
        except Exception as e:
            print(f"[vte] launch_default_for_uri failed: {e}", flush=True)
            return False

    def _spawn_claude(self, vte: Vte.Terminal, sid: str, cwd: str, resume: bool = True):
        claude_bin = shutil.which("claude") or "claude"
        argv = [claude_bin, "--dangerously-skip-permissions"]

        # Resolve model with strict isolation so global-setting drift can't leak
        # across sessions. Priority: explicit pin → session's historical model
        # from the indexer → Serena's snapshot of global at launch.
        chosen_model: str | None = None
        chosen_effort: str | None = None
        try:
            m = meta_sync.get_meta(sid) if sid else {}
            chosen_model = m.get("model") or None
            chosen_effort = m.get("effort") or None
        except Exception as e:
            print(f"[spawn] meta lookup failed: {e}", flush=True)

        if not chosen_model and sid and not sid.startswith("new-"):
            try:
                s = get_session(sid)
                if s and s.get("model"):
                    chosen_model = s["model"]
            except Exception:
                pass
        if not chosen_model:
            chosen_model = _DEFAULT_MODEL_SNAPSHOT

        if chosen_model:
            argv += ["--model", str(chosen_model)]
        if chosen_effort:
            argv += ["--effort", str(chosen_effort)]

        if resume and sid:
            argv += ["-r", sid]
        envv = [f"{k}={v}" for k, v in os.environ.items()]

        t0 = time.monotonic()
        print(f"[vte] spawning cwd={cwd} sid={sid[:8]}", flush=True)

        def on_spawn(term, pid, error, _user):
            dt = time.monotonic() - t0
            if error is not None:
                print(f"[vte] spawn error after {dt:.2f}s: {error.message}", flush=True)
                return
            self._vte_pids[sid] = pid
            print(f"[vte] {sid[:8]} pid={pid} after {dt:.2f}s", flush=True)
            GLib.idle_add(vte.grab_focus)

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
                print(f"[vte] {sid[:8]} pid={pid} (sync)", flush=True)
                GLib.idle_add(vte.grab_focus)

    def _hide_stack(self):
        """Collapse the terminal overlay to zero area, keeping every VTE mapped.

        We never actually unmap the stack — that's what makes re-showing fast.
        """
        self._stack_hidden = True
        self._stack.set_visible_child_name("__blank__")
        self.overlay.queue_resize()

    def _kill_session(self, sid: str | None):
        if not sid:
            return
        pid = self._vte_pids.pop(sid, None)
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception as e:
                print(f"[vte] kill error: {e}", flush=True)
        vte = self._vtes.pop(sid, None)
        if vte is not None:
            # If the user was looking at this one, fall back to the blank placeholder
            if self._stack.get_visible_child_name() == sid:
                self._stack.set_visible_child_name("__blank__")
                self._stack_hidden = True
                self.overlay.queue_resize()
            self._stack.remove(vte)
            vte.destroy()
        # Notify JS so the sidebar marker clears immediately
        self._eval_js(f"window.onGtkCodeExit && window.onGtkCodeExit({json.dumps(sid)});")

    def _make_child_exit_handler(self, sid: str):
        def _on_child_exit(term, status):
            self._vte_pids.pop(sid, None)
            vte = self._vtes.pop(sid, None)
            if vte is not None:
                self._stack.remove(vte)
                vte.destroy()
            # Tell JS — it might currently be showing this session
            self._eval_js(f"window.onGtkCodeExit && window.onGtkCodeExit({json.dumps(sid)});")
        return _on_child_exit

    # ------------------------------------------------------------------
    # Drag-drop on VTE — same handler shared across all per-session VTEs
    # ------------------------------------------------------------------
    def _on_drag_data_received(self, widget, context, x, y, data, info, time_):
        from urllib.parse import unquote, urlparse

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
        name = self._stack.get_visible_child_name()
        return self._vtes.get(name)

    def _on_key_press(self, widget, event):
        state = event.state
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(state & Gdk.ModifierType.MOD1_MASK)
        key = Gdk.keyval_to_lower(event.keyval)

        # Copy/paste bindings:
        #   Ctrl+C  → copy IF there's a selection, else falls through to SIGINT
        #   Ctrl+V  → paste
        #   Ctrl+Shift+C/V → same (kept as fallbacks, standard terminal muscle memory)
        if ctrl and not alt:
            vte = self._current_vte()
            if vte is not None and vte.has_focus():
                if key == Gdk.KEY_c:
                    if vte.get_has_selection():
                        vte.copy_clipboard_format(Vte.Format.TEXT)
                        return True
                    # No selection — let Ctrl+C pass through to claude as SIGINT
                    return False
                if key == Gdk.KEY_v:
                    vte.paste_clipboard()
                    return True

        # Terminal input remaps — translate keyboard events that the kernel/xterm
        # spec can't distinguish into the actual control bytes claude expects.
        # These only fire when VTE has keyboard focus.
        vte_focused = self._current_vte()
        if vte_focused is not None and vte_focused.has_focus():
            # Ctrl+Backspace → Ctrl+W (0x17): readline "delete previous word"
            if ctrl and not shift and not alt and event.keyval == Gdk.KEY_BackSpace:
                vte_focused.feed_child(b"\x17")
                return True
            # Shift+Enter → Ctrl+J (0x0a): claude CLI insert-newline in multiline input
            if shift and not ctrl and not alt and event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                vte_focused.feed_child(b"\x0a")
                return True

        # User-configurable shortcuts (default: Alt+<letter>). Match the event
        # against the exact (keyval, modmask) entries from the config.
        mods_only = int(state) & int(_RELEVANT_MASKS)
        action = self._shortcut_map.get((event.keyval, mods_only)) \
            or self._shortcut_map.get((key, mods_only))
        if action is None:
            return False
        self.web.grab_focus()
        self._eval_js(f"window.__gtkShortcut && window.__gtkShortcut({json.dumps(action)})")
        return True

    def _on_destroy(self, *_):
        for sid in list(self._vte_pids):
            self._kill_session(sid)
        Gtk.main_quit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(width: int = 1400, height: int = 900) -> None:
    print("Updating session index...", flush=True)
    update_index()
    print("Updating knowledge index...", flush=True)
    update_knowledge_index()

    host = "127.0.0.1"
    port = _find_free_port()
    url = f"http://{host}:{port}"

    threading.Thread(target=_serve, args=(host, port), daemon=True).start()
    if not _wait_for_server(url):
        print(f"Flask didn't start at {url}", flush=True)
        return

    print(f"Opening GTK window -> {url}", flush=True)
    win = ChatsApp(url, width, height)
    win.maximize()
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    run()
