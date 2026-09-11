from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core import codex_scanner

SESSION_ID = "019f7b3a-354e-7363-9281-4e1d4e7a374d"


@pytest.mark.parametrize("custom", [False, True])
def test_scanner_uses_same_codex_home_as_native_runtime(tmp_path, custom):
    home = tmp_path / "home"
    home.mkdir()
    codex_home = tmp_path / "custom codex" if custom else home / ".codex"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    path = _rollout(sessions, "cli")
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(codex_home) if custom else "")
    result = subprocess.run([sys.executable, "-c",
        "import json; from core.codex_scanner import CODEX_SESSIONS_ROOT, scan_codex_sessions; "
        "print(json.dumps([str(CODEX_SESSIONS_ROOT),[str(path) for _,path in scan_codex_sessions()]]))"],
        cwd=Path(__file__).resolve().parents[1], env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [str(sessions), [str(path)]]


def _rollout(tmp_path, source: str):
    path = tmp_path / f"rollout-2026-07-19T12-34-00-{SESSION_ID}.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": SESSION_ID, "source": source},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_cli_session_remains_visible_without_metadata(tmp_path, monkeypatch) -> None:
    path = _rollout(tmp_path, "cli")
    monkeypatch.setattr(codex_scanner.meta_sync, "get_meta", lambda _sid: {})

    assert codex_scanner._is_user_initiated(path)


def test_resident_exec_is_visible_but_generic_exec_stays_hidden(
    tmp_path,
    monkeypatch,
) -> None:
    path = _rollout(tmp_path, "exec")
    monkeypatch.setattr(
        codex_scanner.meta_sync,
        "get_meta",
        lambda sid: {"resident_work": sid == SESSION_ID},
    )

    assert codex_scanner._is_user_initiated(path)
    monkeypatch.setattr(codex_scanner.meta_sync, "get_meta", lambda _sid: {})
    assert not codex_scanner._is_user_initiated(path)


def test_resident_desktop_session_is_visible_but_unmarked_desktop_stays_hidden(
    tmp_path,
    monkeypatch,
) -> None:
    path = _rollout(tmp_path, "vscode")
    monkeypatch.setattr(
        codex_scanner.meta_sync,
        "get_meta",
        lambda sid: {"resident_work": sid == SESSION_ID},
    )

    assert codex_scanner._is_user_initiated(path)
    monkeypatch.setattr(codex_scanner.meta_sync, "get_meta", lambda _sid: {})
    assert not codex_scanner._is_user_initiated(path)
