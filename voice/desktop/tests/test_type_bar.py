"""The overlay's type bar: input channel when the microphone is unusable.

Typing must produce the same turn speaking does, and the overlay must still
be there to type into after a spoken conversation ends, otherwise the
fallback disappears at exactly the moment the microphone is the problem.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from voice.brain_bridge import parse_client_message

DESKTOP = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("fix the phev tracker", "fix the phev tracker"),
        ("  spaced   out   words  ", "spaced out words"),
        ("line\nbreaks\tcollapse", "line breaks collapse"),
    ],
)
def test_typed_messages_are_accepted_and_normalised(text: str, expected: str) -> None:
    out = parse_client_message(json.dumps({"type": "typed", "text": text}))
    assert out is not None
    assert json.loads(out) == {"type": "typed", "text": expected}


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "typed"},
        {"type": "typed", "text": ""},
        {"type": "typed", "text": "   "},
        {"type": "typed", "text": 42},
        {"type": "typed", "text": "x" * 4_001},
        {"type": "typed", "text": "ok", "extra": "field"},
    ],
)
def test_malformed_typed_messages_are_rejected(payload: dict) -> None:
    assert parse_client_message(json.dumps(payload)) is None


def test_amplitude_still_works_alongside_typing() -> None:
    out = parse_client_message(json.dumps({"type": "amplitude", "value": 0.5}))
    assert out is not None and json.loads(out)["type"] == "amplitude"


def test_overlay_survives_a_finished_conversation() -> None:
    """A clean desk-voice exit must not take the type bar down with it."""
    source = (DESKTOP / "supervisor.py").read_text(encoding="utf-8")
    assert 'if name == "desk-voice" and return_code == 0:' in source
    assert "keeping the overlay and bridge up for typing" in source


def test_wake_restarts_the_unit_so_the_microphone_returns() -> None:
    """With the unit staying up, a plain `start` would never rearm the mic."""
    source = (DESKTOP.parent / "desk" / "wake_listener.py").read_text(encoding="utf-8")
    assert '"restart",' in source
    assert '"start",' not in source.split("FULL_VOICE_UNIT")[0][-400:]


@pytest.mark.parametrize("script", ["main.js", "preload.js", "renderer/app.js"])
def test_electron_sources_parse(script: str) -> None:
    result = subprocess.run(
        ["node", "--check", str(DESKTOP / script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_type_bar_is_wired_end_to_end() -> None:
    html = (DESKTOP / "renderer" / "index.html").read_text(encoding="utf-8")
    css = (DESKTOP / "renderer" / "styles.css").read_text(encoding="utf-8")
    app = (DESKTOP / "renderer" / "app.js").read_text(encoding="utf-8")
    preload = (DESKTOP / "preload.js").read_text(encoding="utf-8")
    main = (DESKTOP / "main.js").read_text(encoding="utf-8")

    assert 'id="type-input"' in html and 'id="type-bar"' in html
    assert "#type-bar" in css
    assert "sendTyped" in preload and "typed-message" in preload
    assert "typed-message" in main and "type: 'typed'" in main
    assert "sendTyped" in app
    # Typing must never be swallowed by the overlay's global shortcuts.
    assert "stopPropagation" in app
