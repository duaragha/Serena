"""Cross-agent talk: let claude (or any caller) drive an already-running
codex terminal in Serena's split view, instead of spawning a fresh codex MCP
session every consultation.

Flow:
  1. Caller posts {target_sid, prompt} → /api/codex-bridge
  2. Endpoint locates the xterm PTY or native VTE for target_sid
  3. Wakes that runtime and submits through its terminal backend
  4. Polls the codex JSONL for the resulting agent_message
  5. Returns the response text

Notes on response detection:
  Codex writes events to its rollout .jsonl as it goes. The terminal turn
  marker is `event_msg` with payload.type=task_complete. We snapshot the
  file's line count BEFORE feeding, then read forward and collect
  agent_message text until task_complete (or timeout).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"

_DEFAULT_TIMEOUT = 300.0  # seconds — codex turns can be long
_POLL_INTERVAL = 0.5


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def _wait_for_gtk_vte(inst, target_sid: str, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        vte = inst._vtes.get(target_sid)
        if vte is not None:
            pid = inst._vte_pids.get(target_sid)
            if _pid_alive(pid):
                return vte
        time.sleep(0.1)
    return None


def _gtk_resume_target(inst, target_sid: str) -> tuple[bool, str]:
    """Mount/resume a linked codex session in the GTK app before feeding it."""
    reservation_lookup = getattr(inst, "runtime_work_reservation", None)
    if reservation_lookup is not None:
        item_id = reservation_lookup(target_sid)
        if item_id:
            return False, f"runtime reserved by work item {item_id}"
    try:
        from gi.repository import GLib  # type: ignore
    except ImportError:
        return False, "GTK shell not available"

    done = threading.Event()
    err: list[str] = []
    was_alive = _pid_alive(inst._vte_pids.get(target_sid))

    rect = getattr(inst, "_vte_rect", None)
    payload = {"sid": target_sid}
    if rect:
        x, y, w, h = rect
        payload["rect"] = {"x": x, "y": y, "w": w, "h": h}

    def _do_show():
        try:
            if target_sid in inst._vtes:
                inst._wake_runtime(target_sid, "bridge", focus=False)
            else:
                inst._show_session(payload)
        except Exception as e:
            err.append(str(e))
        finally:
            done.set()
        return False

    GLib.idle_add(_do_show)
    done.wait(2.0)
    if err:
        return False, f"resume failed: {err[-1]}"
    vte = _wait_for_gtk_vte(inst, target_sid, timeout=10.0)
    if vte is None:
        return False, f"No live terminal for {target_sid[:8]}"

    inst.runtime_begin_turn(target_sid)
    if not was_alive:
        # A cold process still needs its one-time TUI initialization. Hot
        # standby resumes are ready immediately and skip this delay entirely.
        time.sleep(1.0)
    return True, "resumed"


def find_codex_jsonl(session_id: str) -> Path | None:
    """Locate the codex rollout file for a session id. Codex stores them
    under YYYY/MM/DD subdirs so we glob across all of them."""
    if not CODEX_SESSIONS_ROOT.exists():
        return None
    pattern = f"rollout-*-{session_id}.jsonl"
    matches = list(CODEX_SESSIONS_ROOT.rglob(pattern))
    if not matches:
        # Some installations might not include the suffix structure we expect
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def _gtk_feed(target_sid: str, text: str) -> tuple[bool, str]:
    """Schedule a feed_child on the GTK main thread. Returns (ok, message).

    Codex's TUI treats fast bursts as a paste — Enter inside a paste doesn't
    submit. So we wrap the body in bracketed-paste markers, then send a bare
    \\r as a separate keypress 200ms later to trigger submit.
    """
    try:
        from gi.repository import GLib  # type: ignore

        from desktop.app_gtk import ChatsApp
    except ImportError:
        return False, "GTK shell not available"
    inst = ChatsApp.INSTANCE
    if inst is None:
        return False, "GTK window not initialized"
    if not getattr(inst, "_use_native_vte", True):
        return False, "GTK shell uses the xterm backend"
    ok, msg = _gtk_resume_target(inst, target_sid)
    if not ok:
        return False, msg
    vte = _wait_for_gtk_vte(inst, target_sid, timeout=2.0)
    if vte is None:
        return False, f"No live terminal for {target_sid[:8]}"

    # Bracketed paste: ESC[200~ ... ESC[201~ — codex enables it for multi-line
    # input handling. Anything between markers is treated as text, not keys.
    body = b"\x1b[200~" + text.encode("utf-8") + b"\x1b[201~"

    def _do_paste():
        try:
            vte.feed_child(body)
        except Exception as e:
            print(f"[codex-bridge] paste failed: {e}", flush=True)
        return False

    def _do_submit():
        try:
            vte.feed_child(b"\r")
        except Exception as e:
            print(f"[codex-bridge] submit failed: {e}", flush=True)
        return False

    GLib.idle_add(_do_paste)
    GLib.timeout_add(200, _do_submit)
    return True, "queued"


def _pty_feed(target_sid: str, text: str) -> tuple[bool, str]:
    """Feed the prompt into the PTY owned by the xterm web terminal."""
    try:
        from ui import pty_terminal
    except ImportError:
        return False, "pty_terminal unavailable"
    tid = pty_terminal.tid_for_session(target_sid)
    if not tid:
        return False, (
            f"No live terminal for {target_sid[:8]} — open the chat's Live "
            "view in Serena first"
        )
    item_id = pty_terminal.reservation(tid)
    if item_id:
        return False, f"runtime reserved by work item {item_id}"
    if not pty_terminal.resume(tid):
        return False, "PTY resume failed"
    jsonl = find_codex_jsonl(target_sid)
    version = None
    if jsonl:
        try:
            stat = jsonl.stat()
            version = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            pass
    pty_terminal.mark_turn_started(tid, version)
    body = b"\x1b[200~" + text.encode("utf-8") + b"\x1b[201~"
    if not pty_terminal.write(tid, body):
        return False, "PTY write failed"
    time.sleep(0.25)
    if not pty_terminal.write(tid, b"\r"):
        return False, "PTY submit failed"
    return True, "queued (pty)"


def _feed(target_sid: str, text: str) -> tuple[bool, str]:
    """Feed native VTE when enabled, otherwise use the xterm PTY."""
    ok, msg = _gtk_feed(target_sid, text)
    if ok:
        return ok, msg
    ok2, msg2 = _pty_feed(target_sid, text)
    return (ok2, msg2) if ok2 else (False, f"{msg}; {msg2}")


def _read_jsonl_lines(path: Path, start_index: int) -> list[dict]:
    """Read JSONL lines starting at line index `start_index`. Each line is
    parsed as JSON; bad lines are skipped."""
    out: list[dict] = []
    if not path.exists():
        return out
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i < start_index:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _strip_trailing_blocks(text: str) -> str:
    # Codex sometimes wraps its replies with terminal-control sequences when
    # streaming through the VTE. The JSONL itself is clean text, but defend
    # against stray escapes anyway.
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text).strip()


def _collect_response(jsonl: Path, start_index: int, timeout: float) -> dict[str, Any]:
    """Wait for a complete codex turn after feeding our prompt. Returns
    {ok, response, events_seen, message}."""
    deadline = time.time() + timeout
    saw_user_msg = False
    response_parts: list[str] = []
    last_seen = start_index
    finished_reason = None

    while time.time() < deadline:
        events = _read_jsonl_lines(jsonl, last_seen)
        for ev in events:
            last_seen += 1
            if ev.get("type") != "event_msg":
                continue
            payload = ev.get("payload") or {}
            kind = payload.get("type")
            if kind == "user_message":
                saw_user_msg = True
            elif kind in ("agent_message", "assistant_message"):
                text = payload.get("message") or payload.get("text") or ""
                if isinstance(text, str) and text.strip():
                    response_parts.append(_strip_trailing_blocks(text))
            elif kind in ("task_complete", "task_completed"):
                finished_reason = "complete"
                break
            elif kind == "error":
                finished_reason = "error"
                response_parts.append(f"[codex error] {payload.get('message', '')}")
                break
        if finished_reason and saw_user_msg:
            break
        time.sleep(_POLL_INTERVAL)

    if not saw_user_msg:
        return {
            "ok": False,
            "response": "",
            "message": "Prompt didn't reach the codex session (user_message never appeared in JSONL)",
        }

    if finished_reason is None:
        return {
            "ok": False,
            "response": "\n\n".join(response_parts),
            "message": f"Timed out after {timeout:.0f}s waiting for task_complete",
        }

    return {
        "ok": True,
        "response": "\n\n".join(response_parts) or "[codex returned no text]",
        "message": f"finished ({finished_reason})",
    }


def call_codex_via_bridge(target_sid: str, prompt: str, timeout: float = _DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Send `prompt` into the running codex VTE for `target_sid` and wait
    for the response.

    Returns: {ok: bool, response: str, message: str}
    """
    if not target_sid:
        return {"ok": False, "response": "", "message": "target_sid required"}
    if not prompt or not prompt.strip():
        return {"ok": False, "response": "", "message": "prompt required"}

    jsonl = find_codex_jsonl(target_sid)
    if jsonl is None:
        return {
            "ok": False,
            "response": "",
            "message": f"No codex JSONL found for {target_sid[:8]} — is it a codex session?",
        }

    lease_inst = None
    try:
        from desktop.app_gtk import ChatsApp

        lease_inst = ChatsApp.INSTANCE
        if lease_inst is not None:
            lease_inst.runtime_acquire_lease(target_sid)
    except ImportError:
        lease_inst = None

    try:
        start_index = _line_count(jsonl)
        ok, msg = _feed(target_sid, prompt)
        if not ok:
            return {"ok": False, "response": "", "message": msg}
        result = _collect_response(jsonl, start_index, timeout)
        if result.get("ok") and lease_inst is not None:
            lease_inst.notify_runtime_turn_finished(target_sid)
        return result
    finally:
        if lease_inst is not None:
            lease_inst.runtime_release_lease(target_sid)


