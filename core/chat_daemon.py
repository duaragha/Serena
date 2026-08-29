"""Headless chat protocol for Serena's mobile clients.

Speaks the WebSocket protocol defined in `mobile/src/types.ts`. Reuses the
existing index (`list_sessions` / `get_session`) for listing + history, and
drives claude/codex in non-interactive print mode for live replies.

Wired into the Flask app at `/ws/chat` (see ui/web.py) and served headless via
`chats serve`. Remote clients authenticate with the private shared token. The
desktop coding app resumes chats through its interactive terminal instead.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

from core import codex_records
from core.process_launch import platform_launch_argv

CONFIG_DIR = Path.home() / ".config" / "serena"
TOKEN_FILE = CONFIG_DIR / "chat_token"
CHAT_PROMPT_DIR = CONFIG_DIR / "chat-prompts"
_MOBILE_QUERY: ContextVar[str] = ContextVar("serena_mobile_memory_query", default="")
_PENDING_SESSION_TTL_SECONDS = 60 * 60
_MAX_PENDING_SESSIONS = 32
_pending_sessions: dict[str, dict] = {}
_pending_sessions_lock = threading.Lock()
_active_turns: set[str] = set()
_active_turns_lock = threading.Lock()
_chat_turn_executor = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="serena-chat-turn",
)


# ── auth ──────────────────────────────────────────────────────────────────
def get_or_create_token() -> str:
    try:
        if TOKEN_FILE.exists():
            t = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if t:
                return t
    except OSError:
        pass
    t = secrets.token_urlsafe(24)
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(t, encoding="utf-8")
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return t


# ── helpers ───────────────────────────────────────────────────────────────
def _epoch_ms(ts) -> int:
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return 0
    if isinstance(ts, datetime):
        return int(ts.timestamp() * 1000)
    return 0


def _agent_of(row: dict) -> str:
    a = (row.get("agent") or "").lower()
    return "codex" if "codex" in a else "claude"


def _title_of(row: dict) -> str:
    return (
        row.get("custom_title")
        or row.get("display_title")
        or row.get("title")
        or (row.get("session_id") or "")[:8]
        or "chat"
    )


def _cwd_of(row: dict) -> str | None:
    # claude/codex --resume needs to run in the session's project dir.
    return row.get("cwd") or row.get("last_cwd") or row.get("project_dir") or None


def _pending_summary(record: dict) -> dict:
    return {
        "id": record["id"],
        "title": record["title"],
        "agent": record["agent"],
        "updated": record["updated"],
        "preview": "",
        "starred": False,
        "pending": True,
        "cwd": record["cwd"],
    }


def _expire_pending_sessions_locked(now: float) -> None:
    for sid, record in list(_pending_sessions.items()):
        if now - record["created_monotonic"] > _PENDING_SESSION_TTL_SECONDS:
            _pending_sessions.pop(sid, None)


def create_pending_session(
    agent: str,
    *,
    cwd: str | None = None,
    title: str | None = None,
) -> dict:
    """Reserve a real Claude session id before its first headless turn."""

    normalized_agent = str(agent or "claude").strip().lower()
    if normalized_agent == "serena":
        normalized_agent = "claude"
    if normalized_agent != "claude":
        raise ValueError("new headless chats currently use Serena; open Code for Codex")

    from core.config import resolve_session_cwd

    now = time.monotonic()
    record = {
        "id": str(uuid.uuid4()),
        "title": (str(title or "").strip() or "New chat")[:160],
        "agent": normalized_agent,
        "cwd": resolve_session_cwd(cwd),
        "updated": int(time.time() * 1000),
        "created_monotonic": now,
    }
    with _pending_sessions_lock:
        _expire_pending_sessions_locked(now)
        if len(_pending_sessions) >= _MAX_PENDING_SESSIONS:
            oldest = min(
                _pending_sessions,
                key=lambda item: _pending_sessions[item]["created_monotonic"],
            )
            _pending_sessions.pop(oldest, None)
        _pending_sessions[record["id"]] = record
    return _pending_summary(record)


def _get_pending_session(sid: str) -> dict | None:
    now = time.monotonic()
    with _pending_sessions_lock:
        _expire_pending_sessions_locked(now)
        record = _pending_sessions.get(sid)
        return dict(record) if record else None


def _drop_pending_session(sid: str) -> None:
    with _pending_sessions_lock:
        _pending_sessions.pop(sid, None)


def _claim_turn(sid: str) -> bool:
    with _active_turns_lock:
        if sid in _active_turns:
            return False
        _active_turns.add(sid)
        return True


def _release_turn(sid: str) -> None:
    with _active_turns_lock:
        _active_turns.discard(sid)


def _mark_turn_finished(sid: str) -> None:
    """Persist background completion even when its browser has moved away."""

    try:
        from core import chat_attention

        chat_attention.mark(sid)
    except Exception:
        pass


# ── protocol payloads (read paths — no agent cost) ──────────────────────────
def list_sessions_payload(limit: int = 200) -> dict:
    from core.indexer import list_sessions

    try:
        from core import metadata as meta_sync

        all_meta = meta_sync.get_all_meta()
    except Exception:
        all_meta = {}

    out = []
    indexed_ids: set[str] = set()
    for r in list_sessions(limit=limit):
        sid = r.get("session_id")
        if not sid:
            continue
        indexed_ids.add(sid)
        summary = {
            "id": sid,
            "title": _title_of(r),
            "agent": _agent_of(r),
            "updated": _epoch_ms(r.get("last_timestamp")),
            "preview": (r.get("last_prompt") or r.get("first_message") or "")[:140],
            "starred": bool(r.get("starred")),
        }
        gid = (all_meta.get(sid) or {}).get("group")
        if gid:
            summary["group"] = gid
        out.append(summary)
    now = time.monotonic()
    with _pending_sessions_lock:
        _expire_pending_sessions_locked(now)
        pending = [
            _pending_summary(record)
            for sid, record in _pending_sessions.items()
            if sid not in indexed_ids
        ]
    pending.sort(key=lambda item: item["updated"], reverse=True)
    out = pending + out
    return {"type": "sessions", "sessions": out}


def _codex_messages(path: Path, sid: str) -> list[dict]:
    """Parse a codex rollout (.jsonl) into chat messages.

    Codex records a turn differently from claude, and differently between its
    own versions; core.codex_records reconciles both shapes.
    """
    return [
        {"id": f"{sid}-{i}", "role": role, "text": text.strip(), "ts": _epoch_ms(ts)}
        for i, (role, text, ts) in enumerate(codex_records.read_messages(path))
    ]


def _slug_copies(sid: str) -> list[Path]:
    """All real session-file copies of a sid across slug folders (claude files
    a session under projects/<slug-of-cwd>/, so the same chat used from
    laptop/PC/renamed dirs ends up duplicated). Excludes staging + backups."""
    out = []
    try:
        for p in _projects_root().glob(f"*/{sid}.jsonl"):
            s = str(p)
            if "serena-resume" in s:
                continue
            # Skip symlinks — they're ensure_session_visible cross-OS resume
            # aids, not distinct copies. Unioning/writing THROUGH them corrupts
            # (writes land on the link target, which may be a deleted/stale dir).
            if p.is_symlink():
                continue
            out.append(p)
    except Exception:
        pass
    return out


def _atomic_write(path: Path, text: str) -> None:
    """Replace a session file without ever exposing a partial one.

    Opening a transcript "w" truncates it before the new bytes land, and these
    files run to tens of megabytes. Anything reading during that window sees an
    empty or half-written session: on the PC that surfaced as a chat that opens,
    reports live, then dies, because `claude -r` found nothing to resume and
    exited. A failed write left the file at zero bytes permanently.

    Writing beside the target and renaming means a reader sees either the old
    file or the new one. os.replace is atomic on POSIX and on Windows.
    """
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        # Leave the existing file alone; a stale copy beats a truncated one.
        try:
            tmp.unlink()
        except OSError:
            pass


def _canonicalize_session(sid: str) -> Path | None:
    """Union all slug-copies of a session by record uuid and propagate the
    merged content to every copy. This keeps the copies eventually-consistent
    no matter which slug a given device wrote (hardlinks don't survive claude's
    rewrite-on-save, so we reconcile at the data level instead). Returns the
    canonical (longest) path. Only rewrites copies that are out of date."""
    copies = _slug_copies(sid)
    if len(copies) <= 1:
        return copies[0] if copies else None

    def rlines(p):
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                return [ln.rstrip("\n") for ln in f if ln.strip()]
        except OSError:
            return []

    def key(s):
        try:
            return json.loads(s).get("uuid") or s
        except json.JSONDecodeError:
            return s

    def tsof(s):
        try:
            return json.loads(s).get("timestamp", "") or ""
        except json.JSONDecodeError:
            return ""

    data = {p: rlines(p) for p in copies}
    base_path = max(copies, key=lambda p: len(data[p]))
    base = data[base_path]
    seen = {key(s) for s in base}
    extra = []
    for p in copies:
        if p == base_path:
            continue
        for s in data[p]:
            k = key(s)
            if k not in seen:
                seen.add(k)
                extra.append(s)
    extra.sort(key=tsof)
    union = base + extra
    union_text = "\n".join(union) + "\n"
    for p in copies:
        if len(data[p]) != len(union):  # out of date → bring up to the union
            _atomic_write(p, union_text)
    return base_path


def heal_sync_conflicts() -> int:
    """Fold any Syncthing `.sync-conflict-*` files back into their session by
    record-uuid union, then archive them. This is the safety net for the
    Syncthing setup: a conflict (two devices edited the same chat) would
    otherwise leave the winner missing the loser's messages (how PHEV/Questions
    lost turns before). Union-merge makes both sides' messages survive. Only
    touches sessions that actually have conflict files. Returns # healed."""
    import re as _re
    import shutil as _shutil

    root = _projects_root()
    sid_re = _re.compile(r"([0-9a-fA-F-]{36})\.sync-conflict-.*\.jsonl$")
    by_sid: dict[str, list[Path]] = {}
    try:
        for p in root.glob("*/*.sync-conflict-*.jsonl"):
            if "serena-resume" in str(p):
                continue
            m = sid_re.search(p.name)
            if m:
                by_sid.setdefault(m.group(1), []).append(p)
    except Exception:
        return 0
    if not by_sid:
        return 0

    archive = Path(os.environ.get("CHATS_DATA_DIR", "/data")) / "sync-conflict-archive"

    def rlines(p):
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                return [ln.rstrip("\n") for ln in f if ln.strip()]
        except OSError:
            return []

    def key(s):
        try:
            return json.loads(s).get("uuid") or s
        except json.JSONDecodeError:
            return s

    def tsof(s):
        try:
            return json.loads(s).get("timestamp", "") or ""
        except json.JSONDecodeError:
            return ""

    healed = 0
    for sid, conflicts in by_sid.items():
        reals = _slug_copies(sid)
        sources = reals + conflicts
        if not sources:
            continue
        data = {p: rlines(p) for p in sources}
        base_path = max(sources, key=lambda p: len(data[p]))
        base = list(data[base_path])
        seen = {key(s) for s in base}
        extra = []
        for p in sources:
            if p == base_path:
                continue
            for s in data[p]:
                k = key(s)
                if k not in seen:
                    seen.add(k)
                    extra.append(s)
        extra.sort(key=tsof)
        union = base + extra
        union_text = "\n".join(union) + "\n"
        # write the healed union into every REAL copy that's out of date
        for p in reals:
            if len(data.get(p, [])) != len(union):
                try:
                    p.write_text(union_text, encoding="utf-8")
                except OSError:
                    pass
        # if there were no real copies (conflict-only), materialize one
        if not reals:
            try:
                base_path.with_name(sid + ".jsonl").write_text(union_text, encoding="utf-8")
            except OSError:
                pass
        # archive the conflict files (folded in now) so they stop lingering
        for c in conflicts:
            try:
                dest = archive / (sid + "__" + c.name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                _shutil.move(str(c), str(dest))
            except OSError:
                pass
        healed += 1
    return healed


def history_payload(sid: str) -> dict:
    from core.indexer import get_session

    row = get_session(sid)
    if not row or not row.get("file_path"):
        return {"type": "history", "sessionId": sid, "messages": []}

    if _agent_of(row) == "codex":
        path = Path(row["file_path"])
        if not path.exists():
            return {"type": "history", "sessionId": sid, "messages": []}
        return {"type": "history", "sessionId": sid, "messages": _codex_messages(path, sid)}

    # claude: reconcile all slug-copies first, then read the canonical union so
    # the phone always sees everything regardless of which device wrote where.
    path = _canonicalize_session(sid) or Path(row["file_path"])
    if not path.exists():
        return {"type": "history", "sessionId": sid, "messages": []}

    from core.parser import parse_full

    messages = []
    for i, m in enumerate(parse_full(path)):
        role = m.role if m.role in ("user", "assistant") else "system"
        text = (m.text or "").strip()
        if not text:
            continue
        messages.append(
            {"id": f"{sid}-{i}", "role": role, "text": text, "ts": _epoch_ms(m.timestamp)}
        )
    return {"type": "history", "sessionId": sid, "messages": messages}


# ── agent runner (write path — fires a real turn) ───────────────────────────
def _agent_command(
    agent: str,
    sid: str,
    *,
    prompt_path: Path | None = None,
    is_new: bool = False,
) -> list[str]:
    if agent == "codex":
        # codex exec resume <id>: appends the turn to the rollout IN PLACE
        # (verified), by id. A dash keeps the user turn in stdin, not argv.
        return [
            "codex",
            "exec",
            "resume",
            sid,
            "--json",
            "--skip-git-repo-check",
            "-",
        ]
    # Claude print mode reads the user turn from stdin and streams JSON events.
    session_flag = "--session-id" if is_new else "--resume"
    cmd = [
        "claude", "-p",
        session_flag, sid,
        "--output-format", "stream-json",
        "--verbose",
    ]
    # Keep private context out of argv. The CLI reads this user-only file while
    # starting, and run_agent removes the unique file after the child exits.
    ctx = _injected_context()
    if ctx.strip():
        if prompt_path is None:
            raise ValueError("Claude mobile turns require a private prompt path")
        from core.brain_lifetime import write_text_atomic

        private_prompt = write_text_atomic(prompt_path, ctx)
        cmd += ["--append-system-prompt-file", str(private_prompt)]
    return cmd


def _knowledge_index() -> str:
    """A lightweight index of the knowledge base (slugs + titles). Full topic
    content is too large to inject every turn, so we list what's available and
    where to read it; claude can pull specific topics on demand."""
    try:
        from core.config import KNOWLEDGE_DIR
        from knowledge.reader import list_topics

        topics = list_topics()
        if not topics:
            return ""
        lines = [
            "# Knowledge base",
            f"Reference material available ({len(topics)} topics). Full content "
            f"lives under `{KNOWLEDGE_DIR}/<slug>/` — read a topic's files when "
            "relevant. Topics:",
        ]
        for t in topics:
            slug = t.get("slug", "")
            title = t.get("title", "") or slug
            lines.append(f"- {slug}: {title}" if title != slug else f"- {slug}")
        return "\n".join(lines)
    except Exception:
        return ""


def _mobile_memory_context(query: str) -> str:
    """Return active state plus a bounded query-aware canonical memory pack."""

    try:
        from core.brain_state import active_state, compact_active
        from memory.retrieval import pack_memory_context

        active = active_state()
        return pack_memory_context(
            query,
            active_state=compact_active(active),
            active_records=active.records,
            surface="mobile",
            max_characters=7_000,
            max_tokens=1_800,
            max_records=5,
            active_max_characters=1_200,
        ).text
    except Exception:
        return ""


def _injected_context() -> str:
    """Persona, bounded canonical memory, and the knowledge index for Claude."""

    parts = []
    try:
        from core.config import read_agent_context

        parts.append(read_agent_context())  # persona + tooling
    except Exception:
        pass
    memory_context = _mobile_memory_context(_MOBILE_QUERY.get())
    if memory_context:
        parts.append(memory_context)
    k = _knowledge_index()
    if k:
        parts.append(k)
    return "\n\n".join(p for p in parts if p and p.strip())


def _codex_turn_input(text: str) -> str:
    """Prefix Codex stdin with the same private bounded memory context."""

    memory_context = _mobile_memory_context(text)
    return memory_context + "\n\n" + text if memory_context else text


def _extract_full_text(agent: str, obj: dict) -> str:
    """Pull assistant text out of one streamed JSON event."""
    if agent == "codex":
        # codex: {"type":"item.completed","item":{"type":"agent_message","text":...}}
        if obj.get("type") == "item.completed":
            item = obj.get("item") or {}
            if item.get("type") == "agent_message":
                return item.get("text", "") or ""
        return ""
    # claude: {"type":"assistant","message":{"content":[{"type":"text","text":...}]}}
    if obj.get("type") == "assistant":
        parts = (obj.get("message") or {}).get("content") or []
        return "".join(
            b.get("text", "")
            for b in parts
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


# A stable POSIX cwd the daemon resumes from. claude maps a cwd to
# ~/.claude/projects/<slug> by replacing non-alphanumerics with '-', then scans
# THAT folder for the session id. So to resume ANY chat — Linux- or
# Windows-origin — from this one (Linux) daemon, we stage a copy of the target
# session into that folder, resume, then write the reply back to the canonical
# session file. (Verified behaviour: claude resumes only from the cwd's project.)
_RESUME_CWD = "/srv/serena-resume"


def _claude_slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _projects_root() -> Path:
    return Path(os.environ.get("CLAUDE_DIR", str(Path.home() / ".claude"))) / "projects"


def _orig_session_file(sid: str) -> Path | None:
    try:
        from core.indexer import get_session

        row = get_session(sid) or {}
        fp = row.get("file_path")
        # Never resume off a staging copy (it shadows the real session). If the
        # index handed us one, find the real file by sid under projects/.
        if fp and "serena-resume" not in str(fp) and Path(fp).exists():
            return Path(fp)
        for p in _projects_root().glob(f"*/{sid}.jsonl"):
            if "serena-resume" not in str(p):
                return p
        return Path(fp) if fp else None
    except Exception:
        return None


def run_agent(
    sid: str,
    agent: str,
    text: str,
    cwd: str | None,
    emit,
    *,
    is_new: bool = False,
) -> bool:
    """Spawn the agent in print mode and stream its reply as protocol events.

    Resumes any chat regardless of origin OS by staging the session under a
    resumable cwd, then syncing the continued file back. New chats run directly
    in the selected project and materialize the caller-reserved UUID.
    """
    message_id = "a" + uuid.uuid4().hex[:12]
    emit({"type": "message_start", "sessionId": sid, "messageId": message_id, "role": "assistant"})

    orig_file = None if is_new else _orig_session_file(sid)
    staged: Path | None = None
    # A session's recorded cwd may belong to the other OS. Codex resumes by id;
    # Claude is cwd-scoped, so stage it under a known local cwd when necessary.
    run_cwd: str | None = None

    if is_new:
        from core.config import resolve_session_cwd

        run_cwd = resolve_session_cwd(cwd)

    if not is_new and agent == "claude" and orig_file and orig_file.exists():
        try:
            if os.name == "nt":
                from core.config import resolve_session_cwd

                resume_cwd = resolve_session_cwd(cwd)
            else:
                resume_cwd = _RESUME_CWD
            os.makedirs(resume_cwd, exist_ok=True)
            staged_dir = _projects_root() / _claude_slug(resume_cwd)
            staged_dir.mkdir(parents=True, exist_ok=True)
            staged = staged_dir / orig_file.name
            if staged.resolve() == orig_file.resolve():
                staged = None
            else:
                shutil.copy2(orig_file, staged)
            run_cwd = resume_cwd
        except OSError:
            staged = None
            run_cwd = None

    sent = ""
    success = False
    prompt_path = (
        CHAT_PROMPT_DIR / f"chat-{uuid.uuid4().hex}.md"
        if agent == "claude"
        else None
    )
    try:
        query_token = _MOBILE_QUERY.set(text)
        try:
            command = _agent_command(
                agent,
                sid,
                prompt_path=prompt_path,
                is_new=is_new,
            )
        finally:
            _MOBILE_QUERY.reset(query_token)
        turn_input = _codex_turn_input(text) if agent == "codex" else text
        child_env = os.environ.copy()
        command = platform_launch_argv(command, child_env)
        proc = subprocess.Popen(
            command,
            cwd=run_cwd,
            env=child_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdin is not None
        proc.stdin.write(turn_input)
        proc.stdin.close()
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            full = _extract_full_text(agent, obj)
            if not full:
                continue
            # claude resends the whole assistant text per event — emit only the
            # new tail so the phone streams instead of duplicating.
            delta = full[len(sent):] if full.startswith(sent) else full
            if delta:
                sent = full
                emit({"type": "chunk", "sessionId": sid, "messageId": message_id, "delta": delta})
        exit_code = proc.wait(timeout=600)
        success = exit_code == 0
        if not success:
            emit({
                "type": "error",
                "sessionId": sid,
                "message": f"agent exited with code {exit_code}",
            })
    except Exception as e:
        success = False
        emit({
            "type": "error",
            "sessionId": sid,
            "message": f"agent run failed: {e}",
        })
    finally:
        if prompt_path is not None:
            prompt_path.unlink(missing_ok=True)
        # Write the continued session back to its canonical file, then clean up
        # the staged copy so it doesn't linger as a phantom project.
        if staged is not None:
            try:
                if staged.exists() and orig_file is not None:
                    shutil.copy2(staged, orig_file)
            except OSError:
                pass
            try:
                staged.unlink()
            except OSError:
                pass

    # Propagate the new turn to every slug-copy so the PC desktop (which may
    # read a different slug) sees it too.
    if agent == "claude" and not is_new:
        try:
            _canonicalize_session(sid)
        except Exception:
            pass

    if success:
        _mark_turn_finished(sid)
    emit({"type": "message_done", "sessionId": sid, "messageId": message_id})
    return success


# ── dispatch ────────────────────────────────────────────────────────────────
def handle_client_message(raw: str, emit) -> None:
    """Dispatch one ClientMsg (JSON string) → emit ServerMsg(s) via `emit`."""
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        emit({"type": "error", "message": "bad json"})
        return

    mtype = msg.get("type")
    if mtype == "list_sessions":
        emit(list_sessions_payload())
    elif mtype == "open":
        emit(history_payload(msg.get("sessionId", "")))
    elif mtype == "send":
        from core.indexer import get_session

        sid = msg.get("sessionId", "")
        text = msg.get("text", "")
        row = get_session(sid) or {}
        pending = _get_pending_session(sid) if not row else None
        if not sid or not isinstance(text, str) or not text.strip():
            emit({
                "type": "error",
                "sessionId": sid,
                "message": "sessionId and text are required",
            })
            return
        if not row and not pending:
            emit({"type": "error", "sessionId": sid, "message": "session not found"})
            return
        if not _claim_turn(sid):
            emit({
                "type": "error",
                "sessionId": sid,
                "message": "that chat already has an active turn",
            })
            return
        agent = pending["agent"] if pending else _agent_of(row)
        cwd = pending["cwd"] if pending else _cwd_of(row)
        try:
            success = run_agent(
                sid,
                agent,
                text,
                cwd,
                emit,
                is_new=bool(pending),
            )
        finally:
            _release_turn(sid)
        if success:
            if pending:
                from core import metadata

                metadata.set_custom_title(sid, pending["title"])
            try:
                from core.indexer import update_index

                update_index(skip_if_running=True)
            except Exception:
                pass
            if pending and get_session(sid):
                _drop_pending_session(sid)
            emit(list_sessions_payload())
    elif mtype == "new_session":
        try:
            session = create_pending_session(
                msg.get("agent", "claude"),
                cwd=msg.get("cwd"),
                title=msg.get("title"),
            )
        except ValueError as exc:
            emit({"type": "error", "message": str(exc)})
            return
        emit({"type": "session_created", "session": session})
        emit({"type": "history", "sessionId": session["id"], "messages": []})
    elif mtype == "stop":
        pass  # v1: turns run to completion
    else:
        emit({"type": "error", "message": f"unknown message type: {mtype}"})


def dispatch_client_message(raw: str, emit) -> Future | None:
    """Handle control messages inline and agent turns concurrently.

    A single mobile WebSocket carries every conversation. Running a ``send``
    inline blocks that socket until the agent exits, which prevents a second
    chat from starting and makes navigation look like it killed the first
    turn. Per-session admission still lives in ``_claim_turn``; this executor
    only lets different session ids make progress at the same time.
    """

    try:
        message = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        handle_client_message(raw, emit)
        return None
    if message.get("type") == "send":
        def run_turn() -> None:
            try:
                handle_client_message(raw, emit)
            except Exception as exc:
                emit({
                    "type": "error",
                    "sessionId": message.get("sessionId", ""),
                    "message": str(exc),
                })

        return _chat_turn_executor.submit(run_turn)
    handle_client_message(raw, emit)
    return None
