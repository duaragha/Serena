"""Prefetched, local-only greeting audio for sub-1.5 second desk wakes."""

from __future__ import annotations

import asyncio
import base64
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from core.brain_lifetime import write_json_atomic
from voice.call.brain import BrainBackend
from voice.call.tts import AsyncTTSBackend

DEFAULT_CACHE_PATH = (
    Path.home() / ".cache" / "serena" / "desk-greetings.json"
)
MAX_GREETING_BYTES = 2 * 1024 * 1024
MAX_GREETING_AGE_SECONDS = 12 * 60 * 60
# Every serving-voice change bumps this schema so a cached greeting cannot
# speak in a retired voice before the warmed runtime takes over.
SCHEMA_VERSION = 3


class GreetingRuntime(Protocol):
    brain: BrainBackend
    tts: AsyncTTSBackend

    async def warm(self) -> dict[str, str]: ...


@dataclass(frozen=True, slots=True)
class GreetingAudio:
    greeting_id: str
    sample_rate: int
    pcm: bytes
    text: str
    created_at: float

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.greeting_id,
            "sample_rate": self.sample_rate,
            "pcm_b64": base64.b64encode(self.pcm).decode("ascii"),
            "text": self.text,
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, value: object) -> GreetingAudio | None:
        if not isinstance(value, dict):
            return None
        try:
            greeting_id = str(value["id"])
            sample_rate = int(value["sample_rate"])
            pcm = base64.b64decode(str(value["pcm_b64"]), validate=True)
            text = str(value["text"]).strip()
            created_at = float(value["created_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if (
            not greeting_id
            or sample_rate not in {16_000, 22_050, 24_000, 44_100, 48_000}
            or not pcm
            or len(pcm) > MAX_GREETING_BYTES
            or len(pcm) % 2
            or not text
            or not 0 < created_at <= time.time() + 60
        ):
            return None
        return cls(greeting_id, sample_rate, pcm, text, created_at)


class DeskGreetingPool:
    """Maintain a tiny persisted queue of assistant-generated PCM greetings."""

    def __init__(
        self,
        runtime: GreetingRuntime,
        *,
        path: Path = DEFAULT_CACHE_PATH,
        target_size: int = 2,
        max_age_seconds: float = MAX_GREETING_AGE_SECONDS,
        refill_after_take_seconds: float = 60.0,
        tts_factory: Callable[[], AsyncTTSBackend] | None = None,
    ) -> None:
        self.runtime = runtime
        self.path = Path(path).expanduser()
        self.target_size = max(1, min(int(target_size), 4))
        self.max_age_seconds = max(60.0, float(max_age_seconds))
        self.refill_after_take_seconds = max(
            0.0, float(refill_after_take_seconds)
        )
        self.tts_factory = tts_factory
        self._lock = threading.RLock()
        self._refilling = False
        self._thread: threading.Thread | None = None
        self._refill_timer: threading.Timer | None = None
        self._last_error = ""
        self._items = self._load()

    @property
    def status(self) -> dict[str, object]:
        with self._lock:
            self._discard_expired_locked()
            return {
                "ready": bool(self._items),
                "cached": len(self._items),
                "target": self.target_size,
                "refilling": self._refilling,
                "refill_scheduled": bool(
                    self._refill_timer is not None
                    and self._refill_timer.is_alive()
                ),
                "last_error": self._last_error or None,
            }

    def start_refill(self) -> None:
        with self._lock:
            self._discard_expired_locked()
            timer = self._refill_timer
            self._refill_timer = None
            if timer is not None and timer is not threading.current_thread():
                timer.cancel()
            if self._refilling or len(self._items) >= self.target_size:
                return
            self._refilling = True
            self._thread = threading.Thread(
                target=self._refill_thread,
                name="serena-desk-greeting-refill",
                daemon=True,
            )
            self._thread.start()

    def schedule_refill(self) -> None:
        with self._lock:
            self._discard_expired_locked()
            if self._refilling or len(self._items) >= self.target_size:
                return
            timer = self._refill_timer
            if timer is not None and timer.is_alive():
                if self._items:
                    return
                timer.cancel()
                self._refill_timer = None
            delay = self.refill_after_take_seconds if self._items else 0.0
            if delay <= 0:
                start_now = True
            else:
                start_now = False
                timer = threading.Timer(delay, self._run_scheduled_refill)
                timer.name = "serena-desk-greeting-refill-delay"
                timer.daemon = True
                self._refill_timer = timer
                timer.start()
        if start_now:
            self.start_refill()

    def _run_scheduled_refill(self) -> None:
        with self._lock:
            self._refill_timer = None
        self.start_refill()

    def take(self) -> GreetingAudio | None:
        with self._lock:
            self._discard_expired_locked()
            item = self._items.pop(0) if self._items else None
            if item is not None:
                self._persist_locked()
        self.schedule_refill()
        return item

    def wait(self, timeout: float = 120.0) -> bool:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
        return bool(self.status["ready"])

    def _refill_thread(self) -> None:
        try:
            asyncio.run(self._refill())
        except Exception as exc:
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._refilling = False

    async def _refill(self) -> None:
        status = await self.runtime.warm()
        failed = [value for value in status.values() if value != "ready"]
        if failed:
            raise RuntimeError(f"desk voice models are not ready: {failed[0]}")

        tts = self.tts_factory() if self.tts_factory is not None else self.runtime.tts
        owns_tts = tts is not self.runtime.tts
        try:
            if owns_tts:
                await tts.warm()
            while True:
                with self._lock:
                    self._discard_expired_locked()
                    if len(self._items) >= self.target_size:
                        self._last_error = ""
                        return
                item = await self._generate(tts=tts)
                with self._lock:
                    self._items.append(item)
                    self._persist_locked()
        finally:
            if owns_tts:
                await tts.cancel(None)

    async def _generate(
        self, *, tts: AsyncTTSBackend | None = None
    ) -> GreetingAudio:
        tts = tts or self.runtime.tts
        greeting_id = uuid.uuid4().hex
        request_id = uuid.uuid4().hex
        prompt = (
            f'<desk-wake-greeting id="{greeting_id}">\n'
            'Raghav has just said "hey Serena" at his desk and is listening. '
            "Give one fresh, short spoken greeting in your normal voice. Lead "
            "him into speaking, but do not ask an open-ended question, mention "
            "the time of day, describe the automation, or include stage directions.\n"
            "</desk-wake-greeting>"
        )
        deltas: list[str] = []
        done = ""
        async for event in self.runtime.brain.stream_turn(
            prompt,
            call_id="desk-greeting-pool",
            turn_id=f"desk-greeting:{greeting_id}",
            request_id=request_id,
            journal=False,
        ):
            if event.type == "delta":
                deltas.append(event.delta)
            elif event.type == "done":
                done = event.say
        text = " ".join((done or "".join(deltas)).split()).strip()
        if not text or len(text) > 300:
            raise RuntimeError("resident brain returned an invalid desk greeting")

        generation = secrets.randbits(62) | (1 << 62)
        allow = getattr(self.runtime.tts, "allow_generation", None)
        if callable(allow):
            allow(generation)
        chunks: list[bytes] = []
        sample_rate: int | None = None
        try:
            async for chunk in tts.stream(text, generation=generation):
                if sample_rate is None:
                    sample_rate = chunk.sample_rate
                elif sample_rate != chunk.sample_rate:
                    raise RuntimeError("desk greeting sample rate changed")
                chunks.append(bytes(chunk.pcm))
                if sum(map(len, chunks)) > MAX_GREETING_BYTES:
                    raise RuntimeError("desk greeting audio exceeded the size limit")
        finally:
            retire = getattr(tts, "retire_generation", None)
            if callable(retire):
                retire(generation)
        pcm = b"".join(chunks)
        if sample_rate is None or not pcm or len(pcm) % 2:
            raise RuntimeError("desk TTS returned no valid PCM")
        return GreetingAudio(greeting_id, sample_rate, pcm, text, time.time())

    def _load(self) -> list[GreetingAudio]:
        try:
            import json

            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return []
        if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
            return []
        items = [GreetingAudio.from_json(value) for value in payload.get("items", [])]
        now = time.time()
        return [
            item
            for item in items
            if item is not None and now - item.created_at <= self.max_age_seconds
        ][: self.target_size]

    def _discard_expired_locked(self) -> None:
        cutoff = time.time() - self.max_age_seconds
        retained = [item for item in self._items if item.created_at >= cutoff]
        if len(retained) != len(self._items):
            self._items = retained
            self._persist_locked()

    def _persist_locked(self) -> None:
        write_json_atomic(
            self.path,
            {
                "version": SCHEMA_VERSION,
                "items": [item.to_json() for item in self._items],
            },
        )
