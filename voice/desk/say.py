"""Type to Serena and hear her answer on this machine.

For when Raghav cannot speak out loud (at work, someone in the room) but is
sitting at the laptop and wants the real thing: her actual brain, her actual
voice out of these speakers, and the dot field reacting as it would on a
spoken turn. Text goes in, everything downstream is the normal path.

    .venv/bin/python3 -m voice.desk.say "can you fix the phev tracker"

This is a desk-local input surface, not a bypass. The turn is tagged as a
spoken desk turn because that is what it is, Raghav present at the machine
addressing her directly, so her brokered tools behave exactly as they do
when he talks. Nothing here can start work by itself; only she can, and the
capability broker still re-reads these words.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path

BRAIN_SOCK = Path.home() / ".config" / "serena" / "brain.sock"
VOICE_STATE = Path.home() / ".config" / "serena" / "voice_state"


def set_state(state: str) -> None:
    """Drive the dot field through the same state file the daemon watches.

    Written the way the desk client writes it (whole line, atomic replace) so
    a reader never catches a half-written word and both writers agree on the
    file's shape.
    """
    import os
    import uuid

    temporary = None
    try:
        VOICE_STATE.parent.mkdir(parents=True, exist_ok=True)
        temporary = VOICE_STATE.parent / f".{VOICE_STATE.name}.{uuid.uuid4().hex}.tmp"
        temporary.write_text(state + "\n", encoding="utf-8")
        os.replace(temporary, VOICE_STATE)
    except OSError:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


async def ask_brain(text: str, *, call_id: str, turn_id: str, timeout: float) -> str:
    reader, writer = await asyncio.open_unix_connection(str(BRAIN_SOCK))
    request = {
        "type": "turn",
        "request_id": f"{call_id}-{turn_id}",
        "protocol": "voice",
        "text": text,
        "stream": True,
        "call_id": call_id,
        "turn_id": turn_id,
    }
    writer.write((json.dumps(request) + "\n").encode("utf-8"))
    await writer.drain()
    said = ""
    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not line:
                break
            event = json.loads(line)
            kind = event.get("type")
            if kind == "response.done":
                said = event.get("say") or ""
                break
            if kind == "error":
                said = f"(brain error: {event.get('error')})"
                break
    finally:
        writer.close()
    return said.strip()


_tts_backend = None
_tts_lock: asyncio.Lock | None = None
_generation = 0


def _next_generation() -> int:
    """One generation per clause, never reused.

    Pocket tracks cancellation per generation. Sending every clause of every
    turn as generation 0 meant an interrupt aimed at one sentence could only
    be expressed as "cancel generation 0", which is every sentence she will
    ever say on this process, so cancelling correctly was impossible.
    """
    global _generation

    _generation += 1
    return _generation


def _tts_environment() -> None:
    """Match serena-mobile-host.service so the local engine actually loads."""
    import os

    os.environ.setdefault("SERENA_CALL_TTS_BACKEND", "pocket")
    pocket_python = Path.home() / "Documents/Projects/serena/.venv-pocket/bin/python"
    if pocket_python.exists():
        os.environ.setdefault("SERENA_CALL_POCKET_PYTHON", str(pocket_python))
    os.environ.setdefault("SERENA_CALL_POCKET_VOICE", "anna")
    os.environ.setdefault("HF_HOME", str(Path.home() / ".cache/serena/pocket-tts"))


async def _backend():
    """One warm backend for the process. Spawning a worker per clause would
    cost more than the streaming buys."""
    global _tts_backend
    if _tts_backend is None:
        _tts_environment()
        from voice.call.tts import create_tts_backend

        _tts_backend = create_tts_backend()
    return _tts_backend


async def warm_backend() -> object:
    """Pay Pocket's cold start before anyone is waiting on an answer.

    The engine is a sandboxed child process that loads a model and primes
    itself; measured cold, the first PCM arrives about seven seconds after
    the first clause is handed over, and warm it arrives in about a tenth of
    a second. Creating the backend lazily on the first clause charged that
    seven seconds to the first thing Raghav typed after a restart: her reply
    finished appearing on screen before she had made a sound. Warming at
    startup moves the wait to a moment nobody is listening.
    """
    backend = await _backend()
    warm = getattr(backend, "warm", None)
    if warm is not None:
        await warm()
    return backend


async def speak_stream(sentences: "asyncio.Queue[str | None]") -> None:
    """Play clauses in order as they are produced, never overlapping."""
    global _tts_lock
    if _tts_lock is None:
        _tts_lock = asyncio.Lock()
    async with _tts_lock:
        backend = await _backend()
        while True:
            clause = await sentences.get()
            if clause is None:
                return
            await speak_clause(backend, clause)


async def speak_clause(backend, clause: str) -> None:
    """Say one clause, starting playback on the first chunk of audio.

    Collecting a whole clause before opening the speaker wasted the entire
    synthesis time as silence, twice: once before her first word and again in
    the gap between clauses, which is what made her sound like she had given
    up halfway through a sentence.
    """
    generation = _next_generation()
    allow = getattr(backend, "allow_generation", None)
    if allow is not None:
        allow(generation)
    player: _StreamingPlayer | None = None
    rate = 24_000
    try:
        try:
            async for chunk in backend.stream(clause, generation=generation):
                pcm = getattr(chunk, "pcm", chunk)
                rate = getattr(chunk, "sample_rate", rate) or rate
                if not pcm:
                    continue
                if player is None:
                    player = await _StreamingPlayer.open(rate)
                await player.feed(pcm)
            if player is not None:
                # close() waits for buffered audio to finish, and a
                # cancellation landing in that wait must still kill the
                # speaker, or the old clause talks over the reply that
                # interrupted it. That is why close() sits INSIDE this
                # handler's reach.
                await player.close()
        except asyncio.CancelledError:
            # Interrupted mid-sentence. The speaker is a separate process and
            # the engine is a separate process; both keep going over the next
            # turn unless they are told to stop here.
            if player is not None:
                await player.kill()
            cancel = getattr(backend, "cancel", None)
            if cancel is not None:
                with contextlib.suppress(Exception):
                    await cancel(generation)
            raise
        if player is None:
            # Never swallow a clause. Silence that nobody logs is exactly the
            # bug that looks like her trailing off mid-sentence.
            print(f"[say] no audio came back for clause: {clause!r}", flush=True)
            return
    finally:
        retire = getattr(backend, "retire_generation", None)
        if retire is not None:
            retire(generation)


class _StreamingPlayer:
    """A speaker process fed while she is still being synthesised."""

    def __init__(self, process) -> None:
        self._process = process

    @staticmethod
    def argv(rate: int) -> list[str]:
        import shutil

        player = shutil.which("aplay") or shutil.which("paplay")
        if player is None:
            raise RuntimeError("no local audio player (aplay/paplay) found")
        if player.endswith("paplay"):
            return [player, "--raw", "--format=s16le", f"--rate={rate}", "--channels=1"]
        return [player, "-q", "-t", "raw", "-f", "S16_LE", "-r", str(rate), "-c", "1"]

    @classmethod
    async def open(cls, rate: int) -> "_StreamingPlayer":
        process = await asyncio.create_subprocess_exec(
            *cls.argv(rate),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        return cls(process)

    async def feed(self, pcm: bytes) -> None:
        try:
            self._process.stdin.write(pcm)
            # Draining is the pacing: the speaker only accepts audio as fast
            # as it plays it, which keeps the engine one buffer ahead instead
            # of an entire clause ahead.
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise RuntimeError(await self._failure()) from exc

    async def close(self) -> None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self._process.stdin.close()
            await self._process.stdin.wait_closed()
        # Waiting here is what makes "the player finished" mean "she stopped
        # talking", which is the only honest moment to go idle.
        await self._process.wait()
        if self._process.returncode != 0:
            raise RuntimeError(await self._failure())

    async def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            self._process.kill()
        with contextlib.suppress(Exception):
            await self._process.wait()

    async def _failure(self) -> str:
        err = b""
        if self._process.stderr is not None:
            with contextlib.suppress(Exception):
                err = await self._process.stderr.read()
        return err.decode("utf-8", "replace").strip() or "playback failed"


async def stream_turn(
    text: str,
    *,
    call_id: str,
    turn_id: str,
    timeout: float,
    on_sentence=None,
) -> str:
    """One turn, speaking each clause the moment it is complete.

    Waiting for the whole reply before making a sound is what makes her feel
    like a form submission instead of someone talking, so the brain's token
    deltas feed the same incremental splitter the call pipeline uses and each
    finished clause goes straight out.
    """
    from voice.call.sentences import IncrementalSentenceSplitter

    splitter = IncrementalSentenceSplitter(first_clause_chars=12, first_clause_hard_chars=56)
    reader, writer = await asyncio.open_unix_connection(str(BRAIN_SOCK))
    request = {
        "type": "turn",
        "request_id": f"{call_id}-{turn_id}",
        "protocol": "voice",
        "text": text,
        "stream": True,
        "call_id": call_id,
        "turn_id": turn_id,
    }
    writer.write((json.dumps(request) + "\n").encode("utf-8"))
    await writer.drain()
    parts: list[str] = []
    said = ""
    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not line:
                break
            event = json.loads(line)
            kind = event.get("type")
            if kind == "response.delta":
                delta = event.get("delta") or ""
                if delta:
                    parts.append(delta)
                    if on_sentence is not None:
                        for clause in splitter.feed(delta):
                            await on_sentence(clause)
            elif kind == "response.done":
                said = (event.get("say") or "").strip() or "".join(parts).strip()
                if on_sentence is not None:
                    for clause in splitter.flush():
                        await on_sentence(clause)
                break
            elif kind == "error":
                said = f"(brain error: {event.get('error')})"
                break
    finally:
        writer.close()
    return said.strip()


async def speak(text: str) -> None:
    """Say it out loud through the same local TTS the voice loop uses.

    Default to the backend the running services actually use (pocket, see
    serena-mobile-host.service). The kokoro default in create_tts_backend
    refuses to load outside its attested parent, so falling back to the
    library default here just fails with a hash mismatch.
    """
    import os

    os.environ.setdefault("SERENA_CALL_TTS_BACKEND", "pocket")
    # Match serena-mobile-host.service: pocket_tts lives in its own venv and
    # the model cache is pinned, so borrow the same interpreter and cache
    # instead of failing on a missing module in the main venv.
    pocket_python = Path.home() / "Documents/Projects/serena/.venv-pocket/bin/python"
    if pocket_python.exists():
        os.environ.setdefault("SERENA_CALL_POCKET_PYTHON", str(pocket_python))
    os.environ.setdefault("SERENA_CALL_POCKET_VOICE", "anna")
    os.environ.setdefault("HF_HOME", str(Path.home() / ".cache/serena/pocket-tts"))
    from voice.call.tts import create_tts_backend

    backend = create_tts_backend()
    chunks: list[bytes] = []
    rate = 24_000
    async for chunk in backend.stream(text, generation=0):
        pcm = getattr(chunk, "pcm", chunk)
        rate = getattr(chunk, "sample_rate", rate) or rate
        if pcm:
            chunks.append(pcm)
    if not chunks:
        return
    # Play through ALSA rather than a python binding: sounddevice only exists
    # in the wake venv, and this runs from the main one.
    import subprocess

    process = subprocess.run(
        _StreamingPlayer.argv(rate), input=b"".join(chunks), capture_output=True
    )
    if process.returncode != 0:
        raise RuntimeError(
            (process.stderr or b"").decode("utf-8", "replace").strip() or "playback failed"
        )


async def main_async(text: str, *, play_audio: bool, timeout: float) -> int:
    if not BRAIN_SOCK.exists():
        print("brain daemon is not running (no brain.sock)", file=sys.stderr)
        return 1
    call_id = f"desk-typed-{int(time.time())}"
    started = time.monotonic()
    set_state("thinking")
    clauses: asyncio.Queue = asyncio.Queue()
    player = asyncio.create_task(speak_stream(clauses)) if play_audio else None
    spoke = False

    async def on_sentence(clause: str) -> None:
        nonlocal spoke
        if not spoke:
            spoke = True
            set_state("speaking")
        await clauses.put(clause)

    try:
        said = await stream_turn(
            text,
            call_id=call_id,
            turn_id=f"{call_id}:1",
            timeout=timeout,
            on_sentence=on_sentence if play_audio else None,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        if player is not None:
            await clauses.put(None)
            with contextlib.suppress(Exception):
                await player
        set_state("idle")
        print(f"could not reach the brain: {exc}", file=sys.stderr)
        return 1
    elapsed = time.monotonic() - started
    print(f"\n  you:    {text}")
    print(f"  serena: {said or '(silence)'}")
    print(f"  ({elapsed:.1f}s)\n")
    if player is not None:
        await clauses.put(None)
        try:
            await player
        except Exception as exc:  # noqa: BLE001 - never fail the turn on audio
            print(f"(could not play audio: {exc})", file=sys.stderr)
    set_state("idle")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="+", help="what you would have said")
    parser.add_argument("--quiet", action="store_true", help="text only, no audio")
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args(argv)
    return asyncio.run(
        main_async(" ".join(args.text), play_audio=not args.quiet, timeout=args.timeout)
    )


if __name__ == "__main__":
    raise SystemExit(main())
