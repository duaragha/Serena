"""Resuming a chat should not read the whole chat.

Every saved-preference lookup selects one event by its method and takes the
newest. The only index was the primary key, so each of those reads walked the
session and json_extracted every row on the way past -- and the rows are large,
because a pasted image is journalled inline. Resuming one long chat spent most
of a second in the database before the runtime was even opened, six lookups
deep, all of it before the first provider call.

An expression index over the method fixes five of the six. The sixth asked for
the newer of two different methods in a single scan with an OR, which no index
can serve, so it is asked as two seeks instead.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from core.workspace_journal import WorkspaceJournal


@pytest.fixture
def journal(tmp_path):
    return WorkspaceJournal(tmp_path / "events.db")


def _add(journal, session_id, *events):
    with sqlite3.connect(journal.path) as conn:
        start = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM workspace_events WHERE session_id=?",
            (session_id,),
        ).fetchone()[0]
        for offset, event in enumerate(events, start=1):
            conn.execute(
                "INSERT INTO workspace_events VALUES (?, ?, ?)",
                (session_id, start + offset, json.dumps(event)),
            )
    return start + len(events)


def _reset(setting):
    return {"method": "workspace/settingReset", "params": {"setting": setting, "failure_id": "f"}}


def _speed():
    return {"method": "workspace/speed", "params": {"model": "gpt-6", "value": "fast"}}


def _personality(confirmed=True):
    params = {"personality": "friendly"}
    if not confirmed:
        params["personalityConfirmed"] = 0
    return {"method": "workspace/settings", "params": params}


def _noise(n):
    """The bulk of a real session: deltas that no lookup ever wants."""
    return [{"method": "item/agentMessage/delta", "params": {"text": "x" * 64}} for _ in range(n)]


# ── the rewritten lookup answers exactly what the old one did ────────────────

# The query as it was, kept here so the rewrite is checked against the original
# rather than against my reading of it.
ORIGINAL = """SELECT COALESCE(MAX(sequence), 0) FROM workspace_events WHERE session_id=? AND (
    (json_extract(event, '$.method')='workspace/settingReset' AND json_extract(event, '$.params.setting')=?)
    OR (?='speed' AND json_extract(event, '$.method')='workspace/speed')
    OR (?='personality' AND json_extract(event, '$.method')='workspace/settings'
        AND json_type(event, '$.params.personality') IS NOT NULL
        AND COALESCE(json_extract(event, '$.params.personalityConfirmed'), 1) != 0))"""


def _original(journal, session_id, setting):
    with sqlite3.connect(journal.path) as conn:
        return conn.execute(ORIGINAL, (session_id, setting, setting, setting)).fetchone()[0]


CASES = {
    "nothing at all": [],
    "only the setting": [_speed(), _personality()],
    "only a reset": [_reset("speed"), _reset("personality")],
    "reset then setting": [_reset("speed"), _speed(), _reset("personality"), _personality()],
    "setting then reset": [_speed(), _reset("speed"), _personality(), _reset("personality")],
    "unconfirmed personality": [_personality(confirmed=False)],
    "unconfirmed after confirmed": [_personality(), _personality(confirmed=False)],
    "the other setting only": [_reset("speed"), _speed()],
    "buried in noise": [*_noise(20), _speed(), *_noise(20), _personality(), *_noise(20)],
}


@pytest.mark.parametrize("name", sorted(CASES))
@pytest.mark.parametrize("setting", ["personality", "speed"])
def test_the_revision_matches_the_query_it_replaced(journal, name, setting) -> None:
    _add(journal, "s", *CASES[name]) if CASES[name] else None

    assert journal.saved_codex_setting_revision("s", setting) == _original(journal, "s", setting), name


def test_a_reset_for_one_setting_does_not_move_the_other(journal) -> None:
    """The single-scan form carried the setting name through the OR; the two
    seeks must keep that separation."""
    _add(journal, "s", _speed())
    _add(journal, "s", _reset("personality"))

    # Personality's revision advanced past speed's, and speed's did not follow.
    assert journal.saved_codex_setting_revision("s", "personality") == 2
    assert journal.saved_codex_setting_revision("s", "speed") == 1


def test_an_unconfirmed_personality_is_not_a_revision(journal) -> None:
    _add(journal, "s", _personality(), _personality(confirmed=False))

    assert journal.saved_codex_setting_revision("s", "personality") == 1


def test_sessions_do_not_see_each_others_events(journal) -> None:
    _add(journal, "other", _speed(), _speed(), _speed())
    _add(journal, "mine", _speed())

    assert journal.saved_codex_setting_revision("mine", "speed") == 1


def test_an_unknown_setting_is_still_refused(journal) -> None:
    with pytest.raises(ValueError):
        journal.saved_codex_setting_revision("s", "model")


# ── and it stops reading the whole session to answer ─────────────────────────

def test_the_method_is_indexed(journal) -> None:
    with sqlite3.connect(journal.path) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}

    assert "workspace_events_method" in names


def test_an_existing_journal_gains_the_index(tmp_path) -> None:
    """The database predates the index, so opening it must add one."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE workspace_events (
            session_id TEXT NOT NULL, sequence INTEGER NOT NULL, event TEXT NOT NULL,
            PRIMARY KEY (session_id, sequence))""")
        conn.execute("INSERT INTO workspace_events VALUES ('s', 1, ?)", (json.dumps(_speed()),))

    WorkspaceJournal(path)

    with sqlite3.connect(path) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "workspace_events_method" in names


@pytest.mark.parametrize("query, params", [
    ("""SELECT event FROM workspace_events WHERE session_id=?
        AND json_extract(event, '$.method')='workspace/bridgeQueue'
        ORDER BY sequence DESC LIMIT 1""", ("s",)),
    ("""SELECT event FROM workspace_events WHERE session_id=?
        AND json_extract(event, '$.method')='workspace/settings'
        AND json_type(event, '$.params.collaborationMode') IS NOT NULL
        ORDER BY sequence DESC LIMIT 1""", ("s",)),
    ("""SELECT COALESCE(MAX(sequence), 0) FROM workspace_events
        WHERE session_id=? AND json_extract(event, '$.method')='workspace/speed'""", ("s",)),
])
def test_the_resume_lookups_seek_instead_of_scanning(journal, query, params) -> None:
    """A plan that says SCAN is the regression this exists to catch."""
    _add(journal, "s", *_noise(50))

    with sqlite3.connect(journal.path) as conn:
        plan = " ".join(str(row[-1]) for row in conn.execute("EXPLAIN QUERY PLAN " + query, params))

    assert "workspace_events_method" in plan, plan
    assert "SCAN" not in plan, plan
