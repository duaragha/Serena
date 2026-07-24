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
import json
import sys
import time
from pathlib import Path

BRAIN_SOCK = Path.home() / ".config" / "serena" / "brain.sock"
VOICE_STATE = Path.home() / ".config" / "serena" / "voice_state"


def _set_state(state: str) -> None:
    """Drive the dot field through the same state file the daemon watches."""
    try:
        VOICE_STATE.parent.mkdir(parents=True, exist_ok=True)
        VOICE_STATE.write_text(state, encoding="utf-8")
    except OSError:
        pass


async def _ask_brain(text: str, *, call_id: str, turn_id: str, timeout: float) -> str:
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


async def _speak(text: str) -> None:
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


async def main_async(text: str, *, speak: bool, timeout: float) -> int:
    if not BRAIN_SOCK.exists():
        print("brain daemon is not running (no brain.sock)", file=sys.stderr)
        return 1
    call_id = f"desk-typed-{int(time.time())}"
    started = time.monotonic()
    _set_state("thinking")
    try:
        said = await _ask_brain(text, call_id=call_id, turn_id=f"{call_id}:1", timeout=timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        _set_state("idle")
        print(f"could not reach the brain: {exc}", file=sys.stderr)
        return 1
    elapsed = time.monotonic() - started
    print(f"\n  you:    {text}")
    print(f"  serena: {said or '(silence)'}")
    print(f"  ({elapsed:.1f}s)\n")
    if speak and said:
        _set_state("speaking")
        try:
            await _speak(said)
        except Exception as exc:  # noqa: BLE001 - never fail the turn on audio
            print(f"(could not play audio: {exc})", file=sys.stderr)
    _set_state("idle")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="+", help="what you would have said")
    parser.add_argument("--quiet", action="store_true", help="text only, no audio")
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args(argv)
    return asyncio.run(
        main_async(" ".join(args.text), speak=not args.quiet, timeout=args.timeout)
    )


if __name__ == "__main__":
    raise SystemExit(main())
