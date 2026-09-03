"""Gemini is a first-class agent alongside Claude and Codex.

Google retired Google-account sign-in for the standalone `gemini` CLI and moved
individuals to the Antigravity suite, so the binary is `agy`. Serena keeps
calling the agent "gemini" because that is the product; only the executable
changed.

Antigravity stores each conversation as a SQLite database of protobuf blobs
with no published schema, which is not worth chasing. It also keeps
history.jsonl, one line per prompt, carrying the conversation id, workspace,
typed text and timestamp — the whole sidebar row without decoding anything.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from core import gemini_scanner
from core.gemini_usage_reader import parse_usage

WEB_SOURCE = Path(__file__).resolve().parents[1] / "ui" / "web.py"


@pytest.fixture()
def antigravity(tmp_path, monkeypatch):
    """A conversation on disk plus the history that describes it."""
    conversations = tmp_path / "conversations"
    conversations.mkdir()
    conversation_id = "9984a527-b5fa-4226-a9e3-e55661c8d9f1"
    db = conversations / f"{conversation_id}.db"
    sqlite3.connect(db).close()

    history = tmp_path / "history.jsonl"
    history.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [
                {"display": "/model", "timestamp": 1788375410649, "workspace": "/home/raghav", "type": "slash_command"},
                {"display": "restructure the parser", "timestamp": 1788375384640,
                 "workspace": "/home/raghav/Projects/serena", "conversationId": conversation_id},
                {"display": "now write the tests", "timestamp": 1788375484640,
                 "workspace": "/home/raghav/Projects/serena", "conversationId": conversation_id},
                {"display": "/usage", "timestamp": 1788375425531, "workspace": "/home/raghav",
                 "conversationId": conversation_id, "type": "slash_command"},
            ]
        ) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(gemini_scanner, "CONVERSATIONS_DIR", conversations)
    monkeypatch.setattr(gemini_scanner, "HISTORY_FILE", history)
    return db, conversation_id


def test_a_conversation_is_discovered_and_described(antigravity) -> None:
    db, conversation_id = antigravity

    found = list(gemini_scanner.scan_gemini_sessions())
    assert found == [("gemini", db)]

    meta = gemini_scanner.parse_gemini_metadata(db)
    assert meta.session_id == conversation_id, "the file stem is the id agy resumes by"
    assert meta.cwd == "/home/raghav/Projects/serena"
    assert meta.model == "gemini"


def test_the_title_is_what_was_typed_not_a_slash_command(antigravity) -> None:
    """`/usage` and `/model` are control, and make useless chat titles."""
    db, _ = antigravity

    meta = gemini_scanner.parse_gemini_metadata(db)

    assert meta.first_message == "restructure the parser"
    assert meta.message_count == 2, "two typed prompts, the slash command excluded"


def test_history_belonging_to_another_conversation_is_ignored(antigravity) -> None:
    """history.jsonl is global; only this conversation's lines describe it."""
    db, _ = antigravity

    meta = gemini_scanner.parse_gemini_metadata(db)

    assert "/model" not in meta.first_message
    assert meta.cwd != "/home/raghav", "picked up an unrelated entry's workspace"


def test_a_conversation_with_no_history_still_lists(antigravity, tmp_path) -> None:
    """A chat can exist before its first prompt is recorded."""
    db, _ = antigravity
    orphan = db.parent / "11111111-2222-3333-4444-555555555555.db"
    sqlite3.connect(orphan).close()

    meta = gemini_scanner.parse_gemini_metadata(orphan)

    assert meta is not None
    assert meta.first_message == ""
    assert meta.last_timestamp is not None, "mtime still orders it in the sidebar"


# --- usage -------------------------------------------------------------------

USAGE_OUTPUT = (
    "Gemini Models\tWeekly Limit Remaining\t100%\t2026-09-10T18:27:58Z\n"
    "Gemini Models\tFive Hour Limit Remaining\t98%\t2026-09-03T23:27:58Z\n"
    "Claude and GPT models\tWeekly Limit Remaining\t42%\t2026-09-10T18:38:54Z\n"
    "Claude and GPT models\tFive Hour Limit Remaining\t7%\t2026-09-03T23:38:54Z\n"
)


def test_usage_is_inverted_because_the_cli_reports_what_is_left() -> None:
    """Every other pill shows what has been used. 98% remaining is 2% used."""
    usage = parse_usage(USAGE_OUTPUT, now=1788000000)

    assert usage["available"] is True
    assert usage["five_hour"]["used_percentage"] == 2.0
    assert usage["seven_day"]["used_percentage"] == 0.0


def test_antigravitys_own_claude_and_gpt_quota_is_left_alone() -> None:
    """That is a different subscription from the user's Claude and Codex CLIs.

    Folding it in would silently blend two accounts into one pill.
    """
    usage = parse_usage(USAGE_OUTPUT, now=1788000000)

    assert usage["five_hour"]["used_percentage"] != 93.0, "read the Claude/GPT row"
    assert usage["seven_day"]["used_percentage"] != 58.0


def test_reset_times_survive() -> None:
    usage = parse_usage(USAGE_OUTPUT, now=1788000000)

    assert usage["five_hour"]["resets_at"] == 1788478078
    assert usage["seven_day"]["resets_at"] == 1789064878


