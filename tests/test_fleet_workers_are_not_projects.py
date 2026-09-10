"""A Fleet worker is part of a run, not a project of its own.

Workers run in disposable checkouts. Fleet's own workspace root sits under the
state directory and was already treated as machinery, but a run handed a
directory beside real work -- `_artifacts/fleet-<name>-<date>/<worker>` -- was
not, so every worker turned its scratch checkout into a project row and the
project tree filled with them. Twenty-four sessions across five runs were
showing that way when this was found.

Excluding them by their own marker rather than by where they sit is what keeps
this true the next time Fleet picks a different directory. The sidebar already
gathers the same sessions under Fleet Chats, keyed on the same marker, so they
stay reachable.
"""

from __future__ import annotations

import pytest

from core import indexer


@pytest.fixture
def projects(monkeypatch):
    """list_projects over a fixed set of sessions and metadata."""

    def run(rows, meta):
        class _Cursor:
            def __init__(self, payload):
                self.payload = payload

            def fetchall(self):
                return self.payload

        class _Conn:
            def execute(self, _sql):
                return _Cursor(rows)

            def close(self):
                pass

        monkeypatch.setattr(indexer, "_get_db", lambda: _Conn())
        from core import metadata as meta_sync

        monkeypatch.setattr(meta_sync, "get_all_meta", lambda: meta)
        return indexer.list_projects()

    return run


WORKER_META = {"fleet_worker": {"run_id": "a7e4e6e3", "worker_label": "Agent A"}}


def _row(session_id, cwd, slug="-home-raghav", stamp="2026-09-08T10:00:00Z"):
    return {
        "session_id": session_id,
        "project_dir": slug,
        "device": "laptop",
        "first_timestamp": stamp,
        "cwd": cwd,
    }


def test_a_worker_checkout_beside_real_work_is_not_a_project(projects) -> None:
    """The exact shape that filled the tree: a worktree under _artifacts."""
    out = projects(
        [
            _row("real", "/home/raghav/Documents/Projects/serena"),
            _row("w1", "/home/raghav/Documents/Projects/_artifacts/fleet-astra-2026-09-08/astra"),
            _row("w2", "/home/raghav/Documents/Projects/_artifacts/fleet-astra-2026-09-08/sol"),
        ],
        {"w1": WORKER_META, "w2": WORKER_META},
    )

    keys = [p["project_key"] for p in out]
    assert keys == ["/home/raghav/Documents/Projects/serena"]
    assert not any("fleet-" in key for key in keys)


def test_a_worker_does_not_inflate_the_project_it_ran_for(projects) -> None:
    out = projects(
        [
            _row("real", "/home/raghav/Documents/Projects/serena"),
            _row("w1", "/home/raghav/Documents/Projects/serena"),
        ],
        {"w1": WORKER_META},
    )

    assert [p["chat_count"] for p in out] == [1]


def test_a_worker_cannot_teach_the_slug_map_its_scratch_path(projects) -> None:
    """A session with no cwd recovers its project through the slug map. A
    worker's throwaway checkout must not be what that map learns, or the
    cwd-less session lands in the scratch directory."""
    out = projects(
        [
            _row("w1", "/home/raghav/Documents/Projects/_artifacts/fleet-astra-2026-09-08/astra", slug="-shared"),
            _row("old", "", slug="-shared"),
        ],
        {"w1": WORKER_META},
    )

    assert not any("fleet-" in (p.get("project_key") or "") for p in out)
    assert not any("fleet-" in (p.get("cwd") or "") for p in out)


def test_an_ordinary_chat_in_artifacts_still_counts(projects) -> None:
    """_artifacts is where finished deliverables live, so the directory itself
    is legitimate. Only Fleet's workers are excluded, and only because they say
    so themselves."""
    out = projects(
        [_row("d1", "/home/raghav/Documents/Projects/_artifacts/unified")],
        {},
    )

    assert [p["project_key"] for p in out] == [
        "/home/raghav/Documents/Projects/_artifacts/unified"
    ]


def test_a_marker_without_a_run_is_not_treated_as_a_worker(projects) -> None:
    """The sidebar requires run_id before it calls a session a Fleet worker;
    this must agree, or the two disagree about the same session."""
    out = projects(
        [_row("s1", "/home/raghav/Documents/Projects/serena")],
        {"s1": {"fleet_worker": {"worker_label": "Agent A"}}},
    )

    assert [p["project_key"] for p in out] == ["/home/raghav/Documents/Projects/serena"]


def test_missing_or_malformed_metadata_does_not_break_the_listing(projects) -> None:
    out = projects(
        [_row("s1", "/home/raghav/Documents/Projects/serena")],
        {"s1": None, "s2": "not-a-dict", "s3": {"fleet_worker": "nope"}},
    )

    assert [p["project_key"] for p in out] == ["/home/raghav/Documents/Projects/serena"]


def test_this_machine_has_no_fleet_worktree_projects_left() -> None:
    """The end the user actually looks at."""
    rows = indexer.list_projects()

    leaked = [
        p for p in rows
        if "fleet-" in (p.get("project_key") or "") or "/fleet-" in (p.get("cwd") or "")
    ]
    assert not leaked, f"fleet worktrees still listed as projects: {leaked[:3]}"
