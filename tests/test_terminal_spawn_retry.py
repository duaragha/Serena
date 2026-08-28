from __future__ import annotations

import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ui import pty_terminal, web


def _extract_function(name: str) -> str:
    async_marker = f"async function {name}("
    marker = f"function {name}("
    start = (
        web.HTML.index(async_marker)
        if async_marker in web.HTML
        else web.HTML.index(marker)
    )
    depth = 0
    for index in range(web.HTML.index("{", start), len(web.HTML)):
        char = web.HTML[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return web.HTML[start : index + 1]
    raise AssertionError(f"{name} is not brace-balanced")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_renderer_retries_one_spawn_drop_only_when_backend_is_healthy(
    tmp_path: Path,
) -> None:
    source = "\n\n".join(
        _extract_function(name)
        for name in ("_terminalBackendReachable", "_spawnTerminalRequest")
    )
    script = tmp_path / "terminal-spawn-retry.cjs"
    script.write_text(
        f"""
'use strict';
const assert = require('node:assert/strict');
let calls = [];
let fetchImpl;
async function fetch(url, options) {{ calls.push(url); return fetchImpl(url, options); }}
{source}

(async () => {{
  let spawnAttempts = 0;
  fetchImpl = async (url) => {{
    if (url === '/api/health') return {{ ok: true }};
    spawnAttempts += 1;
    if (spawnAttempts === 1) throw new TypeError('Failed to fetch');
    return {{ json: async () => ({{ ok: true, terminal_id: 'same-terminal' }}) }};
  }};
  const result = await _spawnTerminalRequest({{ session_id: 'session-1' }});
  assert.equal(result.terminal_id, 'same-terminal');
  assert.equal(spawnAttempts, 2);
  assert.deepEqual(calls, ['/api/spawn-terminal', '/api/health', '/api/spawn-terminal']);

  calls = [];
  spawnAttempts = 0;
  fetchImpl = async (url) => {{
    if (url === '/api/health') throw new TypeError('backend gone');
    spawnAttempts += 1;
    throw new TypeError('Failed to fetch');
  }};
  await assert.rejects(
    _spawnTerminalRequest({{ session_id: 'session-2' }}),
    /Failed to fetch/,
  );
  assert.equal(spawnAttempts, 1);
  assert.deepEqual(calls, ['/api/spawn-terminal', '/api/health']);
  console.log('ok');
}})().catch((error) => {{ console.error(error); process.exit(1); }});
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == "ok"


def test_concurrent_retries_reuse_one_server_terminal(monkeypatch, tmp_path: Path) -> None:
    session_id = "existing-codex-session"
    spawned: list[str] = []
    spawn_attempts: list[int] = []
    registered: dict[str, str] = {}
    state_lock = threading.Lock()

    class PanicException(BaseException):
        pass

    PanicException.__module__ = "pyo3_runtime"

    monkeypatch.setattr(
        web,
        "get_session",
        lambda _sid: {
            "session_id": session_id,
            "agent": "codex",
            "cwd": str(tmp_path),
            "project_dir": str(tmp_path),
        },
    )
    monkeypatch.setattr(web, "_fleet_worker_marker", lambda _sid: False)
    monkeypatch.setattr(web, "_external_runtime_active", lambda _sid: False)
    monkeypatch.setattr(web, "ensure_session_visible", lambda *_args: None)
    monkeypatch.setattr(web, "resolve_session_cwd", lambda value: value)

    def tid_for_session(sid: str) -> str | None:
        with state_lock:
            return registered.get(sid)

    def spawn(*_args, **_kwargs) -> str:
        spawn_attempts.append(len(spawn_attempts) + 1)
        if len(spawn_attempts) == 1:
            raise PanicException(
                "called Result::unwrap() on an Err value: "
                "Error { code: HRESULT(0x800700BB) }"
            )
        spawned.append("terminal-1")
        time.sleep(0.05)
        return "terminal-1"

    def register_session(sid: str, tid: str) -> None:
        with state_lock:
            registered[sid] = tid

    monkeypatch.setattr(pty_terminal, "tid_for_session", tid_for_session)
    monkeypatch.setattr(pty_terminal, "spawn", spawn)
    monkeypatch.setattr(pty_terminal, "register_session", register_session)
    monkeypatch.setattr(web, "_CONPTY_SPAWN_RETRY_DELAY_SECONDS", 0)

    def request_spawn() -> dict:
        with web.app.test_client() as client:
            response = client.post(
                "/api/spawn-terminal",
                json={"session_id": session_id, "rows": 30, "cols": 100},
            )
            assert response.status_code == 200
            return response.get_json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _index: request_spawn(), range(2)))

    assert spawn_attempts == [1, 2]
    assert spawned == ["terminal-1"]
    assert {item["terminal_id"] for item in responses} == {"terminal-1"}
    assert sorted(bool(item.get("reused")) for item in responses) == [False, True]


def test_persistent_pywinpty_panic_returns_json_error(monkeypatch, tmp_path: Path) -> None:
    session_id = "broken-codex-session"
    attempts: list[int] = []

    class PanicException(BaseException):
        pass

    PanicException.__module__ = "pyo3_runtime"

    monkeypatch.setattr(
        web,
        "get_session",
        lambda _sid: {
            "session_id": session_id,
            "agent": "codex",
            "cwd": str(tmp_path),
            "project_dir": str(tmp_path),
        },
    )
    monkeypatch.setattr(web, "_fleet_worker_marker", lambda _sid: False)
    monkeypatch.setattr(web, "_external_runtime_active", lambda _sid: False)
    monkeypatch.setattr(web, "ensure_session_visible", lambda *_args: None)
    monkeypatch.setattr(web, "resolve_session_cwd", lambda value: value)
    monkeypatch.setattr(pty_terminal, "tid_for_session", lambda _sid: None)
    monkeypatch.setattr(web, "_CONPTY_SPAWN_RETRY_DELAY_SECONDS", 0)

    def fail_spawn(*_args, **_kwargs) -> str:
        attempts.append(len(attempts) + 1)
        raise PanicException(
            "called Result::unwrap() on an Err value: "
            "Error { code: HRESULT(0x800700BB) }"
        )

    monkeypatch.setattr(pty_terminal, "spawn", fail_spawn)

    response = web.app.test_client().post(
        "/api/spawn-terminal",
        json={"session_id": session_id, "rows": 30, "cols": 100},
    )

    assert response.status_code == 500
    assert response.is_json
    assert "Windows terminal startup failed" in response.get_json()["error"]
    assert attempts == [1, 2]
