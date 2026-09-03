"""Serena front-door brain, paneless one-shot turns for the app landing.

The Serena app opens on Serena herself, not on a pane picker. She greets
with a take (pulled from ledger/tasks via the injected memory digest),
Raghav says what he's working on, and she decides whether a coding pane is
needed, and which agent(s), then the UI spawns it seeded with context.

The warm path sends the short front-door history to the resident brain daemon
over its local NDJSON stream. A stateless headless `claude -p` call remains the
cold fallback when the resident stream cannot be reached before delivery.
Both paths use the same Serena persona and compact ledger context.

Front-door sessions run with cwd under a *serena-headless* directory so
core/scanner.py never indexes them as real chats.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath

# Contains "serena-headless", the scanner skip convention (core/scanner.py).
FRONTDOOR_CWD = Path.home() / ".cache" / "serena-headless-frontdoor"

_DEFAULT_MODEL = os.environ.get("SERENA_FRONTDOOR_MODEL", "sonnet")
_TURN_TIMEOUT = 120
_STREAM_LINE_LIMIT = 1024 * 1024
_STREAM_TOTAL_LIMIT = 2 * 1024 * 1024
_STREAM_RAW_LIMIT = 1024 * 1024

_ROLE = """
# Front-door mode

You are answering as the Serena app's front door, the landing surface Raghav
sees when he opens the app, before any coding pane exists. Your job here:

1. Greet like yourself and LEAD. The greeting shape: an actual hello first,
   his name, matched to the time of day ("morning raghav", "hey you, late
   one?"), THEN one work steer pulled from the memory digest (an active
   ledger, a stale task, an overdue one). Never open-ended "what do you want
   to work on?", and keep the whole thing to 2-3 sentences. Don't open the
   visit with his heaviest personal threads (relationship, identity, family)
   uninvited. Those stay available if he brings them up, they're just not
   the greeting.
2. Talk normally until it's clear what he's working on. Answer questions
   directly yourself when no coding pane is needed, you are fully Serena
   here, not a receptionist.
3. When (and only when) he's confirmed something that needs a coding agent,
   spawn one. claude and codex are both you; pick whichever fits, both only
   if he asks for both or the work genuinely needs two hands.
4. NO tool calls at the front door. Answer from the context you already
   have; anything that needs files, commands, or lookups happens inside the
   pane you spawn. Speed is the point here.

## Reply protocol, STRICT

Reply with a single JSON object and NOTHING else. No markdown fences, no
prose around it:

{"say": "<your reply, persona voice, short>",
 "spawn": null}

or, when spawning:

{"say": "<what you tell him as you open it>",
 "spawn": {"agents": ["claude"],
           "cwd": "/absolute/path/to/project",
           "seed": "<kickoff prompt for the pane, one paragraph: the thread,"
                   " relevant ledger state, and the first concrete move>"}}

Rules for spawn: agents is ["claude"], ["codex"], or ["claude","codex"].
cwd must be an absolute path, take it from the ledger/task context or
his known project layout (~/Documents/Projects/...); use his home dir only
if genuinely unknown. seed is written as Raghav-to-Serena, it lands as the
pane's first message, and it must carry the SPECIFIC known state from the
digest (exact symptoms, root causes, file paths, what was already tried),
never a generic "dig in and find it". The seed is a BRIEFING, not a work
order: the pane orients and reports ready, it does not start executing,
Raghav kicks off the work himself in that pane (the UI enforces this with
a standing footer, so write the seed as context, not commands). Never
spawn on a guess; when unsure, ask a strict this-or-that first.
""".strip()


def _compact_active() -> str:
    """Tasks + ledgers, clipped hard. The full format_active() runs
    ~60KB and the hook recall payload runs far bigger, both made turns take
    50-70s; this stays ~5-10KB so a turn lands in seconds. Full detail lives
    one spawn away, the PANE gets the real digest via hooks, the front door
    only needs enough to steer."""
    try:
        from memory.store import LEDGER_FIELDS, _is_snoozed, _scan_all  # noqa: F401
    except Exception:
        return ""

    def clip(s: str, n: int = 140) -> str:
        s = " ".join((s or "").split())
        return s if len(s) <= n else s[: n - 1] + "…"

    tasks, ledgers = [], []
    for m in _scan_all():
        if _is_snoozed(m):
            continue
        if m["type"] == "task":
            tasks.append(f"- [{m['id']}] {clip(m['content'])}")
        elif m["type"] == "ledger":
            ledgers.append(
                f"- {m.get('ledger_key', '?')}: goal={clip(m.get('goal', ''), 100)}"
                f" | next={clip(m.get('next_action', ''), 100)}"
            )
    out = []
    if ledgers:
        out.append("# Active ledgers (live thread state)\n" + "\n".join(ledgers))
    if tasks:
        out.append("# His open tasks\n" + "\n".join(tasks[:15]))
    return "\n\n".join(out)


def _context() -> str:
    """Persona + Tooling + compact steer digest + knowledge index + role.
    Deliberately NOT the mobile daemon's full 291KB context, and the calls
    run with SERENA_FRONTDOOR=1 so the memory/recall hooks skip injection
    (measured 2026-07-14: full context + hooks = 50-70s/turn; this = ~10s)."""
    parts = []
    try:
        from core.config import read_agent_context
        parts.append(read_agent_context())  # Persona.md + Tooling.md
    except Exception:
        pass
    parts.append(_compact_active())
    try:
        from core.chat_daemon import _knowledge_index
        parts.append(_knowledge_index())
    except Exception:
        pass
    parts.append(_ROLE)
    return "\n\n".join(p for p in parts if p.strip()).strip()


def _render_history(history: list[dict]) -> str:
    """Serialize this visit's exchanges for the stateless per-turn prompt."""
    lines = []
    for m in history:
        who = "Raghav" if (m.get("role") or "") == "user" else "You (Serena)"
        text = (m.get("text") or "").strip()
        if text:
            lines.append(f"{who}: {text}")
    return "\n".join(lines)


