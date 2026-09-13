"""Filing a chat browses the tree instead of asking you to retype it.

The folder to file a chat under was a text box. It printed the existing folder
names into its own prompt as a hint and then asked the user to type one of them
back -- the lookup the app was already in a position to do.

It browses now: one level at a time, breadcrumbs for where you are, a click to
go deeper. Navigating and choosing stay separate, which is what lets a folder
with no children still be somewhere you can put a chat.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from core import chat_folders
from ui import web


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A small projects root, including the noise a picker must hide."""
    root = tmp_path / "Projects"
    for path in [
        "frameworth/it",
        "frameworth/apps/storefront",
        "serena/chats",
        "_artifacts/codex-runs/2026-08-16",
        "empty-leaf",
        "serena/node_modules/left-pad",
        "serena/.git/objects",
        "serena/__pycache__",
    ]:
        (root / path).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(chat_folders, "projects_root", lambda: root)
    return root


def _names(result):
    return [entry["name"] for entry in result["entries"]]


def test_the_root_lists_its_projects(tree) -> None:
    result = chat_folders.browse_folders("")

    assert _names(result) == ["_artifacts", "empty-leaf", "frameworth", "serena"]
    assert result["relative"] == ""
    assert result["parent"] is None, "the root has nowhere above it"
    assert [c["name"] for c in result["crumbs"]] == ["Projects"]


def test_descending_shows_only_that_level(tree) -> None:
    """The point of browsing: one level, not a flattened dump of everything."""
    result = chat_folders.browse_folders("frameworth")

    assert _names(result) == ["apps", "it"]
    assert result["parent"] == ""
    assert [c["name"] for c in result["crumbs"]] == ["Projects", "frameworth"]


def test_breadcrumbs_can_be_walked_back_up(tree) -> None:
    result = chat_folders.browse_folders("_artifacts/codex-runs/2026-08-16")

    assert [c["relative"] for c in result["crumbs"]] == [
        "",
        "_artifacts",
        "_artifacts/codex-runs",
        "_artifacts/codex-runs/2026-08-16",
    ]
    assert result["parent"] == "_artifacts/codex-runs"


def test_a_row_says_whether_opening_it_shows_anything(tree) -> None:
    """So a leaf does not open onto an empty list with no warning."""
    result = chat_folders.browse_folders("")
    by_name = {entry["name"]: entry for entry in result["entries"]}

    assert by_name["frameworth"]["has_children"] is True
    assert by_name["empty-leaf"]["has_children"] is False


def test_build_output_and_vcs_plumbing_are_never_offered(tree) -> None:
    result = chat_folders.browse_folders("serena")

    assert _names(result) == ["chats"]
    assert "node_modules" not in _names(result)
    assert ".git" not in _names(result)


@pytest.mark.parametrize("escape", ["..", "../..", "../../etc", "frameworth/../..", "/etc"])
def test_the_picker_cannot_leave_the_projects_root(tree, escape) -> None:
    """A path that would climb out is not an error to report, it is simply
    somewhere this picker does not go."""
    result = chat_folders.browse_folders(escape)

    assert result["relative"] == ""
    assert result["absolute"] == str(tree.resolve())


def test_a_symlink_out_of_the_tree_is_not_a_way_out(tree, tmp_path) -> None:
    outside = tmp_path / "outside"
    (outside / "secrets").mkdir(parents=True)
    (tree / "escape-hatch").symlink_to(outside)

    result = chat_folders.browse_folders("escape-hatch")

    assert result["relative"] == "", "a symlink walked out of the projects root"


def test_a_folder_that_is_not_there_lands_at_the_root(tree) -> None:
    result = chat_folders.browse_folders("nope/never/existed")

    assert result["relative"] == ""


def test_browsing_and_creating_agree_on_where_here_is(tree) -> None:
    """Creating resolves its parent through the browser, so the containment
    rule is written once rather than twice."""
    client = web.app.test_client()
    made = client.post(
        "/api/folder-create", json={"parent": "../../frameworth", "name": "Should Not Escape"}
    ).get_json()

    assert made["ok"] is True
    # The parent climbed out, so it was clamped to the root, and the name was
    # normalised the same way every other folder name is.
    assert made["relative"] == "should-not-escape"
    assert (tree / "should-not-escape").is_dir()


def test_creating_rejects_a_name_that_normalises_to_nothing(tree) -> None:
    client = web.app.test_client()
    response = client.post("/api/folder-create", json={"parent": "", "name": "///"})

    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_creating_the_same_folder_twice_is_not_an_error(tree) -> None:
    """The user may well create a folder that a previous attempt already made."""
    client = web.app.test_client()
    first = client.post("/api/folder-create", json={"parent": "frameworth", "name": "leads"}).get_json()
    second = client.post("/api/folder-create", json={"parent": "frameworth", "name": "leads"}).get_json()

    assert first["relative"] == second["relative"] == "frameworth/leads"


