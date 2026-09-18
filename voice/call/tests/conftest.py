"""Keep the call tests off whatever voice stack this machine happens to run.

``config/voice-stack.json`` and ``~/.config/serena/elevenlabs.env`` are real,
synced configuration: between them they are what make the laptop and the PC
sound like the same person. A test that asserts a built-in default starts
failing the moment Raghav changes his voice or pastes a key, which is exactly
how the recognizer-selection test broke here once already.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_machine_voice_stack(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "SERENA_VOICE_STACK_CONFIG", str(tmp_path / "no-voice-stack.json"))
    monkeypatch.setenv(
        "SERENA_CALL_ELEVENLABS_ENV", str(tmp_path / "no-elevenlabs.env"))
