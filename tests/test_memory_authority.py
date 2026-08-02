"""Serena edits her own memory and knowledge on Raghav's word, not her own.

He asked for exactly this: add, delete, edit memories and knowledge when he
tells her to. The broker holds the "when he tells her to" part: every write is
checked against the real spoken turn, and destructive actions need him to have
actually asked for removal or change.
"""

from __future__ import annotations

import pytest

from core.memory_authority import authority_denial, authorize


def _turn(text: str, protocol: str = "voice") -> dict:
    return {"text": text, "protocol": protocol, "call_id": "desk-1", "turn_id": "desk-1:2"}


@pytest.mark.parametrize(
    "spoken",
    [
        "remember that i switched the phev tracker to ios",
        "save that for later",
        "note that jenna moved our session to thursdays",
        "can you keep track of my protein target",
        "add that to your memory",
    ],
)
def test_he_asks_her_to_remember_and_she_may(spoken: str) -> None:
    assert authority_denial("save_memory", _turn(spoken), destructive=False) is None


@pytest.mark.parametrize(
    "spoken",
    [
        "delete that memory about the old apartment",
        "forget what i said about the diet",
        "that engagement stuff is outdated, clean it up",
        "memory 364 is wrong, fix it",
        "get rid of the telegram topic",
    ],
)
def test_he_asks_her_to_forget_or_fix_and_she_may(spoken: str) -> None:
    assert authority_denial("delete_memory", _turn(spoken), destructive=True) is None


@pytest.mark.parametrize(
    "spoken",
    [
        "what's the weather today",
        "how are you doing",
        "tell me about the phev tracker",
    ],
)
def test_ordinary_conversation_authorizes_no_writes(spoken: str) -> None:
    assert authority_denial("save_memory", _turn(spoken), destructive=False) is not None
    assert authority_denial("delete_memory", _turn(spoken), destructive=True) is not None


def test_asking_her_to_remember_does_not_authorize_deletion() -> None:
    """The destructive bar is separate: "remember this" is not "delete that"."""
    turn = _turn("remember that i like my coffee black")
    assert authority_denial("save_memory", turn, destructive=False) is None
    assert authority_denial("delete_memory", turn, destructive=True) is not None


def test_no_spoken_turn_means_no_writes() -> None:
    assert authority_denial("save_memory", {}, destructive=False) is not None
    assert (
        authority_denial("save_memory", _turn("remember this", protocol="frontdoor"), destructive=False)
        is not None
    )


def test_decisions_are_audited_without_raw_speech(tmp_path) -> None:
    audit = tmp_path / "memory-authority.jsonl"
    authorize(
        "delete_memory",
        origin=_turn("what's the weather"),
        destructive=True,
        detail="[42]",
        audit_path=audit,
    )
    body = audit.read_text(encoding="utf-8")
    assert '"allowed":false' in body.replace(" ", "")
    assert "weather" not in body  # only the digest is stored


def test_the_write_tools_are_exposed_and_named_for_their_broker() -> None:
    from core.brain_memory_tools import MEMORY_TOOL_NAMES

    names = {name.rsplit("__", 1)[-1] for name in MEMORY_TOOL_NAMES}
    assert names == {
        "save_memory",
        "edit_memory",
        "delete_memory",
        "save_knowledge",
        "delete_knowledge_topic",
    }
    assert all(name.startswith("mcp__serena-memory__") for name in MEMORY_TOOL_NAMES)


def test_refusals_are_unmissable() -> None:
    """The live failure mode on the work tool was her claiming success after a
    refusal; the same wording contract applies here."""
    from core.brain_memory_tools import _REFUSAL

    assert "NOT DONE" in _REFUSAL
    assert "never" not in _REFUSAL.split("Reason")[0]  # the header is the verdict