def test_the_browse_route_answers_the_shape_the_picker_reads(tree) -> None:
    payload = web.app.test_client().get("/api/folder-browse?path=frameworth").get_json()

    assert set(payload) >= {"root", "relative", "absolute", "parent", "crumbs", "entries"}
    assert payload["relative"] == "frameworth"


# ── the client-side path helper ──────────────────────────────────────────────

HARNESS = r"""
'use strict';
const assert = require('node:assert/strict');
let _projectsRoot = '/home/raghav/Documents/Projects';
__FUNCTION__
const out = {
  inside: _relativeToProjects('/home/raghav/Documents/Projects/frameworth/it'),
  atRoot: _relativeToProjects('/home/raghav/Documents/Projects'),
  trailing: _relativeToProjects('/home/raghav/Documents/Projects/frameworth/'),
  outside: _relativeToProjects('/home/raghav/elsewhere/thing'),
  lookalike: _relativeToProjects('/home/raghav/Documents/ProjectsOther/thing'),
  empty: _relativeToProjects(''),
};
_projectsRoot = '';
out.noRoot = _relativeToProjects('/home/raghav/Documents/Projects/frameworth');
console.log(JSON.stringify(out));
"""


def _function(name: str) -> str:
    source = web.HTML
    start = source.index(f"function {name}(")
    cursor = source.index("(", start)
    parens = 0
    for index in range(cursor, len(source)):
        if source[index] == "(":
            parens += 1
        elif source[index] == ")":
            parens -= 1
            if parens == 0:
                cursor = index + 1
                break
    depth = 0
    for index in range(source.index("{", cursor), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"{name} is not brace-balanced")


@pytest.fixture(scope="module")
def relative():
    if shutil.which("node") is None:
        pytest.skip("node is required to run page code")
    script = HARNESS.replace("__FUNCTION__", _function("_relativeToProjects"))
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "rel.cjs"
        path.write_text(script, encoding="utf-8")
        done = subprocess.run(
            ["node", str(path)], capture_output=True, text=True, timeout=60,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin"},
        )
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout)


def test_the_explorer_opens_where_the_chat_already_lives(relative) -> None:
    """Moving a chat one folder over should start beside it, not at the top."""
    assert relative["inside"] == "frameworth/it"
    assert relative["trailing"] == "frameworth"
    assert relative["atRoot"] == ""


def test_a_home_outside_the_projects_root_opens_at_the_root(relative) -> None:
    """Chats run from ~ or from a Windows path have a home this tree cannot
    show; the picker starts at the top rather than nowhere."""
    assert relative["outside"] == ""
    assert relative["empty"] == ""
    assert relative["noRoot"] == ""


def test_a_sibling_whose_name_merely_starts_the_same_is_not_inside(relative) -> None:
    assert relative["lookalike"] == "", "prefix match let a neighbouring directory in"


def test_the_flow_opens_the_explorer_and_no_longer_asks_for_typing() -> None:
    page = web.HTML
    start = page.index("async function moveChatToFolderFlow(")
    body = page[start : page.index("async function linkChatPickerFlow(", start)]

    assert "await showFolderExplorer({" in body
    assert "showPrompt({" not in body, "the folder flow still prompts for a typed path"
    assert "'Existing: '" not in page, "the old hint list is still being built"


def test_cancelling_is_distinct_from_unfiling() -> None:
    """Both are falsy, and one of them must not quietly unfile the chat."""
    page = web.HTML
    start = page.index("async function moveChatToFolderFlow(")
    body = page[start : page.index("async function linkChatPickerFlow(", start)]

    assert "if (folder === null) return;" in body


def test_the_chosen_folder_is_committed_by_absolute_path(tree) -> None:
    """A relative path is normalised on the way in: lowercased, spaces dashed.
    So browsing to a folder that is really called "Client Work" and sending its
    relative path would file the chat into a new "client-work" beside it."""
    (tree / "Client Work").mkdir()

    browsed = chat_folders.browse_folders("Client Work")
    assert browsed["relative"] == "Client Work"

    # What the explorer commits is the absolute path, which is taken as given.
    assert chat_folders.resolve_folder(browsed["absolute"]) == (tree / "Client Work").resolve()
    # The relative form is what would have gone wrong.
    assert chat_folders.resolve_folder(browsed["relative"]) != (tree / "Client Work")


def test_the_explorer_sends_the_absolute_path_not_the_relative_one() -> None:
    body = _function("showFolderExplorer")

    assert "close(view ? view.absolute : '')" in body
    assert "close(view ? view.relative : '')" not in body, (
        "the relative path is normalised on the way in and would land elsewhere"
    )


def test_committing_is_a_button_not_a_stray_keystroke() -> None:
    """Enter opens the focused folder. If it also filed the chat, a fast
    keyboard user would file into whatever happened to be highlighted."""
    body = _function("showFolderExplorer")

    assert "else enter(rows[focusedIdx]);" in body
    assert "if (e.ctrlKey || e.metaKey) close(view ? view.absolute : '');" in body
