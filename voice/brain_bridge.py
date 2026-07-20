#!/usr/bin/env python3
"""Brain bridge for the Electron voice overlay.

The overlay connects to ws://localhost:8765 and animates its dot field on
`state_change` messages (idle | listening | thinking | speaking | working). This bridge
watches a tiny state file and broadcasts changes, so anything on the machine can
drive the brain just by writing one word to the file:

    ~/.config/serena/voice_state   ->  "idle" | "thinking" | "speaking" | "listening"
    ~/.config/serena/voice_working ->  active background coding lease

Serena's TTS (kokoro_speak.py) writes "speaking" while she talks and "idle" when
she stops; the UserPromptSubmit hook writes "thinking" while she works. The file
write is harmless whether or not this bridge is running.
"""
import asyncio
import json
import math
import os
import socket
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
            if message is not None:
                await broadcast(message, exclude=ws)
    except websockets.ConnectionClosed:
        # Overlay restarts and laptop sleep often end without a close frame.
        # That is a normal client departure, not a bridge failure.
        pass
    finally:
        clients.discard(ws)


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
