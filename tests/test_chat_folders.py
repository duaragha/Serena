"""Filing a chat under a folder of your own choosing.

A chat's home is normally inferred from the directory it ran in. That is right
almost always and useless exactly when the inference has nothing to go on -- a
chat run from ~ or from a Windows path that means nothing here. Moving one is
a statement, so it outranks every guess, and it has to survive the next scan:
the index is rebuilt from transcripts, and a transcript only records where the
chat was typed.

The transcript moves with the chat -- filing one that leaves its transcript
behind is not filing it. Where each CLI still finds a moved session was
measured, not assumed: a real transcript was relocated and resumed for each of
Claude, Codex and Gemini. Gemini needed a symlink left at its flat lookup path;
the other two found the file on their own.
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

    # Every tree this touches lives under the real home, and filing a chat
    # both symlinks into the mirror and MOVES the transcript. Redirect all of
    # them here rather than in the moving-specific fixture: the first run of
    # this file left three stray transcripts in ~/.codex/sessions and ~/.gemini
    # because only the mirror roots were sandboxed.
    from core import project_mirror

    for name in ("CLAUDE_PROJECTS", "CODEX_PROJECTS", "GEMINI_PROJECTS"):
        monkeypatch.setattr(project_mirror, name, tmp_path / "mirror" / name.lower())
    for name in ("CODEX_SESSIONS", "CLAUDE_PROJECTS_DIR", "GEMINI_CONVERSATIONS"):
        target = tmp_path / "cli" / name.lower()
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(chat_folders, name, target)

    db = tmp_path / "index.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, agent TEXT, "
        "project_dir TEXT, slug TEXT, cwd TEXT, last_cwd TEXT, file_path TEXT)"
    )
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
        ("s-1", "codex", "-home-raghav", "-home-raghav", "/home/raghav", "/home/raghav",
         str(transcript)),
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

    def test_the_transcript_goes_with_the_chat(self, workspace) -> None:
        """Filing a chat that leaves its transcript behind is not filing it.

        Each CLI still finds the moved file -- verified against all three by
        relocating a real transcript and resuming it. See the notes on
        transcript_destination for what each one tolerates.
        """
        before = workspace["transcript"].read_bytes()

        result = chat_folders.move_chat("s-1", "frameworth/it")

        landed = Path(result["transcript"]["to"])
        assert landed.is_file()
        assert landed.read_bytes() == before, "the transcript changed in transit"

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


def test_no_test_here_can_touch_the_real_trees(workspace, tmp_path) -> None:
    """The guard that was missing when this file first ran."""
    from core import project_mirror

    roots = [
        chat_folders.CODEX_SESSIONS,
        chat_folders.CLAUDE_PROJECTS_DIR,
        chat_folders.GEMINI_CONVERSATIONS,
        project_mirror.CLAUDE_PROJECTS,
        project_mirror.CODEX_PROJECTS,
        project_mirror.GEMINI_PROJECTS,
    ]
    for root in roots:
        assert str(root).startswith(str(tmp_path)), f"{root} is the user's real tree"


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


# --- the transcript moves too ------------------------------------------------
#
# Where each CLI still finds a session after it moves was measured, not
# assumed: a real transcript was relocated and resumed for each agent. Codex
# scans its tree recursively and found it. Claude looks under the slug of the
# cwd it is launched in and found it. Gemini looks up conversations/<id>.db by
# name, answered "conversation not found" from a subdirectory, and resumed
# normally through a symlink left at the flat path.


@pytest.fixture()
def moving(workspace):
    """The sandboxed CLI trees, named for the tests that plant files in them."""
    return {
        **workspace,
        "CODEX_SESSIONS": chat_folders.CODEX_SESSIONS,
        "CLAUDE_PROJECTS_DIR": chat_folders.CLAUDE_PROJECTS_DIR,
        "GEMINI_CONVERSATIONS": chat_folders.GEMINI_CONVERSATIONS,
    }


def _plant(moving, agent: str, name: str, body: bytes = b'{"x":1}\n') -> Path:
    """Put a transcript where that agent's CLI would have written it."""
    if agent == "claude":
        path = moving["CLAUDE_PROJECTS_DIR"] / "-home-raghav" / name
    elif agent == "codex":
        path = moving["CODEX_SESSIONS"] / "2026" / "07" / "29" / name
    else:
        path = moving["GEMINI_CONVERSATIONS"] / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    conn = sqlite3.connect(moving["db"])
    conn.execute(
        "UPDATE sessions SET agent = ?, file_path = ? WHERE session_id = 's-1'",
        (agent, str(path)),
    )
    conn.commit()
    conn.close()
    return path