def _frontdoor_prompt(history: list[dict]) -> str:
    convo = _render_history(history or [])
    if convo:
        return (
            f"Front-door conversation so far this visit:\n\n{convo}\n\n"
            "Reply to his last message per the front-door protocol."
        )
    return "Raghav just opened the Serena app. Greet him per the front-door protocol."


def _latest_user_query(history: list[dict]) -> str:
    for message in reversed(history or []):
        if str(message.get("role") or "") == "user":
            return str(message.get("text") or "").strip()
    return ""


def _recent_user_queries(history: list[dict], *, limit: int = 4) -> tuple[str, ...]:
    """Bounded user-only context before the current front-door query."""

    bounded_limit = max(0, min(4, int(limit)))
    if not bounded_limit:
        return ()
    users = [
        " ".join(str(message.get("text") or "").split())[:700]
        for message in history or []
        if str(message.get("role") or "") == "user"
        and str(message.get("text") or "").strip()
    ]
    return tuple(users[:-1][-bounded_limit:])


def _frontdoor_memory_context(history: list[dict]) -> str:
    try:
        from memory.retrieval import pack_memory_context

        return pack_memory_context(
            _latest_user_query(history),
            surface="frontdoor",
            recent_context=_recent_user_queries(history),
            max_characters=3_500,
            max_tokens=900,
            max_records=4,
        ).text
    except Exception:
        return ""


def _parse_reply(raw: str) -> dict:
    """Parse the strict-JSON reply, tolerating fences and stray prose."""
    raw = (raw or "").strip()
    candidates = [raw]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1))
    brace = re.search(r"\{.*\}", raw, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for c in candidates:
        try:
            obj = json.loads(c)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict) and "say" in obj:
            spawn = obj.get("spawn")
            if spawn is not None and not _valid_spawn(spawn):
                spawn = None
            return {"say": str(obj.get("say") or "").strip(), "spawn": spawn}
    # Model broke protocol, surface its text rather than nothing.
    return {"say": raw, "spawn": None}


def _valid_spawn(spawn) -> bool:
    if not isinstance(spawn, dict):
        return False
    agents = spawn.get("agents")
    if not isinstance(agents, list) or tuple(agents) not in {
        ("claude",),
        ("codex",),
        ("claude", "codex"),
    }:
        return False
    cwd = spawn.get("cwd") or ""
    if not isinstance(cwd, str) or not (
        Path(cwd).is_absolute() or PureWindowsPath(cwd).is_absolute()
    ):
        return False
    if not os.path.isdir(cwd):
        return False
    if not _spawn_cwd_allowed(cwd):
        return False
    seed = spawn.get("seed")
    if not isinstance(seed, str) or not seed.strip():
        return False
    spawn["seed"] = seed.strip()
    return True