def test_unparseable_output_reports_unavailable_rather_than_zero() -> None:
    """A blank pill is honest; 0% used would be a lie that reads as healthy."""
    assert parse_usage("")["available"] is False
    assert parse_usage("something went wrong")["available"] is False


# --- the app wiring ----------------------------------------------------------

def _page() -> str:
    return WEB_SOURCE.read_text(encoding="utf-8")


def test_gemini_launches_antigravity() -> None:
    page = _page()
    body = page[page.index("def _gemini_argv("): page.index("def _ensure_resumable(")]

    assert '"agy"' in body, "the gemini agent must launch the Antigravity binary"
    assert '"--conversation"' in body, "no way to resume a specific chat"
    assert '"--dangerously-skip-permissions"' in body


def test_the_spawn_endpoint_routes_gemini() -> None:
    page = _page()
    start = page.index("def api_spawn_terminal(")
    body = page[start : start + 4500]

    assert body.count("_gemini_argv(") == 2, "both the resume and fresh paths need it"
    assert "conversation=sid" in body


def test_gemini_appears_everywhere_the_other_agents_do() -> None:
    page = _page()

    assert 'data-agent="gemini"' in page, "cannot start a Gemini chat"
    assert "filterGemini" in page, "no sidebar filter"
    assert "liveUsageCompactHtml('gemini'" in page, "no limits pill"
    assert "_GEMINI_SVG" in page, "no agent badge"


def test_a_group_of_three_lays_out_as_a_square() -> None:
    """Three panes side by side leaves too few columns for an agent TUI."""
    page = _page()

    assert "_QUAD_MIN_PANES = 3" in page
    match = re.search(r"const _AGENT_QUAD_ORDER = \[([^\]]*)\]", page)
    assert match, "no fixed quadrant order"
    assert "claude" in match.group(1) and "codex" in match.group(1) and "gemini" in match.group(1)


def test_the_split_carries_the_whole_group_not_just_one_sibling() -> None:
    """Asking for "the sibling" of three silently drops one of them."""
    page = _page()
    start = page.index("function _activateTermPane(")
    body = page[start : start + 2000]

    assert "_linkedGroupSids(sid)" in body
    assert "_gtkSplitSids = split ? members : null" in body


def test_the_indexer_actually_calls_the_scanner() -> None:
    """The scanner passing its own tests says nothing about being reachable.

    It was written, tested and committed while the three lines in the indexer
    that call it were not, so every Gemini chat indexed once by hand and was
    then pruned as a zombie on the next pass. A scanner nothing invokes is an
    empty sidebar.
    """
    import inspect

    from core import indexer

    body = inspect.getsource(indexer._update_index_locked)

    assert "scan_gemini_sessions()" in body, "discovery never yields Gemini conversations"
    assert "parse_gemini_metadata(" in body, "discovered conversations are never parsed"
    assert 'if agent == "gemini"' in inspect.getsource(indexer._discovered_session_id), (
        "without an id rule the conversation is dropped before it is parsed"
    )


# --- handing work to Gemini --------------------------------------------------

def test_the_menu_offers_every_agent_in_both_directions() -> None:
    """Two hardcoded rows each meant a third agent was simply unreachable."""
    page = _page()

    assert "const _HANDOFF_AGENTS = ['claude', 'codex', 'gemini']" in page
    assert "'Hand off → ' + _agentLabel(agent)" in page
    assert "'Fork context → ' + _agentLabel(agent)" in page
    assert "targetAgent === 'claude' ? 'Claude' : 'Codex'" not in page, (
        "a Gemini handoff would report itself as Codex"
    )


def test_the_handoff_endpoint_accepts_gemini() -> None:
    page = _page()
    start = page.index("def api_handoff(")
    body = page[start : page.index("@app.route", start + 10)]

    assert '("claude", "codex", "gemini")' in body


def test_gemini_can_receive_a_context_fork() -> None:
    from chats.context_fork import build_context_fork

    with pytest.raises(ValueError) as bad:
        build_context_fork("", "gemini")
    assert "target agent" not in str(bad.value), "gemini was rejected as a destination"


def test_gemini_cannot_be_briefed_FROM_and_says_why() -> None:
    """Its transcript is undecoded protobuf, so there is nothing to summarise.

    Failing with "No messages to summarize" would read as an empty chat rather
    than a limitation, and send Raghav looking for a bug that is not there.
    """
    from unittest.mock import patch

    from chats import handoff

    session = {
        "session_id": "9984a527-b5fa-4226-a9e3-e55661c8d9f1",
        "agent": "gemini",
        "file_path": __file__,  # any file that exists
        "cwd": "/home/raghav",
        "title": "restructure the parser",
    }
    with patch.object(handoff, "get_session", return_value=session):
        result = handoff.build_handoff_briefing(session["session_id"])

    assert result["ok"] is False
    assert "gemini" in result["error"].lower()
    assert "claude or codex" in result["error"].lower(), "no way out is offered"


def test_a_menu_row_is_a_label_not_a_sentence() -> None:
    """A sibling row carried the linked chat's whole title.

    An untitled chat falls back to its first message, so the context menu grew
    wider than the chat list it was opened from.
    """
    page = _page()

    assert "_menuLabel(sib.display_title" in page, "sibling rows are still untruncated"
    start = page.index("function _menuLabel(")
    body = page[start : page.index("\nfunction ", start + 10)]
    assert "44" in body, "no default length"
    assert "…" in body, "a silent cut reads as the real title"
