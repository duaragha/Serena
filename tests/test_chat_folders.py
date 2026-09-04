"""Filing a chat under a folder of your own choosing.

A chat's home is normally inferred from the directory it ran in. That is right
almost always and useless exactly when the inference has nothing to go on -- a
chat run from ~ or from a Windows path that means nothing here. Moving one is
a statement, so it outranks every guess, and it has to survive the next scan:
the index is rebuilt from transcripts, and a transcript only records where the
chat was typed.

The transcript itself never moves. It stays where its CLI wrote it, which is
the only place that CLI looks when you resume the chat.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core import chat_folders


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """A projects root, a metadata store and an index, all disposable."""
    root = tmp_path / "Projects"
    (root / "frameworth").mkdir(parents=True)

    monkeypatch.setattr(chat_folders, "projects_root", lambda: root)

    meta_dir = tmp_path / "meta"
    meta_dir.mkdir()
    from core import metadata

    monkeypatch.setattr(metadata, "META_DIR", meta_dir, raising=False)
    monkeypatch.setattr(metadata, "_session_path", lambda sid: meta_dir / f"{sid}.json")

    # The mirror roots are under the real home. Without redirecting them the
    # move writes symlinks into the user's own canonical tree -- this fixture
    # did exactly that on its first run, leaving three links to files in
    # /tmp/pytest behind.
    from core import project_mirror

    for name in ("CLAUDE_PROJECTS", "CODEX_PROJECTS", "GEMINI_PROJECTS"):
        monkeypatch.setattr(project_mirror, name, tmp_path / "mirror" / name.lower())

    db = tmp_path / "index.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, agent TEXT, "
        "project_dir TEXT, slug TEXT, cwd TEXT, file_path TEXT)"
    )
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        ("s-1", "codex", "-home-raghav", "-home-raghav", "/home/raghav", str(transcript)),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(chat_folders, "DB_PATH", db)

    return {"root": root, "db": db, "transcript": transcript}


def _row(db: Path, sid: str = "s-1") -> sqlite3.Row:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM sessions WHERE session_id = ?", (sid,)).fetchone()
    finally:
        conn.close()


class TestTheMove:
    def test_a_folder_that_does_not_exist_yet_is_created(self, workspace) -> None:
        """"Move it into frameworth/it" includes making frameworth/it."""
        result = chat_folders.move_chat("s-1", "frameworth/it")

        assert result["folder_created"] is True
        assert (workspace["root"] / "frameworth" / "it").is_dir()

    def test_the_chat_gets_the_new_slug(self, workspace) -> None:
        chat_folders.move_chat("s-1", "frameworth/it")

        row = _row(workspace["db"])
        assert row["project_dir"].endswith("-frameworth-it")
        assert row["slug"] == row["project_dir"]

    def test_the_transcript_does_not_move(self, workspace) -> None:
        """Its CLI looks where it wrote it; moving the file breaks resume."""
        before = workspace["transcript"].read_text(encoding="utf-8")

        chat_folders.move_chat("s-1", "frameworth/it")

        assert workspace["transcript"].is_file()
        assert workspace["transcript"].read_text(encoding="utf-8") == before

    def test_the_move_is_remembered(self, workspace) -> None:
        chat_folders.move_chat("s-1", "frameworth/it")

        assert chat_folders.get_folder("s-1") == str(workspace["root"] / "frameworth" / "it")

    def test_a_second_move_replaces_the_first(self, workspace) -> None:
        chat_folders.move_chat("s-1", "frameworth/it")
        chat_folders.move_chat("s-1", "frameworth/legal")

        assert chat_folders.get_folder("s-1").endswith("frameworth/legal")
        assert _row(workspace["db"])["project_dir"].endswith("-frameworth-legal")

    def test_clearing_hands_the_chat_back_to_inference(self, workspace) -> None:
        chat_folders.move_chat("s-1", "frameworth/it")
        chat_folders.clear_folder("s-1")

        assert chat_folders.get_folder("s-1") is None

    def test_a_chat_that_is_not_indexed_is_refused(self, workspace) -> None:
        with pytest.raises(ValueError, match="no chat with id"):
            chat_folders.move_chat("nope", "frameworth/it")

    def test_a_folder_is_required(self, workspace) -> None:
        with pytest.raises(ValueError, match="destination folder"):
            chat_folders.move_chat("s-1", "   ")


class TestFolderNames:
    def test_a_relative_folder_is_read_against_the_projects_root(self, workspace) -> None:
        assert chat_folders.resolve_folder("frameworth/it") == (
            workspace["root"] / "frameworth" / "it"
        )

    def test_case_does_not_make_a_second_folder(self, workspace) -> None:
        """Typing IT twice must not leave IT beside it."""
        assert chat_folders.resolve_folder("frameworth/IT") == chat_folders.resolve_folder(
            "frameworth/it"
        )

    def test_a_name_with_spaces_and_punctuation_is_still_a_directory(self, workspace) -> None:
        assert chat_folders.resolve_folder("Client Work!").name == "client-work"

    def test_an_absolute_path_is_taken_as_given(self, workspace, tmp_path) -> None:
        """So a chat can be filed somewhere the projects root does not cover."""
        elsewhere = tmp_path / "elsewhere" / "notes"
        assert chat_folders.resolve_folder(str(elsewhere)) == elsewhere

    def test_a_name_that_is_only_punctuation_is_refused(self, workspace) -> None:
        with pytest.raises(ValueError, match="no usable folder name"):
            chat_folders.resolve_folder("///")


class TestEveryAgent:
    """Claude, Codex and Gemini are filed the same way."""

    @pytest.mark.parametrize("agent", ["claude", "codex", "gemini"])
    def test_the_move_works_for_each(self, workspace, agent) -> None:
        conn = sqlite3.connect(workspace["db"])
        conn.execute("UPDATE sessions SET agent = ? WHERE session_id = 's-1'", (agent,))
        conn.commit()
        conn.close()

        result = chat_folders.move_chat("s-1", "frameworth/it")

        assert result["ok"] is True
        assert result["project_dir"].endswith("-frameworth-it")


def test_the_move_never_writes_outside_its_own_roots(workspace, tmp_path) -> None:
    """A test that files a chat must not touch the real canonical tree."""
    from core import project_mirror

    chat_folders.move_chat("s-1", "frameworth/it")

    for root in (project_mirror.CLAUDE_PROJECTS, project_mirror.CODEX_PROJECTS,
                 project_mirror.GEMINI_PROJECTS):
        assert str(root).startswith(str(tmp_path)), "a mirror root escaped the sandbox"
    assert not (Path.home() / ".codex" / "projects" / "Projects" / "frameworth" / "it"
                / "rollout.jsonl").exists()


def test_the_mirror_follows_the_chosen_folder() -> None:
    """The canonical tree has to agree, or the chat is in two places."""
    from core.project_mirror import folder_subpath

    assert folder_subpath("/home/raghav/Documents/Projects/frameworth/it") == (
        "Projects/frameworth/it"
    )
    assert folder_subpath(None) is None


def test_the_next_scan_does_not_undo_the_move() -> None:
    """The index is rebuilt from transcripts, which know nothing about this."""
    import inspect

    from core import indexer

    body = inspect.getsource(indexer)
    assert 'synced.get("chat_folder")' in body, "a re-index would overwrite the chosen folder"


# --- the surfaces ------------------------------------------------------------

WEB_SOURCE = Path(__file__).resolve().parents[1] / "ui" / "web.py"


def test_the_menu_offers_the_move_for_any_chat() -> None:
    """Not gated on agent: a Gemini chat is filed like a Claude one."""
    page = WEB_SOURCE.read_text(encoding="utf-8")

    assert "'Move to folder…'" in page
    assert "async function moveChatToFolderFlow(sid)" in page


def test_the_picker_offers_existing_folders_without_requiring_one() -> None:
    """The point of the feature is filing a chat somewhere new."""
    page = WEB_SOURCE.read_text(encoding="utf-8")
    start = page.index("async function moveChatToFolderFlow(sid)")
    body = page[start : page.index("\nasync function linkChatPickerFlow", start)]

    assert "/api/chat-folders" in body, "no existing folders are suggested"
    assert "showPrompt(" in body, "the destination must be typeable"
    assert "loadSessions(currentProject" in body, "the sidebar would not show the move"


def test_the_endpoint_exists_and_clears_on_an_empty_folder() -> None:
    page = WEB_SOURCE.read_text(encoding="utf-8")
    start = page.index("def api_chat_folder(")
    body = page[start : page.index("@app.route", start + 10)]

    assert "clear_folder(session_id)" in body, (
        "an empty folder would file the chat at the projects root"
    )
    assert "move_chat(session_id, folder)" in body
