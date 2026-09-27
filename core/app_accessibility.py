"""Make his Chromium and Electron apps publish their widgets to accessibility.

Edge, VS Code, the Serena app, OpenWhispr and Unified keep their accessibility
tree switched off unless started with --force-renderer-accessibility, so
Serena's app steps (core/computer_apps) saw only an empty window and had to
borrow his mouse. This adds the flag where he launches them:

- his local .desktop launchers (and local copies of the system ones, which
  shadow them), including [Desktop Action] entries and Edge's web apps;
- VS Code's ~/.vscode/argv.json, which on Linux accepts the flag and covers
  every way VS Code starts, `code` in a terminal included.

It takes effect the next time each app starts. apply is idempotent, so it can
be re-run after an app update rewrites a launcher; undo restores every file it
changed and deletes the local copies it created. What it did is recorded in
~/.config/serena/app-accessibility.json.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

FLAG = "--force-renderer-accessibility"
ARGV_KEY = "force-renderer-accessibility"
ARGV_MARK = (
    "// Serena: publish VS Code's widgets to accessibility "
    "(chats computer accessibility undo removes this)"
)
SYSTEM = Path("/usr/share/applications")


def _home(home):
    return Path(home) if home else Path.home()


def _state_path(home):
    return _home(home) / ".config/serena/app-accessibility.json"


def launchers(home=None, system=SYSTEM):
    """(app, launcher path, system file to copy when there is no local one)."""
    home = _home(home)
    local = home / ".local/share/applications"
    found = [
        ("Edge", local / "microsoft-edge.desktop", system / "microsoft-edge.desktop"),
        ("Edge", local / "com.microsoft.Edge.desktop", system / "com.microsoft.Edge.desktop"),
        *[("Edge web app", path, None) for path in sorted(local.glob("msedge-*.desktop"))],
        ("Serena", local / "serena-desktop.desktop", None),
        ("Serena Dev", local / "serena-dev.desktop", None),
        ("OpenWhispr", local / "openwhispr.desktop", None),
        ("Unified", home / ".config/autostart/unified.desktop", None),
        ("Unified", local / "unified-inbox.desktop", system / "unified-inbox.desktop"),
    ]
    return [
        (app, path, source)
        for app, path, source in found
        if path.exists() or (source is not None and source.exists())
    ]


def _split_exec(value):
    """The executable (quoted or not) and the rest of an Exec value."""
    value = value.strip()
    if value.startswith('"'):
        index = 1
        while index < len(value):
            if value[index] == "\\":
                index += 2
                continue
            if value[index] == '"':
                return value[: index + 1], value[index + 1 :]
            index += 1
        return value, ""
    head, _, rest = value.partition(" ")
    return head, (" " + rest) if rest else ""


def with_flag(value):
    if FLAG in value.split():
        return value
    executable, rest = _split_exec(value)
    return f"{executable} {FLAG}{rest}"


def without_flag(value):
    return re.sub(rf"\s+{re.escape(FLAG)}(?=\s|$)", "", value)


def patch_desktop(text, *, add=True):
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("Exec="):
            body = line[5:].rstrip("\n")
            ending = line[len(line.rstrip("\n")) :]
            lines[index] = "Exec=" + (with_flag(body) if add else without_flag(body)) + ending
    return "".join(lines)


def _write(path, text, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".serena-tmp")
    temporary.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(temporary, mode)
    temporary.replace(path)


def _argv_path(home):
    return _home(home) / ".vscode/argv.json"


def _uncommented(text):
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))


def _argv_apply(path):
    if not path.exists():
        _write(path, "{\n\t" + ARGV_MARK + f'\n\t"{ARGV_KEY}": true\n}}\n')
        return "created"
    text = path.read_text(encoding="utf-8")
    plain = _uncommented(text)
    match = re.search(rf'"{ARGV_KEY}"\s*:\s*(true|false)', plain)
    if match and match.group(1) == "true":
        return "already"
    if match:
        raise ValueError(f"{path} sets {ARGV_KEY} to false; change it by hand")
    brace = text.find("{")
    if brace < 0:
        raise ValueError(f"{path} is not a JSON object")
    has_keys = re.search(r'"[^"]+"\s*:', plain) is not None
    insert = "\n\t" + ARGV_MARK + f'\n\t"{ARGV_KEY}": true' + ("," if has_keys else "")
    _write(path, text[: brace + 1] + insert + text[brace + 1 :])
    return "added"


def _argv_undo(path):
    if not path.exists():
        return "absent"
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r"\n[ \t]*" + re.escape(ARGV_MARK) + rf'\n[ \t]*"{ARGV_KEY}": true,?')
    restored = pattern.sub("", text, count=1)
    if restored == text:
        return "untouched"
    _write(path, restored)
    return "removed"


def _load_state(home):
    try:
        return json.loads(_state_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"created": [], "argv": None}


def _save_state(home, state):
    _write(_state_path(home), json.dumps(state, indent=2) + "\n", mode=0o600)


def apply(home=None, system=SYSTEM, *, vscode=True):
    """Add the flag everywhere; returns what changed, launcher by launcher."""
    state = _load_state(home)
    created = set(state.get("created") or [])
    results = []
    for app, path, source in launchers(home, system):
        change = "already"
        if not path.exists():
            # A local copy shadows the system launcher and survives package updates.
            text = source.read_text(encoding="utf-8")
            created.add(str(path))
            change = "created"
        else:
            text = path.read_text(encoding="utf-8")
        patched = patch_desktop(text)
        if patched != text or change == "created":
            mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
            _write(path, patched, mode)
            change = change if change == "created" else "added"
        results.append({"app": app, "path": str(path), "change": change})
    if vscode and (_argv_path(home).exists() or Path("/usr/share/code/code").exists()):
        change = _argv_apply(_argv_path(home))
        if change in {"added", "created"}:
            state["argv"] = change
        results.append({"app": "VS Code", "path": str(_argv_path(home)), "change": change})
    state["created"] = sorted(created)
    _save_state(home, state)
    return results


def undo(home=None, system=SYSTEM):
    """Put back every launcher as it was and delete the local copies apply made."""
    state = _load_state(home)
    created = set(state.get("created") or [])
    results = []
    for app, path, _source in launchers(home, system):
        if not path.exists():
            continue
        if str(path) in created:
            path.unlink()
            results.append({"app": app, "path": str(path), "change": "deleted"})
            continue
        text = path.read_text(encoding="utf-8")
        restored = patch_desktop(text, add=False)
        if restored != text:
            _write(path, restored, path.stat().st_mode & 0o777)
            results.append({"app": app, "path": str(path), "change": "removed"})
    argv = _argv_path(home)
    if state.get("argv") == "created" and argv.exists():
        text = argv.read_text(encoding="utf-8")
        if _uncommented(text).split() == ["{", f'"{ARGV_KEY}":', "true", "}"]:
            argv.unlink()
            results.append({"app": "VS Code", "path": str(argv), "change": "deleted"})
    elif state.get("argv"):
        change = _argv_undo(argv)
        if change == "removed":
            results.append({"app": "VS Code", "path": str(argv), "change": change})
    _state_path(home).unlink(missing_ok=True)
    return results


def status(home=None, system=SYSTEM):
    rows = []
    for app, path, _source in launchers(home, system):
        on = path.exists() and all(
            FLAG in line.split()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("Exec=")
        )
        rows.append({"app": app, "path": str(path), "flag": on, "local": path.exists()})
    argv = _argv_path(home)
    if argv.exists():
        on = re.search(rf'"{ARGV_KEY}"\s*:\s*true', _uncommented(argv.read_text(encoding="utf-8")))
        rows.append({"app": "VS Code", "path": str(argv), "flag": bool(on), "local": True})
    return rows
