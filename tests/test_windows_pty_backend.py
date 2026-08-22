"""Windows runs the same terminal host as Linux, and must keep doing so.

This replaces a Windows-only ConPTY module that ui.web's imports were swapped
onto at startup. The swap looked harmless because a parity test compared the two
modules across a hand-written list of 16 names, while ui.web actually reached
for 29. Opening a chat on Windows threw AttributeError on the first missing one
and took the terminal websocket down with it.

The tests below derive the required surface from the call sites instead of
restating it, so a function added to ui.web cannot silently go missing again.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "ui" / "web.py"
HOST = REPO / "ui" / "pty_terminal.py"
SIDECAR = REPO / "desktop-electron" / "windows" / "sidecar-win.py"


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _names_used_on(module: ast.Module, target: str) -> set[str]:
    """Every attribute ui.web reads off the terminal host."""
    return {
        node.attr
        for node in ast.walk(module)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == target
    }


def _top_level_names(module: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_the_terminal_host_provides_everything_the_app_asks_of_it():
    used = _names_used_on(_module(WEB), "pty_terminal")
    assert used, "sanity: ui.web should reach for the terminal host"

    missing = sorted(used - _top_level_names(_module(HOST)))

    assert not missing, f"ui.web calls pty_terminal.{missing} which does not exist"


def test_the_windows_sidecar_does_not_substitute_a_different_host():
    """A partial stand-in passes import and fails on the first uncovered call."""
    source = SIDECAR.read_text(encoding="utf-8")

    assert "pty_windows" not in source
    assert "activate_backend" not in source
    assert "sys.modules[" not in source


def test_the_host_imports_nothing_that_is_missing_on_windows():
    """termios, fcntl and friends exist only on POSIX and fail at import time."""
    posix_only = {"termios", "fcntl", "pty", "tty", "grp", "pwd", "resource"}

    imported = set()
    for node in ast.walk(_module(HOST)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    assert not imported & posix_only, f"POSIX-only imports: {sorted(imported & posix_only)}"


@pytest.mark.parametrize("call", ["killpg", "getpgid", "setsid", "select"])
def test_posix_only_process_calls_stay_behind_a_platform_check(call: str):
    """These raise AttributeError on Windows, so every user must branch first.

    Checked at function scope rather than by proximity: the real guards are
    early returns at the top of the function, sometimes twenty lines above the
    call they protect.
    """
    module = _module(HOST)
    offenders = []

    for func in ast.walk(module):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        uses = [
            node
            for node in ast.walk(func)
            if isinstance(node, ast.Attribute) and node.attr == call
        ]
        if not uses:
            continue
        guarded = any(
            isinstance(node, ast.Name) and node.id == "_IS_WINDOWS"
            for node in ast.walk(func)
        )
        if not guarded:
            offenders.append(f"{func.name} (line {uses[0].lineno})")

    assert not offenders, f"{call} used without a platform check in: {offenders}"
