"""The sidebar's background index refresh must run in the packaged app.

Packaged, sys.executable is the desktop sidecar binary. It has no `-c`, so the
refresh child imported the UI and died on an argument error: no chat created
after startup reached the sidebar, and a handoff's new chat never linked.
"""

import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "apps" / "desktop" / "sidecar.py"


def test_packaged_refresh_launches_the_sidecar_flag(monkeypatch):
    from ui import web

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert web._index_refresh_argv() == [sys.executable, "--index-refresh"]
    monkeypatch.delattr(sys, "frozen")
    argv = web._index_refresh_argv()
    assert argv[:2] == [sys.executable, "-c"] and "update_index" in argv[2]


def _isolated_home(tmp_path):
    home = tmp_path / "home"
    sid = str(uuid.uuid4())
    transcript = home / ".claude" / "projects" / "-home-raghav" / f"{sid}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({
        "type": "user", "sessionId": sid, "cwd": "/home/raghav",
        "timestamp": "2026-09-28T15:22:33Z",
        "message": {"role": "user", "content": "@.serena-handoff.md this is a handoff briefing"},
    }) + "\n", encoding="utf-8")
    env = {**os.environ, "HOME": str(home), "CHATS_DATA_DIR": str(tmp_path / "data"),
           "SERENA_RUNTIME_LEASE_DIR": str(tmp_path / "leases")}
    env.pop("XDG_CONFIG_HOME", None)
    return sid, env


def _indexed(tmp_path, sid):
    db = tmp_path / "data" / "index.db"
    if not db.exists():
        return False
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (sid,)).fetchone() is not None


def test_sidecar_index_refresh_flag_indexes_a_new_chat(tmp_path):
    sid, env = _isolated_home(tmp_path)
    done = subprocess.run([sys.executable, str(SIDECAR), "--index-refresh"], env=env,
                          cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
    assert _indexed(tmp_path, sid)


@pytest.mark.skipif(os.name == "nt", reason="the frozen-argv failure is shown on POSIX")
def test_the_old_packaged_launch_never_indexed(tmp_path):
    # What the packaged app used to run: the sidecar entry with `-c <code>`.
    sid, env = _isolated_home(tmp_path)
    done = subprocess.run([sys.executable, str(SIDECAR), "-c", "from core.indexer import update_index"],
                          env=env, cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert done.returncode != 0
    assert not _indexed(tmp_path, sid)
