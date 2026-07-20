"""Read-only in-process tools for the brain daemon (spec v2.5c).

"Check github for the latest changes in the repo" answered by voice with
ZERO panes spawned. Everything here is strictly read-only: git/gh queries,
memory recall, ledger/task reads. Anything state-changing stays behind the
wait-for-go pane protocol, these tools cannot write, by construction.

In-process SDK MCP server: plain async python functions, no subprocess
IPC per call (the functions themselves may run read-only subprocesses).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sqlite3
import subprocess
from pathlib import Path

from claude_agent_sdk import create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations

from core.config import DB_PATH

_PROJECTS = Path(
    os.environ.get("SERENA_PROJECTS_DIR")
    or next(
        (
            str(path)
            for path in (
                Path.home() / "Documents" / "Projects",
                Path.home() / "Projects",
            )
            if path.is_dir()
        ),
        str(Path.home() / "Documents" / "Projects"),
    )
).expanduser().resolve()
_CHAT_DB_PATH = DB_PATH

_LOCAL_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_REMOTE_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

def _trusted_system_executable(candidates: tuple[Path, ...]) -> str | None:
    """Resolve only an administrator-owned, non-writable system binary."""

    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if not resolved.is_file():
            continue
        if os.name != "nt" and (metadata.st_uid != 0 or metadata.st_mode & 0o022):
            continue
        return str(resolved)
    return None


if os.name == "nt":
    _GIT_CANDIDATES = (
        Path("C:/Program Files/Git/cmd/git.exe"),
        Path("C:/Program Files/Git/bin/git.exe"),
    )
    _GH_CANDIDATES = (Path("C:/Program Files/GitHub CLI/gh.exe"),)
else:
    _GIT_CANDIDATES = (Path("/usr/bin/git"), Path("/bin/git"))
    _GH_CANDIDATES = (
        Path("/usr/bin/gh"),
        Path("/bin/gh"),
        Path("/usr/local/bin/gh"),
    )

_GIT_BINARY = _trusted_system_executable(_GIT_CANDIDATES)
_GH_BINARY = _trusted_system_executable(_GH_CANDIDATES)
_GIT_READ_ONLY_PREFIX = [
    _GIT_BINARY or "",
    "--no-optional-locks",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
]


def _subprocess_group_options() -> dict[str, object]:
    if os.name == "nt":
        return {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        }
    return {"start_new_session": True}


async def _run_ro(cmd: list[str], cwd: str | None = None, timeout: float = 20) -> str:
    """Run a read-only command, return combined output (clipped)."""
    env = os.environ.copy()
    env.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "GH_PROMPT_DISABLED": "1",
            "GH_PAGER": "cat",
            "PAGER": "cat",
        }
    )
    proc: asyncio.subprocess.Process | None = None
    communication: asyncio.Task[tuple[bytes, bytes | None]] | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            **_subprocess_group_options())
        communication = asyncio.create_task(proc.communicate())
        out, _ = await asyncio.wait_for(
            asyncio.shield(communication), timeout=timeout
        )
        text = out.decode("utf-8", errors="replace").strip()
        return text[:4000] if text else "(no output)"
    except asyncio.TimeoutError:
        await _terminate_and_reap(proc, communication)
        return f"(timed out after {timeout}s)"
    except asyncio.CancelledError:
        await _terminate_and_reap(proc, communication)
        raise
    except OSError as e:
        await _terminate_and_reap(proc, communication)
        return f"(failed: {e})"


async def _terminate_and_reap(
    proc: asyncio.subprocess.Process | None,
    communication: asyncio.Task[tuple[bytes, bytes | None]] | None,
) -> None:
    if proc is None:
        return
    if os.name == "nt":
        await _kill_windows_process_tree(proc)
    else:
        _signal_posix_process_group(proc, signal.SIGTERM)
    waiter = asyncio.create_task(proc.wait())
    try:
        await asyncio.wait_for(asyncio.shield(waiter), timeout=1.0)
    except asyncio.TimeoutError:
        if os.name == "nt":
            await _kill_windows_process_tree(proc)
        else:
            _signal_posix_process_group(proc, signal.SIGKILL)
        await waiter
    finally:
        if os.name != "nt":
            # The group leader can exit before a child. Kill the remaining
            # group after reaping the leader so no helper survives unattended.
            _signal_posix_process_group(proc, signal.SIGKILL)
    if communication is not None:
        try:
            await asyncio.wait_for(asyncio.shield(communication), timeout=1.0)
        except asyncio.TimeoutError:
            communication.cancel()
            await asyncio.gather(communication, return_exceptions=True)


def _signal_posix_process_group(
    proc: asyncio.subprocess.Process,
    requested_signal: signal.Signals,
) -> None:
    try:
        os.killpg(proc.pid, requested_signal)
    except ProcessLookupError:
        return
    except (AttributeError, PermissionError):
        if proc.returncode is None:
            try:
                if requested_signal == signal.SIGTERM:
                    proc.terminate()
                else:
                    proc.kill()
            except ProcessLookupError:
                pass


async def _kill_windows_process_tree(proc: asyncio.subprocess.Process) -> None:
    def kill_tree() -> bool:
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    killed_tree = await asyncio.to_thread(kill_tree)
    if not killed_tree and proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


def _resolve_repo(repo: str) -> str | None:
    """Map a spoken repo name to a path under ~/Documents/Projects."""
    q = (repo or "").strip().lower().replace(" ", "_")
    if not q:
        return None
    direct = (_PROJECTS / q).resolve()
    if direct.is_relative_to(_PROJECTS) and (direct / ".git").exists():
        return str(direct)
    for p in _PROJECTS.rglob(".git"):
        candidate = p.parent.resolve()
        if not candidate.is_relative_to(_PROJECTS):
            continue
        if candidate.name.lower().replace("-", "_") == q.replace("-", "_"):
            return str(candidate)
    return None


@tool("git_latest", "Latest changes in a repo: recent commits, working-tree "
      "status, current branch. Read-only. repo is a project name under "
      "~/Documents/Projects (e.g. 'serena', 'atrium').",
      {"repo": str}, annotations=_LOCAL_READ_ONLY)
async def git_latest(args):
    path = _resolve_repo(args.get("repo", ""))
    if not path:
        return {"content": [{"type": "text",
                             "text": f"no repo matching '{args.get('repo')}' found"}]}
    if not _GIT_BINARY:
        return {"content": [{"type": "text", "text": "trusted git CLI not installed"}]}
    log = await _run_ro(
        [*_GIT_READ_ONLY_PREFIX, "log", "--oneline", "-10", "--decorate"],
        cwd=path,
    )
    status = await _run_ro(
        [*_GIT_READ_ONLY_PREFIX, "status", "--short", "--branch"],
        cwd=path,
    )
    return {"content": [{"type": "text",
                         "text": f"[{path}]\n\nbranch/status:\n{status}\n\nrecent commits:\n{log}"}]}


@tool("github_activity", "Recent GitHub activity for a repo: open PRs and "
      "recent issues via gh CLI. Read-only. repo name as spoken.",
      {"repo": str}, annotations=_REMOTE_READ_ONLY)
async def github_activity(args):
    path = _resolve_repo(args.get("repo", ""))
    if not path:
        return {"content": [{"type": "text",
                             "text": f"no repo matching '{args.get('repo')}' found"}]}
    if not _GH_BINARY:
        return {"content": [{"type": "text", "text": "trusted gh CLI not installed"}]}
    prs = await _run_ro([_GH_BINARY, "pr", "list", "--limit", "5"], cwd=path)
    issues = await _run_ro([_GH_BINARY, "issue", "list", "--limit", "5"], cwd=path)
    return {"content": [{"type": "text",
                         "text": f"open PRs:\n{prs}\n\nrecent issues:\n{issues}"}]}


def _recall_chat_index(query: str, limit: int = 10) -> str:
    """Search the existing chat index through a query-only SQLite handle."""
    query = (query or "").strip()
    if not query:
        return "(chat recall query is empty)"
    path = Path(_CHAT_DB_PATH).expanduser().resolve()
    if not path.is_file():
        return "(chat index unavailable)"

    # immutable prevents SQLite from creating or updating WAL/SHM sidecars.
    # Recall can tolerate a slightly stale snapshot; the unattended tool may
    # not mutate persistent state just to make a read current.
    uri = f"{path.as_uri()}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        content_rows = conn.execute(
            """
            SELECT session_id,
                   snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                   timestamp
            FROM messages_fts
            WHERE content MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()

        seen: set[str] = set()
        results: list[dict[str, str]] = []
        for row in content_rows:
            sid = str(row["session_id"] or "")
            if not sid or sid in seen:
                continue
            session = conn.execute(
                """
                SELECT title, custom_title, first_timestamp, agent
                FROM sessions
                WHERE session_id = ?
                """,
                (sid,),
            ).fetchone()
            seen.add(sid)
            results.append(
                {
                    "sid": sid,
                    "title": (
                        str(session["custom_title"] or session["title"] or "Untitled")
                        if session
                        else "Untitled"
                    ),
                    "timestamp": (
                        str(session["first_timestamp"] or "")
                        if session
                        else str(row["timestamp"] or "")
                    ),
                    "agent": (
                        str(session["agent"] or "claude")
                        if session
                        else "claude"
                    ),
                    "snippet": str(row["snippet"] or ""),
                    "kind": "content",
                }
            )

        pattern = f"%{query}%"
        title_rows = conn.execute(
            """
            SELECT session_id, title, custom_title, first_timestamp, agent,
                   first_message
            FROM sessions
            WHERE title LIKE ? COLLATE NOCASE
               OR custom_title LIKE ? COLLATE NOCASE
            ORDER BY last_timestamp DESC
            LIMIT ?
            """,
            (pattern, pattern, limit),
        ).fetchall()
        for row in title_rows:
            sid = str(row["session_id"] or "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            results.append(
                {
                    "sid": sid,
                    "title": str(row["custom_title"] or row["title"] or "Untitled"),
                    "timestamp": str(row["first_timestamp"] or ""),
                    "agent": str(row["agent"] or "claude"),
                    "snippet": str(row["first_message"] or "")[:160],
                    "kind": "title",
                }
            )
    except sqlite3.Error as exc:
        return f"(chat recall unavailable: {exc})"
    finally:
        if "conn" in locals():
            conn.close()

    if not results:
        return f"no matches for: {query}"
    lines = [f"{len(results[:limit])} match(es) for: {query}"]
    for row in results[:limit]:
        date = row["timestamp"][:10] or "unknown-date"
        snippet = row["snippet"].replace("\n", " ")
        title = row["title"].replace("\n", " ")
        lines.append(
            f"[{date}] [{row['agent'].lower()}] {row['sid'][:8]} | "
            f"{title} ({row['kind']})\n  {snippet}"
        )
    return "\n".join(lines)[:4000]


@tool("recall_chats", "Full-text search across all past claude+codex "
      "conversations on this machine. Read-only.",
      {"query": str}, annotations=_LOCAL_READ_ONLY)
async def recall_chats(args):
    out = await asyncio.to_thread(_recall_chat_index, args.get("query", ""))
    return {"content": [{"type": "text", "text": out}]}


@tool("read_ledger", "Current state of an active ledger thread (or all of "
      "them when name is empty). Read-only.",
      {"name": str}, annotations=_LOCAL_READ_ONLY)
async def read_ledger(args):
    try:
        from core.brain_state import format_ledgers
    except Exception as e:
        return {"content": [{"type": "text", "text": f"(memory unavailable: {e})"}]}
    return {"content": [{"type": "text",
                         "text": format_ledgers(args.get("name") or "")}]}


BRAIN_TOOLS = (git_latest, github_activity, recall_chats, read_ledger)
BRAIN_TOOL_NAMES = [f"mcp__serena-ro__{item.name}" for item in BRAIN_TOOLS]


def brain_tools_server():
    """The daemon plugs this into ClaudeAgentOptions.mcp_servers."""
    return create_sdk_mcp_server(
        name="serena-ro",
        tools=list(BRAIN_TOOLS),
    )
