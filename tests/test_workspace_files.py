import os
import subprocess

import pytest

from core.workspace_files import search_project_files


def test_git_lookup_respects_ignore_scope_spaces_and_environment(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("ignored*\n")
    (root / "selected file.py").write_text("private contents never returned")
    (root / "ignored.py").write_text("ignored")
    sibling = tmp_path / "elsewhere"
    sibling.mkdir()
    (sibling / "other.py").write_text("outside")
    monkeypatch.setenv("GIT_WORK_TREE", str(sibling))
    assert search_project_files(root, ".py") == {"paths": ["selected file.py"]}


@pytest.mark.skipif(os.name == "nt", reason="Symlink creation requires Windows privileges")
def test_lookup_does_not_follow_outside_symlinks(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "outside.py").write_text("outside")
    (root / "linked.py").symlink_to(tmp_path / "outside.py")
    (root / "nested").symlink_to(tmp_path, target_is_directory=True)
    assert search_project_files(root, ".py") == {"paths": []}


def test_non_git_lookup_is_bounded_and_skips_generated_directories(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "hidden.py").write_text("generated")
    for index in range(105):
        (tmp_path / f"file-{index:03}.py").touch()
    result = search_project_files(tmp_path, ".py")
    assert len(result["paths"]) == 100
    assert result["paths"][0] == "file-000.py"
    for query in ("", "x" * 201, "bad\0"):
        with pytest.raises(ValueError):
            search_project_files(tmp_path, query)
