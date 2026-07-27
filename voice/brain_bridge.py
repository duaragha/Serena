#!/usr/bin/env python3
"""Brain bridge for the Electron voice overlay.

The overlay connects to ws://localhost:8765 and animates its dot field on
`state_change` messages (idle | listening | thinking | speaking | working). This bridge
watches a tiny state file and broadcasts changes, so anything on the machine can
drive the brain just by writing one word to the file:

    ~/.config/serena/voice_state   ->  "idle" | "thinking" | "speaking" | "listening"
    ~/.config/serena/voice_working ->  active background coding lease

The supported call and desk runtimes write the listening, thinking, speaking,
and idle states. The private coding supervisor publishes working leases and
structured activity events. Writes are harmless when the bridge is not running.
"""
import asyncio
import json
import math
import os
import socket
import time
from contextlib import suppress
from pathlib import Path

import websockets

STATE_FILE = Path.home() / ".config" / "serena" / "voice_state"
WORKING_FILE = Path.home() / ".config" / "serena" / "voice_working"
EVENT_SOCKET = Path.home() / ".local" / "state" / "serena" / "brain-events.sock"
VALID = ("idle", "listening", "thinking", "speaking", "working")
HOST, PORT = "127.0.0.1", 8765

clients = set()
state = "idle"


def read_state():
    try:
        s = STATE_FILE.read_text(encoding="utf-8").strip()
        state = s if s in VALID else "idle"
    except Exception:
        state = "idle"
    if state == "idle" and WORKING_FILE.is_file():
        return "working"
    return state


def parse_client_message(raw):
    """Validate a local real-time overlay update from a trusted desk process."""

    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 60_000:
        return None
    local_event = parse_local_event(raw.encode("utf-8"))
    if local_event is not None:
        return local_event
    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(message, dict) and message.get("type") == "typed":
        # The overlay's type bar: same turn a spoken one would make, for when
        # the mic is unusable (bad driver, noisy room, someone nearby).
        if set(message) != {"type", "text"}:
            return None
        text = message.get("text")
        if not isinstance(text, str):
            return None
        text = " ".join(text.split())
        if not text or len(text) > 4_000:
            return None
        return json.dumps(
            {"type": "typed", "text": text}, ensure_ascii=False, separators=(",", ":")
        )
    if not isinstance(message, dict) or set(message) != {"type", "value"}:
        return None
    if message.get("type") != "amplitude":
        return None
    value = message.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return None
    return json.dumps(
        {"type": "amplitude", "value": value}, separators=(",", ":")
    )


def parse_local_event(raw: bytes):
    """Validate one user-local coding event before forwarding it to Electron."""

    if not isinstance(raw, bytes) or not raw or len(raw) > 60_000:
        return None
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(message, dict):
        return None
    kind = message.get("type")
    if kind == "code_start":
        project = message.get("project")
        if not isinstance(project, str) or not project or len(project) > 200:
            return None
        clean = {"type": kind, "project": project}
        status = message.get("status")
        if status is not None:
            if not isinstance(status, str) or not status or len(status) > 200:
                return None
            clean["status"] = status
    elif kind == "code_hide":
        if set(message) != {"type"}:
            return None
        clean = {"type": kind}
    elif kind == "code_done":
        summary = message.get("summary")
        if not isinstance(summary, str) or len(summary) > 2_000:
            return None
        clean = {"type": kind, "summary": summary}
    elif kind == "code_event":
        event = message.get("event")
        if not isinstance(event, dict):
            return None
        allowed = {"kind", "summary", "detail", "filename", "command", "tool_name"}
        if not set(event).issubset(allowed):
            return None
        if event.get("kind") not in {"file_edit", "bash", "text", "tool_call"}:
            return None
        for value in event.values():
            if not isinstance(value, str) or len(value) > 8_000:
                return None
        clean = {"type": kind, "event": event}
    else:
        return None
    return json.dumps(clean, ensure_ascii=False, separators=(",", ":"))


async def handler(ws):
    clients.add(ws)
    try:
        await ws.send(json.dumps({"type": "state_change", "state": state}))
        async for raw in ws:
            message = parse_client_message(raw)
            if message is None:
                continue
            if '"type":"typed"' in message:
                text = json.loads(message)["text"]
                asyncio.create_task(run_typed_turn(text))
                continue
            await broadcast(message, exclude=ws)
    except websockets.ConnectionClosed:
        # Overlay restarts and laptop sleep often end without a close frame.
        # That is a normal client departure, not a bridge failure.
        pass
    finally:
        clients.discard(ws)


