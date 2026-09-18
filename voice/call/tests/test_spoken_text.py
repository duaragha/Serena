from __future__ import annotations

from voice.call.spoken_text import prepare_spoken_text


def test_spoken_text_removes_visual_syntax() -> None:
    assert prepare_spoken_text(
        "**open** [the report](https://example.com/report), then use `voice_model` & retry 20%."
    ) == "open the report, then use voice model and retry 20 percent."


def test_spoken_text_collapses_lists_and_whitespace() -> None:
    assert prepare_spoken_text("1. first\n2. second\n\nthird") == "first second third"



def test_a_fleet_failure_is_speakable_down_a_phone() -> None:
    """She read a hash, a Windows path and agent:a out loud, character by character."""

    said = prepare_spoken_text(
        "Fleet 8ba9ee7a for duaragha__locket.task-1054 is failed in Code, with 1 of 4 "
        "agent steps complete. Last error: execute: could not refresh the prior "
        "worktree for agent:a: error: failed to delete "
        "'C:/Users/ragha/.local/state/serena/fleet-worktrees/"
        "8ba9ee7a-f1fa-416d-b7d0-6d02e893d3bd/agent-a': Filename too long."
    )

    assert "8ba9ee7a" not in said
    assert "C:/Users" not in said
    assert "agent:a" not in said
    assert "the fleet run" in said
    # The number is the one identifier he actually says back to her.
    assert "task 1054 in locket" in said
    assert "Filename too long" in said


def test_a_posix_path_keeps_only_the_name_he_could_act_on() -> None:
    said = prepare_spoken_text(
        "the file is at /home/raghav/Documents/Projects/serena/core/phone_line.py"
    )

    assert "/home/raghav" not in said
    assert "phone line.py" in said


def test_a_bare_run_id_is_not_spelled_out() -> None:
    assert "that run" in prepare_spoken_text("run 3f8a9c2d1e is done")
    assert "3f8a9c2d1e" not in prepare_spoken_text("run 3f8a9c2d1e is done")


def test_ordinary_speech_is_left_alone() -> None:
    """The sanitiser must not start rewriting the way she actually talks."""

    for line in (
        "hey, i queued that as #1061 and it is running now",
        "task 1054 is blocked, text retry 1054 to rerun it",
        "yeah, i hear you loud and clear",
    ):
        assert prepare_spoken_text(line) == line
