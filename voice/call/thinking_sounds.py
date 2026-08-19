"""Pre-rendered "hmm" clips for the gap before her first real word.

Serena is a cascade (whisper -> brain -> pocket TTS) and cannot copy the
speech-to-speech architectures in knowledge/ai-desktop-assistant/
natural-voice-models-2026.md.  She can only close the perceived gap, and that
note is explicit about how: play ONE short thinking sound, only when the brain
is genuinely slow, varied, never every turn.

The every-turn version already failed once.  Memory 414 records that Raghav
disliked the automatic "yeah" backchannel on every turn, so the clip here is
latency-gated and has a kill switch.  Clips are rendered ahead of time in her
own Pocket voice and read straight off disk, following the mic-off precedent in
voice/desk/io.py: synthesising a filler live would cost TTS latency at exactly
the moment we are trying to hide latency.

When no clip is installed this pool stays silent on purpose.  A generic beep in
the middle of her thinking reads as a fault, which is the same mistake the
mic-off tone made before it was given real words.
"""

from __future__ import annotations

import os
import random
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .protocol import TTS_SAMPLE_RATES

DEFAULT_CLIP_DIR = Path.home() / ".config" / "serena" / "audio"
CLIP_GLOB = "thinking-*.raw"
DEFAULT_SAMPLE_RATE = 24_000
DEFAULT_DELAY_MS = 1_500.0
MAX_DELAY_MS = 5_000.0
# A thinking sound is a breath, not a sentence.  Capping the duration keeps a
# wrong or truncated file from talking over the answer it was meant to cover.
MAX_CLIP_SECONDS = 2.0
ENV_ENABLED = "SERENA_VOICE_THINKING_SOUNDS"
ENV_DELAY_MS = "SERENA_VOICE_THINKING_DELAY_MS"
ENV_CLIP_DIR = "SERENA_VOICE_THINKING_DIR"

_RATE_IN_NAME = re.compile(r"-(?:(\d+)k|(\d+)hz)-")
_FALSEY = {"0", "false", "no", "off"}


@dataclass(frozen=True, slots=True)
class ThinkingClip:
    """One pre-rendered filler, already in her serving voice."""

    clip_id: str
    sample_rate: int
    pcm: bytes


def _clip_sample_rate(path: Path) -> int:
    """Read the rate out of the filename, the way the mic-off clip encodes it."""

    match = _RATE_IN_NAME.search(path.name)
    if match is None:
        return DEFAULT_SAMPLE_RATE
    if match.group(1) is not None:
        return int(match.group(1)) * 1_000
    return int(match.group(2))


def load_clips(directory: Path) -> tuple[ThinkingClip, ...]:
    """Read and validate every installed clip, skipping anything unusable."""

    try:
        paths = sorted(directory.glob(CLIP_GLOB))
    except OSError:
        return ()
    clips: list[ThinkingClip] = []
    for path in paths:
        sample_rate = _clip_sample_rate(path)
        if sample_rate not in TTS_SAMPLE_RATES:
            continue
        try:
            pcm = path.read_bytes()
        except OSError:
            continue
        # Whole PCM16 samples only, and short enough to stay a breath.
        if not pcm or len(pcm) % 2:
            continue
        if len(pcm) > int(sample_rate * 2 * MAX_CLIP_SECONDS):
            continue
        clips.append(ThinkingClip(path.stem, sample_rate, pcm))
    return tuple(clips)


