from __future__ import annotations

from voice.desk.input_mute import (
    read_voice_input_muted,
    write_voice_input_muted,
)


def test_microphone_mute_is_private_and_survives_restart(tmp_path) -> None:
    """A UI-only toggle would let Serena hear again after Electron restarts."""

    path = tmp_path / "private" / "microphone_muted"
    assert write_voice_input_muted(True, path) is True
    assert read_voice_input_muted(path) is True
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600

    assert write_voice_input_muted(False, path) is False
    assert read_voice_input_muted(path) is False


def test_microphone_mute_environment_override_is_test_only(tmp_path, monkeypatch) -> None:
    """Tests need a deterministic switch without touching the live setting."""

    path = tmp_path / "microphone_muted"
    path.write_text("0\n", encoding="utf-8")
    monkeypatch.setenv("SERENA_VOICE_INPUT_MUTED", "true")
    assert read_voice_input_muted(path) is True
