"""Auto-spawn a codex session linked to a claude chat. Used by `chats ask-codex`
when the calling claude doesn't yet have a linked codex sibling — converts
the solo claude view to split + new codex pane in real time, then links them
in Serena's group metadata so the bridge can drive the new VTE.

Flow:
  1. Caller passes claude_sid + claude_cwd
  2. If claude already has a linked codex sibling → return that sid (no-op)
  3. Snapshot ~/.codex/sessions/ — record currently-known codex sids
  4. Generate a pseudo sid (`new-spawn-...`) — placeholder for the codex VTE map
  5. Schedule on GTK main thread: enter split view with claude_sid + pseudo
  6. GTK spawns codex in the right pane; codex starts and writes a new
     rollout file `~/.codex/sessions/<date>/rollout-<ts>-<uuid>.jsonl`
  7. Poll the filesystem; the uuid in the new file is the real codex sid
  8. Schedule on GTK main thread: rename the VTE map key pseudo → real
  9. Link in metadata via `meta.link_sessions([claude_sid, real_codex_sid])`
 10. Return the real codex sid

Returns: {ok: bool, codex_sid: str, message: str}
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path

CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
ROLLOUT_PATTERN = re.compile(r"rollout-[\dT_-]+-([0-9a-f-]{36})\.jsonl$")
_SPAWN_TIMEOUT = 30.0


def _existing_codex_sids() -> set[str]:
    """Return the set of all codex session UUIDs currently on disk."""
    if not CODEX_SESSIONS_ROOT.exists():
        return set()
    out: set[str] = set()
    for p in CODEX_SESSIONS_ROOT.rglob("rollout-*.jsonl"):
        m = ROLLOUT_PATTERN.search(p.name)
        if m:
            out.add(m.group(1))
    return out


def _find_existing_linked_codex(claude_sid: str) -> str | None:
    """If claude_sid is already in a group that contains a codex member,
    return that codex sid. Otherwise None."""
    from core.linked_sessions import find_linked_session

    return find_linked_session(claude_sid, "codex")


def _resolve_claude_cwd(claude_sid: str, fallback: str = "") -> str:
    """Look up claude's cwd from the indexer if the caller didn't provide one."""
    if fallback:
        return fallback
    try:
        from core.indexer import get_session
        s = get_session(claude_sid) or {}
        return s.get("cwd") or s.get("project_dir") or str(Path.home())
    except Exception:
        return str(Path.home())


def _start_split_with_codex(inst, claude_sid: str, cwd: str) -> str:
    """Schedule a split-view entry that puts a fresh codex on the right.
    Returns the pseudo sid we used as the codex placeholder."""
    from gi.repository import GLib  # type: ignore
    pseudo_sid = "new-spawn-" + secrets.token_hex(4) + "-" + format(int(time.time() * 1000), "x")
    payload = {
        "sids": [claude_sid, pseudo_sid],
        "spawn_meta": {
            claude_sid: {"cwd": cwd, "agent": "claude", "isNew": False},
            pseudo_sid: {"cwd": cwd, "agent": "codex", "isNew": True},
        },
        "ratio": 0.5,
    }

    def _do_split():
        try:
            if getattr(inst, "_use_native_vte", True):
                inst._enter_split(payload)
            else:
                inst._eval_js(
                    "window.__spawnLinkedTerminal && window.__spawnLinkedTerminal("
                    f"{json.dumps(claude_sid)}, {json.dumps(pseudo_sid)}, "
                    f"'codex', {json.dumps(cwd)});"
                )
        except Exception as e:
            print(f"[codex-spawn] _enter_split failed: {e}", flush=True)
        return False

    GLib.idle_add(_do_split)
    return pseudo_sid


def _wait_for_new_session_file(pre_sids: set[str], deadline: float) -> str | None:
    """Poll ~/.codex/sessions/ for a new rollout file appearing after we
    triggered the spawn. Returns the new session_id (uuid) or None on timeout."""
    while time.time() < deadline:
        time.sleep(0.5)
        new_sids = _existing_codex_sids() - pre_sids
        if not new_sids:
            continue
        best, best_t = None, 0.0
        for sid in new_sids:
            for p in CODEX_SESSIONS_ROOT.rglob(f"rollout-*-{sid}.jsonl"):
                t = p.stat().st_mtime
                if t > best_t:
                    best_t = t
                    best = sid
        if best:
            return best
    return None


def _migrate_vte_key(inst, pseudo_sid: str, real_sid: str, claude_sid: str) -> None:
    """Schedule a rename on GTK main thread: vte map key pseudo → real."""
    from gi.repository import GLib  # type: ignore

    def _do_migrate():
        try:
            if getattr(inst, "_use_native_vte", True):
                vte = inst._vtes.pop(pseudo_sid, None)
                if vte is not None:
                    inst._vtes[real_sid] = vte
                    if pseudo_sid in inst._vte_pids:
                        inst._vte_pids[real_sid] = inst._vte_pids.pop(pseudo_sid)
                    if pseudo_sid in inst._sid_agent:
                        inst._sid_agent[real_sid] = inst._sid_agent.pop(pseudo_sid)
                    inst._migrate_runtime_sid(pseudo_sid, real_sid)
                    if inst._split_active and pseudo_sid in inst._split_sids:
                        a, b = inst._split_sids
                        inst._split_sids = (
                            real_sid if a == pseudo_sid else a,
                            real_sid if b == pseudo_sid else b,
                        )
            else:
                from ui import pty_terminal

                pty_terminal.migrate_session(pseudo_sid, real_sid)
            inst._eval_js(
                f"window.__onLinkedCodexSpawned && window.__onLinkedCodexSpawned("
                f"{json.dumps(pseudo_sid)}, {json.dumps(real_sid)}, {json.dumps(claude_sid)});"
            )
        except Exception as e:
            print(f"[codex-spawn] migrate failed: {e}", flush=True)
        return False

    GLib.idle_add(_do_migrate)


def _runtime_ready(inst, sid: str) -> bool:
    if getattr(inst, "_use_native_vte", True):
        return sid in inst._vtes
    from ui import pty_terminal

    return pty_terminal.tid_for_session(sid) is not None


def spawn_linked_codex(claude_sid: str, claude_cwd: str = "", timeout: float = _SPAWN_TIMEOUT) -> dict:
    """Idempotent: ensure claude_sid has a linked codex sibling. Spawns one
    in split view next to claude if needed.

    NOTE: codex doesn't write its session_meta event to disk until it
    receives its first user input. So this function CANNOT block waiting
    for a session file — there's nothing to wait for. Instead it returns
    the pseudo_sid as a placeholder. Callers should use `ask_linked_codex`
    below for the full spawn-and-prompt flow when the codex is brand-new.
    """
    if not claude_sid:
        return {"ok": False, "codex_sid": "", "message": "claude_sid required"}
    existing = _find_existing_linked_codex(claude_sid)
    if existing:
        return {"ok": True, "codex_sid": existing, "message": "already linked"}
    try:
        from desktop.app_gtk import ChatsApp
    except ImportError:
        return {"ok": False, "codex_sid": "", "message": "GTK shell not available"}
    inst = ChatsApp.INSTANCE
    if inst is None:
        return {"ok": False, "codex_sid": "", "message": "GTK window not initialized"}
    # Same pseudo→real migration race as ask_linked_codex below: give a
    # freshly spawned pane a few seconds to get re-keyed before failing.
    deadline = time.time() + 10
    while not _runtime_ready(inst, claude_sid) and time.time() < deadline:
        time.sleep(0.5)
    if not _runtime_ready(inst, claude_sid):
        return {
            "ok": False,
            "codex_sid": "",
            "message": f"claude {claude_sid[:8]} has no live terminal in Serena",
        }
    cwd = _resolve_claude_cwd(claude_sid, claude_cwd)
    pseudo_sid = _start_split_with_codex(inst, claude_sid, cwd)
    return {
        "ok": True,
        "codex_sid": pseudo_sid,
        "message": "split spawned; codex sid will resolve after first prompt",
        "pseudo": True,
    }


def ask_linked_codex(claude_sid: str, prompt: str, claude_cwd: str = "",
                     spawn_warmup: float = 4.0, response_timeout: float = 300.0) -> dict:
    """End-to-end: ensure claude has a linked codex (spawn one if not),
    feed the prompt to its VTE, return the response. Single round-trip
    so the caller (CLI / claude) doesn't have to coordinate spawn + feed.

    Why this exists: codex's TUI sits on a startup screen until it gets its
    first input. So the spawn-then-poll-for-session-file approach deadlocks
    — the file only appears AFTER the prompt is fed. We unify the two.
    """
    if not claude_sid or not prompt or not prompt.strip():
        return {"ok": False, "codex_sid": "", "response": "", "message": "claude_sid and prompt required"}

    # Linked already → straight to bridge
    existing = _find_existing_linked_codex(claude_sid)
    if existing:
        from core.codex_bridge import call_codex_via_bridge
        result = call_codex_via_bridge(existing, prompt, timeout=response_timeout)
        result["codex_sid"] = existing
        result["spawned"] = False
        return result

    # Need to spawn — verify GTK
    try:
        from desktop.app_gtk import ChatsApp
        from gi.repository import GLib  # type: ignore
    except ImportError:
        return {"ok": False, "codex_sid": "", "response": "", "message": "GTK shell not available"}
    inst = ChatsApp.INSTANCE
    if inst is None:
        return {"ok": False, "codex_sid": "", "response": "", "message": "GTK window not initialized"}
    # A freshly spawned pane (front door, new chat) is keyed by its pseudo sid
    # for the first few seconds until the reconciler migrates it to the real
    # session id. An ask-codex fired from inside that pane races the migrate,
    # so wait out the window instead of failing (observed live 2026-07-14:
    # two calls in the same second, migrate landed between them).
    deadline = time.time() + 10
    while not _runtime_ready(inst, claude_sid) and time.time() < deadline:
        time.sleep(0.5)
    if not _runtime_ready(inst, claude_sid):
        return {
            "ok": False, "codex_sid": "", "response": "",
            "message": f"claude {claude_sid[:8]} has no live terminal in Serena",
        }

    cwd = _resolve_claude_cwd(claude_sid, claude_cwd)
    pre_sids = _existing_codex_sids()
    pseudo_sid = _start_split_with_codex(inst, claude_sid, cwd)

    # Wait for the target renderer and PTY to exist before the first prompt.
    deadline = time.time() + max(10.0, spawn_warmup)
    while not _runtime_ready(inst, pseudo_sid) and time.time() < deadline:
        time.sleep(0.1)
    if not _runtime_ready(inst, pseudo_sid):
        return {
            "ok": False, "codex_sid": pseudo_sid, "response": "",
            "message": "codex terminal did not start",
        }
    if getattr(inst, "_use_native_vte", True):
        time.sleep(spawn_warmup)

    # Feed the prompt — codex's TUI will accept the bracketed paste, render
    # it as user input, hit Enter via our delayed \r, and create its session
    # file as it processes the turn.
    body = b"\x1b[200~" + prompt.encode("utf-8") + b"\x1b[201~"

    if getattr(inst, "_use_native_vte", True):
        def _do_feed():
            vte = inst._vtes.get(pseudo_sid)
            if vte is None:
                print(f"[codex-spawn] no VTE for {pseudo_sid} at feed time", flush=True)
                return False
            try:
                vte.feed_child(body)
            except Exception as e:
                print(f"[codex-spawn] feed failed: {e}", flush=True)
            return False

        def _do_submit():
            vte = inst._vtes.get(pseudo_sid)
            if vte is None:
                return False
            try:
                vte.feed_child(b"\r")
            except Exception as e:
                print(f"[codex-spawn] submit failed: {e}", flush=True)
            return False

        GLib.idle_add(_do_feed)
        GLib.timeout_add(250, _do_submit)
    else:
        from ui import pty_terminal

        tid = pty_terminal.tid_for_session(pseudo_sid)
        if not tid or not pty_terminal.write(tid, body):
            return {
                "ok": False, "codex_sid": pseudo_sid, "response": "",
                "message": "codex PTY write failed",
            }
        pty_terminal.mark_turn_started(tid)
        time.sleep(0.25)
        if not pty_terminal.write(tid, b"\r"):
            return {
                "ok": False, "codex_sid": pseudo_sid, "response": "",
                "message": "codex PTY submit failed",
            }

    # Now wait for the new session file to appear (codex creates it once
    # it receives our input).
    deadline = time.time() + 30
    real_sid = _wait_for_new_session_file(pre_sids, deadline)
    if not real_sid:
        return {
            "ok": False, "codex_sid": pseudo_sid, "response": "",
            "message": "codex didn't create a session file after prompt — TUI may be stuck on a setup screen",
        }

    # Reconcile pseudo → real and link in metadata
    _migrate_vte_key(inst, pseudo_sid, real_sid, claude_sid)
    try:
        from core import metadata as meta
        meta.link_sessions([claude_sid, real_sid])
    except Exception as e:
        print(f"[codex-spawn] link failed: {e}", flush=True)

    # Wait for codex's response to complete in the JSONL we just identified
    from core.codex_bridge import find_codex_jsonl, _collect_response, _line_count
    jsonl = find_codex_jsonl(real_sid)
    if jsonl is None:
        return {
            "ok": False, "codex_sid": real_sid, "response": "",
            "message": "session file appeared but couldn't locate JSONL",
        }
    # Start scanning from line 0 — we want the user_message we just wrote
    # plus everything after.
    resp = _collect_response(jsonl, 0, response_timeout)
    if resp.get("ok") and getattr(inst, "_use_native_vte", True):
        inst.notify_runtime_turn_finished(real_sid)
    resp["codex_sid"] = real_sid
    resp["spawned"] = True
    return resp
