"""The git hook shim records his commits and pushes as vcs events."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "ambient-git-hook.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
        timeout=30,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    return repo


def _env(tmp_path: Path) -> dict:
    env = dict(os.environ)
    env["SERENA_AMBIENT_DB_PATH"] = str(tmp_path / "ambient.sqlite3")
    return env


def test_hook_records_a_commit_with_repo_and_action(tmp_path):
    from core.ambient_store import AmbientStore

    repo = _repo(tmp_path)
    probe = subprocess.run(
        [sys.executable, str(HOOK), "commit"], cwd=repo,
        capture_output=True, text=True, timeout=30, env=_env(tmp_path),
    )

    assert probe.returncode == 0
    events = AmbientStore(tmp_path / "ambient.sqlite3").recent_from_disk(seconds=None)
    assert [(event.kind, event.app, event.title) for event in events] == [
        ("vcs", "work", "commit")]


def test_hook_install_links_all_three_hooks_and_skips_real_ones(tmp_path):
    repo = _repo(tmp_path)
    hooks = repo / ".git" / "hooks"
    (hooks / "pre-push").write_text("#!/bin/sh\necho his-hook\n")

    probe = subprocess.run(
        [sys.executable, str(HOOK), "--install", "--repo", str(repo)],
        capture_output=True, text=True, timeout=30, env=_env(tmp_path),
    )

    assert probe.returncode == 0
    assert (hooks / "post-commit").is_symlink()
    assert (hooks / "post-merge").is_symlink()
    assert not (hooks / "pre-push").is_symlink()
    assert (hooks / "pre-push").read_text() == "#!/bin/sh\necho his-hook\n"


def test_hook_never_breaks_git_outside_a_repo(tmp_path):
    probe = subprocess.run(
        [sys.executable, str(HOOK), "commit"], cwd=tmp_path,
        capture_output=True, text=True, timeout=30, env=_env(tmp_path),
    )

    assert probe.returncode == 0


def test_hook_name_wins_over_git_hook_parameters(tmp_path):
    """pre-push passes '<name> <url>'; a strict parse would exit 2 and abort it."""

    from core.ambient_store import AmbientStore

    repo = _repo(tmp_path)
    push_link = tmp_path / "pre-push"
    push_link.symlink_to(HOOK)
    probe = subprocess.run(
        [sys.executable, str(push_link), "origin", "git@example.com:work.git"],
        cwd=repo, capture_output=True, text=True, timeout=30, env=_env(tmp_path),
    )

    assert probe.returncode == 0
    events = AmbientStore(tmp_path / "ambient.sqlite3").recent_from_disk(seconds=None)
    assert [(event.kind, event.app, event.title) for event in events] == [
        ("vcs", "work", "push")]


def test_post_merge_squash_flag_is_not_recorded_as_the_action(tmp_path):
    from core.ambient_store import AmbientStore

    repo = _repo(tmp_path)
    merge_link = tmp_path / "post-merge"
    merge_link.symlink_to(HOOK)
    probe = subprocess.run(
        [sys.executable, str(merge_link), "0"],
        cwd=repo, capture_output=True, text=True, timeout=30, env=_env(tmp_path),
    )

    assert probe.returncode == 0
    events = AmbientStore(tmp_path / "ambient.sqlite3").recent_from_disk(seconds=None)
    assert [(event.kind, event.app, event.title) for event in events] == [
        ("vcs", "work", "merge")]
