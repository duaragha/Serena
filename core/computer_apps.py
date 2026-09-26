"""His real desktop apps, driven through accessibility instead of his mouse.

Every GTK, Qt, LibreOffice and (with accessibility on) Chromium/Electron window
publishes a tree of its widgets on the session's AT-SPI bus: role, name, state,
text, value, and the actions a screen reader can invoke. Pressing a button,
setting a field's text or picking a list item through that bus goes straight to
the widget. His pointer never moves and his keyboard focus stays where he is
typing, so Serena can work in his apps while he works in others.

Windows are listed with refs (w3); a window's snapshot gives every visible
widget a ref (a17) valid until the next snapshot. Steps target a ref or a role
and name. When a step makes the app raise a window (a dialog, say) while he was
in another app, his focus is handed back at once; the dialog stays open behind
and still takes steps.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import os
import re
import subprocess
import time
import warnings
from pathlib import Path

from core.computer_platform import ComputerError

MAX_STEPS = 25
MAX_STEP_TIMEOUT = 20.0
CALL_TIMEOUT = 60.0
SNAPSHOT_NODES = 450
SNAPSHOT_CHARS = 14000
RESULT_SNAPSHOT_CHARS = 6000
VISIT_LIMIT = 4000
NAME_CHARS = 90
VALUE_CHARS = 120
READ_CHARS = 3000
DEAD_APP_SECONDS = 300.0
# An app that takes this long just to give its name and pid is hung or gone.
SLOW_APP_SECONDS = 0.7
WINDOW_ROLES = frozenset({"frame", "dialog", "window", "alert", "file chooser", "color chooser"})
INTERACTIVE = frozenset({
    "push button", "toggle button", "check box", "radio button", "menu item",
    "check menu item", "radio menu item", "combo box", "entry", "password text",
    "spin button", "slider", "link", "list item", "page tab", "tree item",
    "menu", "table cell", "tool bar item", "switch", "toggle", "terminal", "editbar",
})
TEXTUAL = frozenset({
    "label", "heading", "static", "paragraph", "text", "status bar", "alert",
    "notification", "caption", "document text", "tool tip", "page tab list",
    "dialog", "title bar", "header", "info bar",
})
# Browser-style role words the worker already uses, mapped to AT-SPI roles.
ROLE_ALIASES = {
    "button": {"push button", "toggle button"},
    "textbox": {"entry", "text", "password text", "terminal", "document text", "editbar"},
    "checkbox": {"check box", "check menu item", "switch"},
    "radio": {"radio button", "radio menu item"},
    "combobox": {"combo box"},
    "menuitem": {"menu item", "check menu item", "radio menu item"},
    "tab": {"page tab"},
    "listitem": {"list item"},
    "row": {"table row", "list item", "tree item"},
    "cell": {"table cell"},
    "spinbutton": {"spin button"},
}
PREFERRED_ACTIONS = ("click", "press", "activate", "jump", "toggle", "open", "select")
KINDS = ("press", "set_text", "check", "uncheck", "select", "set_value", "read", "wait_for", "menu")
STOPPED = "stopped: input is held or the session ended"


class AppsError(ComputerError):
    pass


def _clean(text, limit=NAME_CHARS):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _target(value, where):
    if not isinstance(value, dict) or not value:
        raise AppsError(f"{where} needs a target such as {{ref:'a12'}} or {{role:'button', name:'Save'}}")
    unknown = set(value) - {"ref", "role", "name", "exact", "nth"}
    if unknown:
        raise AppsError(f"{where} target has unknown keys: {', '.join(sorted(unknown))}")
    if "ref" in value:
        if not isinstance(value["ref"], str) or not re.fullmatch(r"a\d{1,6}", value["ref"]):
            raise AppsError(f"{where} ref must look like a12")
    elif not isinstance(value.get("name", ""), str) or not (value.get("role") or value.get("name")):
        raise AppsError(f"{where} target needs a ref, or a role and/or name")
    if "nth" in value and (type(value["nth"]) is not int or not 0 <= value["nth"] < 100):
        raise AppsError(f"{where} nth must be 0-99")


def validate_steps(steps):
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise AppsError(f"send 1-{MAX_STEPS} steps")
    for index, step in enumerate(steps, 1):
        where = f"step {index}"
        if not isinstance(step, dict):
            raise AppsError(f"{where} must be an object")
        kinds = [key for key in step if key in KINDS]
        if len(kinds) != 1:
            raise AppsError(f"{where} needs exactly one of: {', '.join(KINDS)}")
        kind = kinds[0]
        extra = set(step) - {kind, "text", "option", "value", "timeout"}
        if extra:
            raise AppsError(f"{where} has unknown keys: {', '.join(sorted(extra))}")
        timeout = step.get("timeout", 5)
        if not isinstance(timeout, (int, float)) or not 0 <= timeout <= MAX_STEP_TIMEOUT:
            raise AppsError(f"{where} timeout must be 0-{MAX_STEP_TIMEOUT:g} seconds")
        body = step[kind]
        if kind == "menu":
            if (not isinstance(body, list) or not 1 <= len(body) <= 6
                    or not all(isinstance(item, str) and item.strip() for item in body)):
                raise AppsError(f"{where} menu needs a path such as ['File', 'Save As…']")
            continue
        if kind == "wait_for":
            if not isinstance(body, dict):
                raise AppsError(f"{where} wait_for needs {{target}} or {{gone}}")
            keys = set(body) & {"target", "gone"}
            if len(keys) != 1 or set(body) - {"target", "gone"}:
                raise AppsError(f"{where} wait_for takes exactly one of target or gone")
            _target(body[keys.pop()], where)
            continue
        _target(body, where)
        if kind == "set_text" and (not isinstance(step.get("text"), str) or len(step["text"]) > 20000):
            raise AppsError(f"{where} set_text needs text (at most 20000 characters)")
        if kind == "select" and (not isinstance(step.get("option"), str) or not step["option"].strip()):
            raise AppsError(f"{where} select needs the option's name")
        if kind == "set_value" and not isinstance(step.get("value"), (int, float)):
            raise AppsError(f"{where} set_value needs a number")


class HisApps:
    """AT-SPI access to the windows on his display. One worker thread owns it."""

    def __init__(self, env, *, own_display=None):
        self.env = dict(env)
        self.display = self.env.get("DISPLAY", "")
        # Her nested desktop's apps share his accessibility bus; they are not his.
        self.own_display = own_display
        self.executor = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="computer-apps")
        self.windows = {}
        self.refs = {}
        self.ref_window = None
        self.next_ref = 0
        self.dead = {}
        self.displays = {}
        self.atspi = None
        self.scope = None

    # -- plumbing ----------------------------------------------------------
    def call(self, function, *args, timeout=CALL_TIMEOUT):
        future = self.executor.submit(function, *args)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise AppsError("his apps did not answer in time") from None

    def _bus(self):
        if self.atspi is None:
            import gi

            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi

            # One slow or hung app must cost about a second, not D-Bus's 25.
            Atspi.set_timeout(1000, 3000)
            self.atspi = Atspi
        return self.atspi

    def available(self):
        try:
            return self.call(lambda: self._bus().get_desktop(0) is not None, timeout=10)
        except Exception:
            return False

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _display_of(self, pid):
        if pid not in self.displays:
            display = None
            try:
                data = Path(f"/proc/{pid}/environ").read_bytes()
                for item in data.split(b"\0"):
                    if item.startswith(b"DISPLAY="):
                        display = item[8:].decode(errors="replace")
            except OSError:
                pass
            if display is None and self.own_display and self._has_windows_on(self.own_display, pid):
                # Chromium overwrites its environment block with its process
                # title, so ask her display whether it holds this pid's windows.
                display = self.own_display
            self.displays[pid] = display
        return self.displays[pid]

    def _has_windows_on(self, display, pid):
        try:
            found = subprocess.run(
                ["xdotool", "search", "--pid", str(pid)],
                env={**self.env, "DISPLAY": display},
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return False
        return bool(found)

    def _mine(self, pid):
        """A window on his display, not her nested desktop's, not our own HUD."""
        if pid == os.getpid():
            return False
        display = self._display_of(pid)
        if display is None:
            # No readable DISPLAY (Chromium, a sandbox): his only if it has
            # windows on his display.
            return not self.display or self._has_windows_on(self.display, pid)
        if self.own_display and display == self.own_display:
            return False
        return not self.display or display.split(".")[0] == self.display.split(".")[0]

    def _has(self, node, name):
        Atspi = self._bus()
        return node.get_state_set().contains(getattr(Atspi.StateType, name))

    @staticmethod
    def _ifaces(node):
        try:
            return set(node.get_interfaces())
        except Exception:
            return set()

    # Interfaces are called through their classes: an Accessible implements
    # them all, and a name like get_text is also a deprecated Accessible method.
    def _text(self, node, tail=False, limit=READ_CHARS):
        Atspi = self._bus()
        if "Text" not in self._ifaces(node):
            return None
        count = Atspi.Text.get_character_count(node)
        start = max(0, count - limit) if tail else 0
        return Atspi.Text.get_text(node, start, min(count, start + limit))

    def _number(self, node):
        Atspi = self._bus()
        if "Value" not in self._ifaces(node):
            return None
        return Atspi.Value.get_current_value(node)

    def _actions(self, node):
        Atspi = self._bus()
        if "Action" not in self._ifaces(node):
            return []
        with warnings.catch_warnings():
            # get_action_name is deprecated in newer libatspi, whose get_name
            # this one lacks.
            warnings.simplefilter("ignore", DeprecationWarning)
            return [
                (Atspi.Action.get_action_name(node, index) or "").lower()
                for index in range(Atspi.Action.get_n_actions(node))
            ]

    def _select_child(self, parent, index):
        Atspi = self._bus()
        return "Selection" in self._ifaces(parent) and Atspi.Selection.select_child(parent, index)

    def _extents(self, node):
        Atspi = self._bus()
        try:
            if "Component" not in self._ifaces(node):
                return None
            rect = Atspi.Component.get_extents(node, Atspi.CoordType.SCREEN)
            return rect.x, rect.y, rect.width, rect.height
        except Exception:
            return None

    def _in_scope(self, window):
        if self.scope is None:
            return True
        extents = self._extents(window)
        if not extents or extents[2] <= 0 or extents[3] <= 0:
            return True
        x, y, width, height = extents
        cx, cy = x + width // 2, y + height // 2
        sx, sy, sw, sh = self.scope
        return sx <= cx < sx + sw and sy <= cy < sy + sh

    # -- windows -----------------------------------------------------------
    def _list(self):
        Atspi = self._bus()
        desktop = Atspi.get_desktop(0)
        now = time.monotonic()
        found = []
        for index in range(desktop.get_child_count()):
            try:
                app = desktop.get_child_at_index(index)
            except Exception:
                continue
            if app is None or self.dead.get(app, 0) > now:
                continue
            probed = time.monotonic()
            try:
                pid = app.get_process_id()
                count = app.get_child_count()
                name = app.get_name() or ""
            except Exception:
                self.dead[app] = now + DEAD_APP_SECONDS
                continue
            if count <= 0 and time.monotonic() - probed > SLOW_APP_SECONDS:
                self.dead[app] = now + DEAD_APP_SECONDS
                continue
            if count <= 0 or not self._mine(pid):
                continue
            for child in range(min(count, 30)):
                try:
                    window = app.get_child_at_index(child)
                    if window is None or window.get_role_name() not in WINDOW_ROLES:
                        continue
                    if not self._in_scope(window):
                        continue
                    found.append((app, name, pid, window))
                except Exception:
                    continue
        self.windows = {}
        rows = []
        for number, (_app, name, pid, window) in enumerate(found, 1):
            ref = f"w{number}"
            self.windows[ref] = (window, pid, name)
            try:
                title = _clean(window.get_name(), 120)
                flags = [
                    flag
                    for flag, state in (("active", "ACTIVE"), ("minimized", "ICONIFIED"))
                    if self._has(window, state)
                ]
                opaque = window.get_child_count() > 0 and window.get_child_at_index(0) is None
            except Exception:
                continue
            row = {"window": ref, "app": name, "title": title, "role": window.get_role_name()}
            if flags:
                row["state"] = flags
            if opaque:
                # Chromium and Electron publish nothing until started with
                # accessibility on (--force-renderer-accessibility).
                row["contents"] = "hidden: this app keeps its accessibility off; use screenshots"
            rows.append(row)
        return rows

    def list(self):
        return {"ok": True, "windows": self.call(self._list)}

    def _window(self, spec):
        if isinstance(spec, str) and spec in self.windows:
            window = self.windows[spec][0]
            if self._title(window) is not None:
                return window
            raise AppsError(f"window {spec} has closed; list the windows again")
        if not isinstance(spec, str) or not spec.strip():
            raise AppsError("name a window: its ref from the list (w3) or words from its title or app")
        needle = spec.strip().lower()

        def matching():
            return [
                window
                for window, _pid, app in self.windows.values()
                if needle in (self._title(window) or "\0").lower() or needle == app.lower()
            ]

        # Cached windows first; a new dialog or a closed one means listing again.
        matches = matching() if self.windows else []
        if not matches:
            self._list()
            matches = matching()
        if not matches:
            raise AppsError(f"no window matches {spec!r}; list the windows first")
        return matches[0]

    @staticmethod
    def _title(window):
        """The window's title, or None once it has closed."""
        try:
            return window.get_name() or ""
        except Exception:
            return None

    # -- snapshots ---------------------------------------------------------
    def _line(self, node, role, name, ref):
        Atspi = self._bus()
        states = node.get_state_set()
        flags = []
        for label, state in (
            ("checked", "CHECKED"), ("pressed", "PRESSED"), ("selected", "SELECTED"),
            ("expanded", "EXPANDED"), ("focused", "FOCUSED"),
        ):
            if states.contains(getattr(Atspi.StateType, state)):
                flags.append(label)
        if role in INTERACTIVE and not states.contains(Atspi.StateType.ENABLED):
            flags.append("disabled")
        if role == "password text":
            flags.append("password")
        line = f"- {role}" + (f' "{name}"' if name else "") + f" [ref={ref}]"
        value = self._value(node, role)
        if value is not None:
            line += f" value={value}"
        return line + (f" ({', '.join(flags)})" if flags else "")

    def _value(self, node, role):
        if role == "password text":
            return None
        try:
            if role in {"slider", "spin button", "scroll bar", "progress bar"}:
                number = self._number(node)
                if number is not None:
                    return f"{number:g}"
            if role in {"entry", "text", "combo box", "document text", "terminal", "editbar"} or self._has(
                node, "EDITABLE"
            ):
                content = self._text(node, tail=role == "terminal", limit=VALUE_CHARS * 3)
                if content is not None:
                    content = _clean(content, 10**6)
                    if role == "terminal":
                        content = content[-VALUE_CHARS:]
                    return '"' + _clean(content, VALUE_CHARS).replace('"', "'") + '"'
        except Exception:
            return None
        return None

    def _render(self, window, limit):
        self.refs = {}
        self.ref_window = window
        lines = []
        size = 0
        visited = 0
        stack = [(window, 0)]
        while stack:
            node, depth = stack.pop()
            visited += 1
            if visited > VISIT_LIMIT or len(lines) >= SNAPSHOT_NODES or size > limit:
                lines.append("… snapshot truncated")
                break
            try:
                role = node.get_role_name()
                name = _clean(node.get_name())
                if node is not window and not self._has(node, "VISIBLE"):
                    continue
                count = node.get_child_count()
            except Exception:
                continue
            if not name and role in TEXTUAL:
                # Labels often carry their words only as text content.
                with contextlib.suppress(Exception):
                    name = _clean(self._text(node, limit=NAME_CHARS * 2))
            keep = node is window or role in INTERACTIVE or (name and role in TEXTUAL)
            if not keep and role in {"entry", "text"} and "EditableText" in self._ifaces(node):
                keep = True
            indent = depth
            if keep:
                self.next_ref += 1
                ref = f"a{self.next_ref}"
                self.refs[ref] = node
                line = "  " * depth + self._line(node, role, name, ref)
                lines.append(line)
                size += len(line) + 1
                indent = depth + 1
            if role in {"terminal"}:
                continue
            children = []
            for index in range(min(count, 250)):
                try:
                    child = node.get_child_at_index(index)
                except Exception:
                    continue
                if child is not None:
                    children.append((child, indent))
            if count > 250:
                lines.append("  " * indent + f"- … {count - 250} more items")
            stack.extend(reversed(children))
        text = "\n".join(lines)
        if len(text) > limit:
            text = text[:limit] + f"\n… snapshot truncated at {limit} characters"
        return text

    def _snapshot(self, spec, limit=SNAPSHOT_CHARS):
        window = self._window(spec)
        return {"ok": True, "window": _clean(window.get_name(), 120), "snapshot": self._render(window, limit)}

    def snapshot(self, window):
        return self.call(self._snapshot, window)

    # -- finding -----------------------------------------------------------
    def _roles(self, role):
        if not role:
            return None
        role = str(role).lower()
        return ROLE_ALIASES.get(role.replace(" ", "").replace("_", ""), {role})

    def _search(self, root, target, *, visible=True):
        roles = self._roles(target.get("role"))
        name = str(target.get("name", "")).strip().lower()
        exact = target.get("exact", False)
        found = []
        stack = [root]
        visited = 0
        while stack and visited < VISIT_LIMIT and len(found) <= target.get("nth", 0) + 5:
            node = stack.pop()
            visited += 1
            try:
                if visible and node is not root and not self._has(node, "VISIBLE"):
                    continue
                role = node.get_role_name()
                label = " ".join((node.get_name() or "").split()).lower()
                if (roles is None or role in roles) and (
                    not name or (label == name if exact else (label == name or name in label))
                ):
                    found.append((label != name, node))
                count = node.get_child_count()
            except Exception:
                continue
            children = []
            for index in range(min(count, 500)):
                try:
                    child = node.get_child_at_index(index)
                except Exception:
                    continue
                if child is not None:
                    children.append(child)
            stack.extend(reversed(children))
        # Exact names first, then substring matches, each in tree order.
        return [node for _partial, node in sorted(found, key=lambda item: item[0])]

    def _resolve(self, window, target):
        if "ref" in target:
            node = self.refs.get(target["ref"])
            if node is None or self.ref_window is not window:
                raise AppsError(f"ref {target['ref']} is not from this window's latest snapshot")
            return node
        matches = self._search(window, target)
        nth = target.get("nth", 0)
        if not matches:
            raise AppsError("no element matches " + _describe(target))
        if len(matches) > 1 and "nth" not in target:
            names = [f"{m.get_role_name()} \"{_clean(m.get_name(), 40)}\"" for m in matches[:5]]
            raise AppsError(
                f"{len(matches)} elements match {_describe(target)}: " + "; ".join(names)
                + ". Retry with nth or a ref from the snapshot."
            )
        if nth >= len(matches):
            raise AppsError(f"only {len(matches)} elements match {_describe(target)}")
        return matches[nth]

    # -- actions -----------------------------------------------------------
    def _press(self, node):
        Atspi = self._bus()
        names = self._actions(node)
        if names:
            index = next((names.index(word) for word in PREFERRED_ACTIONS if word in names), 0)
            if Atspi.Action.do_action(node, index):
                return names[index]
            raise AppsError(f"the app refused {names[index]!r}")
        parent = node.get_parent()
        if parent is not None and self._select_child(parent, node.get_index_in_parent()):
            return "select"
        raise AppsError("this element offers no action; use a screenshot act for it")

    def _set_text(self, node, text):
        Atspi = self._bus()
        if node.get_role_name() == "password text":
            raise AppsError("password fields are his to type: hand off")
        if "EditableText" not in self._ifaces(node):
            raise AppsError("this element is not an editable text field")
        if not Atspi.EditableText.set_text_contents(node, text):
            raise AppsError("the app refused the text")
        return self._value(node, node.get_role_name())

    def _check(self, node, wanted):
        checked = self._has(node, "CHECKED") or self._has(node, "PRESSED")
        if checked != wanted:
            self._press(node)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                if (self._has(node, "CHECKED") or self._has(node, "PRESSED")) == wanted:
                    break
                time.sleep(0.05)
            else:
                raise AppsError("the box did not change state")
        return "checked" if wanted else "unchecked"

    def _select(self, node, option):
        choice = next(
            (item for item in self._search(node, {"name": option}, visible=False) if item is not node),
            None,
        )
        if choice is None:
            raise AppsError(f"no option named {option!r} under that element")
        # A combo box's items activate through their own action; selecting
        # them in the hidden menu only highlights them.
        if self._actions(choice):
            self._press(choice)
            return option
        parent = choice.get_parent()
        if parent is not None and self._select_child(parent, choice.get_index_in_parent()):
            return option
        raise AppsError(f"could not select {option!r}")

    def _read(self, node):
        role = node.get_role_name()
        facts = {"role": role, "name": _clean(node.get_name(), 200)}
        if role == "password text":
            return facts
        try:
            text = self._text(node, tail=role == "terminal")
            if text is not None:
                facts["text"] = text
            number = self._number(node)
            if number is not None:
                facts["value"] = number
        except Exception:
            pass
        description = _clean(node.get_description(), 200)
        if description:
            facts["description"] = description
        return facts

    def _menu(self, window, path):
        node = window
        for depth, label in enumerate(path):
            roles = {"menu"} if depth < len(path) - 1 else {"menu item", "check menu item", "radio menu item", "menu"}
            matches = [
                match for match in self._search(node, {"name": label, "exact": True}, visible=False)
                if match.get_role_name() in roles
            ]
            if not matches:
                raise AppsError(f"no menu entry {label!r} under {' > '.join(path[:depth]) or 'the window'}")
            node = matches[0]
        return self._press(node)

    def _active(self):
        try:
            window = subprocess.run(
                ["xdotool", "getactivewindow"], env=self.env, capture_output=True, text=True, timeout=2
            ).stdout.strip()
            pid = subprocess.run(
                ["xdotool", "getwindowpid", window], env=self.env, capture_output=True, text=True, timeout=2
            ).stdout.strip()
            return window, int(pid) if pid.isdigit() else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None, None

    def _keep_focus(self, before, app_pid):
        """Hand his focus back if her step made the target app raise a window."""
        window, pid = before
        if not window or pid == app_pid:
            return False
        time.sleep(0.15)
        after, after_pid = self._active()
        if after and after != window and after_pid == app_pid:
            subprocess.run(
                ["xdotool", "windowactivate", window], env=self.env, capture_output=True, timeout=2
            )
            return True
        return False

    def _step(self, window, step, kind):
        body = step[kind]
        timeout = float(step.get("timeout", 5))
        if kind == "menu":
            return {"did": self._menu(window, body)}
        if kind == "wait_for":
            key = "target" if "target" in body else "gone"
            deadline = time.monotonic() + timeout
            while True:
                present = bool(self._search(window, {k: v for k, v in body[key].items() if k != "nth"}))
                if present == (key == "target"):
                    return {}
                if time.monotonic() >= deadline:
                    raise AppsError(f"timed out waiting for {_describe(body[key])} to "
                                    + ("appear" if key == "target" else "go away"))
                time.sleep(0.2)
        deadline = time.monotonic() + timeout
        while True:
            try:
                node = self._resolve(window, body)
                break
            except AppsError as exc:
                # A widget that is not there yet (a page still loading, a
                # dialog opening) gets the step's timeout; ambiguity does not.
                if "ref" in body or not str(exc).startswith("no element"):
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        if kind == "press":
            return {"did": self._press(node)}
        if kind == "set_text":
            return {"value": self._set_text(node, step["text"])}
        if kind in {"check", "uncheck"}:
            return {"did": self._check(node, kind == "check")}
        if kind == "select":
            return {"did": self._select(node, step["option"])}
        if kind == "set_value":
            Atspi = self._bus()
            if "Value" not in self._ifaces(node) or not Atspi.Value.set_current_value(
                node, float(step["value"])
            ):
                raise AppsError("this element has no settable value")
            return {"value": self._number(node)}
        return {"read": self._read(node)}

    def _run(self, spec, steps, cancelled):
        window = self._window(spec)
        app_pid, title = None, ""
        with contextlib.suppress(Exception):
            app_pid, title = window.get_process_id(), _clean(window.get_name(), 120)
        results = []
        ok = True
        kept = False
        for index, step in enumerate(steps, 1):
            if cancelled():
                results.append({"step": index, "ok": False, "detail": STOPPED})
                ok = False
                break
            kind = next(key for key in step if key in KINDS)
            before = self._active() if kind not in {"read", "wait_for"} else (None, None)
            try:
                detail = self._step(window, step, kind)
                results.append({"step": index, "ok": True, **detail})
            except Exception as exc:
                results.append({"step": index, "ok": False, "detail": str(exc).split("\n")[0][:400]})
                ok = False
                break
            finally:
                if before[0]:
                    kept = self._keep_focus(before, app_pid) or kept
        result = {"ok": ok, "steps": results, "window": title}
        if kept:
            result["focus"] = "the app raised a window; his focus was handed back"
        try:
            window.get_role_name()
            result["snapshot"] = self._render(window, RESULT_SNAPSHOT_CHARS)
        except Exception:
            # The window closed (a dialog's OK button, say).
            result["snapshot"] = "window closed"
            self.windows = {}
        return result

    def run(self, window, steps, *, cancelled=lambda: False):
        validate_steps(steps)
        return self.call(self._run, window, steps, cancelled,
                         timeout=sum(float(s.get("timeout", 5)) for s in steps) + 30)


def _describe(target):
    parts = []
    if target.get("role"):
        parts.append(f"role {target['role']!r}")
    if target.get("name"):
        parts.append(f"name {target['name']!r}")
    return " and ".join(parts) or "that target"