# ---------------------------------------------------------------------------
# Accepted voice-work dispatch
# ---------------------------------------------------------------------------

_WORK_FINISHED = {
    "task_complete",
    "task_completed",
    "task_cancelled",
    "turn_aborted",
    "turn_cancelled",
}


def _work_result(
    *,
    ok: bool,
    message: str,
    response: str = "",
    committed: bool = False,
    start_offset: int | None = None,
    end_offset: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    result = {
        "ok": ok,
        "response": response,
        "message": message,
        "committed": committed,
        "start_offset": start_offset,
        "end_offset": end_offset,
    }
    result.update(extra)
    return result


def _unsafe_work_metadata(target_sid: str) -> str | None:
    try:
        from core import metadata

        if metadata.external_runtime_active(target_sid):
            return "runtime has an external owner"
        meta = metadata.get_meta(target_sid)
        if meta.get("done"):
            return "runtime is marked done"
        marker = meta.get("fleet_worker")
        if isinstance(marker, dict) and marker.get("run_id"):
            return "fleet worker runtimes are read-only"
    except Exception as error:
        return f"runtime metadata unavailable: {error}"
    return None


def _gtk_owner_call(inst, method: str, *args, timeout: float = 3.0):
    """Run an owner mutation on GTK's main thread and return its value."""
    from gi.repository import GLib  # type: ignore

    done = threading.Event()
    values: list[Any] = []
    errors: list[BaseException] = []

    def invoke():
        try:
            values.append(getattr(inst, method)(*args))
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()
        return False

    GLib.idle_add(invoke)
    if not done.wait(timeout):
        raise TimeoutError(f"GTK owner did not run {method}")
    if errors:
        raise RuntimeError(str(errors[-1]))
    return values[0] if values else None


def _native_owner(target_sid: str):
    module = sys.modules.get("desktop.app_gtk")
    if module is None:
        return None
    ChatsApp = getattr(module, "ChatsApp", None)
    if ChatsApp is None:
        return None
    inst = ChatsApp.INSTANCE
    if inst is None or not getattr(inst, "_use_native_vte", True):
        return None
    if target_sid not in getattr(inst, "_vtes", {}):
        return None
    if not _pid_alive(getattr(inst, "_vte_pids", {}).get(target_sid)):
        return None
    return inst


def _acquire_work_owner(target_sid: str, item_id: str) -> tuple[dict | None, str]:
    """Reserve the one existing owner without spawning or resuming elsewhere."""
    unsafe = _unsafe_work_metadata(target_sid)
    if unsafe:
        return None, unsafe

    inst = _native_owner(target_sid)
    if inst is not None:
        try:
            ok, message = _gtk_owner_call(
                inst, "runtime_reserve_work", target_sid, item_id
            )
        except Exception as error:
            return None, f"GTK reservation failed: {error}"
        if not ok:
            return None, message
        unsafe = _unsafe_work_metadata(target_sid)
        if unsafe:
            _gtk_owner_call(inst, "runtime_release_work", target_sid, item_id)
            return None, unsafe
        return {"kind": "gtk", "instance": inst, "sid": target_sid}, "reserved"

    try:
        from core.runtime_activity import TurnActivityReader
        from ui import pty_terminal
    except ImportError as error:
        return None, f"terminal owner unavailable: {error}"
    tid = pty_terminal.tid_for_session(target_sid)
    if not tid:
        return None, "target has no live GTK or xterm owner"
    jsonl = find_codex_jsonl(target_sid)
    if jsonl is not None and TurnActivityReader().read(jsonl, "codex") is True:
        return None, "runtime has an active turn"
    ok, message = pty_terminal.reserve_work(tid, item_id)
    if not ok:
        return None, message
    unsafe = _unsafe_work_metadata(target_sid)
    if unsafe:
        pty_terminal.release_work(tid, item_id)
        return None, unsafe
    return {"kind": "pty", "tid": tid, "sid": target_sid}, "reserved"


def _release_work_owner(owner: dict, item_id: str) -> bool:
    if owner.get("kind") == "gtk":
        try:
            return bool(
                _gtk_owner_call(
                    owner["instance"], "runtime_release_work", owner["sid"], item_id
                )
            )
        except Exception:
            return False
    from ui import pty_terminal

    return pty_terminal.release_work(owner["tid"], item_id)


def _owner_alive(owner: dict) -> bool:
    if owner.get("kind") == "gtk":
        inst = owner["instance"]
        return _pid_alive(getattr(inst, "_vte_pids", {}).get(owner["sid"]))
    from ui import pty_terminal

    return pty_terminal.is_alive(owner["tid"])


def _feed_gtk_work(owner: dict, prompt: str, item_id: str) -> tuple[bool, str, bool]:
    """Feed from GTK's main thread. The bool tail reports a partial write."""
    from gi.repository import GLib  # type: ignore

    inst = owner["instance"]
    target_sid = owner["sid"]
    done = threading.Event()
    result: list[tuple[bool, str, bool]] = []
    partial_written = threading.Event()
    body = b"\x1b[200~" + prompt.encode("utf-8") + b"\x1b[201~"

    def submit(vte):
        try:
            if inst.runtime_work_reservation(target_sid) != item_id:
                result.append((False, "work reservation was lost before submit", True))
            else:
                vte.feed_child(b"\r")
                inst.runtime_begin_turn(target_sid)
                result.append((True, "queued (gtk work owner)", True))
        except Exception as error:
            with suppress(Exception):
                vte.feed_child(b"\x03")
            result.append((False, f"GTK submit failed: {error}", True))
        finally:
            done.set()
        return False

    def paste():
        try:
            if inst.runtime_work_reservation(target_sid) != item_id:
                result.append((False, "work reservation was lost before feed", False))
                done.set()
                return False
            inst._wake_runtime(target_sid, "voice-work", focus=False)
            vte = inst._vtes.get(target_sid)
            pid = inst._vte_pids.get(target_sid)
            if vte is None or not _pid_alive(pid):
                result.append((False, "GTK runtime stopped before feed", False))
                done.set()
                return False
            vte.feed_child(body)
            partial_written.set()
            GLib.timeout_add(200, submit, vte)
        except Exception as error:
            result.append((False, f"GTK feed failed: {error}", False))
            done.set()
        return False

    GLib.idle_add(paste)
    if not done.wait(3.0):
        return False, "GTK feed timed out", partial_written.is_set()
    return result[-1]


def _feed_pty_work(owner: dict, prompt: str, item_id: str) -> tuple[bool, str, bool]:
    from ui import pty_terminal

    tid = owner["tid"]
    if not pty_terminal.resume(tid):
        return False, "PTY resume failed", False
    jsonl = find_codex_jsonl(owner["sid"])
    version = None
    if jsonl is not None:
        try:
            stat = jsonl.stat()
            version = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            pass
    body = b"\x1b[200~" + prompt.encode("utf-8") + b"\x1b[201~"
    if not pty_terminal.write_reserved(tid, body, item_id):
        return False, "PTY work feed failed", False
    time.sleep(0.25)
    if not pty_terminal.write_reserved(tid, b"\r", item_id):
        pty_terminal.write_reserved(tid, b"\x03", item_id)
        return False, "PTY work submit failed", True
    pty_terminal.mark_turn_started(tid, version)
    return True, "queued (pty work owner)", True


def _feed_work_owner(owner: dict, prompt: str, item_id: str) -> tuple[bool, str, bool]:
    if owner.get("kind") == "gtk":
        return _feed_gtk_work(owner, prompt, item_id)
    return _feed_pty_work(owner, prompt, item_id)


def _mark_route_dispatch(
    item_id: str,
    state: str,
    *,
    start_offset: int | None,
    end_offset: int | None,
    prompt_sha256: str,
) -> None:
    from core.voice_inbox import get_default_voice_inbox

    changed = get_default_voice_inbox().mark_route_dispatch(
        item_id,
        state,
        start_offset=start_offset,
        end_offset=end_offset,
        prompt_sha256=prompt_sha256,
    )
    if changed is False:
        raise RuntimeError(f"route dispatch state {state} was rejected")


def _read_indexed_events(path: Path, start_index: int) -> tuple[list[dict], int]:
    events: list[dict] = []
    line_count = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, raw in enumerate(handle):
                line_count = index + 1
                if index < start_index:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except OSError:
        pass
    return events, line_count


def _collect_work_response(
    jsonl: Path,
    start_index: int,
    prompt_sha256: str,
    timeout: float,
    owner: dict,
) -> dict[str, Any]:
    """Wait for the exact submitted prompt, not merely the next Codex turn."""
    deadline = time.time() + timeout
    saw_prompt = False
    response_parts: list[str] = []
    last_seen = start_index

    while time.time() < deadline:
        events, end_index = _read_indexed_events(jsonl, last_seen)
        last_seen = max(last_seen, end_index)
        for event in events:
            if event.get("type") != "event_msg":
                continue
            payload = event.get("payload") or {}
            kind = payload.get("type")
            if kind == "user_message":
                message = payload.get("message")
                digest = hashlib.sha256(
                    (message if isinstance(message, str) else "").encode("utf-8")
                ).hexdigest()
                if digest != prompt_sha256:
                    return {
                        "ok": False,
                        "response": "",
                        "message": "another prompt entered the reserved transcript slice",
                        "finished": False,
                    }
                saw_prompt = True
            elif saw_prompt and kind in {"agent_message", "assistant_message"}:
                text = payload.get("message") or payload.get("text") or ""
                if isinstance(text, str) and text.strip():
                    response_parts.append(_strip_trailing_blocks(text))
            elif saw_prompt and kind in _WORK_FINISHED:
                cancelled = kind not in {"task_complete", "task_completed"}
                return {
                    "ok": not cancelled,
                    "response": "\n\n".join(response_parts),
                    "message": "turn cancelled" if cancelled else "finished (complete)",
                    "finished": True,
                }
            elif saw_prompt and kind == "error":
                text = payload.get("message") or "codex reported an error"
                return {
                    "ok": False,
                    "response": "\n\n".join(response_parts),
                    "message": str(text),
                    "finished": True,
                }
        if not _owner_alive(owner):
            return {
                "ok": False,
                "response": "\n\n".join(response_parts),
                "message": "runtime owner exited before task_complete",
                "finished": False,
            }
        time.sleep(_POLL_INTERVAL)

    return {
        "ok": False,
        "response": "\n\n".join(response_parts),
        "message": (
            f"timed out after {timeout:.0f}s waiting for the submitted prompt"
            if not saw_prompt
            else f"timed out after {timeout:.0f}s waiting for task_complete"
        ),
        "finished": False,
    }


def _finish_work_owner_turn(owner: dict) -> None:
    if owner.get("kind") == "gtk":
        with suppress(Exception):
            owner["instance"].notify_runtime_turn_finished(owner["sid"])
        return
    from ui import pty_terminal

    pty_terminal.mark_turn_finished(owner["tid"])


def call_codex_work_via_bridge(
    target_sid: str,
    prompt: str,
    item_id: str,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Dispatch one accepted job through its existing owner with durable bounds."""
    target_sid = (target_sid or "").strip()
    item_id = (item_id or "").strip()
    if not target_sid or not item_id:
        return _work_result(ok=False, message="target_sid and item_id are required")
    if not prompt or not prompt.strip():
        return _work_result(ok=False, message="prompt is required")
    jsonl = find_codex_jsonl(target_sid)
    if jsonl is None:
        return _work_result(ok=False, message="target codex transcript was not found")

    owner, message = _acquire_work_owner(target_sid, item_id)
    if owner is None:
        return _work_result(ok=False, message=message)

    start_offset = _line_count(jsonl)
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    committed = False
    release_owner = True
    try:
        fed, feed_message, partial = _feed_work_owner(owner, prompt, item_id)
        if not fed:
            if partial:
                with suppress(Exception):
                    _mark_route_dispatch(
                        item_id,
                        "uncertain",
                        start_offset=start_offset,
                        end_offset=_line_count(jsonl),
                        prompt_sha256=prompt_sha256,
                    )
            return _work_result(
                ok=False,
                message=feed_message,
                committed=False,
                start_offset=start_offset,
                end_offset=_line_count(jsonl),
            )

        committed = True
        release_owner = False
        committed_error = ""
        try:
            _mark_route_dispatch(
                item_id,
                "committed",
                start_offset=start_offset,
                end_offset=None,
                prompt_sha256=prompt_sha256,
            )
        except Exception as error:
            committed_error = str(error)

        result = _collect_work_response(
            jsonl, start_offset, prompt_sha256, timeout, owner
        )
        end_offset = _line_count(jsonl)
        final_state = "completed" if result.get("ok") else "uncertain"
        final_error = ""
        try:
            _mark_route_dispatch(
                item_id,
                final_state,
                start_offset=start_offset,
                end_offset=end_offset,
                prompt_sha256=prompt_sha256,
            )
        except Exception as error:
            final_error = str(error)

        if result.get("finished"):
            _finish_work_owner_turn(owner)
        if result.get("ok") and not committed_error and not final_error:
            release_owner = True
            return _work_result(
                ok=True,
                response=result.get("response") or "[codex returned no text]",
                message=result.get("message") or "finished (complete)",
                committed=True,
                start_offset=start_offset,
                end_offset=end_offset,
            )

        if result.get("finished") and not final_error:
            release_owner = True
        persistence_error = committed_error or final_error
        message = result.get("message") or "work dispatch is uncertain"
        if persistence_error:
            message = f"{message}; route persistence failed: {persistence_error}"
        return _work_result(
            ok=False,
            response=result.get("response") or "",
            message=message,
            committed=True,
            start_offset=start_offset,
            end_offset=end_offset,
            reserved=not release_owner,
        )
    except Exception as error:
        end_offset = _line_count(jsonl)
        if committed:
            with suppress(Exception):
                _mark_route_dispatch(
                    item_id,
                    "uncertain",
                    start_offset=start_offset,
                    end_offset=end_offset,
                    prompt_sha256=prompt_sha256,
                )
            release_owner = False
        return _work_result(
            ok=False,
            message=f"work dispatch failed: {error}",
            committed=committed,
            start_offset=start_offset,
            end_offset=end_offset,
            reserved=committed,
        )
    finally:
        if release_owner:
            _release_work_owner(owner, item_id)


def work_reservation(target_sid: str) -> str | None:
    """Return the exact work item holding either terminal owner."""
    inst = _native_owner(target_sid)
    if inst is not None:
        try:
            return inst.runtime_work_reservation(target_sid)
        except Exception:
            return None
    try:
        from ui import pty_terminal

        return pty_terminal.reservation_for_session(target_sid)
    except ImportError:
        return None


def interrupt_codex_work(target_sid: str, item_id: str) -> dict[str, Any]:
    """Send Ctrl+C only when ``item_id`` still owns the exact target runtime."""
    target_sid = (target_sid or "").strip()
    item_id = (item_id or "").strip()
    if not target_sid or not item_id:
        return {"ok": False, "message": "target_sid and item_id are required"}
    if work_reservation(target_sid) != item_id:
        return {"ok": False, "message": "work item does not own this runtime"}

    inst = _native_owner(target_sid)
    if inst is not None:
        try:
            sent = bool(
                _gtk_owner_call(
                    inst, "runtime_interrupt_work", target_sid, item_id
                )
            )
        except Exception as error:
            return {"ok": False, "message": f"GTK interrupt failed: {error}"}
        return {"ok": sent, "message": "interrupt sent" if sent else "interrupt rejected"}

    from ui import pty_terminal

    tid = pty_terminal.tid_for_session(target_sid)
    if not tid or not pty_terminal.write_reserved(tid, b"\x03", item_id):
        return {"ok": False, "message": "PTY interrupt rejected"}
    return {"ok": True, "message": "interrupt sent"}
