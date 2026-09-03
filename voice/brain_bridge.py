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
import errno
import json
import math
import os
import socket
import time
import uuid
from contextlib import suppress
from pathlib import Path

import websockets

from core.image_input import MAX_IMAGE_WIRE_BYTES, clean_image_input

STATE_FILE = Path.home() / ".config" / "serena" / "voice_state"
WORKING_FILE = Path.home() / ".config" / "serena" / "voice_working"
EVENT_SOCKET = Path.home() / ".local" / "state" / "serena" / "brain-events.sock"
VALID = ("idle", "listening", "thinking", "speaking", "working")
HOST, PORT = "127.0.0.1", 8765

clients = set()
state = "idle"


def _skip_json_value(raw: str, start: int) -> int | None:
    """Find the end of one JSON value without materialising a large image."""

    length = len(raw)
    if start >= length:
        return None
    first = raw[start]
    if first == '"':
        index = start + 1
        while index < length:
            char = raw[index]
            if char == "\\":
                index += 2
                continue
            if char == '"':
                return index + 1
            index += 1
        return None
    if first in "[{":
        closing = {"[": "]", "{": "}"}
        stack = [closing[first]]
        index = start + 1
        while index < length:
            char = raw[index]
            if char == '"':
                end = _skip_json_value(raw, index)
                if end is None:
                    return None
                index = end
                continue
            if char in "[{":
                stack.append(closing[char])
            elif char in "]}":
                if not stack or char != stack.pop():
                    return None
                if not stack:
                    return index + 1
            index += 1
        return None
    index = start
    while index < length and raw[index] not in ",}":
        index += 1
    return index