class ThinkingSoundPool:
    """Pick a latency-gated filler clip without repeating the last one."""

    def __init__(
        self,
        *,
        directory: Path | None = None,
        env: Mapping[str, str] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        environ = os.environ if env is None else env
        configured = str(environ.get(ENV_CLIP_DIR, "") or "").strip()
        self.directory = (
            Path(configured).expanduser()
            if configured
            else (DEFAULT_CLIP_DIR if directory is None else directory)
        )
        self.enabled = (
            str(environ.get(ENV_ENABLED, "") or "").strip().casefold() not in _FALSEY
        )
        self.delay_ms = self._read_delay(environ)
        self._rng = rng or random.Random()
        self._clips: tuple[ThinkingClip, ...] | None = None
        self._last_id = ""

    @staticmethod
    def _read_delay(environ: Mapping[str, str]) -> float:
        try:
            delay = float(environ.get(ENV_DELAY_MS, "") or DEFAULT_DELAY_MS)
        except ValueError:
            delay = DEFAULT_DELAY_MS
        return max(0.0, min(delay, MAX_DELAY_MS))

    @property
    def clips(self) -> tuple[ThinkingClip, ...]:
        if self._clips is None:
            self._clips = load_clips(self.directory) if self.enabled else ()
        return self._clips

    def take(self, *, sample_rate: int | None = None) -> ThinkingClip | None:
        """Return the clip to play now, or None to stay silent.

        A clip whose rate does not match the voice already streaming this turn
        is skipped rather than converted.  The worker refuses a rate change
        inside one generation, so a mismatched filler would fail the whole
        reply to save 1.5 seconds of silence.
        """

        if not self.enabled:
            return None
        usable = [
            clip
            for clip in self.clips
            if sample_rate is None or clip.sample_rate == sample_rate
        ]
        if not usable:
            return None
        # Back-to-back repeats are what make a filler sound like a machine.
        fresh = [clip for clip in usable if clip.clip_id != self._last_id] or usable
        chosen = self._rng.choice(fresh)
        self._last_id = chosen.clip_id
        return chosen


# Short on purpose.  These are breaths between his question and her answer,
# not sentences, and the note in knowledge/ai-desktop-assistant is explicit
# that Grok got more human by saying less, not by adding noises.
DEFAULT_PHRASES = ("hmm.", "mm.", "uh...", "hmm, okay.")


def clip_path(directory: Path, name: str, sample_rate: int) -> Path:
    """Name a clip the way voice/desk/io.py names the mic-off render."""

    rate = (
        f"{sample_rate // 1_000}k"
        if sample_rate % 1_000 == 0
        else f"{sample_rate}hz"
    )
    return directory / f"thinking-{name}-{rate}-mono-s16.raw"


async def render_clips(
    phrases: Mapping[str, str],
    *,
    directory: Path = DEFAULT_CLIP_DIR,
    backend=None,
) -> list[Path]:
    """Render each phrase once in her serving voice and store the raw PCM.

    Run this after a voice change, the same way the mic-off clip is refreshed:

        .venv/bin/python3 -m voice.call.thinking_sounds

    A clip rendered by a retired voice is worse than no clip, because the
    filler and the answer would not sound like the same person.
    """

    from .tts import create_tts_backend

    engine = backend if backend is not None else create_tts_backend()
    await engine.warm()
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, phrase in phrases.items():
        pcm = b""
        sample_rate = DEFAULT_SAMPLE_RATE
        async for chunk in engine.stream(phrase, generation=0):
            pcm += chunk.pcm
            sample_rate = chunk.sample_rate
        if not pcm:
            raise RuntimeError(f"thinking clip {name!r} rendered no audio")
        path = clip_path(directory, name, sample_rate)
        path.write_bytes(pcm)
        written.append(path)
    return written


async def ensure_thinking_clips(
    *,
    directory: Path | None = None,
    backend=None,
) -> list[Path]:
    """Provision any missing defaults from the already-selected local voice."""

    if str(os.environ.get(ENV_ENABLED, "") or "").strip().casefold() in _FALSEY:
        return []
    configured = str(os.environ.get(ENV_CLIP_DIR, "") or "").strip()
    target = (
        Path(configured).expanduser()
        if configured
        else (DEFAULT_CLIP_DIR if directory is None else directory)
    )
    from .tts import create_tts_backend

    engine = backend if backend is not None else create_tts_backend()
    await engine.warm()
    sample_rate = int(getattr(engine, "sample_rate", DEFAULT_SAMPLE_RATE))
    if sample_rate not in TTS_SAMPLE_RATES:
        raise RuntimeError(f"thinking clip sample rate {sample_rate} is unsupported")
    installed = {clip.clip_id for clip in load_clips(target)}
    named = {
        re.sub(r"[^a-z0-9]+", "-", phrase.casefold()).strip("-")
        or f"clip{index}": phrase
        for index, phrase in enumerate(DEFAULT_PHRASES, start=1)
    }
    missing = {
        name: phrase
        for name, phrase in named.items()
        if clip_path(target, name, sample_rate).stem not in installed
    }
    if not missing:
        return []
    return await render_clips(missing, directory=target, backend=engine)


def main() -> int:
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=DEFAULT_CLIP_DIR)
    parser.add_argument("phrases", nargs="*", default=list(DEFAULT_PHRASES))
    args = parser.parse_args()
    phrases = {
        re.sub(r"[^a-z0-9]+", "-", phrase.casefold()).strip("-") or f"clip{index}": phrase
        for index, phrase in enumerate(args.phrases or DEFAULT_PHRASES, start=1)
    }
    written = asyncio.run(render_clips(phrases, directory=args.directory))
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
