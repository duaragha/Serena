import json

from core import codex_attention_watcher as watcher


def _event(kind: str) -> str:
    return json.dumps({"type": "event_msg", "payload": {"type": kind}}) + "\n"


def test_exact_watch_detects_completion_in_old_rollout(tmp_path, monkeypatch):
    sid = "019f5de0-a3ff-7e31-acb1-571c86ca5fd1"
    path = tmp_path / f"rollout-2026-01-01T00-00-00-{sid}.jsonl"
    path.write_text(_event("task_started"), encoding="utf-8")
    marked = []
    monkeypatch.setattr(watcher.chat_attention, "mark", marked.append)
    watcher._offsets.clear()
    watcher._marked_at_offset.clear()
    watcher._watched_paths.clear()

    assert watcher.watch(sid, path) is True
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_event("task_complete"))

    assert watcher._scan_file(path) == sid
    assert marked == [sid]


def test_exact_watch_starts_at_current_end(tmp_path, monkeypatch):
    sid = "019f5de0-a3ff-7e31-acb1-571c86ca5fd1"
    path = tmp_path / f"rollout-2026-01-01T00-00-00-{sid}.jsonl"
    path.write_text(_event("task_complete"), encoding="utf-8")
    marked = []
    monkeypatch.setattr(watcher.chat_attention, "mark", marked.append)
    watcher._offsets.clear()
    watcher._marked_at_offset.clear()
    watcher._watched_paths.clear()

    assert watcher.watch(sid, path) is True

    assert watcher._scan_file(path) is None
    assert marked == []
