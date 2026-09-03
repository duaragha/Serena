from __future__ import annotations

from voice.desk.output_mute import (
    read_voice_output_muted,
    write_voice_output_muted,
)


def test_output_mute_defaults_to_audible_and_persists_atomically(tmp_path) -> None:
    setting = tmp_path / "voice_muted"

    assert read_voice_output_muted(setting) is False
    assert write_voice_output_muted(True, setting) is True
    assert setting.read_text(encoding="utf-8") == "1\n"
    assert read_voice_output_muted(setting) is True
    assert not setting.with_name("voice_muted.tmp").exists()

    assert write_voice_output_muted(False, setting) is False
    assert read_voice_output_muted(setting) is False


def test_output_mute_environment_override_is_strict(monkeypatch, tmp_path) -> None:
    setting = tmp_path / "voice_muted"
    setting.write_text("1\n", encoding="utf-8")

    monkeypatch.setenv("SERENA_VOICE_OUTPUT_MUTED", "off")
    assert read_voice_output_muted(setting) is False
    monkeypatch.setenv("SERENA_VOICE_OUTPUT_MUTED", "yes")
    assert read_voice_output_muted(setting) is True
