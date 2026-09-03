from __future__ import annotations

from voice.call.stt import (
    DEFAULT_BEAM_SIZE,
    DEFAULT_VOCABULARY_PATH,
    load_whisper_beam_size,
    load_whisper_hotwords,
)


def test_default_vocabulary_is_packaged_with_the_call_runtime() -> None:
    hotwords = load_whisper_hotwords(DEFAULT_VOCABULARY_PATH)

    assert "Serena" in hotwords
    assert "Raghav" in hotwords


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


def test_decoder_uses_a_real_search_beam_by_default(monkeypatch) -> None:
    """Greedy beam one made short spoken commands needlessly inaccurate."""

    monkeypatch.delenv("SERENA_CALL_WHISPER_BEAM_SIZE", raising=False)
    assert load_whisper_beam_size() == DEFAULT_BEAM_SIZE == 5


def test_decoder_beam_override_is_bounded(monkeypatch) -> None:
    """A typo in the environment must not make local transcription explode."""

    monkeypatch.setenv("SERENA_CALL_WHISPER_BEAM_SIZE", "999")
    assert load_whisper_beam_size() == 10
    monkeypatch.setenv("SERENA_CALL_WHISPER_BEAM_SIZE", "not-a-number")
    assert load_whisper_beam_size() == DEFAULT_BEAM_SIZE
