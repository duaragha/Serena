from __future__ import annotations

from voice.call.spoken_text import prepare_spoken_text


def test_spoken_text_removes_visual_syntax() -> None:
    assert prepare_spoken_text(
        "**open** [the report](https://example.com/report), then use `voice_model` & retry 20%."
    ) == "open the report, then use voice model and retry 20 percent."


def test_spoken_text_collapses_lists_and_whitespace() -> None:
    assert prepare_spoken_text("1. first\n2. second\n\nthird") == "first second third"

