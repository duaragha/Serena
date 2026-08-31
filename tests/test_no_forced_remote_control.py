"""Serena must not switch Remote Control on by itself.

Opening a project chat in the legacy TUI passed `--remote-control <title>` to
claude, and a `w` binding existed purely to force it on for any chat. Neither
asked. Remote Control lets another device claim the session, which is how a chat
gets taken over mid-work with "another connection took over the session".

The decision predates the Electron app and nobody removed it, so it kept turning
itself on for a user who had never enabled it.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCES = (REPO / "ui" / "tui.py", REPO / "ui" / "web.py", REPO / "core" / "claude_spawn.py")


def test_nothing_launches_claude_with_remote_control() -> None:
    for path in SOURCES:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # the comment recording why it was removed
            assert "--remote-control" not in line, (
                f"{path.name}:{line_no} turns Remote Control on without asking"
            )


def test_the_force_remote_control_action_is_gone() -> None:
    """It existed only to enable the thing, and its binding went with it."""
    text = (REPO / "ui" / "tui.py").read_text(encoding="utf-8")

    assert "def action_remote_control" not in text
    assert '"remote_control"' not in text, "a binding still points at the removed action"


def test_every_binding_still_resolves_to_a_method() -> None:
    """Removing an action without its binding leaves a key that raises."""
    source = (REPO / "ui" / "tui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    bound = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "Binding" or len(node.args) < 2:
            continue
        action = node.args[1]
        if isinstance(action, ast.Constant) and isinstance(action.value, str):
            bound.add(action.value)

    # Textual's App and Screen supply a few actions of their own.
    framework = {"quit", "toggle_dark", "screenshot", "focus_next", "focus_previous", "back"}

    missing = sorted(
        a for a in bound
        if a not in framework and f"action_{a}" not in methods and a not in methods
    )
    assert not missing, f"bindings with no method: {missing}"
