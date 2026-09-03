from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from pathlib import Path

from core.daypart import current_daypart
from voice.call.brain import BrainEvent
from voice.call.tts import DeterministicTTSStub
from voice.desk.greetings import SCHEMA_VERSION, DeskGreetingPool, GreetingAudio


class _Brain:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def stream_turn(
        self,
        text: str,
        *,
        call_id: str,
        turn_id: str,
        request_id: str | None = None,
        journal: bool = True,
    ):
        self.calls.append(
            {
                "text": text,
                "call_id": call_id,
                "turn_id": turn_id,
                "journal": journal,
            }
        )
        response = f"there you are, greeting {len(self.calls)}."
        yield BrainEvent("start", request_id or "request")
        yield BrainEvent("delta", request_id or "request", delta=response)
        yield BrainEvent("done", request_id or "request", say=response)


class _Runtime:
    def __init__(self) -> None:
        self.brain = _Brain()
        self.tts = DeterministicTTSStub(samples_per_sentence=240)

    async def warm(self) -> dict[str, str]:
        return {"stt": "ready", "tts": "ready", "vad": "ready"}


def test_pool_generates_persists_and_refills_after_take(tmp_path: Path) -> None:
    runtime = _Runtime()
    path = tmp_path / "desk-greetings.json"
    pool = DeskGreetingPool(
        runtime,
        path=path,
        target_size=2,
        refill_after_take_seconds=0,
    )

    asyncio.run(pool._refill())
    assert pool.status["cached"] == 2
    assert len(runtime.brain.calls) == 2
    assert all(call["journal"] is False for call in runtime.brain.calls)
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600

    first = pool.take()
    assert first is not None
    assert first.text == "there you are, greeting 1."
    assert pool.wait(2)
    assert pool.status["cached"] == 2
    assert len(runtime.brain.calls) == 3

    restored = DeskGreetingPool(runtime, path=path, target_size=2)
    assert restored.status["cached"] == 2


def test_greeting_prompt_carries_the_real_clock_without_dictating_words(
    tmp_path: Path,
) -> None:
    runtime = _Runtime()
    pool = DeskGreetingPool(
        runtime,
        path=tmp_path / "cache.json",
        target_size=1,
        clock=lambda: datetime(2026, 8, 5, 23, 40).astimezone(),
    )

    asyncio.run(pool._refill())

    prompt = runtime.brain.calls[0]["text"]
    assert 'day-part="late-night"' in prompt
    assert 'clock="11:40 pm"' in prompt
    assert 'weekday="Wednesday"' in prompt
    assert "never reach for a stock phrase" in prompt
    assert "mention the time of day" not in prompt
    assert "good evening" not in prompt.lower()
    assert pool.status["daypart"] == "late-night"

    cached = pool.take()
    assert cached is not None
    assert cached.daypart == "late-night"


def test_pool_never_serves_a_greeting_written_for_another_part_of_day(
    tmp_path: Path,
) -> None:
    path = tmp_path / "desk-greetings.json"
    hour = {"value": 9}
    pool = DeskGreetingPool(
        _Runtime(),
        path=path,
        target_size=1,
        refill_after_take_seconds=0,
        clock=lambda: datetime(2026, 8, 5, hour["value"], 0).astimezone(),
    )
    asyncio.run(pool._refill())
    assert pool.status["cached"] == 1

    hour["value"] = 23

    assert pool.status["cached"] == 0
    assert pool.take() is None
    assert pool.wait(2)
    assert pool.status["cached"] == 1
    persisted = path.read_text(encoding="utf-8")

    reloaded = DeskGreetingPool(
        _Runtime(),
        path=path,
        target_size=1,
        clock=lambda: datetime(2026, 8, 5, 9, 0).astimezone(),
    )
    assert reloaded.status["cached"] == 0

    path.write_text(persisted, encoding="utf-8")
    late = DeskGreetingPool(
        _Runtime(),
        path=path,
        target_size=1,
        clock=lambda: datetime(2026, 8, 5, 23, 30).astimezone(),
    )
    served = late.take()
    assert served is not None
    assert served.daypart == "late-night"


def test_pool_discards_expired_audio(tmp_path: Path) -> None:
    path = tmp_path / "desk-greetings.json"
    item = GreetingAudio(
        "old", 24_000, b"\x01\x00", "old", time.time() - 120, current_daypart()
    )
    from core.brain_lifetime import write_json_atomic

    write_json_atomic(path, {"version": SCHEMA_VERSION, "items": [item.to_json()]})
    pool = DeskGreetingPool(
        _Runtime(), path=path, target_size=1, max_age_seconds=60
    )

    assert pool.status["cached"] == 0


def test_pool_rejects_pre_voice_contract_cache(tmp_path: Path) -> None:
    path = tmp_path / "desk-greetings.json"
    item = GreetingAudio("alba", 24_000, b"\x01\x00", "old voice", time.time())
    from core.brain_lifetime import write_json_atomic

    write_json_atomic(path, {"version": 1, "items": [item.to_json()]})

    pool = DeskGreetingPool(_Runtime(), path=path, target_size=1)

    assert pool.status["cached"] == 0


