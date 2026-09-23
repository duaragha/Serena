import json
import sqlite3

import pytest

from core import indexer, metadata


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(metadata, "METADATA_DIR", tmp_path / "meta")
    monkeypatch.setattr(metadata, "_ensure_migrated", lambda: None)


def test_one_member_leaves_a_thread_of_three_and_the_rest_stay_linked(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    group = metadata.link_sessions(["claude", "codex", "muse"])
    metadata.unlink_session("muse")
    assert metadata.get_group("muse") is None
    assert metadata.get_group("claude") == metadata.get_group("codex") == group
    assert sorted(metadata.list_group_members(group)) == ["claude", "codex"]


def test_leaving_a_thread_of_two_leaves_no_group_of_one(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    metadata.link_sessions(["claude", "codex"])
    metadata.unlink_session("codex")
    assert metadata.get_group("claude") is None and metadata.get_group("codex") is None


@pytest.mark.parametrize("disband", [False, True])
def test_refresh_cannot_undo_explicit_unlink_or_disband(monkeypatch, tmp_path, disband):
    _isolate(monkeypatch, tmp_path)
    sids = ["claude", "codex", "gemini"]
    group = metadata.link_sessions(sids)
    for sid in sids:
        metadata.set_custom_title(sid, "Unified Changes")
    if disband:
        metadata.unlink_group(group)
    else:
        metadata.unlink_session("gemini")
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE sessions (session_id, project_dir, cwd, last_cwd, title, custom_title, is_teammate)")
        conn.executemany("INSERT INTO sessions VALUES (?, '', '/unified', '/unified', 'Original', 'Unified Changes', 0)",
                         [(sid,) for sid in sids])
        for _ in range(3):
            assert indexer._repair_custom_title_groups(conn) == 0
            assert metadata.get_group("gemini") is None
            assert metadata.get_group("claude") == (None if disband else group)
            assert metadata.get_group("codex") == (None if disband else group)
    assert all(metadata.get_meta(sid)["custom_title"] == "Unified Changes" for sid in sids)


def test_manual_relink_clears_only_selected_unlink_markers(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    group = metadata.link_sessions(["claude", "codex", "gemini"])
    metadata.unlink_group(group)
    assert metadata.link_sessions(["claude", "gemini"], automatic=True) is None
    new = metadata.link_sessions(["claude", "gemini"])
    assert new and metadata.get_group("gemini") == new
    assert not metadata.get_meta("gemini").get("group_unlinked")
    assert metadata.get_meta("codex")["group_unlinked"] is True


def test_stale_title_write_and_older_app_group_repair_cannot_undo_unlink(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    group = metadata.link_sessions(["claude", "codex", "gemini"])
    stale = metadata.get_meta("gemini")
    metadata.unlink_session("gemini")
    stale["custom_title"] = "Renamed"
    metadata._save_one("gemini", stale)
    assert metadata.get_group("gemini") is None
    path = metadata._session_path("gemini")
    old_app = json.loads(path.read_text())
    old_app["group"] = group
    path.write_text(json.dumps(old_app))
    assert metadata.get_group("gemini") is None
    assert "group" not in metadata.get_all_meta()["gemini"]
    assert sorted(metadata.list_group_members(group)) == ["claude", "codex"]
    assert metadata.get_meta("gemini")["custom_title"] == "Renamed"


def test_automatic_link_rechecks_unlink_after_a_stale_scan(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    metadata.link_sessions(["claude", "gemini"])
    stale = metadata.get_all_meta()
    metadata.unlink_session("gemini")
    assert stale["gemini"].get("group")
    assert metadata.link_sessions(["claude", "gemini"], automatic=True) is None
    assert metadata.get_group("claude") is None
    assert metadata.get_group("gemini") is None


def test_atomic_metadata_write_failure_keeps_previous_file(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    metadata.set_custom_title("gemini", "Keep this")
    original = metadata._session_path("gemini").read_bytes()

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(metadata.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        metadata.unlink_session("gemini")
    assert metadata._session_path("gemini").read_bytes() == original
    assert not list(metadata.METADATA_DIR.glob("*.tmp"))


def test_fleet_group_assignment_does_not_override_explicit_unlink(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    metadata.unlink_session("worker")
    metadata.surface_fleet_worker("worker", run_id="run", leg_id="leg", phase="review",
                                  provider="codex", model="model", effort="medium",
                                  worker_key="worker", worker_group_id="fleet-group", title="Review")
    assert metadata.get_group("worker") is None
    assert metadata.get_meta("worker")["fleet_worker"]["worker_group_id"] == "fleet-group"
