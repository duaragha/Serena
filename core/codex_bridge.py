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

import json
import os
import re
import threading
import time
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
        from desktop.app_gtk import ChatsApp
        from gi.repository import GLib  # type: ignore
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
