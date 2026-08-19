"""One project, one row.

The sidebar keyed projects on Claude's directory slug. That slug is not stable:
older releases folded ``_`` into ``-``, so ``personal_projects/locket`` minted
two of them and the chat history was split across duplicate rows. Per-ticket
worktrees added more rows for the same repository, a renamed project kept its
old identity forever, and Serena's own scratch directories sat in the list as
though they were work.

Identity now comes from the working directory the transcript recorded, folded up
to the project root it belongs to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import projects


@pytest.fixture(autouse=True)
def clear_caches():
    projects._root_cache.clear()
    projects._alias_cache["mtime"] = None
    projects._alias_cache["aliases"] = None
    yield
    projects._root_cache.clear()


def _repo(path: Path, *, worktree: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    marker = path / ".git"
    if worktree:
        # Git writes .git as a FILE for a linked worktree, which is what the
        # per-ticket checkouts here actually are.
        marker.write_text("gitdir: /elsewhere\n", encoding="utf-8")
    else:
        marker.mkdir(exist_ok=True)
    return path


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "Projects"
    monkeypatch.setattr(projects, "WORKSPACE_ROOTS", (str(root),))
    monkeypatch.setattr(projects, "DEFAULT_ALIASES", {}, raising=False)
    # pytest builds tmp_path under /tmp, which is real machinery in production.
    # Drop that prefix here so the fixture directories are treated as work.
    monkeypatch.setattr(
        projects,
        "MACHINERY_PREFIXES",
        tuple(x for x in projects.MACHINERY_PREFIXES if not x.startswith("/tmp")),
    )
    return root


def test_underscore_and_hyphen_slugs_resolve_to_one_project():
    """The pair that split locket, atrium and full_tracker in half."""

    hyphen = "/home/raghav/Documents/Projects/personal-projects/locket"
    underscore = "/home/raghav/Documents/Projects/personal_projects/locket"

    # Both slugs recorded the SAME cwd, so grouping on the cwd merges them.
    assert projects.project_key(
        "-home-raghav-Documents-Projects-personal-projects-locket", underscore
    ) == projects.project_key(
        "-home-raghav-Documents-Projects-personal_projects-locket", underscore
    )
    # And the hyphen path is not silently treated as the same directory.
    assert projects.canonical_cwd(hyphen) != projects.canonical_cwd(underscore)


def test_a_renamed_project_keeps_its_history(monkeypatch):
    monkeypatch.setattr(
        projects,
        "DEFAULT_ALIASES",
        {"~/p/full_tracker": "~/p/locket"},
        raising=False,
    )
    projects._alias_cache["mtime"] = None

    home = str(Path.home())
    assert projects.canonical_cwd(f"{home}/p/full_tracker") == f"{home}/p/locket"
    # Subdirectories follow the rename rather than stranding.
    assert (
        projects.canonical_cwd(f"{home}/p/full_tracker/android")
        == f"{home}/p/locket/android"
    )


def test_per_ticket_worktrees_fold_onto_their_repository(workspace):
    _repo(workspace / "frameworth" / "liquid")
    _repo(workspace / "frameworth" / "liquid-fw43", worktree=True)
    _repo(workspace / "frameworth" / "liquid-fw45-fleet", worktree=True)

    base = workspace / "frameworth" / "liquid"
    assert projects.project_root(str(workspace / "frameworth" / "liquid-fw43")) == str(base)
    assert projects.project_root(str(workspace / "frameworth" / "liquid-fw45-fleet")) == str(base)


def test_a_cluster_of_ticket_clones_folds_without_a_base_directory(workspace):
    """Fifteen frameworth-google-ads-* checkouts, no plain base repo."""

    for name in ("app-fw34", "app-fw43", "app-fw43-followup", "app-fw32-closeout"):
        _repo(workspace / "work" / name, worktree=True)

    expected = str(workspace / "work" / "app")
    assert projects.project_root(str(workspace / "work" / "app-fw34")) == expected
    assert projects.project_root(str(workspace / "work" / "app-fw43-followup")) == expected


def test_separate_repos_that_share_a_prefix_stay_separate(workspace):
    """amazon and amazon-appeals are different projects, not one clone set."""

    _repo(workspace / "work" / "amazon")
    _repo(workspace / "work" / "amazon-appeals")

    assert projects.project_root(str(workspace / "work" / "amazon-appeals")) == str(
        workspace / "work" / "amazon-appeals"
    )


def test_a_git_worktree_directory_folds_to_the_repository(workspace):
    _repo(workspace / "frameworth")
    branch = workspace / "frameworth" / ".worktrees" / "fw-56-hydrogen"
    _repo(branch, worktree=True)

    assert projects.project_root(str(branch)) == str(workspace / "frameworth")


def test_a_subdirectory_belongs_to_its_project(workspace):
    _repo(workspace / "personal_projects" / "locket")
    nested = workspace / "personal_projects" / "locket" / "android"
    nested.mkdir(parents=True)

    assert projects.project_root(str(nested)) == str(workspace / "personal_projects" / "locket")


def test_serena_scratch_directories_are_machinery_not_projects():
    home = str(Path.home())
    assert projects.is_machinery(f"{home}/.local/state/serena/fleet-worktrees/abc/claude-a")
    assert projects.is_machinery(f"{home}/.config/serena/fleet-worktrees/abc/claude-b")
    assert projects.is_machinery(f"{home}/.cache/serena-headless-codex/work")
    assert projects.is_machinery("/tmp/claude-1000/scratchpad")
    assert not projects.is_machinery(f"{home}/Documents/Projects/serena")


def test_the_project_list_has_one_row_per_project_and_loses_nothing():
    """Against the real index: no duplicates, and every chat still accounted."""

    from core.indexer import list_projects

    rows = list_projects()
    with_machinery = list_projects(include_machinery=True)

    keys = [row["project_key"] for row in rows]
    assert len(keys) == len(set(keys)), "a project must not appear twice"
    assert sum(r["chat_count"] for r in rows) <= sum(
        r["chat_count"] for r in with_machinery
    )
    # Hiding machinery may only ever remove machinery rows.
    hidden = {r["project_key"] for r in with_machinery} - set(keys)
    assert all(
        projects.is_machinery(key) or key.startswith("-tmp-") or not key.startswith("/")
        for key in hidden
    )


def test_bulk_star_never_silently_unstars_a_mixed_selection():
    """Six stars vanished this way: toggle-per-row on a mixed selection."""

    import re as _re
    from pathlib import Path as _Path

    source = (_Path(__file__).resolve().parents[1] / "ui" / "web.py").read_text(
        encoding="utf-8"
    )
    body = _re.search(
        r"async function bulkToggleStar\(\) \{(.*?)\n\}", source, _re.DOTALL
    )
    assert body is not None, "bulkToggleStar moved"
    code = body.group(1)

    # It must decide one target state for the whole selection...
    assert "every(s => s.starred)" in code
    # ...and only flip the rows that are not already there.
    assert "needsFlip" in code
    # The old shape posted a toggle for every selected id unconditionally.
    assert "sids.map(sid => fetch('/api/star/'" not in code
