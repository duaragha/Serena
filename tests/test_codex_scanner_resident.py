from __future__ import annotations

import json

from core import codex_scanner

SESSION_ID = "019f7b3a-354e-7363-9281-4e1d4e7a374d"


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
