"""Per-session project bindings for reusable coding chats."""

from __future__ import annotations

import subprocess

import pytest

from core import metadata


def _isolated_metadata(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(metadata, "METADATA_DIR", tmp_path / "meta")
    monkeypatch.setattr(metadata, "METADATA_PATH", tmp_path / "legacy.json")
    monkeypatch.setattr(metadata, "_migrated", True)


def _git_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


def test_project_binding_changes_only_the_named_session(monkeypatch, tmp_path) -> None:
    """Binding one pane must not contaminate its linked siblings."""

    _isolated_metadata(monkeypatch, tmp_path)
    repo = _git_repo(tmp_path / "serena")
    metadata._save_one("selected", {"group": "linked", "custom_title": "Tightening Serena"})
    metadata._save_one("sibling", {"group": "linked"})

    canonical = metadata.set_work_project_root("selected", repo)

    assert canonical == str(repo)
    assert metadata.get_work_project_root("selected") == str(repo)
    assert metadata.get_work_project_root("sibling") is None
    assert metadata.get_meta("selected")["group"] == "linked"
    assert metadata.get_meta("selected")["custom_title"] == "Tightening Serena"


def test_same_project_binding_is_idempotent(monkeypatch, tmp_path) -> None:
    """A retry of the accepted route must not damage session metadata."""

    _isolated_metadata(monkeypatch, tmp_path)
    repo = _git_repo(tmp_path / "serena")

    first = metadata.set_work_project_root("selected", repo)
    second = metadata.set_work_project_root("selected", repo / ".")

    assert first == second == str(repo)


def test_project_binding_cannot_be_retargeted(monkeypatch, tmp_path) -> None:
    """A session already owned by one repo must never drift to another."""

    _isolated_metadata(monkeypatch, tmp_path)
    serena = _git_repo(tmp_path / "serena")
    locket = _git_repo(tmp_path / "locket")
    metadata.set_work_project_root("selected", serena)

    with pytest.raises(ValueError, match="different project"):
        metadata.set_work_project_root("selected", locket)

    assert metadata.get_work_project_root("selected") == str(serena)


def test_project_binding_rejects_an_empty_session_id(monkeypatch, tmp_path) -> None:
    """An empty identity must not create a shared metadata husk."""

    _isolated_metadata(monkeypatch, tmp_path)
    repo = _git_repo(tmp_path / "serena")

    with pytest.raises(ValueError, match="session id"):
        metadata.set_work_project_root("", repo)
