"""Run the real entry-point body against Windows and Unix signal surfaces."""

from __future__ import annotations

import ast
import io
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("has_hup", [False, True])
def test_web_server_starts_with_only_the_platforms_available_signals(tmp_path, has_hup):
    source = Path(__file__).resolve().parents[1] / "ui/web.py"
    function = next(
        node
        for node in ast.parse(source.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == "run_web"
    )
    calls = []
    registered = {}

    def register(number, callback):
        registered[number] = callback

    signals = SimpleNamespace(SIGTERM=15, SIGINT=2, signal=register)
    if has_hup:
        signals.SIGHUP = 1

    def shutdown():
        calls.append("shutdown")

    scope = {
        "pty_terminal": SimpleNamespace(
            reap_orphaned_scopes=lambda: None,
            sweep_stranded_agents=lambda: [],
            shutdown_all=shutdown,
        ),
        "update_index": lambda: None,
        "update_knowledge_index": lambda: None,
        "Path": SimpleNamespace(home=lambda: tmp_path),
        "_LAZY_CALL_RUNTIME": True,
        "atexit": SimpleNamespace(register=lambda callback: calls.append("atexit")),
        "signal": signals,
        "sys": SimpleNamespace(platform="linux" if has_hup else "win32", stderr=io.StringIO()),
        "app": SimpleNamespace(run=lambda **kwargs: calls.append(kwargs)),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), scope)
    scope["run_web"](host="127.0.0.1", port=8123)
    assert calls[-1] == {"host": "127.0.0.1", "port": 8123, "debug": False, "threaded": True}
    assert set(registered) == ({15, 2, 1} if has_hup else {15, 2})
    with pytest.raises(SystemExit) as stopped:
        registered[15](15, None)
    assert stopped.value.code == 143 and calls[-1] == "shutdown"