class TestTheTranscriptMoves:
    def test_claude_lands_under_the_slug_of_its_new_folder(self, moving) -> None:
        """That is the only place `claude -r` looks when launched there."""
        source = _plant(moving, "claude", "s-1.jsonl")

        result = chat_folders.move_chat("s-1", "frameworth/it")

        assert result["transcript"]["moved"] is True
        assert not source.exists(), "the original was left behind"
        landed = Path(result["transcript"]["to"])
        assert landed.is_file()
        assert landed.parent.name.endswith("-frameworth-it")

    def test_codex_lands_in_the_nested_project_folder(self, moving) -> None:
        source = _plant(moving, "codex", "rollout-2026-07-29T09-18-56-s-1.jsonl")

        result = chat_folders.move_chat("s-1", "frameworth/it")

        assert not source.exists()
        landed = Path(result["transcript"]["to"])
        assert landed.is_file()
        assert landed.parent == moving["CODEX_SESSIONS"] / "Projects" / "frameworth" / "it"

    def test_gemini_leaves_a_link_the_cli_can_still_open(self, moving) -> None:
        """It looks up <id>.db by name and does not recurse."""
        source = _plant(moving, "gemini", "s-1.db")

        result = chat_folders.move_chat("s-1", "frameworth/it")

        landed = Path(result["transcript"]["to"])
        assert landed.is_file() and not landed.is_symlink(), "the real file must move"
        assert source.is_symlink(), "the Gemini CLI could no longer find this chat"
        assert source.resolve() == landed.resolve()
        assert source.read_bytes() == landed.read_bytes()

    def test_the_index_points_at_where_the_file_now_is(self, moving) -> None:
        _plant(moving, "codex", "rollout-2026-07-29T09-18-56-s-1.jsonl")

        result = chat_folders.move_chat("s-1", "frameworth/it")

        assert _row(moving["db"])["file_path"] == result["transcript"]["to"]

    def test_the_folder_becomes_the_working_directory(self, moving) -> None:
        """`claude -r` resolves a session against the cwd's slug, so a stale
        cwd would make Serena stage a second copy under the old slug on every
        resume -- the exact opposite of filing the chat."""
        _plant(moving, "claude", "s-1.jsonl")

        chat_folders.move_chat("s-1", "frameworth/it")

        assert _row(moving["db"])["cwd"].endswith("frameworth/it")


