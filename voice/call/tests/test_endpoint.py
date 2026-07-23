from __future__ import annotations

import threading
import time

import pytest

from voice.call.endpoint import (
    VAD_WINDOW_SAMPLES,
    ProcessEndpointDetector,
    SileroEndpointDetector,
    SileroProcessPool,
)


class ScriptedVAD:
    def __init__(self, probabilities: list[float]) -> None:
        self.probabilities = iter(probabilities)
        self.window_sizes: list[int] = []
        self.reset_count = 0

    def __call__(self, samples, sample_rate: int) -> float:
        self.window_sizes.append(len(samples))
        assert sample_rate == 16_000
        return next(self.probabilities)

    def reset_states(self) -> None:
        self.reset_count += 1


def pcm_windows(count: int, value: int = 100) -> bytes:
    return value.to_bytes(2, "little", signed=True) * VAD_WINDOW_SAMPLES * count


def test_endpoint_uses_sample_counts_exact_windows_and_pre_roll() -> None:
    model = ScriptedVAD([0.0, 0.0, 0.9, 0.9, 0.0, 0.0])
    detector = SileroEndpointDetector(
        model,
        threshold=0.5,
        silence_ms=64,
        pre_roll_ms=32,
    )
    result = detector.process_pcm(pcm_windows(6))
    assert result is not None
    assert result.reason == "silence"
    assert result.trailing_silence_samples == 2 * VAD_WINDOW_SAMPLES
    assert result.sample_count == 5 * VAD_WINDOW_SAMPLES
    assert model.window_sizes == [VAD_WINDOW_SAMPLES] * 6
    assert model.reset_count == 1


def test_partial_batches_accumulate_until_512_samples() -> None:
    model = ScriptedVAD([0.9, 0.0])
    detector = SileroEndpointDetector(
        model, threshold=0.5, silence_ms=32, pre_roll_ms=0
    )
    assert detector.process_pcm(b"\x01\x00" * 300) is None
    assert model.window_sizes == []
    assert detector.process_pcm(b"\x01\x00" * 212) is None
    result = detector.process_pcm(pcm_windows(1))
    assert result is not None
    assert result.sample_count == 2 * VAD_WINDOW_SAMPLES


def test_forced_ptt_end_preserves_unvoiced_audio() -> None:
    model = ScriptedVAD([0.0, 0.0])
    detector = SileroEndpointDetector(model, pre_roll_ms=32)
    detector.process_pcm(pcm_windows(2))
    result = detector.finish(force=True)
    assert result is not None
    assert result.reason == "ptt_end_forced"
    assert result.sample_count == 2 * VAD_WINDOW_SAMPLES


class FakeProcessWorker:
    def __init__(self) -> None:
        self.warm_count = 0
        self.closed = 0
        self.requests: list[dict] = []

    def warm_sync(self) -> dict:
        self.warm_count += 1
        return {"provider": "test"}

    def request_sync(self, payload: dict, *, generation: int) -> dict:
        self.requests.append({**payload, "generation": generation})
        return {"ok": True, "result": None}

    def close(self) -> None:
        self.closed += 1


def test_process_endpoint_mirrors_state_and_passes_commit_reason() -> None:
    class StatefulWorker(FakeProcessWorker):
        def request_sync(self, payload: dict, *, generation: int) -> dict:
            self.requests.append({**payload, "generation": generation})
            if payload["op"] == "process":
                return {
                    "ok": True,
                    "result": None,
                    "speech_active": True,
                    "trailing_ms": 720,
                }
            return {"ok": True, "result": None}

    worker = StatefulWorker()
    detector = ProcessEndpointDetector(worker)

    assert detector.process_pcm(b"\x01\x00" * 512) is None
    assert detector.speech_active is True
    assert detector.trailing_ms == 720

    assert detector.finish(reason="silence") is None
    assert detector.speech_active is False
    assert detector.trailing_ms == 0
    assert worker.requests[-1]["reason"] == "silence"


def test_vad_process_pool_is_bounded_and_reuses_clean_slot() -> None:
    worker = FakeProcessWorker()
    pool = SileroProcessPool(size=1, worker_factory=lambda: worker)
    pool.warm()
    first = pool.endpoint()
    second = pool.endpoint()

    first.warm()
    with pytest.raises(RuntimeError, match="capacity is busy"):
        second.warm()

    first.close(reusable=True)
    second.warm()
    assert any(request["op"] == "reset" for request in worker.requests)
    second.close(reusable=True)


def test_vad_process_pool_waits_for_unclean_slot_restore(monkeypatch) -> None:
    monkeypatch.setenv("SERENA_CALL_VAD_SLOT_WAIT", "2")

    class BlockingRestoreWorker(FakeProcessWorker):
        def __init__(self) -> None:
            super().__init__()
            self.restore_started = threading.Event()
            self.release_restore = threading.Event()

        def warm_sync(self) -> dict:
            if self.closed:
                self.restore_started.set()
                assert self.release_restore.wait(timeout=1)
            return super().warm_sync()

    worker = BlockingRestoreWorker()
    pool = SileroProcessPool(size=1, worker_factory=lambda: worker)
    first = pool.endpoint()
    second = pool.endpoint()
    first.warm()
    first.close(reusable=False)
    assert worker.restore_started.wait(timeout=1)

    errors: list[BaseException] = []

    def acquire_second() -> None:
        try:
            second.warm()
        except BaseException as exc:
            errors.append(exc)

    waiting = threading.Thread(target=acquire_second)
    waiting.start()
    time.sleep(0.05)
    assert waiting.is_alive()
    worker.release_restore.set()
    waiting.join(timeout=1)

    assert not waiting.is_alive()
    assert errors == []
    second.close(reusable=True)


def test_call_vad_defaults_to_ptt_release_not_mid_utterance_silence(
    monkeypatch,
) -> None:
    monkeypatch.delenv("SERENA_CALL_VAD_SILENCE_MS", raising=False)
    worker = FakeProcessWorker()
    pool = SileroProcessPool(size=1, worker_factory=lambda: worker)

    assert pool.detector_config["silence_ms"] == 30_000
    assert pool.detector_config["max_utterance_ms"] == 30_000


def test_endpoint_pool_accepts_desk_timing_without_mutating_call_env(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SERENA_CALL_VAD_SILENCE_MS", "30000")
    pool = SileroProcessPool(
        silence_ms=650,
        pre_roll_ms=320,
        max_utterance_ms=20_000,
        worker_factory=FakeProcessWorker,
    )

    assert pool.detector_config == {
        "threshold": 0.5,
        "silence_ms": 650,
        "pre_roll_ms": 320,
        "max_utterance_ms": 20_000,
    }
