"""Auto-spawn a Claude session linked to a Codex chat.

This is the Claude-side mirror of :mod:`core.codex_spawn`. It converts a
solo Codex view into a split view, starts a fresh Claude VTE, feeds its first
prompt, discovers the resulting Claude JSONL, then replaces the temporary VTE
key with the real session id and links both sessions in Serena metadata.

Flow:
  1. Caller passes codex_sid + cwd
  2. If Codex already has a linked Claude sibling, return that sid
  3. Snapshot the Claude session ids currently on disk
  4. Enter split view with a temporary ``new-spawn-claude-*`` sid
  5. Feed the first prompt through bracketed paste and submit it
  6. Poll ``~/.claude/projects/<cwd-slug>/<uuid>.jsonl`` for the new session
  7. Rename the VTE map key from the temporary sid to the real UUID
  8. Link the Codex and Claude sessions in group metadata
  9. Wait for Claude's response in the new JSONL

Public functions return dictionaries matching ``core.codex_spawn`` with
``claude_sid`` in place of ``codex_sid``.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

from core.claude_bridge import CLAUDE_PROJECTS_ROOT, find_claude_jsonl

_SPAWN_TIMEOUT = 30.0


def _existing_claude_sids() -> set[str]:
    """Return every UUID-shaped Claude session id currently on disk."""
    if not CLAUDE_PROJECTS_ROOT.exists():
        return set()
    out: set[str] = set()
    for path in CLAUDE_PROJECTS_ROOT.rglob("*.jsonl"):
        sid = path.stem
        if _looks_like_uuid(sid):
            out.add(sid)
    return out


def _looks_like_uuid(value: str) -> bool:
    parts = value.split("-")
    if [len(part) for part in parts] != [8, 4, 4, 4, 12]:
        return False
    try:
        int("".join(parts), 16)
    except ValueError:
        return False
    return True


def _find_existing_linked_claude(codex_sid: str) -> str | None:
    """Return Codex's linked Claude sibling, if one already exists."""
    from core.linked_sessions import find_linked_session

    return find_linked_session(
        codex_sid,
        "claude",
        fallback_match=lambda sid: find_claude_jsonl(sid) is not None,
    )


def _resolve_codex_cwd(codex_sid: str, fallback: str = "") -> str:
    """Use the caller-provided cwd, then Codex's indexed cwd, then HOME."""
    if fallback:
        return fallback
    try:
        from core.indexer import get_session

        session = get_session(codex_sid) or {}
        return session.get("cwd") or session.get("project_dir") or str(Path.home())
    except Exception:
        return str(Path.home())


def _claude_project_dir(cwd: str) -> Path:
    """Return the Claude project bucket used for a process launched at cwd."""
    from core.config import claude_project_dir_for

    return CLAUDE_PROJECTS_ROOT / claude_project_dir_for(cwd)


def _start_split_with_claude(inst, codex_sid: str, cwd: str) -> str:
    """Schedule a split with existing Codex left and fresh Claude right."""
    from gi.repository import GLib  # type: ignore

    pseudo_sid = (
        "new-spawn-claude-"
        + secrets.token_hex(4)
        + "-"
        + format(int(time.time() * 1000), "x")
    )
    payload = {
        "sids": [codex_sid, pseudo_sid],
        "spawn_meta": {
            codex_sid: {"cwd": cwd, "agent": "codex", "isNew": False},
            pseudo_sid: {"cwd": cwd, "agent": "claude", "isNew": True},
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
                    f"{json.dumps(codex_sid)}, {json.dumps(pseudo_sid)}, "
                    f"'claude', {json.dumps(cwd)});"
                )
        except Exception as exc:
            print(f"[claude-spawn] _enter_split failed: {exc}", flush=True)
        return False

    GLib.idle_add(_do_split)
    return pseudo_sid


def _new_claude_candidates(pre_sids: set[str]) -> list[Path]:
    """Return new Claude JSONLs, newest first."""
    if not CLAUDE_PROJECTS_ROOT.exists():
        return []
    candidates = [
        path
        for path in CLAUDE_PROJECTS_ROOT.rglob("*.jsonl")
        if path.stem not in pre_sids and _looks_like_uuid(path.stem)
    ]
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)