_typed_turn: asyncio.Task | None = None


async def _interrupt_typed_turn() -> None:
    """Cut off whatever she is saying so the new message wins.

    Turning a message away with "one sec, still on the last one" is not how
    interrupting someone works, and it lands constantly, because she speaks far
    slower than he types. Talking over her is allowed on the spoken side; it
    should be allowed here too.
    """
    global _typed_turn

    previous = _typed_turn
    if previous is None or previous.done() or previous is asyncio.current_task():
        return
    previous.cancel()
    with suppress(BaseException):
        await previous


async def run_typed_turn(text: str) -> None:
    """Run one typed desk turn: show it, think, speak as she forms it.

    Deliberately the same path a spoken turn takes, including her brokered
    tools, so typing is an input channel and not a second personality. Clauses
    go to the speaker the moment they are complete, because waiting for the
    whole reply makes her sound like a form submission rather than a person.
    """

    global _typed_turn

    from voice.desk.say import set_state, speak_stream, stream_turn

    await _interrupt_typed_turn()
    _typed_turn = asyncio.current_task()
    await broadcast(
        json.dumps({"type": "transcription", "text": text}, ensure_ascii=False)
    )
    call_id = f"desk-typed-{int(time.time())}"
    clauses: asyncio.Queue = asyncio.Queue()
    player = asyncio.create_task(speak_stream(clauses))
    spoke = False

    async def on_sentence(clause: str) -> None:
        nonlocal spoke
        if not spoke:
            spoke = True
            set_state("speaking")
        await clauses.put(clause)

    set_state("thinking")
    try:
        said = await stream_turn(
            text,
            call_id=call_id,
            turn_id=f"{call_id}:1",
            timeout=240.0,
            on_sentence=on_sentence,
        )
    except asyncio.CancelledError:
        player.cancel()
        with suppress(BaseException):
            await player
        raise
    except (OSError, asyncio.TimeoutError) as exc:
        await clauses.put(None)
        with suppress(Exception):
            await player
        set_state("idle")
        await broadcast(
            json.dumps({"type": "response", "text": f"(could not reach the brain: {exc})"})
        )
        return
    await broadcast(
        json.dumps(
            {"type": "response", "text": said or "(silence)"}, ensure_ascii=False
        )
    )
    await clauses.put(None)
    try:
        await player
    except asyncio.CancelledError:
        player.cancel()
        with suppress(BaseException):
            await player
        raise
    except Exception as exc:  # noqa: BLE001 - audio must not break a turn
        print(f"[brain_bridge] typed turn audio failed: {exc}", flush=True)
    set_state("idle")


async def broadcast(msg, *, exclude=None):
    dead = set()
    # A send yields to the event loop, so handlers may add or remove clients
    # before it resumes. Iterate a stable snapshot rather than the live set.
    for ws in tuple(clients):
        if ws is exclude:
            continue
        try:
            await ws.send(msg)
        except Exception:
            dead.add(ws)
    clients.difference_update(dead)


async def watch():
    global state
    while True:
        s = read_state()
        if s != state:
            state = s
            await broadcast(json.dumps({"type": "state_change", "state": state}))
        await asyncio.sleep(0.1)


async def watch_local_events(event_socket):
    loop = asyncio.get_running_loop()
    while True:
        raw = await loop.sock_recv(event_socket, 65_536)
        message = parse_local_event(raw)
        if message is not None:
            await broadcast(message)


def open_event_socket():
    EVENT_SOCKET.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    EVENT_SOCKET.unlink(missing_ok=True)
    event_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    event_socket.setblocking(False)
    event_socket.bind(str(EVENT_SOCKET))
    os.chmod(EVENT_SOCKET, 0o600)
    return event_socket


async def main():
    event_socket = open_event_socket()
    try:
        async with websockets.serve(handler, HOST, PORT):
            print(f"[brain_bridge] ws://{HOST}:{PORT} watching {STATE_FILE}", flush=True)
            await asyncio.gather(watch(), watch_local_events(event_socket))
    finally:
        event_socket.close()
        EVENT_SOCKET.unlink(missing_ok=True)


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main())
