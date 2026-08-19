"""Autolink must only chain genuine plan→exec continuations.

Regression for the recurring wrong-group bug: two interactive codex chats
opened by hand in the same cwd within five minutes were linked as if one were
the other's exec continuation, merging their groups and overwriting the newer
chat's title with the older one's (the "Rizzler" → "Ecosystem Mismatch"
incident, 2026-08-10).
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from core import autolink


def _make_index(path, rows):
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, cwd TEXT, project_dir TEXT,
            first_timestamp TEXT, last_timestamp TEXT,
            custom_title TEXT, title TEXT, originator TEXT, agent TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'codex')",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture
def index_env(tmp_path, monkeypatch):
    db = tmp_path / "index.db"
    monkeypatch.setattr(autolink, "DB_PATH", db)
    meta_dir = tmp_path / "meta"
    monkeypatch.setattr(autolink.meta, "METADATA_DIR", meta_dir, raising=False)
    from core import metadata

    monkeypatch.setattr(metadata, "METADATA_DIR", meta_dir)
    monkeypatch.setattr(metadata, "METADATA_PATH", tmp_path / "legacy.json")
    monkeypatch.setattr(metadata, "_migrated", False)
    return db


def _ts(dt):
    return dt.isoformat()


def test_two_interactive_chats_in_same_cwd_are_never_linked(index_env):
    now = datetime.now(timezone.utc)
    _make_index(
        index_env,
        [
            ("older-tui", "/home/x", "", _ts(now - timedelta(minutes=20)),
             _ts(now - timedelta(minutes=2)), "Ecosystem Mismatch", "auto", "codex-tui:cli"),
            ("newer-tui", "/home/x", "", _ts(now - timedelta(minutes=1)),
             _ts(now), None, "Rizzler", "codex-tui:cli"),
        ],
    )
    assert autolink.auto_link_codex_chains(dry_run=False) == []
    from core import metadata as meta

    assert meta.get_group("newer-tui") is None
    assert meta.get_meta("newer-tui").get("custom_title") is None


def test_exec_continuation_still_chains_and_inherits_title(index_env):
    now = datetime.now(timezone.utc)
    _make_index(
        index_env,
        [
            ("plan-tui", "/home/x", "", _ts(now - timedelta(minutes=20)),
             _ts(now - timedelta(minutes=2)), "Big Task", "auto", "codex-tui:cli"),
            ("exec-child", "/home/x", "", _ts(now - timedelta(minutes=1)),
             _ts(now), None, "a previous agent produced…", "codex_exec:exec"),
        ],
    )
    linked = autolink.auto_link_codex_chains(dry_run=False)
    assert [(new, pre) for new, pre, _gid in linked] == [("exec-child", "plan-tui")]
    from core import metadata as meta

    gid = meta.get_group("exec-child")
    assert gid and gid == meta.get_group("plan-tui")
    assert meta.get_meta("exec-child").get("custom_title") == "Big Task"


def test_uppercase_exec_originator_still_chains_and_inherits_title(index_env):
    now = datetime.now(timezone.utc)
    _make_index(
        index_env,
        [
            ("uppercase-plan-tui", "/home/x", "", _ts(now - timedelta(minutes=20)),
             _ts(now - timedelta(minutes=2)), "Uppercase Task", "auto", "codex-tui:cli"),
            ("uppercase-exec-child", "/home/x", "", _ts(now - timedelta(minutes=1)),
             _ts(now), None, "a previous agent produced…", "CODEX_EXEC:EXEC"),
        ],
    )
    linked = autolink.auto_link_codex_chains(dry_run=False)
    assert [(new, pre) for new, pre, _gid in linked] == [
        ("uppercase-exec-child", "uppercase-plan-tui")
    ]
    from core import metadata as meta

    gid = meta.get_group("uppercase-exec-child")
    assert gid and gid == meta.get_group("uppercase-plan-tui")
    assert meta.get_meta("uppercase-exec-child").get("custom_title") == "Uppercase Task"
