"""Only rollouts the Codex CLI can actually resume belong in the sidebar.

The scanner walked the whole sessions tree with rglob. Syncthing keeps a
file-versioning archive at ``.stversions`` under any folder it syncs, holding
the previous bytes of every rollout it replaced or deleted. Those copies parse
perfectly and describe real conversations, so they were indexed as chats --
and clicking one gets "No saved session found with ID ...", because the CLI
looks in its own tree and that id is not in it. The pane dies immediately,
which reads as "codex chats aren't loading".

Seven such rows were live when this was written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import codex_scanner

# The shape the scanner reads: an interactive CLI session, which is the only
# kind it surfaces at all.
FIRST_LINE = {
    "timestamp": "2026-08-25T09:25:54.000Z",
    "type": "session_meta",
    "payload": {
        "id": "019f0000-1111-2222-3333-444444444444",
        "timestamp": "2026-08-25T09:25:54.000Z",
        "cwd": "/home/raghav",
        "originator": "codex_cli_rs",
        "cli_version": "0.153.2",
        "source": "cli",
    },
}


def _rollout(directory: Path, sid: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-08-25T09-25-54-{sid}.jsonl"
    meta = json.loads(json.dumps(FIRST_LINE))
    meta["payload"]["id"] = sid
    path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def sessions_root(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_scanner, "CODEX_SESSIONS_ROOT", tmp_path)
    return tmp_path


def _found(root: Path) -> set[Path]:
    return {path for _agent, path in codex_scanner.scan_codex_sessions()}


def test_a_real_rollout_is_still_found(sessions_root) -> None:
    live = _rollout(sessions_root / "2026" / "08" / "25", "019f1111-1111-2222-3333-444444444444")

    assert _found(sessions_root) == {live}


@pytest.mark.parametrize("copies_dir", [".stversions", ".stfolder", "archived_sessions"])
def test_a_copy_is_not_a_chat(sessions_root, copies_dir: str) -> None:
    """Syncthing's archive and Codex's own bin hold copies, not sessions."""
    live = _rollout(sessions_root / "2026" / "08" / "25", "019f1111-1111-2222-3333-444444444444")
    _rollout(sessions_root / copies_dir / "2026" / "08" / "25",
             "019f2222-1111-2222-3333-444444444444")

    assert _found(sessions_root) == {live}, f"{copies_dir} was indexed as real chats"


def test_the_archived_twin_of_a_live_chat_is_not_a_second_row(sessions_root) -> None:
    """Syncthing archives the OLD bytes under the SAME session id.

    Both copies describe the same conversation, so indexing both is a race for
    which one wins -- and the archived one loses on resume.
    """
    sid = "019f1111-1111-2222-3333-444444444444"
    live = _rollout(sessions_root / "2026" / "08" / "25", sid)
    _rollout(sessions_root / ".stversions" / "2026" / "08" / "25", sid)

    assert _found(sessions_root) == {live}
