import json
from pathlib import Path
import sqlite3
import time

from core import indexer
from core.indexer import _dedupe_discovered
from ui import web


class _FakeProcess:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode


def test_index_refresh_runs_out_of_process_and_coalesces(monkeypatch):
    processes = []

    def fake_launch():
        process = _FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(web, "_INDEX_REFRESH_PROC", None)
    monkeypatch.setattr(web, "_launch_index_refresh_process", fake_launch)

    started = time.perf_counter()
    assert web._schedule_index_refresh() is True
    assert time.perf_counter() - started < 0.1
    assert web._schedule_index_refresh() is False
    assert len(processes) == 1

    processes[0].returncode = 0
    assert web._schedule_index_refresh() is True
    assert len(processes) == 2


def test_terminal_page_loads_accelerated_renderers_before_images():
    webgl = web.HTML.index("@xterm/addon-webgl@0.18.0")
    canvas = web.HTML.index("@xterm/addon-canvas@0.7.0")
    images = web.HTML.index("@xterm/addon-image@0.8.0")

    assert webgl < images
    assert canvas < images
    assert "renderer: rendererKind" in web.HTML
    assert "setInterval(_syncWebRuntimePolicy, 2000)" in web.HTML


def test_duplicate_session_copies_choose_complete_then_stable_path(tmp_path):
    sid = "11111111-2222-3333-4444-555555555555"
    first = tmp_path / "first" / f"{sid}.jsonl"
    second = tmp_path / "second" / f"{sid}.jsonl"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"short")
    second.write_bytes(b"more-complete")
    rows = [
        ("claude", "first", first),
        ("claude", "second", second),
    ]

    assert _dedupe_discovered(rows, {}) == [("claude", "second", second)]

    first.write_bytes(b"same-size-data")
    second.write_bytes(b"same-size-data")
    assert _dedupe_discovered(rows, {sid: str(first)}) == [
        ("claude", "first", first)
    ]


def test_duplicate_session_copies_prefer_transcript_cwd_over_stale_path(tmp_path):
    sid = "11111111-2222-3333-4444-555555555555"
    wrong = tmp_path / "wrong" / f"{sid}.jsonl"
    right = tmp_path / "right" / f"{sid}.jsonl"
    wrong.parent.mkdir()
    right.parent.mkdir()
    cwd = "/home/raghav/Documents/Projects/serena"
    payload = (json.dumps({"type": "user", "cwd": cwd}) + "\n").encode()
    wrong.write_bytes(payload)
    right.write_bytes(payload)
    right_slug = "-home-raghav-Documents-Projects-serena"
    rows = [
        ("claude", "-home-raghav-Documents-Projects-personal-projects-locket", wrong),
        ("claude", right_slug, right),
    ]

    assert _dedupe_discovered(rows, {sid: str(wrong)}) == [
        ("claude", right_slug, right)
    ]


def test_custom_title_repairs_same_project_duplicate_group(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            project_dir TEXT,
            cwd TEXT,
            last_cwd TEXT,
            title TEXT,
            custom_title TEXT,
            is_teammate INTEGER DEFAULT 0
        )"""
    )
    conn.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, 0)",
        [
            ("custom", "-home-raghav-Documents-Projects-serena", None,
             "/home/raghav/Documents/Projects/serena", "generated", "My Chat"),
            ("restored", "C--Users-ragha-Projects-serena", None,
             "C:/Users/ragha/Projects/serena", "My Chat", None),
            ("other", "-home-raghav-Documents-Projects-locket", None,
             "/home/raghav/Documents/Projects/locket", "My Chat", None),
        ],
    )
    linked = []
    monkeypatch.setattr(indexer.meta_sync, "get_all_meta", lambda: {})
    monkeypatch.setattr(indexer.meta_sync, "link_sessions", lambda sids: linked.append(sids))

    assert indexer._repair_custom_title_groups(conn) == 1
    assert linked == [["custom", "restored"]]


def test_internal_brain_rotations_are_hidden_from_chat_rows():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT, project_dir TEXT, is_teammate INTEGER)"
    )
    conn.executemany(
        "INSERT INTO sessions VALUES (?, ?, 0)",
        [
            ("brain", "-home-raghav--cache-serena-headless-brain"),
            ("test", "-tmp-serena-http-rotation-cwd"),
            ("chat", "-home-raghav-Documents-Projects-serena"),
        ],
    )

    indexer._hide_internal_sessions(conn)

    hidden = {
        row["session_id"]: row["is_teammate"]
        for row in conn.execute("SELECT * FROM sessions")
    }
    assert hidden == {"brain": 1, "test": 1, "chat": 0}


def test_modal_keyboard_is_exclusive():
    guard = "document.getElementById('modalBackdrop')?.classList.contains('visible')"
    assert web.HTML.count(guard) >= 2
    assert "e.preventDefault(); e.stopImmediatePropagation(); close(true);" in web.HTML
    assert "e.preventDefault(); e.stopImmediatePropagation(); close(false);" in web.HTML


def test_xterm_editing_shortcuts_reach_the_pty_before_widget_shortcuts():
    root = Path(web.__file__).resolve().parents[1]
    web_source = (root / "ui" / "web.py").read_text(encoding="utf-8")
    gtk_source = (root / "desktop" / "app_gtk.py").read_text(encoding="utf-8")

    assert r"ws.send('\n')" in web_source
    assert r"ws.send('\x17')" in web_source
    assert "function _extendTermKeyboardSelection(state, key)" in web_source
    assert "term.select(start % cols, Math.floor(start / cols), end - start);" in web_source
    assert "if (_extendTermKeyboardSelection(s, k))" in web_source
    assert r"arrowup: '\x1b[1;2A'" not in web_source
    assert r"arrowdown: '\x1b[1;2B'" not in web_source
    assert r"arrowright: '\x1b[1;2C'" not in web_source
    assert r"arrowleft: '\x1b[1;2D'" not in web_source
    assert 'if action.startswith("term-") and self._focused_vte() is None:' in gtk_source