def _spawn_cwd_allowed(cwd: str) -> bool:
    """Keep model-selected panes in the home or known project tree."""

    path = Path(cwd)
    if path.is_absolute():
        try:
            candidate = path.resolve(strict=True)
            home = Path.home().resolve(strict=True)
            projects = (home / "Documents" / "Projects").resolve(strict=False)
        except OSError:
            return False
        return candidate in (home, projects) or projects in candidate.parents

    windows = PureWindowsPath(cwd)
    parts = windows.parts
    if (
        not windows.is_absolute()
        or len(parts) < 3
        or parts[1].casefold() != "users"
        or any(part == ".." for part in parts)
    ):
        return False
    tail = tuple(part.casefold() for part in parts[3:])
    return not tail or tail[0] == "projects" or tail[:2] == (
        "documents",
        "projects",
    )


GREETING_CACHE = FRONTDOOR_CWD / "greeting-cache.json"
_GREETING_FRESH_SECS = 15 * 60
_GREETING_WARM_CONDITION = threading.Condition()
_GREETING_WARMING = False


def _cached_greeting() -> dict | None:
    try:
        obj = json.loads(GREETING_CACHE.read_text(encoding="utf-8"))
        if time.time() - float(obj.get("ts", 0)) < _GREETING_FRESH_SECS and obj.get("say"):
            return {"ok": True, "say": obj["say"], "spawn": None, "error": ""}
    except (OSError, ValueError):
        pass
    return None


def _store_greeting(say: str) -> None:
    say = str(say or "").strip()
    if not say:
        return
    try:
        GREETING_CACHE.parent.mkdir(parents=True, exist_ok=True)
        GREETING_CACHE.write_text(
            json.dumps({"say": say, "ts": time.time()}),
            encoding="utf-8",
        )
    except OSError:
        pass


def warm_greeting(block: bool = False, delay_seconds: float = 0.0) -> None:
    """Optional prewarm for clients that request a generated greeting.

    The current GTK front door uses an instant local time banner and does not
    call this path. It remains available to other local clients explicitly.
    """
    global _GREETING_WARMING

    with _GREETING_WARM_CONDITION:
        if _GREETING_WARMING:
            if not block:
                return
            while _GREETING_WARMING:
                _GREETING_WARM_CONDITION.wait()
            return
        _GREETING_WARMING = True

    def _work() -> None:
        global _GREETING_WARMING
        try:
            r = turn([], _skip_greeting_cache=True)
            if r.get("ok") and r.get("say"):
                _store_greeting(r["say"])
        finally:
            with _GREETING_WARM_CONDITION:
                _GREETING_WARMING = False
                _GREETING_WARM_CONDITION.notify_all()

    if block:
        _work()
        return
    timer = threading.Timer(max(0.0, float(delay_seconds)), _work)
    timer.name = "serena-frontdoor-greeting"
    timer.daemon = True
    timer.start()


BRAIN_FILE = Path.home() / ".config" / "serena" / "brain.json"


def _brain_turn(history: list[dict]) -> dict | None:
    """Route the turn through the resident brain daemon when it's alive.
    Returns None when the daemon is down/unreachable (caller falls back to
    the cold claude -p path). ~3s grounded replies vs ~5s cold."""
    import urllib.request
    try:
        info = json.loads(BRAIN_FILE.read_text(encoding="utf-8"))
        port = int(info["port"])
        token = str(info.get("token") or "")
    except (OSError, ValueError, KeyError):
        return None
    text = _frontdoor_prompt(history)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/turn",
            data=json.dumps(
                {
                    "text": text,
                    "memory_query": _latest_user_query(history),
                    "recent_context": list(_recent_user_queries(history)),
                    "protocol": "frontdoor",
                }
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            }, method="POST")
        resp = json.loads(urllib.request.urlopen(req, timeout=90).read())
    except Exception:
        return None
    if not resp.get("ok") or not (resp.get("say") or resp.get("spawn")):
        return None
    return {"ok": True, "say": resp.get("say", ""), "spawn": resp.get("spawn"),
            "error": "", "via": "brain"}


class _BrainStreamUnavailable(RuntimeError):
    """The resident stream could not be reached before a turn was sent."""


class _BrainStreamError(RuntimeError):
    """A resident stream failed after delivery became possible."""