def _wait_for_new_session_file(
    pre_sids: set[str], cwd: str, deadline: float
) -> tuple[str, Path] | None:
    """Poll for the new Claude JSONL and prefer the requested cwd bucket."""
    expected_parent = _claude_project_dir(cwd)
    while time.time() < deadline:
        time.sleep(0.5)
        candidates = _new_claude_candidates(pre_sids)
        if not candidates:
            continue
        expected = [path for path in candidates if path.parent == expected_parent]
        chosen = expected[0] if expected else candidates[0]
        return chosen.stem, chosen
    return None


def _migrate_vte_key(
    inst, pseudo_sid: str, real_sid: str, codex_sid: str
) -> None:
    """Rename the temporary Claude VTE key and notify the web layer."""
    from gi.repository import GLib  # type: ignore

    def _do_migrate():
        try:
            if getattr(inst, "_use_native_vte", True):
                vte = inst._vtes.pop(pseudo_sid, None)
                if vte is not None:
                    inst._vtes[real_sid] = vte
                    if pseudo_sid in inst._vte_pids:
                        inst._vte_pids[real_sid] = inst._vte_pids.pop(pseudo_sid)
                    sid_agent = getattr(inst, "_sid_agent", None)
                    if isinstance(sid_agent, dict) and pseudo_sid in sid_agent:
                        sid_agent[real_sid] = sid_agent.pop(pseudo_sid)
                    inst._migrate_runtime_sid(pseudo_sid, real_sid)
                    if inst._split_active and pseudo_sid in inst._split_sids:
                        left, right = inst._split_sids
                        inst._split_sids = (
                            real_sid if left == pseudo_sid else left,
                            real_sid if right == pseudo_sid else right,
                        )
            else:
                from ui import pty_terminal

                pty_terminal.migrate_session(pseudo_sid, real_sid)
            inst._eval_js(
                "window.__onLinkedClaudeSpawned && "
                "window.__onLinkedClaudeSpawned("
                f"{json.dumps(pseudo_sid)}, {json.dumps(real_sid)}, "
                f"{json.dumps(codex_sid)});"
            )
        except Exception as exc:
            print(f"[claude-spawn] migrate failed: {exc}", flush=True)
        return False

    GLib.idle_add(_do_migrate)


def _runtime_ready(inst, sid: str) -> bool:
    if getattr(inst, "_use_native_vte", True):
        return sid in inst._vtes
    from ui import pty_terminal

    return pty_terminal.tid_for_session(sid) is not None


def spawn_linked_claude(
    codex_sid: str, cwd: str = "", timeout: float = _SPAWN_TIMEOUT
) -> dict:
    """Ensure Codex has a linked Claude pane, returning a temporary sid if new.

    Claude does not produce the real session JSONL until its first prompt is
    submitted. Use :func:`ask_linked_claude` when the caller needs the fully
    resolved session id and response in one operation.
    """
    del timeout  # Kept for interface symmetry with spawn_linked_codex.
    if not codex_sid:
        return {"ok": False, "claude_sid": "", "message": "codex_sid required"}

    existing = _find_existing_linked_claude(codex_sid)
    if existing:
        return {"ok": True, "claude_sid": existing, "message": "already linked"}

    try:
        from desktop.app_gtk import ChatsApp
    except ImportError:
        return {"ok": False, "claude_sid": "", "message": "GTK shell not available"}
    inst = ChatsApp.INSTANCE
    if inst is None:
        return {"ok": False, "claude_sid": "", "message": "GTK window not initialized"}
    deadline = time.time() + 10
    while not _runtime_ready(inst, codex_sid) and time.time() < deadline:
        time.sleep(0.5)
    if not _runtime_ready(inst, codex_sid):
        return {
            "ok": False,
            "claude_sid": "",
            "message": f"codex {codex_sid[:8]} has no live terminal in Serena",
        }

    resolved_cwd = _resolve_codex_cwd(codex_sid, cwd)
    pseudo_sid = _start_split_with_claude(inst, codex_sid, resolved_cwd)
    return {
        "ok": True,
        "claude_sid": pseudo_sid,
        "message": "split spawned; claude sid will resolve after first prompt",
        "pseudo": True,
    }


