"""Symmetric to core/codex_bridge.py — lets a codex chat (or anyone holding
a claude session sid) feed a prompt into a running claude terminal and read the
response back. Same idea: drive the linked sibling that's already on screen,
don't spawn anything new.

Flow:
  1. Locate the claude JSONL at ~/.claude/projects/*/<sid>.jsonl
  2. Snapshot line count BEFORE feeding so we know where the response starts
  3. Wake the xterm PTY or native VTE and submit with bracketed paste + delayed \\r
  4. Poll the JSONL for new `assistant` messages, considering the turn done
     when a configurable idle window elapses with no further writes (claude
     doesn't emit an explicit "turn_complete" event the way codex does)
  5. Return the concatenated assistant text
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

_DEFAULT_TIMEOUT = 300.0
_POLL_INTERVAL = 0.5
# Claude doesn't emit a turn_complete marker. Heuristic: turn is done when
# the JSONL has been silent for this many seconds AFTER we've seen at least
# one assistant message since our prompt.
_TURN_IDLE_SECONDS = 4.0


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
    """Mount/resume a linked claude session in the GTK app before feeding it."""
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


def find_claude_jsonl(session_id: str) -> Path | None:
    """Locate the claude rollout for a session id. Claude stores them under
    ~/.claude/projects/<project_slug>/<UUID>.jsonl — slug is the project's
    cwd path with `/` replaced. We don't know the slug, so glob the lot."""
    if not CLAUDE_PROJECTS_ROOT.exists():
        return None
    matches = list(CLAUDE_PROJECTS_ROOT.rglob(f"{session_id}.jsonl"))
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def _gtk_feed(target_sid: str, text: str) -> tuple[bool, str]:
    """Schedule a feed_child on the GTK main thread for the claude VTE.
    Same mechanic as codex_bridge — bracketed paste + delayed \\r so the
    receiving CLI treats the body as one paste then triggers submit."""
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
    vte = inst._vtes.get(target_sid)
    pid = inst._vte_pids.get(target_sid)
    print(f"[claude-bridge] feed target={target_sid[:8]} vte={'yes' if vte is not None else 'NO'} "
          f"pid={pid} alive={_pid_alive(pid)} keys={[k[:8] for k in inst._vtes]}", flush=True)
    ok, msg = _gtk_resume_target(inst, target_sid)
    print(f"[claude-bridge] prepare target ok={ok} msg={msg}", flush=True)
    if not ok:
        return False, msg
    vte = _wait_for_gtk_vte(inst, target_sid, timeout=2.0)
    if vte is None:
        return False, f"No live terminal for {target_sid[:8]}"

    body = b"\x1b[200~" + text.encode("utf-8") + b"\x1b[201~"

    def _do_paste():
        try:
            vte.feed_child(body)
            print(f"[claude-bridge] pasted {len(body)}B into {target_sid[:8]} "
                  f"(vte_pid={inst._vte_pids.get(target_sid)})", flush=True)
        except Exception as e:
            print(f"[claude-bridge] paste failed: {e}", flush=True)
        return False

    def _do_submit():
        try:
            vte.feed_child(b"\r")
            print(f"[claude-bridge] submitted \\r to {target_sid[:8]}", flush=True)
        except Exception as e:
            print(f"[claude-bridge] submit failed: {e}", flush=True)
        return False

    GLib.idle_add(_do_paste)
    GLib.timeout_add(250, _do_submit)
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
    jsonl = find_claude_jsonl(target_sid)
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


def _strip_terminal_escapes(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text).strip()


def _extract_assistant_text(ev: dict) -> str:
    """Pull rendered text from a claude `assistant` event. Claude's message
    format wraps content in a list of blocks (text, tool_use, tool_result,
    thinking, ...). We only care about text blocks for the user-facing reply."""
    if not isinstance(ev, dict):
        return ""
    msg = ev.get("message") or {}
    if not isinstance(msg, dict):
        return ""
    if msg.get("role") != "assistant":
        return ""
    content = msg.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                t = block.get("text") or ""
                if isinstance(t, str) and t.strip():
                    parts.append(t)
    return "\n".join(parts).strip()


def _collect_response(jsonl: Path, start_index: int, timeout: float) -> dict[str, Any]:
    """Wait for claude's reply by polling the JSONL. Turn is done when the
    file has been idle for _TURN_IDLE_SECONDS AND we've seen at least one
    assistant message since the user prompt landed."""
    deadline = time.time() + timeout
    last_seen = start_index
    response_parts: list[str] = []
    saw_user_msg = False
    last_change = time.time()

    while time.time() < deadline:
        events = _read_jsonl_lines(jsonl, last_seen)
        if events:
            last_change = time.time()
            for ev in events:
                last_seen += 1
                msg = ev.get("message") or {}
                if isinstance(msg, dict):
                    if msg.get("role") == "user":
                        saw_user_msg = True
                    elif msg.get("role") == "assistant":
                        text = _extract_assistant_text(ev)
                        if text:
                            response_parts.append(_strip_terminal_escapes(text))
        # Idle window check — only stop after we've seen the user_message
        # (otherwise we'd return immediately if the file hasn't changed yet)
        if saw_user_msg and response_parts and (time.time() - last_change) > _TURN_IDLE_SECONDS:
            return {
                "ok": True,
                "response": "\n\n".join(response_parts),
                "message": "finished (idle window)",
            }
        time.sleep(_POLL_INTERVAL)

    if not saw_user_msg:
        return {
            "ok": False,
            "response": "",
            "message": "Prompt didn't reach the claude session (user message never appeared in JSONL)",
        }
    return {
        "ok": False,
        "response": "\n\n".join(response_parts),
        "message": f"Timed out after {timeout:.0f}s waiting for response to settle",
    }


def call_claude_via_bridge(target_sid: str, prompt: str, timeout: float = _DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Send `prompt` into the running claude VTE for `target_sid` and wait
    for the assistant response. Returns {ok, response, message}."""
    if not target_sid:
        return {"ok": False, "response": "", "message": "target_sid required"}
    if not prompt or not prompt.strip():
        return {"ok": False, "response": "", "message": "prompt required"}

    jsonl = find_claude_jsonl(target_sid)
    if jsonl is None:
        return {
            "ok": False,
            "response": "",
            "message": f"No claude JSONL found for {target_sid[:8]} — is it a claude session?",
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
