"""Her coding sessions shown in the app window: endpoint and page module."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ui import web

MODULE = Path(__file__).resolve().parents[1] / "ui" / "static" / "serena_work.js"


def test_endpoint_lists_her_sessions_without_calling_back_into_the_app(monkeypatch):
    calls = {}

    def fake_list(**kwargs):
        calls.update(kwargs)
        return [
            {"id": "serena-task-1", "title": "Unified emoji picker button", "project": "unified",
             "agent": "claude", "session_id": "sid-live", "state": "working", "outcome": "",
             "where": "his Serena app", "opened_minutes_ago": 2, "transcript": "/private"},
            {"id": "serena-task-0", "title": "old", "project": "serena", "agent": "claude",
             "session_id": "sid-done", "state": "done", "outcome": "DONE: x",
             "opened_minutes_ago": 90},
        ]

    import core.serena_coding as coding

    monkeypatch.setattr(coding, "list_sessions", fake_list)
    monkeypatch.setattr(web, "get_session", lambda sid: {"session_id": sid} if sid == "sid-done" else None)
    titled = []
    monkeypatch.setattr(web, "set_title", lambda sid, title: titled.append((sid, title)))
    refreshed = []
    monkeypatch.setattr(web, "_schedule_index_refresh", lambda: refreshed.append(True) or True)

    response = web.app.test_client().get("/api/serena-coding")
    data = response.get_json()

    assert response.status_code == 200
    assert calls["adopt"] is False
    live, done = data["sessions"]
    assert live["title"] == "Unified emoji picker button" and live["indexed"] is False
    assert done["indexed"] is True
    assert "transcript" not in live  # file paths stay on the server
    # A live chat the index has not seen yet cannot be opened; index it.
    assert refreshed == [True]
    # Named once indexed, so the sidebar does not show her raw brief.
    assert titled == [("sid-done", "old")]


def test_endpoint_survives_a_broken_state_file(monkeypatch):
    import core.serena_coding as coding

    def boom(**_kwargs):
        raise ValueError("bad state")

    monkeypatch.setattr(coding, "list_sessions", boom)
    data = web.app.test_client().get("/api/serena-coding").get_json()
    assert data["sessions"] == [] and "bad state" in data["error"]


def test_page_loads_the_module():
    html = web.app.test_client().get("/").get_data(as_text=True)
    assert '<script src="/static/serena_work.js"></script>' in html


def test_module_announces_starts_and_ends_and_picks_the_watch_view():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = f"""
const assert = require('node:assert/strict');
const work = require({json.dumps(str(MODULE))});
const known = new Map();
const first = [
  {{id: 'a', state: 'working', opened_minutes_ago: 1, title: 'emoji'}},
  {{id: 'b', state: 'working', opened_minutes_ago: 45, title: 'old but live'}},
  {{id: 'c', state: 'done', opened_minutes_ago: 2, title: 'finished before'}},
];
let seen = work.changes(first, known);
assert.deepEqual(seen.started.map(s => s.id), ['a']);
assert.deepEqual(seen.ended, []);
for (const s of first) known.set(s.id, s.state);
seen = work.changes([
  {{id: 'a', state: 'done', opened_minutes_ago: 3}},
  {{id: 'b', state: 'working', opened_minutes_ago: 46}},
  {{id: 'c', state: 'done', opened_minutes_ago: 3}},
], known);
assert.deepEqual(seen.started, []);
assert.deepEqual(seen.ended.map(s => s.id), ['a']);
assert.equal(work.chipLabel(first), 'serena coding \\u00b7 2');
assert.equal(work.chipLabel([{{state: 'done'}}]), '');
assert.equal(work.watchMode(false), 'live');
assert.equal(work.watchMode(true), 'read');
"""
    completed = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