class TestNothingIsEverLost:
    """A misfiled chat is an annoyance; a destroyed transcript is not."""

    def test_a_different_file_of_the_same_name_is_never_overwritten(self, moving) -> None:
        source = _plant(moving, "codex", "rollout-2026-07-29T09-18-56-s-1.jsonl")
        occupied = (moving["CODEX_SESSIONS"] / "Projects" / "frameworth" / "it"
                    / source.name)
        occupied.parent.mkdir(parents=True, exist_ok=True)
        occupied.write_bytes(b'{"someone":"else"}\n')

        result = chat_folders.move_chat("s-1", "frameworth/it")

        assert result["transcript"]["moved"] is False
        assert "already exists" in result["transcript"]["error"]
        assert occupied.read_bytes() == b'{"someone":"else"}\n'
        assert source.is_file(), "the original was removed anyway"

    def test_moving_the_same_chat_twice_is_harmless(self, moving) -> None:
        _plant(moving, "codex", "rollout-2026-07-29T09-18-56-s-1.jsonl")

        first = chat_folders.move_chat("s-1", "frameworth/it")
        second = chat_folders.move_chat("s-1", "frameworth/it")

        assert second["transcript"]["moved"] is True
        assert Path(first["transcript"]["to"]).is_file()

    def test_a_transcript_that_is_not_there_is_not_invented(self, moving) -> None:
        conn = sqlite3.connect(moving["db"])
        conn.execute("UPDATE sessions SET file_path = '/nope/gone.jsonl' WHERE session_id='s-1'")
        conn.commit()
        conn.close()

        result = chat_folders.move_chat("s-1", "frameworth/it")

        assert result["transcript"]["moved"] is False
        assert result["ok"] is True, "the chat is still filed even with no file"

    def test_a_failed_copy_leaves_the_original_alone(self, moving, monkeypatch) -> None:
        source = _plant(moving, "codex", "rollout-2026-07-29T09-18-56-s-1.jsonl")
        original = source.read_bytes()

        import shutil

        def explode(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(shutil, "copy2", explode)
        result = chat_folders.move_chat("s-1", "frameworth/it")

        assert result["transcript"]["moved"] is False
        assert source.read_bytes() == original

    def test_a_second_move_does_not_strand_the_first_copy(self, moving) -> None:
        _plant(moving, "codex", "rollout-2026-07-29T09-18-56-s-1.jsonl")

        first = chat_folders.move_chat("s-1", "frameworth/it")
        chat_folders.move_chat("s-1", "frameworth/legal")

        assert not Path(first["transcript"]["to"]).exists(), "the chat is in two folders"

    def test_a_chat_already_filed_is_not_moved_again_by_its_own_link(self, moving) -> None:
        """Gemini leaves a symlink behind; a later move must not follow it and
        file the link itself somewhere."""
        _plant(moving, "gemini", "s-1.db")
        chat_folders.move_chat("s-1", "frameworth/it")

        conn = sqlite3.connect(moving["db"])
        conn.execute(
            "UPDATE sessions SET file_path = ? WHERE session_id='s-1'",
            (str(moving["GEMINI_CONVERSATIONS"] / "s-1.db"),),
        )
        conn.commit()
        conn.close()

        result = chat_folders.move_chat("s-1", "frameworth/legal")

        assert result["transcript"]["moved"] is False, "a symlink was treated as the transcript"


class TestUnfiling:
    """A move you cannot undo is a trap.

    Clearing has to put the transcript back where its CLI wrote it. Leaving it
    in a folder the chat no longer claims is worse than never moving it: the
    index, the metadata and the disk would each say something different.
    """

    def test_the_transcript_comes_home(self, moving) -> None:
        source = _plant(moving, "codex", "rollout-2026-07-29T09-18-56-s-1.jsonl")
        original = source.read_bytes()
        filed = Path(chat_folders.move_chat("s-1", "frameworth/it")["transcript"]["to"])

        result = chat_folders.clear_folder("s-1")

        assert result["restored"] is True
        assert source.read_bytes() == original
        assert not filed.exists(), "the transcript is in two places"
        assert _row(moving["db"])["file_path"] == str(source)

    def test_geminis_link_is_cleaned_up_too(self, moving) -> None:
        source = _plant(moving, "gemini", "s-1.db")
        chat_folders.move_chat("s-1", "frameworth/it")
        assert source.is_symlink()

        chat_folders.clear_folder("s-1")

        assert source.is_file() and not source.is_symlink(), "a dangling link was left behind"

    def test_clearing_a_chat_that_was_never_filed_does_nothing(self, moving) -> None:
        _plant(moving, "codex", "rollout-2026-07-29T09-18-56-s-1.jsonl")

        result = chat_folders.clear_folder("s-1")

        assert result["restored"] is False

    def test_the_origin_is_the_first_home_not_the_last(self, moving) -> None:
        """Two moves then a clear must land back at the original, not at the
        folder it happened to be filed in most recently."""
        source = _plant(moving, "codex", "rollout-2026-07-29T09-18-56-s-1.jsonl")
        chat_folders.move_chat("s-1", "frameworth/it")
        chat_folders.move_chat("s-1", "frameworth/legal")

        chat_folders.clear_folder("s-1")

        assert source.is_file()
        assert chat_folders.get_folder("s-1") is None

    def test_something_already_at_the_original_path_is_not_clobbered(self, moving) -> None:
        source = _plant(moving, "codex", "rollout-2026-07-29T09-18-56-s-1.jsonl")
        filed = Path(chat_folders.move_chat("s-1", "frameworth/it")["transcript"]["to"])
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b'{"someone":"else"}\n')

        result = chat_folders.clear_folder("s-1")

        assert result["restored"] is False
        assert source.read_bytes() == b'{"someone":"else"}\n'
        assert filed.is_file(), "the filed transcript was removed with nowhere to go"