def _json_string_prefix(value: str) -> tuple[str, bool]:
    """Return the complete encoded prefix of one partial JSON string value."""

    index = 0
    safe_end = 0
    while index < len(value):
        char = value[index]
        if char == '"':
            return value[:safe_end], True
        if char == "\\":
            if index + 1 >= len(value):
                break
            escape = value[index + 1]
            if escape == "u":
                end = index + 6
                if end > len(value):
                    break
                digits = value[index + 2 : end]
                if len(digits) != 4 or any(
                    digit not in "0123456789abcdefABCDEF" for digit in digits
                ):
                    raise _BrainStreamError("front-door stream contains invalid JSON escape")
                code_point = int(digits, 16)
                if 0xD800 <= code_point <= 0xDBFF:
                    tail = value[end:]
                    if not tail:
                        break
                    if tail[0] == "\\":
                        if len(tail) < 2:
                            break
                        if tail[1] == "u":
                            pair_end = end + 6
                            if pair_end > len(value):
                                break
                            pair_digits = value[end + 2 : pair_end]
                            if any(
                                digit not in "0123456789abcdefABCDEF"
                                for digit in pair_digits
                            ):
                                raise _BrainStreamError(
                                    "front-door stream contains invalid JSON escape"
                                )
                            pair_point = int(pair_digits, 16)
                            index = (
                                pair_end
                                if 0xDC00 <= pair_point <= 0xDFFF
                                else end
                            )
                        else:
                            index = end
                    else:
                        index = end
                else:
                    index = end
            elif escape in '"\\/bfnrt':
                index += 2
            else:
                raise _BrainStreamError("front-door stream contains invalid JSON escape")
        else:
            if ord(char) < 0x20:
                raise _BrainStreamError("front-door stream contains invalid JSON text")
            index += 1
        safe_end = index
    return value[:safe_end], False


def _top_level_say_value_start(raw: str) -> int | None:
    """Locate a direct ``say`` member without matching nested JSON keys."""

    decoder = json.JSONDecoder()

    def skip_space(index: int) -> int:
        while index < len(raw) and raw[index] in " \t\r\n":
            index += 1
        return index

    index = skip_space(0)
    if index >= len(raw) or raw[index] != "{":
        return None
    index += 1
    while True:
        index = skip_space(index)
        if index >= len(raw) or raw[index] == "}":
            return None
        try:
            key, index = decoder.raw_decode(raw, index)
        except json.JSONDecodeError:
            return None
        if not isinstance(key, str):
            return None
        index = skip_space(index)
        if index >= len(raw) or raw[index] != ":":
            return None
        index = skip_space(index + 1)
        if index >= len(raw):
            return None
        if key == "say":
            return index + 1 if raw[index] == '"' else None
        try:
            _, index = decoder.raw_decode(raw, index)
        except json.JSONDecodeError:
            return None
        index = skip_space(index)
        if index >= len(raw):
            return None
        if raw[index] != ",":
            return None
        index += 1


class _SayDeltaDecoder:
    """Extract the top-level say string while strict front-door JSON arrives."""

    def __init__(self) -> None:
        self._raw = ""
        self._value_start: int | None = None
        self.decoded = ""
        self.complete = False

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._raw += chunk
        if len(self._raw) > _STREAM_RAW_LIMIT:
            raise _BrainStreamError("front-door stream exceeded its JSON limit")
        if self.complete:
            return ""
        if self._value_start is None:
            self._value_start = _top_level_say_value_start(self._raw)
            if self._value_start is None:
                return ""
        encoded, self.complete = _json_string_prefix(
            self._raw[self._value_start :]
        )
        try:
            decoded = json.loads(f'"{encoded}"')
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _BrainStreamError("front-door stream contains invalid say JSON") from exc
        if not isinstance(decoded, str) or not decoded.startswith(self.decoded):
            raise _BrainStreamError("front-door stream rewrote emitted text")
        delta = decoded[len(self.decoded) :]
        self.decoded = decoded
        return delta


def _open_brain_stream() -> tuple[socket.socket, str | None, str]:
    connection: socket.socket | None = None
    try:
        info = json.loads(BRAIN_FILE.read_text(encoding="utf-8"))
        stream = info["stream"]
        if not isinstance(stream, dict):
            raise ValueError("stream discovery is not an object")
        transport = str(stream.get("transport") or "")
        if transport == "unix":
            path = str(stream.get("path") or "")
            expected_path = BRAIN_FILE.with_name("brain.sock")
            if not path or Path(path) != expected_path:
                raise ValueError("brain Unix stream path is invalid")
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            address: str | tuple[str, int] = path
            token = None
            backend = "brain.sock"
        elif transport == "tcp":
            host = str(stream.get("host") or "")
            if host != "127.0.0.1":
                raise ValueError("brain TCP stream is not literal loopback")
            port = int(stream["port"])
            token = str(stream.get("token") or "")
            if not token:
                raise ValueError("brain TCP stream has no token")
            connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            address = (host, port)
            backend = "brain.tcp"
        else:
            raise ValueError("brain discovery has no supported stream")
        connection.settimeout(float(_TURN_TIMEOUT))
        connection.connect(address)
        return connection, token, backend
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        if connection is not None:
            with contextlib.suppress(OSError):
                connection.close()
        raise _BrainStreamUnavailable("resident brain stream is unavailable") from exc


