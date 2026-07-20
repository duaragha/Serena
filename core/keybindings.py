"""Shared keybinding loader.

Both the Linux GTK shell (desktop/app_gtk.py) and the JS frontend (Windows /
macOS path through the WebView) consume the same `~/.config/serena/keybindings.json`
file via this module — Linux pulls (keyval, modmask) tuples for GTK matching,
Windows/macOS get a JS-friendly form via the /api/keybindings route.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

KEYBINDINGS_PATH = Path.home() / ".config" / "serena" / "keybindings.json"

DEFAULT_BINDINGS: dict[str, str] = {
    # ── Tab switching ───────────────────────────────────────────
    "view-chats":         "Alt+1",
    "view-memory":        "Alt+2",
    "view-knowledge":     "Alt+3",
    "view-usage":         "Alt+4",

    # ── Chat list navigation ────────────────────────────────────
    "next":               "Alt+j",
    "prev":               "Alt+k",
    "focus-search":       "Alt+slash",

    # ── Per-chat actions (Alt-prefixed; fire even with terminal focused)
    "toggle-done":        "Alt+d",
    "close-terminal":     "Alt+w",
    "delete":             "Alt+Delete",
    "rename":             "Alt+r",
    "retitle":            "Alt+t",
    "star":               "Alt+s",
    "resume-ext":         "Alt+o",
    "new-chat-external":  "Alt+n",
    "new-chat-pick-dir":  "Alt+Shift+n",
    "toggle-files":       "Alt+b",

    # ── In-terminal text editing (inside the live VTE / xterm) ──
    "term-newline":       "Shift+Return",       # Shift+Enter → insert \n
    "term-delete-word":   "Ctrl+BackSpace",     # delete previous word
    "term-copy":          "Ctrl+Shift+c",       # copy selection
    "term-paste":         "Ctrl+Shift+v",       # paste clipboard
}


def load_combos() -> dict[str, str]:
    """Return action → combo string, merging the user file over defaults.
    Auto-extends an existing user file with any defaults it's missing so new
    actions show up without forcing a manual file edit."""
    merged = dict(DEFAULT_BINDINGS)
    try:
        if KEYBINDINGS_PATH.exists():
            user = json.loads(KEYBINDINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                for action, combo in user.items():
                    if isinstance(combo, str):
                        merged[action] = combo
                missing = {a: c for a, c in DEFAULT_BINDINGS.items() if a not in user}
                if missing:
                    user_extended = dict(user)
                    user_extended.update(missing)
                    KEYBINDINGS_PATH.write_text(
                        json.dumps(user_extended, indent=2) + "\n", encoding="utf-8"
                    )
        else:
            KEYBINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            KEYBINDINGS_PATH.write_text(
                json.dumps(DEFAULT_BINDINGS, indent=2) + "\n", encoding="utf-8"
            )
    except Exception as e:
        print(f"[keybindings] load failed: {e}", flush=True)
    return merged


_KEYNAME_TO_JS = {
    # GTK key-name → JS event.key value
    "delete":     "Delete",
    "backspace":  "Backspace",
    "return":     "Enter",
    "enter":      "Enter",
    "escape":     "Escape",
    "tab":        "Tab",
    "space":      " ",
    "slash":      "/",
    "minus":      "-",
    "equal":      "=",
    "comma":      ",",
    "period":     ".",
    "semicolon":  ";",
    "apostrophe": "'",
    "grave":      "`",
    "left":       "ArrowLeft",
    "right":      "ArrowRight",
    "up":         "ArrowUp",
    "down":       "ArrowDown",
    "home":       "Home",
    "end":        "End",
    "pageup":     "PageUp",
    "pagedown":   "PageDown",
    "page_up":    "PageUp",
    "page_down":  "PageDown",
}


def parse_combo_for_js(combo: str) -> dict | None:
    """Parse 'Alt+d' / 'Ctrl+Shift+X' / 'Alt+Delete' into a JS-friendly shape:
    {"key": "d", "alt": true, "ctrl": false, "shift": false, "meta": false}.

    The "key" field is normalized to match what JS event.key would report
    (single letter for letters, named values for special keys)."""
    if not combo:
        return None
    parts = [p.strip() for p in re.split(r"\+", combo) if p.strip()]
    if not parts:
        return None
    out = {"alt": False, "ctrl": False, "shift": False, "meta": False}
    for mod in parts[:-1]:
        m = mod.lower()
        if m in ("alt", "mod1"):
            out["alt"] = True
        elif m == "ctrl" or m == "control":
            out["ctrl"] = True
        elif m == "shift":
            out["shift"] = True
        elif m == "super" or m == "meta":
            out["meta"] = True
        else:
            return None
    key_name = parts[-1]
    if len(key_name) == 1:
        out["key"] = key_name.lower()
    else:
        out["key"] = _KEYNAME_TO_JS.get(key_name.lower(), key_name)
    return out


def load_for_js() -> dict[str, dict]:
    """Return action → JS combo descriptor, ready to send to the frontend."""
    out: dict[str, dict] = {}
    for action, combo in load_combos().items():
        parsed = parse_combo_for_js(combo)
        if parsed is not None:
            out[action] = parsed
    return out