def ask_linked_claude(
    codex_sid: str,
    prompt: str,
    cwd: str = "",
    spawn_warmup: float = 4.0,
    response_timeout: float = 300.0,
) -> dict:
    """Ensure linked Claude exists, feed prompt, link sessions, and return reply."""
    if not codex_sid or not prompt or not prompt.strip():
        return {
            "ok": False,
            "claude_sid": "",
            "response": "",
            "message": "codex_sid and prompt required",
        }

    existing = _find_existing_linked_claude(codex_sid)
    if existing:
        from core.claude_bridge import call_claude_via_bridge

        result = call_claude_via_bridge(
            existing, prompt, timeout=response_timeout
        )
        result["claude_sid"] = existing
        result["spawned"] = False
        return result

    try:
        from desktop.app_gtk import ChatsApp
        from gi.repository import GLib  # type: ignore
    except ImportError:
        return {
            "ok": False,
            "claude_sid": "",
            "response": "",
            "message": "GTK shell not available",
        }
    inst = ChatsApp.INSTANCE
    if inst is None:
        return {
            "ok": False,
            "claude_sid": "",
            "response": "",
            "message": "GTK window not initialized",
        }
    deadline = time.time() + 10
    while not _runtime_ready(inst, codex_sid) and time.time() < deadline:
        time.sleep(0.5)
    if not _runtime_ready(inst, codex_sid):
        return {
            "ok": False,
            "claude_sid": "",
            "response": "",
            "message": f"codex {codex_sid[:8]} has no live terminal in Serena",
        }

    resolved_cwd = _resolve_codex_cwd(codex_sid, cwd)
    pre_sids = _existing_claude_sids()
    pseudo_sid = _start_split_with_claude(inst, codex_sid, resolved_cwd)

    deadline = time.time() + max(10.0, spawn_warmup)
    while not _runtime_ready(inst, pseudo_sid) and time.time() < deadline:
        time.sleep(0.1)
    if not _runtime_ready(inst, pseudo_sid):
        return {
            "ok": False,
            "claude_sid": pseudo_sid,
            "response": "",
            "message": "claude terminal did not start",
        }
    if getattr(inst, "_use_native_vte", True):
        time.sleep(spawn_warmup)
    body = b"\x1b[200~" + prompt.encode("utf-8") + b"\x1b[201~"

    if getattr(inst, "_use_native_vte", True):
        def _do_feed():
            vte = inst._vtes.get(pseudo_sid)
            if vte is None:
                print(
                    f"[claude-spawn] no VTE for {pseudo_sid} at feed time",
                    flush=True,
                )
                return False
            try:
                vte.feed_child(body)
            except Exception as exc:
                print(f"[claude-spawn] feed failed: {exc}", flush=True)
            return False

        def _do_submit():
            vte = inst._vtes.get(pseudo_sid)
            if vte is None:
                return False
            try:
                vte.feed_child(b"\r")
            except Exception as exc:
                print(f"[claude-spawn] submit failed: {exc}", flush=True)
            return False

        GLib.idle_add(_do_feed)
        GLib.timeout_add(250, _do_submit)
    else:
        from ui import pty_terminal

        tid = pty_terminal.tid_for_session(pseudo_sid)
        if not tid or not pty_terminal.write(tid, body):
            return {
                "ok": False,
                "claude_sid": pseudo_sid,
                "response": "",
                "message": "claude PTY write failed",
            }
        pty_terminal.mark_turn_started(tid)
        time.sleep(0.25)
        if not pty_terminal.write(tid, b"\r"):
            return {
                "ok": False,
                "claude_sid": pseudo_sid,
                "response": "",
                "message": "claude PTY submit failed",
            }

    found = _wait_for_new_session_file(
        pre_sids, resolved_cwd, time.time() + _SPAWN_TIMEOUT
    )
    if found is None:
        return {
            "ok": False,
            "claude_sid": pseudo_sid,
            "response": "",
            "message": (
                "claude didn't create a session file after prompt; "
                "the TUI may be stuck on a setup screen"
            ),
        }
    real_sid, jsonl = found

    _migrate_vte_key(inst, pseudo_sid, real_sid, codex_sid)
    try:
        from core import metadata as meta

        meta.link_sessions([codex_sid, real_sid])
    except Exception as exc:
        print(f"[claude-spawn] link failed: {exc}", flush=True)

    from core.claude_bridge import _collect_response

    response = _collect_response(jsonl, 0, response_timeout)
    if response.get("ok") and getattr(inst, "_use_native_vte", True):
        inst.notify_runtime_turn_finished(real_sid)
    response["claude_sid"] = real_sid
    response["spawned"] = True
    return response
