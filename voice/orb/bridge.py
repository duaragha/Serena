#!/usr/bin/env python3
"""Serve the orb page and run a whole spoken turn behind it.

He talks, Scribe v2 realtime transcribes, the resident brain answers, and
ElevenLabs speaks it back -- the orb follows all three.

The browser never sees the API key. It opens a websocket to this process, sends
raw PCM16 at 16 kHz, and gets transcripts back; the key stays in
``~/.config/serena/elevenlabs.env`` where it already lives. That is not
ceremony: this page sits in the synced Projects tree, so a key pasted into the
HTML would ride Syncthing to the PC and into every backup.

It reuses ``voice.call.stt._ScribeSession`` rather than reimplementing the
protocol, so the demo and the real call path cannot drift apart -- same URL,
same keyterms, same manual commit strategy.

One session per utterance, because a committed session's sender thread exits by
design. Silence detection happens in the browser, which is the only place that
knows when he actually stopped talking.

    python3 bridge.py [port]    # standalone; Electron passes its own port
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import sys
import threading
import urllib.parse
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _repo_root() -> Path:
    for candidate in (Path.home() / "Documents" / "Projects" / "serena",
                      Path.home() / "Projects" / "serena"):
        if (candidate / "voice" / "call" / "stt.py").is_file():
            return candidate
    raise SystemExit("cannot find the serena checkout next to this script")


sys.path.insert(0, str(_repo_root()))

from flask import Flask, send_from_directory  # noqa: E402
from flask_sock import Sock  # noqa: E402

from voice.call.brain import BrainClient  # noqa: E402
from voice.call.protocol import MIC_SAMPLE_RATE  # noqa: E402
from voice.call.sentences import IncrementalSentenceSplitter  # noqa: E402
from voice.call.spoken_text import prepare_spoken_text  # noqa: E402
from voice.call.stt import (  # noqa: E402
    SCRIBE_MODEL,
    SCRIBE_REALTIME_URL,
    _ScribeSession,
    load_elevenlabs_key,
    load_keyterms,
)
from voice.call.tts import create_tts_backend  # noqa: E402

COMMIT_TIMEOUT_SECONDS = 1.5
POLL_SECONDS = 0.10

_tts = None
_tts_lock = threading.Lock()


def tts_backend():
    """Built once; constructing it spins up model processes."""

    global _tts
    with _tts_lock:
        if _tts is None:
            _tts = create_tts_backend()
        return _tts


def wav_bytes(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buf.getvalue()


# A follow-up heard after she has answered may not be meant for her at all --
# the first wake-mode orb sat in the room answering his phone call out loud.
# She is told so, and an empty reply from her closes the orb.
SILENT = "SILENT"
FOLLOW_UP_NOTE = (
    "[Heard in the few seconds after you answered him. If it could be meant for "
    "you -- a question, a request, a reply to what you just said -- answer "
    "normally. Only if it is clearly him talking to someone else, reply with "
    f"exactly {SILENT} and nothing else.]\n"
)


def _is_silent(reply: str) -> bool:
    """Her 'that was not for me'. Asked for as one word; tolerated as a phrase,
    because the first try came back as '*(no response -- not directed at me)*',
    which would otherwise have been read out loud."""

    head = reply.strip().strip("*_()[] ").lower()
    return (head.startswith(SILENT.lower()) or head.startswith("no response")
            or "not directed at me" in head[:80])


async def _answer(send, text: str, turn: int, *, follow_up: bool = False) -> None:
    """Brain, then her voice, one sentence at a time.

    Sentences are synthesized and shipped as they close rather than after the
    whole reply lands, so the first audio arrives while she is still writing
    the rest. Buffering the lot is what made her openings sit in silence.
    """

    brain = BrainClient()
    splitter = IncrementalSentenceSplitter()
    backend = tts_backend()
    reply: list[str] = []
    index = 0

    async def speak(sentence: str) -> None:
        nonlocal index
        spoken = prepare_spoken_text(sentence).strip()
        if not spoken:
            return
        pcm = bytearray()
        rate = 24_000
        async for chunk in backend.stream(spoken, generation=turn):
            pcm.extend(chunk.pcm)
            rate = getattr(chunk, "sample_rate", rate) or rate
        if not pcm:
            return
        index += 1
        publish_state("speaking")
        send({
            "type": "audio",
            "seq": index,
            "text": sentence,
            "wav": base64.b64encode(wav_bytes(bytes(pcm), rate)).decode("ascii"),
        })

    async def emit(piece: str) -> None:
        reply.append(piece)
        send({"type": "reply", "text": "".join(reply)})
        for sentence in splitter.feed(piece):
            await speak(sentence)

    # A follow-up is held back until enough of it is in to tell whether it is
    # her "not for me", so that answer is never spoken or even shown.
    held = ""
    decided = not follow_up
    silent = False
    try:
        async for event in brain.stream_turn(
                (FOLLOW_UP_NOTE + text) if follow_up else text,
                call_id="orb-demo", turn_id=str(turn)):
            piece = event.delta if event.type == "delta" else (
                (event.say or "") if event.type == "done" else "")
            if not piece or silent:
                continue
            if decided:
                await emit(piece)
                continue
            held += piece
            if _is_silent(held):
                silent = True
            elif len(held.strip()) >= 24 or held.rstrip().endswith((".", "?", "!")):
                decided = True
                await emit(held)
        if not decided and not silent and held.strip():
            if _is_silent(held):
                silent = True
            else:
                await emit(held)
        if not silent:
            for sentence in splitter.flush():
                await speak(sentence)
    except Exception as exc:  # the demo says so rather than going quiet
        send({"type": "error", "message": f"brain: {exc}"})
    finally:
        publish_state("idle")
        send({"type": "speech_end"})


class UtteranceSessions:
    """One Scribe session per utterance, opened when the utterance starts.

    Measured 2026-09-19: a realtime session left idle for ten seconds is dead.
    feed() drops into it silently and commit() returns "", which reaches the
    surface as an empty transcript and therefore as no reply at all. Opening
    the next session the moment the previous turn committed meant it spent her
    entire answer going stale, so the second thing he said was fed to a corpse.

    Nothing is opened until his voice arrives, and a session that has failed is
    replaced rather than reused.
    """

    # A refused connection used to be retried on the very next audio frame,
    # every 64ms, for as long as he kept making sound -- which is itself the
    # quickest way to get refused again, and put the same error on screen
    # sixteen times a second. Failures now back off.
    RETRY_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0)

    def __init__(self, factory, *, clock=None) -> None:
        import time as _time

        self._factory = factory
        self._session = None
        self._lock = threading.Lock()
        self._clock = clock or _time.monotonic
        self._failures = 0
        self._retry_at = 0.0
        self.last_error = ""

    @property
    def current(self):
        return self._session

    def for_audio(self):
        """The session this audio belongs to, opening one if needed."""

        with self._lock:
            session = self._session
            if session is not None and not session.failed:
                return session
            if session is not None:
                session.close()
                self._session = None
            if self._clock() < self._retry_at:
                return None
            self._session = self._factory()
            if self._session is None:
                wait = self.RETRY_BACKOFF_SECONDS[
                    min(self._failures, len(self.RETRY_BACKOFF_SECONDS) - 1)]
                self._failures += 1
                self._retry_at = self._clock() + wait
            else:
                self._failures = 0
            return self._session

    @property
    def failing(self) -> bool:
        """True from a refused connection until one succeeds again."""

        return self._failures > 0

    def commit(self, timeout: float) -> str:
        """Close the utterance and hand back its text. Opens nothing."""

        with self._lock:
            session, self._session = self._session, None
        if session is None:
            return ""
        try:
            return session.commit(timeout)
        finally:
            session.close()

    def close(self) -> None:
        with self._lock:
            session, self._session = self._session, None
        if session is not None:
            session.close()


VOICE_STATE = Path.home() / ".config" / "serena" / "voice_state"


def publish_state(state: str) -> None:
    """Announce what the orb is doing, for anything else that speaks.

    ~/.config/serena/voice_state is the shared "is she busy" flag. The brain
    bridge waits on it before announcing a Fleet run out loud, and the orb
    never wrote it -- so the bridge read "idle" all through a conversation and
    talked straight over her. Two voices at once, one of them the fallback.
    """

    with contextlib.suppress(OSError):
        VOICE_STATE.parent.mkdir(parents=True, exist_ok=True)
        VOICE_STATE.write_text(state, encoding="utf-8")


app = Flask(__name__)
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 20}
sock = Sock(app)


def scribe_url() -> str:
    """The same query the call path builds, keyterms included.

    keyterms is what stops "Kamakshi" coming back as "come action", so the
    demo gets his vocabulary too rather than a generic decoder.
    """

    query = [
        ("model_id", SCRIBE_MODEL),
        ("audio_format", f"pcm_{MIC_SAMPLE_RATE}"),
        ("language_code", "en"),
        ("commit_strategy", "manual"),
    ]
    query.extend(("keyterms", term) for term in load_keyterms()[:100])
    return f"{SCRIBE_REALTIME_URL}?{urllib.parse.urlencode(query)}"


@app.get("/")
def index():
    return send_from_directory(HERE / "renderer", "index.html")


@app.get("/<path:name>")
def asset(name: str):
    return send_from_directory(HERE / "renderer", name)


@sock.route("/ws/mic")
def ws_mic(ws) -> None:
    key = load_elevenlabs_key()
    if not key:
        ws.send(json.dumps({"type": "error",
                            "message": "no ELEVENLABS_API_KEY on this machine"}))
        return

    url = scribe_url()
    headers = [f"xi-api-key: {key}"]
    turns = {"n": 0}
    send_lock = threading.Lock()
    stop = threading.Event()

    def open_session() -> _ScribeSession | None:
        session = _ScribeSession(url, headers, MIC_SAMPLE_RATE)
        if not session.start():
            why = getattr(session, "error", "") or "unknown"
            sessions.last_error = why
            print(f"[bridge] scribe connect failed: {why}", flush=True)
            return None
        return session

    sessions = UtteranceSessions(open_session)

    ws.send(json.dumps({"type": "ready", "model": SCRIBE_MODEL,
                        "rate": MIC_SAMPLE_RATE, "keyterms": len(load_keyterms())}))

    def pump() -> None:
        """Push transcripts as they change, so partials land while he talks."""

        last = ("", "")
        while not stop.is_set():
            session = sessions.current
            if session is not None:
                now = (session.partial, session.committed)
                if now != last:
                    last = now
                    try:
                        with send_lock:
                            ws.send(json.dumps({"type": "transcript",
                                                "partial": now[0], "committed": now[1]}))
                    except Exception:
                        return
            stop.wait(POLL_SECONDS)

    threading.Thread(target=pump, name="orb-transcript", daemon=True).start()

    try:
        while True:
            message = ws.receive()
            if message is None:
                break
            if isinstance(message, (bytes, bytearray)):
                if sessions.current is None:
                    publish_state("listening")
                was_failing = sessions.failing
                session = sessions.for_audio()
                if session is None:
                    # Once per failure, not once per frame.
                    if sessions.failing and not was_failing:
                        with send_lock:
                            ws.send(json.dumps({"type": "error",
                                                "message": "Scribe would not connect"}))
                    continue
                if was_failing:
                    with send_lock:
                        ws.send(json.dumps({"type": "ready"}))
                session.feed(bytes(message))
                continue
            try:
                payload = json.loads(message)
            except (TypeError, ValueError):
                continue
            if payload.get("type") != "commit":
                continue
            # End of utterance. Committing closes this session; the next one
            # is not opened until he speaks again.
            text = sessions.commit(COMMIT_TIMEOUT_SECONDS)
            with send_lock:
                ws.send(json.dumps({"type": "final", "text": text}))

            if not text.strip():
                # Nothing usable came back. Silence here reads as her ignoring
                # him, so the surface is told rather than left guessing.
                publish_state("idle")
                with send_lock:
                    ws.send(json.dumps({"type": "unheard"}))
            if text.strip():
                turns["n"] += 1
                publish_state("thinking")
                ws.send(json.dumps({"type": "thinking"}))
                # Its own thread: the receive loop must stay free so he can
                # interrupt, and so the next utterance is not blocked behind
                # her answer.
                follow_up = bool(payload.get("follow_up"))

                def run(payload: str = text, turn: int = turns["n"],
                        follow_up: bool = follow_up) -> None:
                    def send(event: dict) -> None:
                        with send_lock, contextlib.suppress(Exception):
                            ws.send(json.dumps(event))
                    asyncio.run(_answer(send, payload, turn, follow_up=follow_up))
                threading.Thread(target=run, name="orb-answer", daemon=True).start()

            # Deliberately not opening the next one here: it would go stale
            # while she answers. The next frame of his voice opens it.
    finally:
        stop.set()
        sessions.close()
        publish_state("idle")


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8908
    if not load_elevenlabs_key():
        print("warning: no ELEVENLABS_API_KEY found; the mic will not transcribe",
              file=sys.stderr)
    print(f"orb + scribe on http://127.0.0.1:{port}/", flush=True)
    # 127.0.0.1 is a secure context, which getUserMedia requires; file:// is not.
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
