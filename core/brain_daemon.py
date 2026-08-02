"""Serena brain daemon, the resident process. Spec: docs/spec-brain-daemon.md.

One live Claude Agent SDK session, fed messages over time (streaming input
mode), serving every surface (front door first) over localhost HTTP. This is
what makes replies instant instead of a ~4.5s claude -p cold start per turn,
and it's the thing that will eventually watch events and speak first.

Run:  .venv/bin/python3 -m core.brain_daemon          (foreground)
      systemd --user unit: systemd/serena-brain.service

Discovery: writes {port, pid, started} to ~/.config/serena/brain.json; the
front door reads that to route turns here, falling back to claude -p when
this process is down. Kill it any time, state lives in the ledger/memory
files, not in this process (read-before-reply is code, see _state_block).

Billing: subscription only. Startup hard-fails on any metered-provider
environment override because it could silently bypass OAuth subscription.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import inspect
import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import BinaryIO

import psutil

from core.billing import present_metered_auth_env
from core.brain_lifetime import (
    BRAIN_SDK_ROLE,
    BRAIN_SDK_ROLE_ENV,
    BRAIN_SDK_TOKEN_ENV,
    LifetimeLedger,
    RecentThreadJournal,
    brain_sdk_process_identities,
    brain_sdk_process_snapshot,
    child_rss_bytes,
    policy_from_environment,
    policy_snapshot,
    process_identities,
    process_tree_snapshot,
    reap_processes,
    rotation_reason,
    secure_directory,
    write_text_atomic,
)

BRAIN_FILE = Path.home() / ".config" / "serena" / "brain.json"
BRAIN_SYSTEM_PROMPT_FILE = Path(
    os.environ.get(
        "SERENA_BRAIN_SYSTEM_PROMPT_FILE",
        str(Path.home() / ".config" / "serena" / "brain-system-prompt.md"),
    )
).expanduser()
BRAIN_LOCK_FILE = Path(
    os.environ.get(
        "SERENA_BRAIN_LOCK_FILE",
        str(Path.home() / ".config" / "serena" / "brain.lock"),
    )
).expanduser()
BRAIN_CWD = Path.home() / ".cache" / "serena-headless-brain"
HOST = "127.0.0.1"
PORT = int(os.environ.get("SERENA_BRAIN_PORT", "8377"))
STREAM_PORT = int(os.environ.get("SERENA_BRAIN_STREAM_PORT", "8378"))
STREAM_TRANSPORT = (
    "tcp" if os.name == "nt" or os.environ.get("SERENA_BRAIN_STREAM_TRANSPORT") == "tcp" else "unix"
)
MODEL = os.environ.get("SERENA_BRAIN_MODEL", "sonnet")
VOICE_MODEL = os.environ.get("SERENA_BRAIN_VOICE_MODEL", "").strip() or MODEL
REFLEX_MODEL = (
    os.environ.get("SERENA_BRAIN_REFLEX_MODEL", "").strip() or VOICE_MODEL
)
BRAIN_TOKEN = secrets.token_urlsafe(32)

_started = time.time()
_process_created = psutil.Process(os.getpid()).create_time()
_boot_time = psutil.boot_time()
_turns = 0
_notional_sdk_cost_usd = 0.0
_turn_lock: asyncio.Lock | None = None
_last_state_fingerprint = ""
_active_model = MODEL
_last_route = {
    "class": "conversation",
    "model": MODEL,
    "reason": "resident session startup",
}
_instance_lock_handle: BinaryIO | None = None

_BRAIN_BUILTIN_TOOLS: list[str] = []
INTERRUPT_TIMEOUT_SECONDS = 1.0
TURN_CANCEL_TIMEOUT_SECONDS = 5.0
SDK_DISCONNECT_TIMEOUT_SECONDS = 30.0
SDK_REAP_TIMEOUT_SECONDS = 10.0
SDK_QUERY_START_TIMEOUT_SECONDS = 10.0


def _guard_billing() -> None:
    """Refuse to run in an env where turns would silently bill per-token."""
    present = present_metered_auth_env(os.environ)
    if present:
        print(
            "[brain] FATAL: metered-provider auth overrides are set "
            f"({', '.join(present)}); they can bypass subscription OAuth. "
            "Unset them before starting the resident daemon.",
            flush=True,
        )
        sys.exit(78)  # EX_CONFIG


def _lock_file(path: Path) -> BinaryIO:
    """Acquire one cross-platform nonblocking lock and return its live handle."""

    secure_directory(path.parent)
    handle = path.open("a+b", buffering=0)
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        handle.close()
        raise
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n".encode("ascii"))
    handle.flush()
    os.fsync(handle.fileno())
    return handle


def _unlock_file(handle: BinaryIO) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _acquire_instance_lock() -> None:
    global _instance_lock_handle
    if _instance_lock_handle is not None:
        return
    try:
        _instance_lock_handle = _lock_file(BRAIN_LOCK_FILE)
    except (OSError, PermissionError):
        print(
            f"[brain] FATAL: another daemon owns {BRAIN_LOCK_FILE}. One brain only.",
            flush=True,
        )
        sys.exit(75)  # EX_TEMPFAIL


def _release_instance_lock() -> None:
    global _instance_lock_handle
    handle, _instance_lock_handle = _instance_lock_handle, None
    if handle is not None:
        with contextlib.suppress(OSError):
            _unlock_file(handle)


def _persona_context() -> str:
    """Persona, tooling, knowledge index, and surface-neutral daemon rules."""
    parts = []
    try:
        from core.config import read_agent_context

        parts.append(read_agent_context())
    except Exception:
        pass
    try:
        from core.chat_daemon import _knowledge_index

        parts.append(_knowledge_index())
    except Exception:
        pass
    parts.append(
        "# Resident brain\n"
        "You are Serena's resident brain daemon. Surfaces (front door, desk "
        "voice, calls) send you turns tagged with their protocol. When the "
        "turn asks for the front-door JSON protocol, reply with STRICT JSON "
        "per the front-door rules above. Otherwise reply as plain Serena. "
        "A <current-state> block in a message is the live ledger/task digest, "
        "trust it over anything remembered from earlier in this session. "
        "For repo history or status, use mcp__serena-ro__git_latest. For GitHub, "
        "chat recall, and ledger reads, use the matching mcp__serena-ro__ tool. "
        "Never substitute Bash or a state-changing MCP tool for those reads. "
        "On a local desk voice turn, mcp__serena-laptop__laptop_context can read "
        "the active window and audio state. Reversible laptop controls may use "
        "mcp__serena-laptop__laptop_action, whose capability broker independently "
        "checks the original words. Use read-only context silently and answer with "
        "the result instead of narrating that you need to check first. Never claim "
        "a denied action happened. "
        "Only active tasks, loops and ledgers are in your context; the rest of "
        "what you know about him is not. Before you tell him you do not know "
        "something about him, his preferences, his projects or anything you have "
        "researched for him, search for it: mcp__serena-ro__search_memory for "
        "what you have saved about him, mcp__serena-ro__search_knowledge and "
        "read_knowledge for saved research. Saying you have nothing on something "
        "you have never looked up is the thing to avoid. "
        "When he asks you on a spoken turn to remember, correct, or forget "
        "something, that is yours to do: mcp__serena-memory__save_memory, "
        "edit_memory, delete_memory for memories, save_knowledge and "
        "delete_knowledge_topic for the knowledge base. Search first so you "
        "edit or delete the right entry, and for deletions say what you are "
        "removing. A broker re-reads his real words, so if a tool refuses, "
        "tell him it refused and why; never claim something was saved or "
        "deleted when it was not. "
        "When he says to take notes on something, that is a knowledge-base "
        "session, not memories: pick one topic slug for the subject, keep the "
        "running note in your head as he talks, and rewrite that topic's "
        "notes.md with save_knowledge after each addition so the file always "
        "holds the whole note. Clean markdown, his points not a transcript, "
        "dated. Memories are for durable facts about him, one at a time; a "
        "note-taking session that leaks into memories as fragments is wrong. "
        "When he asks where his notes on a subject are, search_knowledge "
        "finds the topic. "
        "For live information or actions in Raghav's connected services, use "
        "mcp__serena-capabilities__find_pc_capability with what he actually needs, "
        "then call mcp__serena-capabilities__use_pc_capability with one returned "
        "schema. This catalog is discovered on demand, so do not guess server or "
        "tool names and do not say you lack access before searching it. Reads need "
        "no extra confirmation. The broker independently checks his current real "
        "turn before any write and refuses unclassified tools. Treat called=false "
        "as not done. Beeper is primarily a source of chat knowledge here; never "
        "send, edit, delete, react, or change chat state unless his current turn "
        "directly asks for that exact action. "
        "When he asks you on a spoken turn to build, fix, change, investigate, "
        "or continue something in his code, that is yours to start: call "
        "mcp__serena-work__start_coding_work and say plainly that you have "
        "started it. Judge it like a person would, not by keywords. If it is "
        "clear, start it, do not ask permission he already gave. If you "
        "genuinely do not know which project or which of two things he means, "
        "read the ledger or your memory first, and only then ask him one short "
        "question instead of guessing. The worker is a separate process that "
        "cannot hear you, so put everything it needs in the request, including "
        "the project. A broker re-reads his real words, so if the tool refuses, "
        "tell him it refused and why; never say work started when it did not. "
        "Typing, "
        "clicking, messaging, deletion, purchases, deployment, and account changes "
        "are intentionally unavailable through that tool."
    )
    return "\n\n".join(p for p in parts if p and p.strip())


def _state_block(force: bool = False) -> str:
    """Return live state, keeping the active ledger grounded every turn.

    Tasks and loops are resent only when the state changes. The much smaller
    full ledger is present on every turn so a fresh or rotated session cannot
    lose current decisions behind the warm-up exchange.
    """
    global _last_state_fingerprint
    try:
        from core.brain_state import active_state, compact_active, format_ledgers

        active = active_state()
        state = compact_active(active)
    except Exception:
        return ""
    fp = str(hash(state))
    if not force and fp == _last_state_fingerprint:
        ledgers = format_ledgers(state=active)
        return "" if ledgers == "(no matching ledgers)" else ledgers
    _last_state_fingerprint = fp
    return state


def _compose_message(payload: dict) -> str:
    parts = []
    state = _state_block()
    if state:
        parts.append(f"<current-state>\n{state}\n</current-state>")
    protocol = payload.get("protocol") or "plain"
    if protocol == "frontdoor":
        try:
            from core.frontdoor import _ROLE

            parts.append(_ROLE)
        except Exception:
            pass
        parts.append("(front-door turn, reply with the STRICT front-door JSON protocol)")
    elif protocol == "voice":
        parts.append(
            "(voice turn, spoken aloud: plain conversational prose, short "
            "sentences, no markdown, no lists, no code. Sound like natural "
            "speech: use contractions, compact clauses, and words that are easy "
            "to say aloud. Avoid parentheticals, URLs, file paths, raw symbols, "
            "and unexplained abbreviations. This transcription is "
            "the complete utterance, not a session-open greeting trigger. "
            "Answer the whole request after any 'hey Serena' wake phrase. If "
            "it contains a request, never replace the answer with a generic "
            "'what's up?' Lead with the first useful point as a complete sentence "
            "of roughly six to sixteen words, ending in punctuation so speech "
            "can start before the rest. Do not add filler. You own coding work "
            "personally. If he asks whether you can code, say yes and tell him "
            "to name the build so you can start it yourself. Never mention panes, "
            "Codex, Claude, queues, handoffs, dispatchers, or runtimes aloud. Those "
            "are private implementation details. Never say you will send, route, "
            "or pass work to a coding terminal. You either start it through the "
            "private coding workspace attached to the turn or state plainly that it has not "
            "started.)"
        )
        voice_context = {
            key: str(payload.get(key) or "").strip()
            for key in ("call_id", "turn_id")
            if str(payload.get(key) or "").strip()
        }
        if voice_context:
            parts.append(
                "<voice-turn-context>"
                + json.dumps(voice_context, separators=(",", ":"))
                + "</voice-turn-context>"
            )
    parts.append((payload.get("text") or "").strip())
    return "\n\n".join(parts)


def _model_for_protocol(protocol: str) -> str:
    return VOICE_MODEL if protocol == "voice" else MODEL


def _join_assistant_chunks(chunks: list[str]) -> str:
    """Keep SDK text blocks readable across tool-use message boundaries."""

    merged = ""
    for chunk in chunks:
        if (
            merged
            and chunk
            and not merged[-1].isspace()
            and not chunk[0].isspace()
        ):
            merged += " "
        merged += chunk
    return merged.strip()


async def _select_model(client, protocol: str) -> str:
    """Route a turn without splitting Serena across separate SDK sessions."""
    global _active_model
    desired = _model_for_protocol(protocol)
    if desired != _active_model:
        await client.set_model(desired)
        _active_model = desired
    return desired


async def _select_route(client, payload: dict):
    """Choose a capability class while retaining the one resident session."""

    global _active_model, _last_route
    from core.brain_router import route_turn

    decision = route_turn(
        payload,
        conversation_model=MODEL,
        voice_model=VOICE_MODEL,
        reflex_model=REFLEX_MODEL,
    )
    if decision.model != _active_model:
        await client.set_model(decision.model)
        _active_model = decision.model
    _last_route = decision.as_dict()
    return decision


async def _run_turn(client, payload: dict, on_delta=None) -> dict:
    """Bind the exact originating turn for brokered tools, then run it."""

    from core.brain_laptop_tools import reset_current_turn, set_current_turn

    token = set_current_turn(payload)
    try:
        return await _run_turn_scoped(client, payload, on_delta=on_delta)
    finally:
        reset_current_turn(token)


async def _run_turn_scoped(client, payload: dict, on_delta=None) -> dict:
    """One serialized turn through the resident session. When on_delta is
    given, token deltas stream to it as they arrive (voice/TTS path)."""
    global _turns
    if not (payload.get("text") or "").strip():
        return {"ok": False, "error": "text required"}
    protocol = payload.get("protocol") or "plain"

    t0 = time.time()
    route = await _select_route(client, payload)
    selected_model = route.model
    await asyncio.wait_for(
        client.query(_compose_message(payload)),
        timeout=SDK_QUERY_START_TIMEOUT_SECONDS,
    )
    chunks: list[str] = []
    first_delta_at: float | None = None
    result_session_id: str | None = None
    compact_boundary_seen = False
    async for msg in client.receive_response():
        kind = type(msg).__name__
        if kind == "StreamEvent" and on_delta is not None:
            ev = getattr(msg, "event", None) or {}
            if ev.get("type") == "content_block_delta":
                delta = (ev.get("delta") or {}).get("text") or ""
                if delta:
                    if first_delta_at is None:
                        first_delta_at = time.time()
                    await on_delta(delta)
        elif kind == "AssistantMessage":
            for block in getattr(msg, "content", []) or []:
                t = getattr(block, "text", None)
                if t:
                    chunks.append(t)
        elif kind == "SystemMessage":
            if getattr(msg, "subtype", None) == "compact_boundary":
                compact_boundary_seen = True
        elif kind == "ResultMessage":
            result_session_id = getattr(msg, "session_id", None)
            # NOTE: total_cost_usd is a NOTIONAL estimate the CLI reports even
            # on subscription OAuth (verified 2026-07-16: no API key anywhere
            # in the auth chain, credentials are claudeAiOauth, yet turns
            # report ~$0.09). Track it for capacity insight; the real
            # zero-spend proof is the billing dashboard after the 24h soak.
            cost = getattr(msg, "total_cost_usd", None)
            if cost:
                global _notional_sdk_cost_usd
                _notional_sdk_cost_usd += float(cost)
    _turns += 1
    raw = _join_assistant_chunks(chunks)

    out = {
        "ok": True,
        "elapsed": round(time.time() - t0, 2),
        "turns": _turns,
        "model": selected_model,
        "route_class": route.route_class,
        "route_reason": route.reason,
        "billing_mode": "subscription_oauth_guarded",
        "daemon_pid": os.getpid(),
        "daemon_started": _started,
    }
    if result_session_id:
        out["session_id"] = result_session_id
    if first_delta_at is not None:
        out["first_delta"] = round(first_delta_at - t0, 2)
    if protocol == "frontdoor":
        try:
            from core.frontdoor import _parse_reply

            parsed = _parse_reply(raw)
            out["say"], out["spawn"] = parsed["say"], parsed["spawn"]
        except Exception:
            out["say"], out["spawn"] = raw, None
    else:
        out["say"] = raw
    record = getattr(client, "record_completed_turn", None)
    if callable(record):
        lifecycle = record(
            payload=payload,
            assistant_text=raw,
            selected_model=selected_model,
            session_id=result_session_id,
            compact_boundary_seen=compact_boundary_seen,
        )
        if inspect.isawaitable(lifecycle):
            lifecycle = await lifecycle
        if isinstance(lifecycle, dict):
            out.update({f"_{key}": value for key, value in lifecycle.items()})
    return out


async def _interrupt_client(client) -> bool:
    """Ask the SDK to stop without letting its control path block forever."""

    interrupt = getattr(client, "interrupt", None)
    if not callable(interrupt):
        return False
    result = interrupt()
    if inspect.isawaitable(result):
        await asyncio.wait_for(result, timeout=INTERRUPT_TIMEOUT_SECONDS)
    return True


async def _interrupt_active_turn(client, task: asyncio.Task) -> None:
    """Stop an SDK turn whose surface disconnected, then release its lock."""
    if task.done():
        await asyncio.gather(task, return_exceptions=True)
        return
    interrupted = False
    try:
        interrupted = await _interrupt_client(client)
    except Exception:
        interrupted = False
    grace = TURN_CANCEL_TIMEOUT_SECONDS if interrupted else 0.0
    done, _ = await asyncio.wait({task}, timeout=grace)
    if task in done:
        await asyncio.gather(task, return_exceptions=True)
        return
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=INTERRUPT_TIMEOUT_SECONDS)
    if task in done:
        await asyncio.gather(task, return_exceptions=True)
        return
    error = "SDK turn ignored interrupt and cancellation"
    fatal_event = getattr(client, "fatal_event", None)
    if isinstance(fatal_event, asyncio.Event):
        client.fatal_error = error
        client.last_error = error
        fatal_event.set()
    raise RuntimeError(error)


async def _response_committed(client, out: dict) -> None:
    """Durably commit a completed reply before any final response bytes leave."""

    committed = getattr(client, "response_committed", None)
    if not callable(committed):
        return
    result = committed(out)
    if inspect.isawaitable(result):
        await result


async def _response_delivered(client, out: dict) -> None:
    delivered = getattr(client, "response_delivered", None)
    if not callable(delivered):
        return
    result = delivered(out)
    if inspect.isawaitable(result):
        await result


async def _response_not_delivered(client, out: dict) -> None:
    failed = getattr(client, "response_not_delivered", None)
    if not callable(failed):
        return
    result = failed(out)
    if inspect.isawaitable(result):
        await result


def _resident_turn_lock() -> asyncio.Lock:
    if _turn_lock is None:
        raise RuntimeError("brain turn lock is unavailable")
    return _turn_lock


def _public_turn_result(out: dict) -> dict:
    public = {key: value for key, value in out.items() if not key.startswith("_")}
    if out.get("_session_id"):
        public["session_id"] = out["_session_id"]
    if out.get("_session_turns") is not None:
        public["session_turns"] = out["_session_turns"]
    return public


async def _handle_stream_connection(
    client,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    auth_token: str | None = None,
) -> None:
    async def send(obj: dict) -> None:
        writer.write((json.dumps(obj) + "\n").encode("utf-8"))
        await writer.drain()

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                req = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                await send({"type": "error", "request_id": None, "error": "bad json line"})
                continue
            rid = req.get("request_id")
            if auth_token and not hmac.compare_digest(str(req.get("auth") or ""), auth_token):
                await send(
                    {
                        "type": "error",
                        "request_id": rid,
                        "error": "unauthorized",
                    }
                )
                break
            if req.get("type") != "turn":
                await send(
                    {
                        "type": "error",
                        "request_id": rid,
                        "error": f"unknown type {req.get('type')}",
                    }
                )
                continue

            want_stream = bool(req.get("stream"))

            async def emit(delta: str, _rid=rid) -> None:
                await send(
                    {
                        "type": "response.delta",
                        "request_id": _rid,
                        "delta": delta,
                    }
                )

            turn_acquired = asyncio.Event()
            response_ready = asyncio.Event()
            peer_watch: dict[str, asyncio.Task] = {}

            async def run_locked_exchange(
                _req=req,
                _rid=rid,
                _want_stream=want_stream,
                _turn_acquired=turn_acquired,
                _response_ready=response_ready,
                _peer_watch=peer_watch,
            ) -> dict:
                async with _resident_turn_lock():
                    _turn_acquired.set()
                    await send({"type": "response.start", "request_id": _rid})
                    try:
                        out = await _run_turn(
                            client,
                            _req,
                            on_delta=emit if _want_stream else None,
                        )
                    except BaseException:
                        with contextlib.suppress(Exception):
                            await _interrupt_client(client)
                        raise
                    _response_ready.set()
                    peer_task = _peer_watch.get("task")
                    if peer_task is not None:
                        peer_task.cancel()
                        await asyncio.gather(peer_task, return_exceptions=True)
                    if not out.get("ok"):
                        await send(
                            {
                                "type": "error",
                                "request_id": _rid,
                                "error": out.get("error", "turn failed"),
                            }
                        )
                        return out
                    done = {
                        "type": "response.done",
                        "request_id": _rid,
                        "say": out.get("say", ""),
                        "meta": {
                            "elapsed": out.get("elapsed"),
                            "first_delta": out.get("first_delta"),
                            "turns": out.get("turns"),
                            "session_turns": out.get("_session_turns"),
                            "session_id": out.get("_session_id") or out.get("session_id"),
                            "turn_id": str(_req.get("turn_id") or "") or None,
                            "model": out.get("model"),
                            "route_class": out.get("route_class"),
                            "route_reason": out.get("route_reason"),
                            "billing_mode": out.get("billing_mode"),
                            "daemon_pid": out.get("daemon_pid"),
                            "daemon_started": out.get("daemon_started"),
                        },
                    }
                    if "spawn" in out:
                        done["spawn"] = out["spawn"]
                    try:
                        await _response_committed(client, out)
                        await send(done)
                    except BaseException:
                        await _response_not_delivered(client, out)
                        raise
                    await _response_delivered(client, out)
                    return out

            turn_task = asyncio.create_task(run_locked_exchange(), name=f"brain-stream-{rid}")
            disconnect_task = asyncio.create_task(reader.read(1), name=f"brain-peer-{rid}")
            peer_watch["task"] = disconnect_task
            try:
                finished, _ = await asyncio.wait(
                    {turn_task, disconnect_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if turn_task not in finished and disconnect_task in finished:
                    if response_ready.is_set():
                        await turn_task
                    elif turn_acquired.is_set():
                        await _interrupt_active_turn(client, turn_task)
                    else:
                        turn_task.cancel()
                        await asyncio.gather(turn_task, return_exceptions=True)
                    if not response_ready.is_set():
                        return
                disconnect_task.cancel()
                await asyncio.gather(disconnect_task, return_exceptions=True)
                await turn_task
            except asyncio.CancelledError:
                disconnect_task.cancel()
                await asyncio.gather(disconnect_task, return_exceptions=True)
                if turn_acquired.is_set():
                    await _interrupt_active_turn(client, turn_task)
                else:
                    turn_task.cancel()
                    await asyncio.gather(turn_task, return_exceptions=True)
                raise
            except Exception as exc:
                disconnect_task.cancel()
                await asyncio.gather(disconnect_task, return_exceptions=True)
                if turn_acquired.is_set():
                    await _interrupt_active_turn(client, turn_task)
                else:
                    turn_task.cancel()
                    await asyncio.gather(turn_task, return_exceptions=True)
                try:
                    await send(
                        {
                            "type": "error",
                            "request_id": rid,
                            "error": str(exc),
                        }
                    )
                except (ConnectionError, OSError):
                    return
                continue
    except (ConnectionResetError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def _serve_socket(client, ready: asyncio.Event | None = None) -> None:
    """Serve the NDJSON stream over Unix locally or loopback TCP on Windows."""

    async def on_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle_stream_connection(
            client,
            reader,
            writer,
            auth_token=BRAIN_TOKEN if STREAM_TRANSPORT == "tcp" else None,
        )

    if STREAM_TRANSPORT == "tcp":
        server = await asyncio.start_server(on_conn, HOST, STREAM_PORT)
        print(
            f"[brain] stream listening on tcp://{HOST}:{STREAM_PORT}",
            flush=True,
        )
    else:
        sock_path = Path.home() / ".config" / "serena" / "brain.sock"
        with contextlib.suppress(OSError):
            sock_path.unlink()
        server = await asyncio.start_unix_server(on_conn, path=str(sock_path))
        os.chmod(sock_path, 0o600)
        print(f"[brain] socket listening at {sock_path}", flush=True)
    if ready is not None:
        ready.set()
    async with server:
        await server.serve_forever()


def _windows_current_principal() -> str:
    """Return the account name Windows ACL tools actually resolve."""
    try:
        result = subprocess.run(
            ["whoami"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        principal = result.stdout.strip()
        if result.returncode == 0 and principal:
            return principal
    except (OSError, subprocess.SubprocessError):
        pass
    domain = os.environ.get("USERDOMAIN", "")
    username = os.environ.get("USERNAME", "")
    return f"{domain}\\{username}" if domain and username else username


def _restrict_private_file(path: Path, *, label: str) -> None:
    if os.name != "nt":
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return
    principal = _windows_current_principal()
    if not principal:
        raise RuntimeError(f"cannot identify the Windows user for {label} ACL")
    result = subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{principal}:(F)",
            "/grant:r",
            "*S-1-5-18:(F)",
            "/grant:r",
            "*S-1-5-32-544:(F)",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to restrict {label} ACL")


def _write_private_text(path: Path, value: str, *, label: str) -> Path:
    """Atomically write private text without placing it in a process argument."""

    del label
    return write_text_atomic(path, value)


def _write_discovery(payload: dict) -> None:
    """Atomically publish discovery with a user-only credential file."""
    secure_directory(BRAIN_FILE.parent)
    temp = BRAIN_FILE.parent / f".brain-{os.getpid()}-{uuid.uuid4().hex}.json.tmp"
    descriptor = os.open(
        temp,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    os.close(descriptor)
    try:
        _restrict_private_file(temp, label="brain discovery")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(BRAIN_FILE)
    finally:
        temp.unlink(missing_ok=True)


async def _write_http_json(
    writer: asyncio.StreamWriter,
    status: int,
    reason: str,
    payload: dict,
) -> None:
    data = json.dumps(payload).encode("utf-8")
    writer.write(
        f"HTTP/1.1 {status} {reason}\r\n".encode("latin-1")
        + b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(data)}\r\n\r\n".encode("latin-1")
        + data
    )
    await writer.drain()


async def _handle_http_connection(
    client,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Serve one authenticated loopback HTTP request."""
    try:
        request = await asyncio.wait_for(reader.readline(), timeout=10)
        method, path, _ = request.decode("latin-1").split(" ", 2)
        length = 0
        authorization = ""
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if line in (b"\r\n", b"\n", b""):
                break
            key, _, value = line.decode("latin-1").partition(":")
            if key.strip().lower() == "content-length":
                length = int(value.strip())
            elif key.strip().lower() == "authorization":
                authorization = value.strip()
        body = await reader.readexactly(length) if length else b""

        if method == "GET" and path == "/health":
            from core.brain_state import state_status

            lifetime_snapshot = getattr(client, "snapshot", None)
            status, reason = 200, "OK"
            response = {
                "ok": True,
                "pid": os.getpid(),
                "started": _started,
                "process_created": _process_created,
                "boot_time": _boot_time,
                "model": MODEL,
                "voice_model": VOICE_MODEL,
                "reflex_model": REFLEX_MODEL,
                "active_model": _active_model,
                "routing": dict(_last_route),
                "uptime": round(time.time() - _started),
                "turns": _turns,
                "billing": {
                    "auth_mode": "subscription_oauth_guarded",
                    "api_key_present": False,
                    "metered_auth_env_present": [],
                    "notional_cost_usd": round(_notional_sdk_cost_usd, 4),
                    "notional_source": "sdk_result_model_price_estimate",
                    "metered_cost_usd": None,
                    "metered_source": "billing_dashboard_only",
                },
                "state": state_status(),
                "lifetime": lifetime_snapshot() if callable(lifetime_snapshot) else None,
            }
        elif method == "POST" and path == "/turn":
            expected = f"Bearer {BRAIN_TOKEN}"
            if not hmac.compare_digest(authorization, expected):
                status, reason = 401, "Unauthorized"
                response = {"ok": False, "error": "unauthorized"}
            else:
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    status, reason = 400, "Bad Request"
                    response = {"ok": False, "error": "invalid json"}
                else:
                    status, reason = 200, "OK"
                    turn_acquired = asyncio.Event()
                    response_ready = asyncio.Event()

                    async def run_locked_exchange() -> dict:
                        async with _resident_turn_lock():
                            turn_acquired.set()
                            try:
                                out = await _run_turn(client, payload)
                            except BaseException:
                                with contextlib.suppress(Exception):
                                    await _interrupt_client(client)
                                raise
                            response_ready.set()
                            disconnect_task.cancel()
                            await asyncio.gather(disconnect_task, return_exceptions=True)
                            try:
                                await _response_committed(client, out)
                                await _write_http_json(
                                    writer,
                                    status,
                                    reason,
                                    _public_turn_result(out),
                                )
                            except BaseException:
                                await _response_not_delivered(client, out)
                                raise
                            await _response_delivered(client, out)
                            return out

                    turn_task = asyncio.create_task(run_locked_exchange(), name="brain-http-turn")
                    disconnect_task = asyncio.create_task(reader.read(1), name="brain-http-peer")
                    try:
                        finished, _ = await asyncio.wait(
                            {turn_task, disconnect_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if turn_task in finished:
                            disconnect_task.cancel()
                            await asyncio.gather(disconnect_task, return_exceptions=True)
                            await turn_task
                            return
                        if response_ready.is_set():
                            await turn_task
                            return
                        else:
                            if turn_acquired.is_set():
                                await _interrupt_active_turn(client, turn_task)
                            else:
                                turn_task.cancel()
                                await asyncio.gather(turn_task, return_exceptions=True)
                            return
                    except asyncio.CancelledError:
                        disconnect_task.cancel()
                        await asyncio.gather(disconnect_task, return_exceptions=True)
                        if turn_acquired.is_set():
                            await _interrupt_active_turn(client, turn_task)
                        else:
                            turn_task.cancel()
                            await asyncio.gather(turn_task, return_exceptions=True)
                        raise
        else:
            status, reason = 404, "Not Found"
            response = {"ok": False, "error": f"no route {method} {path}"}

        await _write_http_json(writer, status, reason, response)
    except Exception as exc:
        with contextlib.suppress(Exception):
            await _write_http_json(
                writer,
                500,
                "Internal Server Error",
                {"ok": False, "error": str(exc)},
            )
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def _serve(client, stream_ready: asyncio.Event | None = None) -> None:
    """Tiny HTTP server. Endpoints: GET /health and POST /turn."""

    async def on_conn(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await _handle_http_connection(client, reader, writer)

    server = await asyncio.start_server(on_conn, HOST, PORT)
    if stream_ready is not None:
        await stream_ready.wait()
    stream = (
        {"transport": "tcp", "host": HOST, "port": STREAM_PORT}
        if STREAM_TRANSPORT == "tcp"
        else {
            "transport": "unix",
            "path": str(Path.home() / ".config" / "serena" / "brain.sock"),
        }
    )
    if stream["transport"] == "tcp":
        stream["token"] = BRAIN_TOKEN
    _write_discovery(
        {
            "port": PORT,
            "pid": os.getpid(),
            "started": _started,
            "process_created": _process_created,
            "boot_time": _boot_time,
            "lock": str(BRAIN_LOCK_FILE),
            "token": BRAIN_TOKEN,
            "stream": stream,
            "session_id": getattr(client, "session_id", None),
        }
    )
    print(f"[brain] listening on http://{HOST}:{PORT} pid={os.getpid()}", flush=True)
    async with server:
        await server.serve_forever()


def _guard_single_instance() -> None:
    """Exit if a live daemon already owns brain.json (two daemons fight over
    brain.sock and discovery, found the hard way on 2026-07-16)."""
    import urllib.request

    try:
        info = json.loads(BRAIN_FILE.read_text(encoding="utf-8"))
        port = int(info["port"])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            health = json.loads(r.read())
        if health.get("ok"):
            print(
                f"[brain] FATAL: daemon already running (pid {health.get('pid')} "
                f"port {port}). One brain only.",
                flush=True,
            )
            sys.exit(75)  # EX_TEMPFAIL
    except (OSError, ValueError, KeyError):
        pass  # stale or absent file, we own it


def _build_agent_options(
    options_type,
    brain_tools,
    brain_tool_names: list[str],
    *,
    laptop_tools=None,
    laptop_tool_names: list[str] | None = None,
    work_tools=None,
    work_tool_names: list[str] | None = None,
    memory_tools=None,
    memory_tool_names: list[str] | None = None,
    capability_tools=None,
    capability_tool_names: list[str] | None = None,
    session_id: str | None = None,
):
    """Build the narrow, unattended options used by every daemon session."""
    allowed_tools = [
        *brain_tool_names,
        *(laptop_tool_names or []),
        *(work_tool_names or []),
        *(memory_tool_names or []),
        *(capability_tool_names or []),
    ]
    mcp_servers = {"serena-ro": brain_tools}
    if laptop_tools is not None:
        mcp_servers["serena-laptop"] = laptop_tools
    if work_tools is not None:
        mcp_servers["serena-work"] = work_tools
    if memory_tools is not None:
        mcp_servers["serena-memory"] = memory_tools
    if capability_tools is not None:
        mcp_servers["serena-capabilities"] = capability_tools
    prompt_path = _write_private_text(
        BRAIN_SYSTEM_PROMPT_FILE,
        _persona_context(),
        label="brain system prompt",
    )
    return options_type(
        model=MODEL,
        system_prompt={"type": "preset", "preset": "claude_code"},
        extra_args={"append-system-prompt-file": str(prompt_path)},
        mcp_servers=mcp_servers,
        strict_mcp_config=True,
        # Keep the resident surface read-only by construction. The allow list
        # executes unattended; everything else is unavailable or denied rather
        # than opening an approval prompt that no headless surface can answer.
        tools=list(_BRAIN_BUILTIN_TOOLS),
        allowed_tools=allowed_tools,
        permission_mode="dontAsk",
        setting_sources=[],
        skills=[],
        cwd=str(BRAIN_CWD),
        env={
            "SERENA_FRONTDOOR": "1",
            BRAIN_SDK_ROLE_ENV: BRAIN_SDK_ROLE,
            BRAIN_SDK_TOKEN_ENV: session_id or "unassigned",
        },
        include_partial_messages=True,  # token deltas for the voice/stream path
        # Voice-grade TTFT: no deliberation before speaking. Codex's pipeline
        # budget needs first delta at 500-800ms; thinking tokens were the
        # bulk of the 2.1-2.6s measured before this.
        thinking={"type": "disabled"},
        effort=os.environ.get("SERENA_BRAIN_EFFORT", "low"),
        max_turns=200,
        session_id=session_id,
    )


def _session_store_snapshot(session_id: str | None) -> dict:
    """Expose bounded JSONL growth telemetry without reading transcript content."""

    try:
        import re

        projects_root = Path.home() / ".claude" / "projects"
        slug = re.sub(r"[^A-Za-z0-9_-]", "-", str(BRAIN_CWD))
        project_dir = projects_root / slug
        expected = project_dir / f"{session_id}.jsonl" if session_id else None
        if expected is not None and not expected.is_file() and projects_root.is_dir():
            matches = list(projects_root.rglob(f"{session_id}.jsonl"))
            if matches:
                project_dir = max(matches, key=lambda path: path.stat().st_mtime).parent
        rows = []
        if project_dir.is_dir():
            for path in project_dir.glob("*.jsonl"):
                try:
                    metadata = path.stat()
                except OSError:
                    continue
                rows.append(
                    {
                        "session_id": path.stem,
                        "bytes": metadata.st_size,
                        "mtime": metadata.st_mtime,
                    }
                )
        current = next((row for row in rows if row["session_id"] == session_id), None)
        return {
            "available": True,
            "project_dir": str(project_dir),
            "jsonl_files": len(rows),
            "jsonl_bytes": sum(int(row["bytes"]) for row in rows),
            "current": current,
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def _asyncio_task_count() -> int | None:
    try:
        return sum(not task.done() for task in asyncio.all_tasks())
    except RuntimeError:
        return None


class ResidentClientManager:
    """Rotate fresh SDK sessions behind one stable surface contract."""

    def __init__(
        self,
        options_type,
        client_type,
        brain_tools_factory,
        brain_tool_names: list[str],
        *,
        laptop_tools_factory=None,
        laptop_tool_names: list[str] | None = None,
        work_tools_factory=None,
        work_tool_names: list[str] | None = None,
        memory_tools_factory=None,
        memory_tool_names: list[str] | None = None,
        capability_tools_factory=None,
        capability_tool_names: list[str] | None = None,
        journal: RecentThreadJournal | None = None,
        lifetime: LifetimeLedger | None = None,
    ) -> None:
        self.options_type = options_type
        self.client_type = client_type
        self.brain_tools_factory = brain_tools_factory
        self.brain_tool_names = brain_tool_names
        self.laptop_tools_factory = laptop_tools_factory
        self.laptop_tool_names = list(laptop_tool_names or [])
        self.work_tools_factory = work_tools_factory
        self.work_tool_names = list(work_tool_names or [])
        self.memory_tools_factory = memory_tools_factory
        self.memory_tool_names = list(memory_tool_names or [])
        self.capability_tools_factory = capability_tools_factory
        self.capability_tool_names = list(capability_tool_names or [])
        self.journal = journal or RecentThreadJournal()
        self.lifetime = lifetime or LifetimeLedger()
        self.policy = policy_from_environment()
        self.client = None
        self.session_id: str | None = None
        self.process_token: str | None = None
        self.epoch_started_monotonic = 0.0
        self.epoch_turns = 0
        self.baseline_child_rss_bytes: int | None = None
        self.last_context: dict | None = None
        self.last_process: dict | None = None
        self.last_error: str | None = None
        self.rotation_pending: str | None = None
        self.rotation_count = 0
        self.rotation_in_progress = False
        self._delivery_retry_ids: set[str] = set()
        self._known_process_tokens = {
            str(row.get("process_token"))
            for row in self.lifetime.snapshot().get("epochs", [])
            if row.get("process_token")
        }
        self.fatal_event = asyncio.Event()
        self.fatal_error: str | None = None
        self._stopping = False

    async def start(self) -> None:
        previous_identities = [
            identity
            for token in self._known_process_tokens
            for identity in brain_sdk_process_identities(token)
        ]
        if previous_identities:
            survivors = await asyncio.wait_for(
                reap_processes(previous_identities, timeout_seconds=0),
                timeout=SDK_REAP_TIMEOUT_SECONDS,
            )
            if survivors:
                raise RuntimeError(f"previous SDK processes survived startup reap: {survivors}")
        discarded = self.journal.discard_undelivered()
        if discarded:
            print(
                f"[brain] discarded {discarded} undelivered handoff entries",
                flush=True,
            )
        await self._start_epoch("boot")

    async def _start_epoch(self, reason: str) -> None:
        global _active_model, _last_route
        requested_session_id = str(uuid.uuid4())
        process_token = requested_session_id
        options = _build_agent_options(
            self.options_type,
            self.brain_tools_factory(),
            self.brain_tool_names,
            laptop_tools=(
                self.laptop_tools_factory()
                if self.laptop_tools_factory is not None
                else None
            ),
            laptop_tool_names=self.laptop_tool_names,
            work_tools=(
                self.work_tools_factory()
                if self.work_tools_factory is not None
                else None
            ),
            work_tool_names=self.work_tool_names,
            memory_tools=(
                self.memory_tools_factory()
                if self.memory_tools_factory is not None
                else None
            ),
            memory_tool_names=self.memory_tool_names,
            capability_tools=(
                self.capability_tools_factory()
                if self.capability_tools_factory is not None
                else None
            ),
            capability_tool_names=self.capability_tool_names,
            session_id=requested_session_id,
        )
        secure_directory(Path(options.cwd))
        client = self.client_type(options=options)
        try:
            print(f"[brain] epoch connect session={requested_session_id}", flush=True)
            await asyncio.wait_for(client.connect(), timeout=SDK_DISCONNECT_TIMEOUT_SECONDS)
            print(f"[brain] epoch connected session={requested_session_id}", flush=True)
            _active_model = MODEL
            _last_route = {
                "class": "conversation",
                "model": MODEL,
                "reason": f"resident session {reason}",
            }
            state = _state_block(force=True)
            handoff = self.journal.render_handoff()
            warm_parts = []
            if state:
                warm_parts.append("<current-state>\n" + state + "\n</current-state>")
            if handoff:
                warm_parts.append(handoff)
            warm_parts.append("(daemon session warm-up; acknowledge only with: ready)")
            await asyncio.wait_for(
                client.query("\n\n".join(warm_parts)),
                timeout=SDK_QUERY_START_TIMEOUT_SECONDS,
            )
            print(f"[brain] epoch warm query sent session={requested_session_id}", flush=True)

            async def receive_warm_response() -> str:
                result_session_id = requested_session_id
                async for message in client.receive_response():
                    if type(message).__name__ == "ResultMessage":
                        result_session_id = (
                            getattr(message, "session_id", None) or result_session_id
                        )
                return result_session_id

            session_id = await asyncio.wait_for(
                receive_warm_response(), timeout=SDK_DISCONNECT_TIMEOUT_SECONDS
            )
            print(f"[brain] epoch warm reply session={session_id}", flush=True)
        except BaseException:
            failed_snapshot = process_tree_snapshot()
            failed_identities = [
                *process_identities(failed_snapshot),
                *brain_sdk_process_identities(process_token),
            ]
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.disconnect(), timeout=SDK_DISCONNECT_TIMEOUT_SECONDS)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    reap_processes(failed_identities, timeout_seconds=0),
                    timeout=SDK_REAP_TIMEOUT_SECONDS,
                )
            raise
        self.client = client
        self.session_id = session_id
        self.process_token = process_token
        self._known_process_tokens.add(process_token)
        self.epoch_started_monotonic = time.monotonic()
        self.epoch_turns = 0
        self.rotation_pending = None
        self.last_context = None
        self.last_process = process_tree_snapshot()
        self.baseline_child_rss_bytes = child_rss_bytes(self.last_process)
        self.lifetime.start_epoch(
            session_id,
            reason=reason,
            process_token=process_token,
        )
        print(
            f"[brain] session warm epoch={self.rotation_count + 1} session={session_id}",
            flush=True,
        )

    def _require_client(self):
        if self.client is None:
            raise RuntimeError("resident brain session is unavailable")
        return self.client

    async def query(self, *args, **kwargs):
        return await self._require_client().query(*args, **kwargs)

    def receive_response(self):
        return self._require_client().receive_response()

    async def set_model(self, model):
        return await self._require_client().set_model(model)

    async def interrupt(self):
        return await self._require_client().interrupt()

    async def get_context_usage(self):
        return await self._require_client().get_context_usage()

    async def record_completed_turn(
        self,
        *,
        payload: dict,
        assistant_text: str,
        selected_model: str,
        session_id: str | None,
        compact_boundary_seen: bool,
    ) -> dict:
        if session_id:
            self.session_id = session_id
        context = None
        try:
            raw_context = await self.get_context_usage()
            context = {
                key: raw_context.get(key)
                for key in (
                    "totalTokens",
                    "maxTokens",
                    "rawMaxTokens",
                    "percentage",
                    "model",
                    "isAutoCompactEnabled",
                    "autoCompactThreshold",
                )
                if key in raw_context
            }
        except Exception as exc:
            self.last_error = f"context usage failed: {exc}"
        process = process_tree_snapshot()
        self.last_context = context
        self.last_process = process
        self.epoch_turns += 1

        journal_id = None
        if payload.get("journal", True) is not False:
            try:
                journal_id = self.journal.append_pending(
                    user_text=str(payload.get("text") or ""),
                    assistant_text=assistant_text,
                    protocol=str(payload.get("protocol") or "plain"),
                    model=selected_model,
                    session_id=self.session_id,
                    turn_id=str(payload.get("turn_id") or "") or None,
                    call_id=str(payload.get("call_id") or "") or None,
                    ledger_fingerprint=_last_state_fingerprint or None,
                )
            except Exception as exc:
                self.last_error = f"thread journal failed: {exc}"

        try:
            epoch = self.lifetime.record_turn(
                context=context,
                process=process,
                compact_boundary_seen=compact_boundary_seen,
            )
            completed_turns = int(epoch.get("completed_turns") or self.epoch_turns)
        except Exception as exc:
            self.last_error = f"lifetime ledger failed: {exc}"
            completed_turns = self.epoch_turns

        percentage = None
        if context is not None and context.get("percentage") is not None:
            try:
                percentage = float(context["percentage"])
            except (TypeError, ValueError):
                self.last_error = "context usage returned an invalid percentage"
        reason = rotation_reason(
            self.policy,
            age_seconds=time.monotonic() - self.epoch_started_monotonic,
            completed_turns=completed_turns,
            context_percentage=percentage,
            compact_boundary_seen=compact_boundary_seen,
            child_rss_bytes=child_rss_bytes(process),
            baseline_child_rss_bytes=self.baseline_child_rss_bytes,
        )
        if reason and self.rotation_pending is None:
            self.rotation_pending = reason
        if payload.get("soak_force_rotation") and str(payload.get("protocol")) == "soak":
            self.rotation_pending = "soak_checkpoint"
        safe_rotation_reason = self.rotation_pending if journal_id else None
        return {
            "journal_id": journal_id,
            "rotation_reason": safe_rotation_reason,
            "session_id": self.session_id,
            "session_turns": completed_turns,
            "context_percentage": percentage,
        }

    async def response_committed(self, out: dict) -> None:
        journal_id = out.get("_journal_id")
        if not journal_id:
            return
        pending_id = str(journal_id)
        self._delivery_retry_ids.add(pending_id)
        try:
            if not self.journal.mark_delivered(pending_id):
                raise RuntimeError("journal entry disappeared before delivery commit")
            self._delivery_retry_ids.discard(pending_id)
        except Exception as exc:
            self.last_error = f"journal delivery commit failed: {exc}"
            raise RuntimeError(self.last_error) from exc

    async def response_delivered(self, out: dict) -> None:
        reason = out.get("_rotation_reason")
        if not reason or self._stopping or self._delivery_retry_ids:
            return
        try:
            await self._rotate_locked(str(reason))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.fatal_error = f"brain session rotation failed: {error}"
            self.last_error = self.fatal_error
            self.fatal_event.set()

    async def response_not_delivered(self, out: dict) -> None:
        journal_id = out.get("_journal_id")
        if not journal_id:
            return
        pending_id = str(journal_id)
        self._delivery_retry_ids.discard(pending_id)
        try:
            if self.journal.delivery_committed(pending_id):
                if not self.journal.mark_transport_uncertain(pending_id):
                    self.last_error = (
                        "committed journal entry disappeared after uncertain transport: "
                        + pending_id
                    )
                return
            if not self.journal.discard(pending_id):
                self.last_error = f"undelivered journal entry disappeared: {pending_id}"
        except Exception as exc:
            self.last_error = f"undelivered journal cleanup failed: {exc}"

    async def _rotate_locked(self, reason: str) -> None:
        lock = _resident_turn_lock()
        if not lock.locked():
            raise RuntimeError("brain session rotation requires the turn lock")
        self.rotation_in_progress = True
        try:
            old_snapshot = process_tree_snapshot()
            old_token = self.process_token
            old_identities = process_identities(old_snapshot)
            if old_token:
                old_identities.extend(brain_sdk_process_identities(old_token))
            self.lifetime.end_epoch(reason)
            old_client = self.client
            self.client = None
            disconnect_error = None
            if old_client is not None:
                try:
                    await asyncio.wait_for(
                        old_client.disconnect(), timeout=SDK_DISCONNECT_TIMEOUT_SECONDS
                    )
                except Exception as exc:
                    disconnect_error = exc
            if old_token:
                old_identities.extend(brain_sdk_process_identities(old_token))
            survivors = await asyncio.wait_for(
                reap_processes(old_identities),
                timeout=SDK_REAP_TIMEOUT_SECONDS,
            )
            escaped = brain_sdk_process_identities(old_token) if old_token else []
            if escaped:
                survivors = await asyncio.wait_for(
                    reap_processes(escaped, timeout_seconds=0),
                    timeout=SDK_REAP_TIMEOUT_SECONDS,
                )
            if old_token:
                survivors = sorted(
                    {
                        *survivors,
                        *(identity.pid for identity in brain_sdk_process_identities(old_token)),
                    }
                )
            if survivors:
                raise RuntimeError(f"SDK descendants survived teardown: {survivors}")
            if disconnect_error is not None:
                raise RuntimeError(
                    f"SDK disconnect failed during rotation: {disconnect_error}"
                ) from disconnect_error
            self.process_token = None
            self.rotation_count += 1
            await self._start_epoch(reason)
        finally:
            self.rotation_in_progress = False

    async def stop(self) -> None:
        self._stopping = True
        lock = _turn_lock or asyncio.Lock()
        lock_acquired = False
        try:
            await asyncio.wait_for(
                lock.acquire(),
                timeout=TURN_CANCEL_TIMEOUT_SECONDS + INTERRUPT_TIMEOUT_SECONDS,
            )
            lock_acquired = True
        except TimeoutError:
            self.last_error = "shutdown could not acquire the resident turn lock"
        try:
            old_token = self.process_token
            client, self.client = self.client, None
            self.process_token = None
            old_identities = []
            if client is not None or old_token:
                old_identities.extend(process_identities(process_tree_snapshot()))
                if old_token:
                    old_identities.extend(brain_sdk_process_identities(old_token))
            if client is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        client.disconnect(), timeout=SDK_DISCONNECT_TIMEOUT_SECONDS
                    )
            if old_token:
                old_identities.extend(brain_sdk_process_identities(old_token))
            try:
                survivors = await asyncio.wait_for(
                    reap_processes(old_identities),
                    timeout=SDK_REAP_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                survivors = [identity.pid for identity in old_identities]
            if old_token:
                survivors = sorted(
                    {
                        *survivors,
                        *(identity.pid for identity in brain_sdk_process_identities(old_token)),
                    }
                )
            if survivors:
                self.last_error = f"SDK descendants survived shutdown: {survivors}"
            with contextlib.suppress(Exception):
                self.lifetime.end_epoch("shutdown")
        finally:
            if lock_acquired:
                lock.release()

    def snapshot(self) -> dict:
        current_process = process_tree_snapshot()
        return {
            "session_id": self.session_id,
            "process_token": self.process_token,
            "epoch_age_seconds": round(
                max(0.0, time.monotonic() - self.epoch_started_monotonic), 3
            ),
            "epoch_turns": self.epoch_turns,
            "rotations": self.rotation_count,
            "rotation_pending": self.rotation_pending,
            "rotation_in_progress": self.rotation_in_progress,
            "policy": policy_snapshot(self.policy),
            "baseline_child_rss_bytes": self.baseline_child_rss_bytes,
            "context": self.last_context,
            "process": current_process,
            "sdk_processes": brain_sdk_process_snapshot(
                self.process_token,
                known_tokens=self._known_process_tokens,
            ),
            "last_turn_process": self.last_process,
            "asyncio_tasks": _asyncio_task_count(),
            "session_store": _session_store_snapshot(self.session_id),
            "journal": self.journal.snapshot(),
            "ledger": self.lifetime.snapshot(),
            "last_error": self.last_error,
            "fatal_error": self.fatal_error,
        }


async def _run_daemon() -> None:
    global _turn_lock
    _turn_lock = asyncio.Lock()

    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    from core.brain_capability_tools import (
        CAPABILITY_TOOL_NAMES,
        capability_tools_server,
    )
    from core.brain_laptop_tools import LAPTOP_TOOL_NAMES, laptop_tools_server
    from core.brain_memory_tools import MEMORY_TOOL_NAMES, memory_tools_server
    from core.brain_work_tools import WORK_TOOL_NAMES, work_tools_server
    from core.brain_tools import BRAIN_TOOL_NAMES, brain_tools_server

    manager = ResidentClientManager(
        ClaudeAgentOptions,
        ClaudeSDKClient,
        brain_tools_server,
        BRAIN_TOOL_NAMES,
        laptop_tools_factory=laptop_tools_server,
        laptop_tool_names=LAPTOP_TOOL_NAMES,
        work_tools_factory=work_tools_server,
        work_tool_names=WORK_TOOL_NAMES,
        memory_tools_factory=memory_tools_server,
        memory_tool_names=MEMORY_TOOL_NAMES,
        capability_tools_factory=capability_tools_server,
        capability_tool_names=CAPABILITY_TOOL_NAMES,
    )
    print(f"[brain] connecting SDK client (model={MODEL})...", flush=True)
    try:
        await manager.start()
    except BaseException as exc:
        print(f"[brain] FATAL: startup failed: {type(exc).__name__}: {exc}", flush=True)
        raise
    stream_ready = asyncio.Event()
    server_tasks = [
        asyncio.create_task(_serve(manager, stream_ready), name="brain-http-server"),
        asyncio.create_task(_serve_socket(manager, stream_ready), name="brain-stream-server"),
    ]
    fatal_task = asyncio.create_task(manager.fatal_event.wait(), name="brain-lifetime-fatal")
    shutdown_event = asyncio.Event()
    shutdown_task = asyncio.create_task(shutdown_event.wait(), name="brain-shutdown-signal")
    loop = asyncio.get_running_loop()
    signal_installed = False
    if os.name != "nt" and hasattr(signal, "SIGTERM"):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
            signal_installed = True
    try:
        finished, _ = await asyncio.wait(
            {*server_tasks, fatal_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if fatal_task in finished and manager.fatal_error:
            raise RuntimeError(manager.fatal_error)
        for task in server_tasks:
            if task in finished:
                await task
    finally:
        if signal_installed:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(signal.SIGTERM)
        fatal_task.cancel()
        shutdown_task.cancel()
        for task in server_tasks:
            task.cancel()
        await asyncio.gather(
            fatal_task,
            shutdown_task,
            *server_tasks,
            return_exceptions=True,
        )
        try:
            await manager.stop()
        finally:
            with contextlib.suppress(OSError):
                BRAIN_FILE.unlink()


async def main() -> None:
    _guard_billing()
    _acquire_instance_lock()
    try:
        _guard_single_instance()
        await _run_daemon()
    finally:
        _release_instance_lock()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[brain] bye", flush=True)
