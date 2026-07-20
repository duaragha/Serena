from __future__ import annotations

from voice.call.stt import load_whisper_hotwords


def test_vocabulary_file_is_loaded_and_comments_are_ignored(tmp_path) -> None:
    vocabulary = tmp_path / "voice-vocabulary.txt"
    vocabulary.write_text(
        "# project names\nLocket\nPocket TTS # engine\nLocket\n",
        encoding="utf-8",
    )

    assert load_whisper_hotwords(vocabulary) == "Locket Pocket TTS"


def test_environment_hotwords_extend_the_vocabulary(tmp_path, monkeypatch) -> None:
    vocabulary = tmp_path / "voice-vocabulary.txt"
    vocabulary.write_text("Locket\n", encoding="utf-8")
    monkeypatch.setenv("SERENA_CALL_WHISPER_HOTWORDS", "Serena, Locket, Raghav")

    assert load_whisper_hotwords(vocabulary) == "Locket Serena Raghav"