def _top_level_message_type(raw: str) -> str | None:
    """Read a top-level type independent of key order and before full parsing."""

    decoder = json.JSONDecoder()
    length = len(raw)
    index = 0
    while index < length and raw[index].isspace():
        index += 1
    if index >= length or raw[index] != "{":
        return None
    index += 1
    while index < length:
        while index < length and raw[index].isspace():
            index += 1
        if index < length and raw[index] == "}":
            return None
        try:
            key, key_end = decoder.raw_decode(raw, index)
        except (TypeError, ValueError):
            return None
        if not isinstance(key, str) or len(key) > 100:
            return None
        index = key_end
        while index < length and raw[index].isspace():
            index += 1
        if index >= length or raw[index] != ":":
            return None
        index += 1
        while index < length and raw[index].isspace():
            index += 1
        value_end = _skip_json_value(raw, index)
        if value_end is None:
            return None
        if key == "type":
            try:
                value, parsed_end = decoder.raw_decode(raw, index)
            except (TypeError, ValueError):
                return None
            if parsed_end != value_end:
                return None
            return value if isinstance(value, str) else None
        index = value_end
        while index < length and raw[index].isspace():
            index += 1
        if index >= length or raw[index] != ",":
            return None
        index += 1


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

    if not isinstance(raw, str):
        return None
    raw_bytes = raw.encode("utf-8")
    # Only the overlay's typed-image envelope may use the larger frame. Keep
    # the cheap 60 KB rejection ahead of json.loads for every other message.
    message_type = _top_level_message_type(raw)
    wire_limit = MAX_IMAGE_WIRE_BYTES if message_type == "typed" else 60_000
    if len(raw_bytes) > wire_limit:
        return None
    local_event = parse_local_event(raw_bytes)
    if local_event is not None:
        return local_event
    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(message, dict) and message.get("type") == "typed":
        # The overlay's type bar: same turn a spoken one would make, for when
        # the mic is unusable (bad driver, noisy room, someone nearby).
        if not set(message).issubset({"type", "text", "image"}) or "text" not in message:
            return None
        text = message.get("text")
        if not isinstance(text, str):
            return None
        text = " ".join(text.split())
        if len(text) > 4_000:
            return None
        clean = {"type": "typed", "text": text}
        if message.get("image") is not None:
            try:
                clean["image"] = clean_image_input(message["image"])
            except ValueError:
                return None
        if not text and "image" not in clean:
            return None
        return json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
    if isinstance(message, dict) and message.get("type") == "code_control":
        action = str(message.get("action") or "").casefold()
        item_id = str(message.get("item_id") or "").strip()
        allowed = {"type", "item_id", "action"}
        if action == "steer":
            allowed.add("text")
        if set(message) != allowed or action not in {"status", "cancel", "steer", "resume"}:
            return None
        if not item_id or len(item_id) > 100 or not all(
            char.isalnum() or char in "-_." for char in item_id
        ):
            return None
        clean = {"type": "code_control", "item_id": item_id, "action": action}
        if action == "steer":
            text = str(message.get("text") or "").strip()
            if not text or len(text) > 4_000:
                return None
            clean["text"] = text
        return json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
    if isinstance(message, dict) and message.get("type") in {
        "transcription",
        "response",
    }:
        # Desk voice reaches this bridge through the same user-local socket as
        # amplitude updates. Keep that feed deliberately text-only: broker
        # metadata, tool traffic, and the raw desk control payload never reach
        # the renderer.
        if set(message) != {"type", "text"}:
            return None
        kind = message["type"]
        text = message.get("text")
        if not isinstance(text, str):
            return None
        text = text.strip()
        limit = 4_000 if kind == "transcription" else 32_000
        if not text or len(text) > limit:
            return None
        return json.dumps(
            {"type": kind, "text": text}, ensure_ascii=False, separators=(",", ":")
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
        if not set(message).issubset({"type", "project", "status", "item_id", "snapshot"}):
            return None
        project = message.get("project")
        if not isinstance(project, str) or not project or len(project) > 200:
            return None
        clean = {"type": kind, "project": project}
        item_id = message.get("item_id")
        if item_id is not None:
            if not isinstance(item_id, str) or not item_id or len(item_id) > 100:
                return None
            clean["item_id"] = item_id
        status = message.get("status")
        if status is not None:
            if not isinstance(status, str) or not status or len(status) > 200:
                return None
            clean["status"] = status
        if "snapshot" in message:
            snapshot = _clean_job_snapshot(message.get("snapshot"))
            if snapshot is not None:
                clean["snapshot"] = snapshot
            elif not _job_snapshot_oversized(message.get("snapshot")):
                return None
    elif kind == "code_hide":
        if set(message) != {"type"}:
            return None
        clean = {"type": kind}
    elif kind == "code_done":
        if not set(message).issubset({"type", "summary", "snapshot"}):
            return None
        summary = message.get("summary")
        if not isinstance(summary, str) or len(summary) > 2_000:
            return None
        clean = {"type": kind, "summary": summary}
        if "snapshot" in message:
            snapshot = _clean_job_snapshot(message.get("snapshot"))
            if snapshot is not None:
                clean["snapshot"] = snapshot
            elif not _job_snapshot_oversized(message.get("snapshot")):
                return None
    elif kind == "code_snapshot":
        if set(message) != {"type", "snapshot"}:
            return None
        snapshot = _clean_job_snapshot(message.get("snapshot"))
        if snapshot is None:
            return None
        clean = {"type": kind, "snapshot": snapshot}
    elif kind == "fleet_notice":
        if set(message) != {"type", "run_id", "state", "token", "text"}:
            return None
        run_id = message.get("run_id")
        notice_state = message.get("state")
        token = message.get("token")
        text = message.get("text")
        if (
            not isinstance(run_id, str)
            or not run_id
            or len(run_id) > 100
            or not all(char.isalnum() or char == "-" for char in run_id)
            or notice_state not in {"completed", "failed"}
            or not isinstance(token, str)
            or not token
            or len(token) > 160
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > 600
        ):
            return None
        clean = {
            "type": kind,
            "run_id": run_id,
            "state": notice_state,
            "token": token,
            "text": " ".join(text.split()),
        }
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
        item_id = message.get("item_id")
        if item_id is not None:
            if (
                not isinstance(item_id, str)
                or not item_id
                or len(item_id) > 100
                or not all(char.isalnum() or char == "-" for char in item_id)
            ):
                return None
            clean["item_id"] = item_id
    else:
        return None
    return json.dumps(clean, ensure_ascii=False, separators=(",", ":"))


def _clean_job_snapshot(value):
    """Validate the bounded durable job projection sent to Electron."""

    if not isinstance(value, dict):
        return None
    allowed = {
        "item_id",
        "state",
        "project",
        "project_root",
        "brief",
        "model",
        "progress",
        "changes",
        "tests",
        "live_proof",
        "evidence",
        "review",
        "controls",
        "summary",
        "changes_truncated",
    }
    if not set(value).issubset(allowed):
        return None
    for required in ("item_id", "state", "project"):
        if not isinstance(value.get(required), str) or not value.get(required):
            return None
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    if len(encoded.encode("utf-8")) > 55_000:
        return None
    return json.loads(encoded)


def _job_snapshot_oversized(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return False
    return len(encoded.encode("utf-8")) > 55_000


def current_durable_job_snapshot():
    """Load the newest running job so an overlay restart does not erase it.

    Only a job with work actually in flight belongs here. The overlay
    reconnects on every bridge restart and every dropped socket, and it opens
    its coding drawer for what this returns, so counting an accepted-but-idle
    or long-abandoned job as active made the drawer reappear with nothing
    running behind it.

    'claimed' is deliberately excluded even though a job passes through it on
    its way to running: a claim that never reaches 'working' is exactly the
    stale record that used to reopen the drawer forever. The cost is that an
    overlay reconnecting inside that sub-second window shows no drawer until
    the supervisor emits code_start, which it does the moment work begins.
    """

    try:
        from core.voice_inbox import get_default_voice_inbox

        store = get_default_voice_inbox()
        jobs = store.recent_jobs(limit=20)
        running = {"working", "resume_queued"}
        job = next((entry for entry in jobs if entry.get("state") in running), None)
        if job is None:
            return None
        return store.overlay_snapshot(str(job["item_id"]))
    except Exception:
        return None


async def apply_code_control(message: dict) -> dict:
    """Persist an overlay control and return the current durable snapshot."""

    from core.voice_inbox import get_default_voice_inbox

    store = get_default_voice_inbox()
    item_id = str(message["item_id"])
    action = str(message["action"])
    if action == "cancel":
        await asyncio.to_thread(store.request_cancel, item_id)
    elif action == "steer":
        await asyncio.to_thread(store.add_steering, item_id, str(message["text"]))
    elif action == "resume":
        await asyncio.to_thread(store.request_resume, item_id)
    snapshot = await asyncio.to_thread(store.overlay_snapshot, item_id)
    if snapshot is None:
        raise ValueError("coding job was not found")
    return snapshot


async def handler(ws):
    clients.add(ws)
    try:
        await ws.send(json.dumps({"type": "state_change", "state": state}))
        snapshot = current_durable_job_snapshot()
        if snapshot is not None:
            message = parse_local_event(
                json.dumps(
                    {"type": "code_snapshot", "snapshot": snapshot},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if message is not None:
                await ws.send(message)
        async for raw in ws:
            raw_type = _top_level_message_type(raw) if isinstance(raw, str) else None
            message = parse_client_message(raw)
            if message is None:
                if raw_type == "typed":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "typed_input_error",
                                "error": "that pasted image could not be accepted; your message is still here.",
                            },
                            separators=(",", ":"),
                        )
                    )
                continue
            if '"type":"typed"' in message:
                typed = json.loads(message)
                await ws.send(json.dumps({"type": "typed_input_accepted"}))
                asyncio.create_task(run_typed_turn(typed["text"], image=typed.get("image")))
                continue
            if '"type":"code_control"' in message:
                control = json.loads(message)
                try:
                    snapshot = await apply_code_control(control)
                except Exception as error:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "code_control_result",
                                "ok": False,
                                "item_id": control["item_id"],
                                "action": control["action"],
                                "error": str(error)[:1_000],
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                else:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "code_control_result",
                                "ok": True,
                                "item_id": control["item_id"],
                                "action": control["action"],
                            },
                            separators=(",", ":"),
                        )
                    )
                    await broadcast(
                        json.dumps(
                            {"type": "code_snapshot", "snapshot": snapshot},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                continue
            await broadcast(message, exclude=ws)
    except websockets.ConnectionClosed:
        # Overlay restarts and laptop sleep often end without a close frame.
        # That is a normal client departure, not a bridge failure.
        pass
    finally:
        clients.discard(ws)


_typed_turn: asyncio.Task | None = None


def new_typed_call_id() -> str:
    """Return a collision-proof call id for one overlay-typed turn."""

    return f"desk-typed-{uuid.uuid4().hex}"


async def _interrupt_typed_turn() -> None:
    """Cut off whatever she is saying so the new message wins.

    Turning a message away with "one sec, still on the last one" is not how
    interrupting someone works, and it lands constantly, because she speaks far
    slower than he types. Talking over her is allowed on the spoken side; it
    should be allowed here too.
    """
    global _typed_turn

    me = asyncio.current_task()
    # Claim ownership BEFORE awaiting the old turn's death. Two messages
    # arriving together otherwise both capture the same predecessor, both
    # await it, and then both run at once, with only the later one recorded
    # as the owner while the orphan burns brain and speaker time.
    previous, _typed_turn = _typed_turn, me
    if previous is None or previous.done() or previous is me:
        return
    previous.cancel()
    # asyncio.wait, not `await previous` under suppress: suppressing there
    # also swallowed a cancellation aimed at US while we waited, so a third
    # message could kill the second and the second would keep running anyway.
    await asyncio.wait([previous], timeout=15)


async def run_typed_turn(text: str, *, image: dict | None = None) -> None:
    """Run one typed desk turn: show it, think, speak as she forms it.

    Deliberately the same path a spoken turn takes, including her brokered
    tools, so typing is an input channel and not a second personality. Clauses
    go to the speaker the moment they are complete, because waiting for the
    whole reply makes her sound like a form submission rather than a person.
    """

    global _typed_turn

    from voice.desk.say import set_state, speak_stream, stream_turn

    await _interrupt_typed_turn()
    display_text = text or "image attached"
    prompt_text = text or "look at this image"
    await broadcast(
        json.dumps({"type": "transcription", "text": display_text}, ensure_ascii=False)
    )
    call_id = new_typed_call_id()
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
        try:
            # systemd starts the overlay and the resident brain together. A
            # typed message in the small warm-up window used to make Raghav
            # repeat himself after a harmless ECONNREFUSED. A fresh resident
            # SDK warm-up can take roughly eight seconds, so wait through a
            # bounded twelve-second local-only window. Never retry a completed
            # or remote turn.
            for attempt in range(12):
                try:
                    turn_options = {
                        "call_id": call_id,
                        "turn_id": f"{call_id}:1",
                        "timeout": 240.0,
                        "on_sentence": on_sentence,
                    }
                    if image is not None:
                        turn_options["images"] = [image]
                    said = await stream_turn(
                        prompt_text,
                        **turn_options,
                    )
                    break
                except OSError as exc:
                    if exc.errno not in {errno.ECONNREFUSED, errno.ENOENT} or attempt == 11:
                        raise
                    await asyncio.sleep(1.0)
        except (OSError, asyncio.TimeoutError) as exc:
            await clauses.put(None)
            with suppress(Exception):
                await player
            _finish_typed_turn()
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
            raise
        except Exception as exc:  # noqa: BLE001 - audio must not break a turn
            print(f"[brain_bridge] typed turn audio failed: {exc}", flush=True)
        _finish_typed_turn()
    finally:
        # A cancellation can land ANYWHERE above, including between the text
        # broadcast and the queue sentinel. An orphaned player waits on that
        # queue forever while holding the tts lock, and every later typed
        # message hangs behind it. Whatever the exit, the player is reaped.
        if not player.done():
            player.cancel()
        with suppress(BaseException):
            await player


def _finish_typed_turn() -> None:
    """Go idle only if this turn is still the one the overlay belongs to.

    A turn that was interrupted by the next message has already handed the dot
    field over, so writing idle on the way out would drop the dot mid-sentence
    of the reply that replaced it.
    """
    from voice.desk.say import set_state

    if _typed_turn is not None and _typed_turn is not asyncio.current_task():
        return
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
        if message is None:
            continue
        parsed = json.loads(message)
        if parsed.get("type") == "fleet_notice":
            _launch_fleet_notice(parsed)
            continue
        await broadcast(message)


_fleet_notice_tasks: set[asyncio.Task] = set()


def _record_fleet_notice(
    notice: dict,
    event_type: str,
    *,
    error: str | None = None,
) -> None:
    from core.fleet_store import FleetStore

    payload = {
        "token": notice["token"],
        "state": notice["state"],
        "channel": "voice",
    }
    if error:
        payload["error"] = " ".join(error.split())[:300]
    FleetStore().append_event(notice["run_id"], event_type, payload)


def _fallback_fleet_notice(notice: dict) -> None:
    """Use the phone only when local speech genuinely failed."""

    from core.fleet_store import FleetStore
    from core.fleet_supervisor import _send_raghav_text

    store = FleetStore()
    if store.terminal_notice_delivered(
        notice["run_id"],
        notice["token"],
        channel="telegram",
    ):
        return
    delivered = _send_raghav_text(notice["text"])
    store.append_event(
        notice["run_id"],
        "run.notification.delivered" if delivered else "run.notification.failed",
        {
            "token": notice["token"],
            "state": notice["state"],
            "channel": "telegram",
        },
    )


async def speak_fleet_notice(notice: dict) -> None:
    """Wait for a conversational gap, then speak one Fleet terminal state."""

    from voice.desk.say import set_state, speak_stream

    while read_state() in {"listening", "thinking", "speaking"}:
        await asyncio.sleep(0.25)
    sentences: asyncio.Queue = asyncio.Queue()
    sentences.put_nowait(notice["text"])
    sentences.put_nowait(None)
    set_state("speaking")
    try:
        await speak_stream(sentences)
    except Exception as error:  # noqa: BLE001 - phone is the deliberate fallback
        with suppress(Exception):
            _record_fleet_notice(
                notice,
                "run.notification.failed",
                error=str(error),
            )
        await asyncio.to_thread(_fallback_fleet_notice, notice)
        print(
            f"[brain_bridge] Fleet notice failed run={notice['run_id'][:8]}: {error}",
            flush=True,
        )
    else:
        _record_fleet_notice(notice, "run.notification.delivered")
        print(
            f"[brain_bridge] Fleet notice spoken run={notice['run_id'][:8]}",
            flush=True,
        )
    finally:
        if _typed_turn is None or _typed_turn.done():
            set_state("idle")


def _launch_fleet_notice(notice: dict) -> None:
    task = asyncio.create_task(speak_fleet_notice(notice))
    _fleet_notice_tasks.add(task)
    task.add_done_callback(_fleet_notice_tasks.discard)


def open_event_socket():
    EVENT_SOCKET.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    EVENT_SOCKET.unlink(missing_ok=True)
    event_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    event_socket.setblocking(False)
    event_socket.bind(str(EVENT_SOCKET))
    os.chmod(EVENT_SOCKET, 0o600)
    return event_socket


async def warm_voice() -> None:
    """Load her voice before the first typed message, not during it.

    The local engine is a sandboxed child that loads a model and primes
    itself, roughly seven seconds on this laptop. Built lazily on the first
    clause, that whole cold start was spent in silence while the reply text
    was already on screen. Doing it here means the very first thing he types
    after a restart is answered out loud within about a second.
    """
    from voice.desk.say import warm_backend

    started = time.monotonic()
    try:
        await warm_backend()
    except Exception as exc:  # noqa: BLE001 - typing must still work mute
        print(f"[brain_bridge] voice warm-up failed: {exc}", flush=True)
        return
    print(
        f"[brain_bridge] voice warm in {time.monotonic() - started:.1f}s",
        flush=True,
    )


async def main():
    event_socket = open_event_socket()
    try:
        async with websockets.serve(handler, HOST, PORT, max_size=MAX_IMAGE_WIRE_BYTES):
            print(f"[brain_bridge] ws://{HOST}:{PORT} watching {STATE_FILE}", flush=True)
            warming = asyncio.create_task(warm_voice())
            try:
                await asyncio.gather(watch(), watch_local_events(event_socket))
            finally:
                warming.cancel()
                with suppress(BaseException):
                    await warming
    finally:
        event_socket.close()
        EVENT_SOCKET.unlink(missing_ok=True)


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main())