def _resident_stream_turn(history: list[dict]) -> Iterator[dict]:
    connection, token, backend = _open_brain_stream()
    request_id = uuid.uuid4().hex
    request = {
        "type": "turn",
        "request_id": request_id,
        "protocol": "frontdoor",
        "text": _frontdoor_prompt(history),
        "memory_query": _latest_user_query(history),
        "recent_context": list(_recent_user_queries(history)),
        "stream": True,
    }
    if token:
        request["auth"] = token
    decoder = _SayDeltaDecoder()
    started = False
    delivered = False
    deadline = time.monotonic() + _TURN_TIMEOUT
    try:
        # Once sendall begins, the daemon may receive a complete line even if
        # the local call later raises. Never duplicate that turn via fallback.
        delivered = True
        connection.sendall(
            (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
        )
        with connection.makefile("rb") as source:
            total_bytes = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _BrainStreamError("resident brain stream timed out")
                set_timeout = getattr(connection, "settimeout", None)
                if callable(set_timeout):
                    set_timeout(remaining)
                line = source.readline(_STREAM_LINE_LIMIT + 1)
                if not line:
                    raise _BrainStreamError("resident brain closed before response.done")
                if len(line) > _STREAM_LINE_LIMIT:
                    raise _BrainStreamError("resident brain response line is too large")
                total_bytes += len(line)
                if total_bytes > _STREAM_TOTAL_LIMIT:
                    raise _BrainStreamError("resident brain response is too large")
                try:
                    payload = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise _BrainStreamError("resident brain returned malformed NDJSON") from exc
                if not isinstance(payload, dict):
                    raise _BrainStreamError("resident brain response is not an object")
                if payload.get("request_id") != request_id:
                    raise _BrainStreamError(
                        "resident brain response request_id does not match"
                    )
                kind = payload.get("type")
                if kind == "error":
                    raise _BrainStreamError(
                        str(payload.get("error") or "resident brain turn failed")
                    )
                if kind == "response.start":
                    if started:
                        raise _BrainStreamError("resident brain sent duplicate start")
                    started = True
                    yield {
                        "type": "start",
                        "request_id": request_id,
                        "backend": backend,
                    }
                    continue
                if not started:
                    raise _BrainStreamError("resident brain sent output before start")
                if kind == "response.delta":
                    raw_delta = payload.get("delta")
                    if not isinstance(raw_delta, str):
                        raise _BrainStreamError("resident brain delta is not text")
                    say_delta = decoder.feed(raw_delta)
                    if say_delta:
                        yield {"type": "delta", "delta": say_delta}
                    continue
                if kind != "response.done":
                    raise _BrainStreamError(
                        f"resident brain returned unknown type {kind!r}"
                    )
                if "say" not in payload:
                    raise _BrainStreamError("resident brain final reply has no say")
                say = payload["say"]
                if not isinstance(say, str):
                    raise _BrainStreamError("resident brain final say is not text")
                if say.startswith(decoder.decoded):
                    remaining = say[len(decoder.decoded) :]
                    if remaining:
                        yield {"type": "delta", "delta": remaining}
                elif say != decoder.decoded:
                    yield {"type": "replace", "say": say}
                spawn = payload.get("spawn")
                if spawn is not None and not _valid_spawn(spawn):
                    raise _BrainStreamError("resident brain final spawn is invalid")
                yield {
                    "type": "done",
                    "say": say,
                    "spawn": spawn,
                    "meta": payload.get("meta")
                    if isinstance(payload.get("meta"), dict)
                    else {},
                    "backend": backend,
                }
                return
    except _BrainStreamError:
        raise
    except (OSError, TimeoutError) as exc:
        if delivered:
            raise _BrainStreamError("resident brain stream failed mid-turn") from exc
        raise _BrainStreamUnavailable("resident brain stream is unavailable") from exc
    finally:
        with contextlib.suppress(OSError):
            connection.close()


def stream_turn(history: list[dict]) -> Iterator[dict]:
    """Stream visible front-door text, then deliver the exact final spawn."""

    if not history:
        cached = _cached_greeting()
        if cached:
            say = cached["say"]
            yield {"type": "start", "backend": "greeting-cache"}
            yield {"type": "delta", "delta": say}
            yield {
                "type": "done",
                "say": say,
                "spawn": None,
                "meta": {"first_delta": 0.0},
                "backend": "greeting-cache",
            }
            return
    try:
        resident = _resident_stream_turn(history)
        try:
            for event in resident:
                if not history and event.get("type") == "done" and event.get("say"):
                    _store_greeting(str(event["say"]))
                yield event
        finally:
            close = getattr(resident, "close", None)
            if callable(close):
                close()
        return
    except _BrainStreamUnavailable:
        yield {"type": "start", "backend": "nonstream-fallback"}
        result = turn(history, _skip_greeting_cache=True)
        if not result.get("ok"):
            yield {
                "type": "error",
                "error": str(result.get("error") or "front-door turn failed"),
            }
            return
        say = str(result.get("say") or "")
        if say:
            yield {"type": "delta", "delta": say}
        if not history and say:
            _store_greeting(say)
        yield {
            "type": "done",
            "say": say,
            "spawn": result.get("spawn"),
            "meta": {},
            "backend": str(result.get("via") or "nonstream-fallback"),
        }
    except _BrainStreamError as exc:
        yield {"type": "error", "error": str(exc)}


def turn(history: list[dict], model: str = "", _skip_greeting_cache: bool = False) -> dict:
    """One front-door turn. Empty history = Raghav just opened the app
    (greeting turn). Returns {ok, say, spawn, error}."""
    # Greeting turns serve from cache when fresh. Explicit boot/idle work may
    # refresh it through warm_greeting without competing with a live turn.
    if not history and not _skip_greeting_cache:
        cached = _cached_greeting()
        if cached:
            return cached

    # Resident brain first, cold claude -p only as fallback (spec v1).
    routed = _brain_turn(history)
    if routed is not None:
        if not history and routed.get("say"):
            _store_greeting(routed["say"])
        return routed

    claude_bin = shutil.which("claude") or "claude"
    FRONTDOOR_CWD.mkdir(parents=True, exist_ok=True)
    # The full context (~300KB) blows the exec argv limit as a flag, so stage
    # it as this cwd's CLAUDE.md, claude -p loads that from disk on its own.
    (FRONTDOOR_CWD / "CLAUDE.md").write_text(_context(), encoding="utf-8")

    now = time.strftime("%A %H:%M")
    convo = _render_history(history or [])
    if convo:
        prompt = (
            f"It's {now}. Front-door conversation so far this visit:\n\n"
            f"{convo}\n\n"
            "Reply to his last message per the front-door protocol."
        )
    else:
        prompt = (
            f"It's {now}. Raghav just opened the Serena app. Greet him per "
            "the front-door protocol: hello with his name matched to the "
            "time of day, then your take on what's live."
        )
    memory_context = _frontdoor_memory_context(history)
    if memory_context:
        prompt = memory_context + "\n\n" + prompt

    cmd = [
        claude_bin, "-p", prompt,
        "--model", model or _DEFAULT_MODEL,
        "--output-format", "text",
        # Front-door turns don't use tools, so don't pay for 7 MCP server
        # connects; low effort keeps the model from deliberating on what is
        # a short routing reply. Measured 2026-07-14: 14-30s -> ~5s/turn.
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--settings", '{"effortLevel":"low"}',
    ]
    env = dict(os.environ)
    env["SERENA_FRONTDOOR"] = "1"  # hook scripts skip injection on this
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_TURN_TIMEOUT, cwd=str(FRONTDOOR_CWD), env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "say": "", "spawn": None,
                "error": f"front-door turn timed out after {_TURN_TIMEOUT}s"}
    except OSError as e:
        return {"ok": False, "say": "", "spawn": None, "error": str(e)}

    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[-500:]
        return {"ok": False, "say": "", "spawn": None,
                "error": f"claude -p exited {proc.returncode}: {err}"}

    parsed = _parse_reply(proc.stdout)
    if not parsed["say"] and not parsed["spawn"]:
        return {"ok": False, "say": "", "spawn": None, "error": "empty reply"}
    result = {
        "ok": True,
        "say": parsed["say"],
        "spawn": parsed["spawn"],
        "error": "",
    }
    if not history and result["say"]:
        _store_greeting(result["say"])
    return result
