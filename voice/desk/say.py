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
    """Drive the dot field through the same state file the daemon watches."""
    try:
        VOICE_STATE.parent.mkdir(parents=True, exist_ok=True)
        VOICE_STATE.write_text(state, encoding="utf-8")
    except OSError:
        pass


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
            chunks: list[bytes] = []
            rate = 24_000
            async for chunk in backend.stream(clause, generation=0):
                pcm = getattr(chunk, "pcm", chunk)
                rate = getattr(chunk, "sample_rate", rate) or rate
                if pcm:
                    chunks.append(pcm)
            if chunks:
                await _play_pcm(b"".join(chunks), rate)


async def _play_pcm(pcm: bytes, rate: int) -> None:
    import shutil

    player = shutil.which("aplay") or shutil.which("paplay")
    if player is None:
        raise RuntimeError("no local audio player (aplay/paplay) found")
    if player.endswith("paplay"):
        argv = [player, "--raw", "--format=s16le", f"--rate={rate}", "--channels=1"]
    else:
        argv = [player, "-q", "-t", "raw", "-f", "S16_LE", "-r", str(rate), "-c", "1"]
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await process.communicate(pcm)
    if process.returncode != 0:
        raise RuntimeError((err or b"").decode("utf-8", "replace").strip() or "playback failed")


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
    import shutil
    import subprocess

    player = shutil.which("aplay") or shutil.which("paplay")
    if player is None:
        raise RuntimeError("no local audio player (aplay/paplay) found")
    if player.endswith("paplay"):
        argv = [player, "--raw", "--format=s16le", f"--rate={rate}", "--channels=1"]
    else:
        argv = [player, "-q", "-t", "raw", "-f", "S16_LE", "-r", str(rate), "-c", "1"]
    process = subprocess.run(argv, input=b"".join(chunks), capture_output=True)
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
