from core import metadata


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