def test_pool_fails_closed_when_runtime_is_not_ready(tmp_path: Path) -> None:
    runtime = _Runtime()

    async def failed_warm() -> dict[str, str]:
        return {"stt": "ready", "tts": "error: missing", "vad": "ready"}

    runtime.warm = failed_warm  # type: ignore[method-assign]
    pool = DeskGreetingPool(runtime, path=tmp_path / "cache.json")

    try:
        asyncio.run(pool._refill())
    except RuntimeError as exc:
        assert "not ready" in str(exc)
    else:
        raise AssertionError("greeting pool accepted an unready voice runtime")


def test_take_delays_refill_while_one_hot_greeting_remains(tmp_path: Path) -> None:
    runtime = _Runtime()
    pool = DeskGreetingPool(
        runtime,
        path=tmp_path / "cache.json",
        target_size=2,
        refill_after_take_seconds=60,
    )
    asyncio.run(pool._refill())

    assert pool.take() is not None
    assert len(runtime.brain.calls) == 2
    assert pool.status["cached"] == 1
    assert pool.status["refill_scheduled"] is True

    pool.start_refill()
    assert pool.wait(2)
    assert len(runtime.brain.calls) == 3
    assert pool.status["cached"] == 2


def test_empty_pool_cancels_delayed_refill_and_replenishes_now(tmp_path: Path) -> None:
    runtime = _Runtime()
    pool = DeskGreetingPool(
        runtime,
        path=tmp_path / "cache.json",
        target_size=2,
        refill_after_take_seconds=60,
    )
    asyncio.run(pool._refill())

    assert pool.take() is not None
    assert pool.status["refill_scheduled"] is True
    assert pool.take() is not None

    assert pool.wait(2)
    assert pool.status["cached"] == 2
    assert pool.status["refill_scheduled"] is False


def test_refill_owns_an_isolated_tts_backend_when_factory_is_set(
    tmp_path: Path,
) -> None:
    runtime = _Runtime()

    class OwnedTTS(DeterministicTTSStub):
        def __init__(self) -> None:
            super().__init__(samples_per_sentence=240)
            self.warm_calls = 0
            self.closed = False

        async def warm(self) -> None:
            self.warm_calls += 1

        async def cancel(self, generation: int | None = None) -> None:
            if generation is None:
                self.closed = True

    owned = OwnedTTS()
    pool = DeskGreetingPool(
        runtime,
        path=tmp_path / "cache.json",
        target_size=1,
        tts_factory=lambda: owned,
    )

    asyncio.run(pool._refill())

    assert pool.status["cached"] == 1
    assert owned.warm_calls == 1
    assert owned.closed is True


def test_a_stale_cached_greeting_is_not_replayed_forever(tmp_path, monkeypatch) -> None:
    """2026-08-21: the pool held one greeting, so every wake after the first
    got a 503 and fell back to the last cached line. One odd greeting
    ("handsome weasel") became the thing she said every single time."""
    import json
    import time

    from voice.desk.io import (
        FALLBACK_MAX_AGE_SECONDS,
        FALLBACK_SCHEMA_VERSION,
        GreetingFetcher,
    )
    from voice.desk.greetings import GreetingAudio

    path = tmp_path / "last-greeting.json"
    fetcher = GreetingFetcher("http://127.0.0.1:1/greeting", "t", fallback_path=path)

    def _write(age_seconds: float) -> None:
        greeting = GreetingAudio(
            "gid", 24_000, b"\x00\x00" * 100, "handsome weasel",
            time.time() - age_seconds, "",
        )
        path.write_text(
            json.dumps({"version": FALLBACK_SCHEMA_VERSION, "greeting": greeting.to_json()}),
            encoding="utf-8",
        )

    _write(60.0)
    assert fetcher.load_fallback() is not None, "a fresh cached line is still good"

    _write(FALLBACK_MAX_AGE_SECONDS + 60.0)
    assert fetcher.load_fallback() is None, (
        "a stale cached line must expire; the plain tone beats saying the same "
        "strange thing every wake"
    )


def test_the_greeting_prompt_forbids_invented_pet_names() -> None:
    """He asked for ordinary. The old prompt pushed 'vary it every time, never
    reach for a stock phrase', which is what produced an invented nickname."""
    from pathlib import Path as _P

    source = _P("voice/desk/greetings.py").read_text()
    prompt = source.split("desk-wake-greeting", 1)[1][:2000]
    # the old novelty pressure is gone as an instruction; it survives only
    # inside the incident note that explains why it was removed
    assert "vary it every time, never reach for a stock phrase" not in prompt
    assert "Never invent a nickname" in prompt
    assert "repeating one is fine" in prompt
    assert "ordinary spoken greeting" in prompt
