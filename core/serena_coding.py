"""Serena's own coding sessions: real terminals in his app that she runs.

She already had two ways to get code written: a headless worker for one
spoken job (core.work_authority) and Fleet for multi-agent runs. Neither is a
session he can watch or she can talk to while it works. This opens one: a
Claude (or Codex) terminal pane inside his Serena desktop app, briefed as the
orchestrator for one task. That session works like any of his own -- it can
spread the work across subagents, ask the linked Codex for a second opinion,
start a Fleet run for a big multi-part change, and ship through the usual
branch -> PR -> merge path -- and it texts him when it is done or stuck.

Serena keeps the thread: she can list her sessions, read what one has been
doing, steer it mid-task, and stop it. The panes are ordinary chats, so he can
open any of them in the app and watch it live or take over.

The pane is found by a tag in its first message rather than by a session id
the app hands back, because a fresh pane only learns its id once the agent
writes its transcript.

    python -m core.serena_coding notify "text"   # how a session reports back
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

SERENA_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = Path.home() / ".local" / "state" / "serena" / "serena-coding.json"
FALLBACK_BACKEND = "http://127.0.0.1:8767"
FIND_SESSION_SECONDS = 30
# Mid-task a session writes to its transcript every few seconds; a quiet file
# means it is waiting on him (or on her), not working.
WORKING_WITHIN_SECONDS = 90
DONE_MARKERS = ("DONE:", "BLOCKED:", "NEEDS YOU:")
# Sessions do not always keep the exact form: "**DONE:**", a bare "DONE".
# Capitals only, so a sentence starting "Done with the tests" is not an ending.
_MARKER = re.compile(r"^[\W_]*(DONE|BLOCKED|NEEDS YOU)(?:[\W_]|$)")


class CodingSessionError(RuntimeError):
    pass


# -- where his app is -------------------------------------------------------

def backend_url() -> str:
    """The backend his desktop app is using, so the pane shows up in it.

    The installed app runs its own sidecar on a random port; the always-on
    mobile host is 8767. A pane spawned on the wrong one would run where he
    cannot see it. The stable app wins: its window opens her pane as the live
    terminal it is. Serena Dev opens chats in structured panes of its own,
    which cannot attach to a terminal, so there he only gets the transcript.
    """

    override = os.environ.get("SERENA_CODING_BACKEND", "").strip()
    if override:
        return override.rstrip("/")
    try:
        import psutil
    except ImportError:
        return FALLBACK_BACKEND
    candidates: list[tuple[int, float, int]] = []
    for proc in psutil.process_iter(["cmdline", "create_time"]):
        cmd = proc.info.get("cmdline") or []
        joined = " ".join(cmd)
        if "sidecar" not in joined or "--port" not in cmd:
            continue
        try:
            port = int(cmd[cmd.index("--port") + 1])
        except (ValueError, IndexError):
            continue
        candidates.append((_sidecar_rank(proc, joined), -(proc.info.get("create_time") or 0), port))
    if not candidates:
        return FALLBACK_BACKEND
    return f"http://127.0.0.1:{sorted(candidates)[0][2]}"


def _sidecar_rank(proc: Any, joined: str) -> int:
    packaged = "AppImage" in joined or ".mount_" in joined or "resources" in joined
    if not packaged:
        return 3  # the mobile host: runs, but no window shows it
    try:
        env = proc.environ()
    except Exception:
        env = {}
    if env.get("SERENA_STRUCTURED_WORKSPACE") == "1":
        return 2
    if env.get("SERENA_DESKTOP_CHANNEL", "stable") == "stable":
        return 0
    return 1


def _request(method: str, path: str, body: dict[str, Any] | None = None,
             *, timeout: float = 30, base: str | None = None) -> dict[str, Any]:
    url = (base or backend_url()) + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise CodingSessionError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise CodingSessionError(f"could not reach his Serena app at {url}: {exc}") from exc


# -- state ------------------------------------------------------------------

def _load() -> dict[str, Any]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _record(session: str) -> dict[str, Any]:
    state = _load()
    needle = session.strip().lower()
    for key, record in state.items():
        if needle in (key.lower(), str(record.get("session_id") or "").lower()[:len(needle)]) \
                or needle in str(record.get("title") or "").lower():
            return record
    raise CodingSessionError(f"no coding session of hers matches {session!r}")


# -- the brief --------------------------------------------------------------

def brief(task: str, *, repo: Path, tag: str) -> str:
    """The first message: the task, and how to run it as her orchestrator."""

    return f"""[{tag}] You are running this task for Raghav as Serena's coding session. Serena
(his assistant) opened this terminal and is keeping track of it; he may open it
and watch.

THE TASK
{task.strip()}

Repository: {repo}

HOW TO RUN IT
- You are the orchestrator. Plan first, then do the work. Use subagents (the
  Agent tool) for independent parallel pieces -- exploring, tests, a second
  implementation path -- and `chats ask-codex "..."` when a second opinion from
  Codex would help. If the task is big and splits into several workstreams,
  start a Fleet run instead of doing it serially (`/fleet <task>`), then watch
  it and report its result.
- Deliver it fully: branch or worktree off the default branch, make the change,
  run the project's tests and type checks, commit with a conventional message,
  push, open a PR with `gh`, and squash-merge it when checks allow. Never
  commit straight to the default branch and never merge broken code.
- Do not stop to ask whether to continue. Ask only if proceeding would be
  unsafe or would make the result useless if you guessed wrong.

WHEN YOU FINISH OR GET STUCK
- End your final message with exactly one line starting with one of:
  DONE: <one sentence on what changed, with the PR link>
  BLOCKED: <what stopped you and what you need>
  NEEDS YOU: <the one decision only he can make>
- And text him that line:
  cd "{SERENA_ROOT}" && "{sys.executable}" -m core.serena_coding notify "<that line>"
"""


# -- opening, finding, reading ---------------------------------------------

def _claude_project_dir(cwd: Path) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    return Path.home() / ".claude" / "projects" / slug


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:  # chats come and go under us
        return 0.0


def _candidates(agent: str, cwd: Path, since: float) -> list[Path]:
    if agent == "codex":
        # Rollouts live in YYYY/MM/DD folders; only the days the pane could
        # have started on are worth listing.
        root = Path.home() / ".codex" / "sessions"
        days = {time.strftime("%Y/%m/%d", time.localtime(t)) for t in (since, time.time())}
        paths = [p for day in days for p in (root / day).glob("rollout-*.jsonl")]
    else:
        # Top level only: subfolders hold subagent and tool-result files.
        paths = list(_claude_project_dir(cwd).glob("*.jsonl"))
    stamped = [(m, p) for p in paths if (m := _mtime(p)) >= since - 5]
    return [p for _, p in sorted(stamped, reverse=True)]


def _user_texts(record: Any) -> list[str]:
    """Text a person typed, from one Claude or Codex transcript line."""

    if not isinstance(record, dict):
        return []
    if record.get("type") == "user":  # claude
        content = (record.get("message") or {}).get("content")
        if isinstance(content, str):
            return [content]
        if isinstance(content, list):
            return [b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"]
        return []
    payload = record.get("payload") or {}
    if payload.get("type") == "user_message":  # codex event_msg
        return [str(payload.get("message") or "")]
    if payload.get("type") == "message" and payload.get("role") == "user":  # codex response_item
        return [b.get("text", "") for b in payload.get("content") or []
                if isinstance(b, dict) and b.get("type") in ("input_text", "text")]
    return []


def _opens_with(path: Path, tag: str) -> bool:
    """Whether this chat was started by her brief carrying `tag`.

    Only a user message that begins with the tag counts. Any other chat can
    mention a tag -- this one did, while it was being built, and got adopted.
    The brief is also not near the top of the file: session hooks write their
    context, often hundreds of KB, before the first message.
    """

    needle, opener = tag.encode("utf-8"), f"[{tag}]"
    try:
        with open(path, "rb") as fh:
            for line in fh:
                if needle not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if any(text.lstrip().startswith(opener) for text in _user_texts(record)):
                    return True
    except OSError:
        pass
    return False


def _find_transcript(agent: str, cwd: Path, tag: str, since: float) -> Path | None:
    for path in _candidates(agent, cwd, since):
        if _opens_with(path, tag):
            return path
    return None


def _session_id(agent: str, path: Path) -> str:
    if agent == "codex":
        found = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
                          path.name)
        return found.group(1) if found else path.stem
    return path.stem


def open_session(task: str, *, project: str = "", agent: str = "claude",
                 title: str = "") -> dict[str, Any]:
    """Open a terminal in his app, briefed to run `task` to completion."""

    from core.coding_job_contract import RepositoryResolutionError, resolve_repository_root

    agent = agent if agent in ("claude", "codex") else "claude"
    try:
        repo = resolve_repository_root(task, project_hint=project)
    except RepositoryResolutionError as exc:
        raise CodingSessionError(f"which project? {exc}") from exc
    tag = f"serena-task-{uuid.uuid4().hex[:10]}"
    base = backend_url()
    started = time.time()
    # The tag stands in as the pane's session key until the real id exists,
    # so the app can hand the same pane back instead of starting a second
    # process on the chat when he opens it.
    spawned = _request("POST", "/api/spawn-terminal", {
        "agent": agent, "cwd": str(repo), "seed": brief(task, repo=repo, tag=tag),
        "client_session_id": tag, "rows": 40, "cols": 140,
    }, base=base)
    if not spawned.get("ok"):
        raise CodingSessionError(f"his app would not open a terminal: {spawned}")
    _keep_flowing(base, spawned["terminal_id"])
    record = {
        "id": tag, "task": task.strip(), "project": repo.name, "cwd": str(repo),
        "agent": agent, "terminal_id": spawned.get("terminal_id"), "backend": base,
        "title": (title or f"Serena: {task.strip()}")[:80], "opened_at": started,
        "session_id": None, "transcript": None, "notified": False,
    }
    record["where"] = (f"his Serena app, {repo.name} project, the chat titled "
                       f"\"{record['title']}\"")
    state = _load()
    state[tag] = record
    _save(state)
    # Proof it exists and where, before it has done anything worth reporting.
    with contextlib.suppress(Exception):
        notify(f"started: {record['title']}. watch it live in your Serena app: "
               f"{repo.name} project, chat \"{record['title']}\". i'll text when it's done.")
    deadline = time.time() + FIND_SESSION_SECONDS
    while time.time() < deadline:
        if _transcript(record) is not None:
            break
        time.sleep(1.5)
    return record


def _keep_flowing(base: str, tid: str) -> None:
    """Hand a pane nobody is watching to the app's detached drain.

    A pane only drains its output while a window is attached. One spawned
    from here has no window yet, so it would stall on its first screenful.
    Attaching and letting go is what a closed browser tab does: the app keeps
    reading on its own, keeps the pane alive while it works, and replays the
    screen when he opens the chat.
    """

    import websocket  # websocket-client

    url = base.replace("http://", "ws://").replace("https://", "wss://") + f"/ws/terminal/{tid}"
    try:
        conn = websocket.create_connection(url, timeout=5)
    except Exception as exc:
        raise CodingSessionError(f"opened the terminal but could not attach to it: {exc}") from exc
    with contextlib.suppress(Exception):
        conn.recv()
    with contextlib.suppress(Exception):
        conn.close()


def _live_terminal(record: dict[str, Any]) -> str:
    """The pane for this session, reopened on its chat if the app reaped it."""

    base = record.get("backend") or backend_url()
    spawned = _request("POST", "/api/spawn-terminal", {
        "session_id": record["session_id"], "rows": 40, "cols": 140}, base=base)
    if not spawned.get("ok"):
        raise CodingSessionError(f"could not reopen that session: {spawned}")
    tid = spawned["terminal_id"]
    if not spawned.get("reused"):
        _keep_flowing(base, tid)
    if tid != record.get("terminal_id"):
        record["terminal_id"] = tid
        state = _load()
        if record["id"] in state:
            state[record["id"]]["terminal_id"] = tid
            _save(state)
    return tid


def _adopt(record: dict[str, Any], path: Path) -> None:
    """Tie a found transcript to the record and the pane to its real chat."""

    sid = _session_id(record["agent"], path)
    record.update(transcript=str(path), session_id=sid)
    with contextlib.suppress(CodingSessionError):
        _request("POST", "/api/terminal-runtime/migrate",
                 {"old_sid": record["id"], "new_sid": sid,
                  "terminal_id": record.get("terminal_id")},
                 base=record.get("backend"), timeout=10)
    with contextlib.suppress(CodingSessionError):  # a missing title is cosmetic
        _request("POST", f"/api/rename/{sid}", {"title": record["title"]},
                 base=record.get("backend"), timeout=10)
    state = _load()
    if record["id"] in state:
        state[record["id"]].update(transcript=str(path), session_id=sid)
        _save(state)


def _transcript(record: dict[str, Any], *, adopt: bool = True) -> Path | None:
    if record.get("transcript") and Path(record["transcript"]).exists():
        return Path(record["transcript"])
    path = _find_transcript(record["agent"], Path(record["cwd"]), record["id"],
                            float(record.get("opened_at") or 0))
    if path and adopt:
        _adopt(record, path)
    elif path:
        record.update(transcript=str(path), session_id=_session_id(record["agent"], path))
    return path


def _messages(path: Path) -> list[tuple[str, str, str]]:
    from core.parser import parse_messages_for_search

    return parse_messages_for_search(path)


def _assistant_text(record: Any) -> str:
    if not isinstance(record, dict):
        return ""
    if record.get("type") == "assistant":  # claude
        content = (record.get("message") or {}).get("content")
        if isinstance(content, list):
            return "".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
        return content if isinstance(content, str) else ""
    payload = record.get("payload") or {}
    if payload.get("type") == "agent_message":  # codex event_msg
        return str(payload.get("message") or "")
    if payload.get("type") == "message" and payload.get("role") == "assistant":
        return "".join(b.get("text", "") for b in payload.get("content") or []
                       if isinstance(b, dict) and b.get("type") in ("output_text", "text"))
    return ""


_LAST_SAID: dict[str, tuple[tuple[int, int], str]] = {}


def _last_said(path: Path, tail_bytes: int = 512 * 1024) -> str:
    """Her session's latest words, from the end of the transcript.

    The app asks every few seconds while a transcript grows by megabytes;
    parsing the whole file each time is what this avoids.
    """

    try:
        stat = path.stat()
    except OSError:
        return ""
    version = (stat.st_size, stat.st_mtime_ns)
    cached = _LAST_SAID.get(str(path))
    if cached and cached[0] == version:
        return cached[1]
    said = ""
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, stat.st_size - tail_bytes))
            lines = fh.read().splitlines()
        for line in reversed(lines[1:] if stat.st_size > tail_bytes else lines):
            try:
                text = _assistant_text(json.loads(line))
            except ValueError:
                continue
            if text.strip():
                said = text.strip()
                break
    except OSError:
        return ""
    if not said and stat.st_size > tail_bytes:
        texts = [t for role, t, _ in _messages(path) if role == "assistant" and t.strip()]
        said = texts[-1].strip() if texts else ""
    _LAST_SAID[str(path)] = (version, said)
    return said


def status(record: dict[str, Any], *, now: float | None = None,
           adopt: bool = True) -> dict[str, Any]:
    moment = time.time() if now is None else now
    path = _transcript(record, adopt=adopt)
    out = {"id": record["id"], "title": record["title"], "project": record["project"],
           "agent": record["agent"], "session_id": record.get("session_id"),
           "where": record.get("where", ""),
           "opened_minutes_ago": int((moment - float(record.get("opened_at") or moment)) // 60)}
    if path is None:
        out.update(state="starting", last="")
        return out
    last = _last_said(path)
    marker = next((line.strip() for line in reversed(last.splitlines())
                   if _MARKER.match(line)), "")
    quiet = moment - (_mtime(path) or moment)
    if record.get("stopped_at"):
        state = "stopped"
    elif marker and _MARKER.match(marker).group(1) == "DONE":
        state = "done"
    elif marker:
        state = "blocked"
    elif quiet <= WORKING_WITHIN_SECONDS:
        state = "working"
    else:
        state = "waiting"
    out.update(state=state, outcome=marker, last=last[-600:],
               last_activity_minutes_ago=int(quiet // 60))
    return out


def list_sessions(*, include_finished: bool = True, limit: int = 12,
                  adopt: bool = True) -> list[dict[str, Any]]:
    """Her sessions, newest first. The app lists them with adopt=False: its
    own request handler must not make HTTP calls back into itself."""

    records = sorted(_load().values(), key=lambda r: -float(r.get("opened_at") or 0))[:limit]
    out = [status(r, adopt=adopt) for r in records]
    return out if include_finished else [s for s in out if s["state"] not in ("done", "stopped")]


def read_session(session: str, *, messages: int = 6) -> dict[str, Any]:
    record = _record(session)
    path = _transcript(record)
    if path is None:
        return {**status(record), "recent": []}
    turns = [(role, text.strip()) for role, text, _ in _messages(path) if text.strip()]
    recent = [{"role": role, "text": text[-1200:]} for role, text in turns[-messages:]]
    return {**status(record), "recent": recent}


def steer_session(session: str, message: str, *, wait_seconds: float = 20) -> dict[str, Any]:
    """Type into her session, like he would. Returns whatever it said back in time."""

    record = _record(session)
    if not record.get("session_id"):
        _transcript(record)
    if not record.get("session_id"):
        raise CodingSessionError("that session has not started writing yet; try again shortly")
    _live_terminal(record)
    route = "/api/codex-bridge" if record["agent"] == "codex" else "/api/claude-bridge"
    result = _request("POST", route, {"target_sid": record["session_id"], "prompt": message,
                                      "timeout": wait_seconds},
                      base=record.get("backend"), timeout=wait_seconds + 15)
    return {"delivered": True, "reply": (result.get("response") or "")[-1200:],
            "note": "" if result.get("ok") else (result.get("message") or "still working on it")}


def stop_session(session: str) -> dict[str, Any]:
    record = _record(session)
    tid = record.get("terminal_id")
    if not tid:
        raise CodingSessionError("no terminal is recorded for that session")
    _request("POST", f"/api/kill-terminal/{tid}", {}, base=record.get("backend"), timeout=15)
    state = _load()
    state[record["id"]]["stopped_at"] = time.time()
    _save(state)
    return {"stopped": record["title"]}


# -- reporting back ---------------------------------------------------------

def notify(text: str) -> bool:
    """Text him on her line; the Jobs topic where the line has topics."""

    from core import phone_line

    body = " ".join(str(text).split())[:900]
    key = "coding:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    kwargs: dict[str, str] = {"key": key}
    if "topic" in inspect.signature(phone_line.send).parameters:  # a line with topics
        kwargs["topic"] = "jobs"
    return bool(phone_line.send(body, **kwargs))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) >= 2 and args[0] == "notify":
        return 0 if notify(" ".join(args[1:])) else 1
    if args[:1] == ["list"]:
        print(json.dumps(list_sessions(), indent=2))
        return 0
    print("usage: python -m core.serena_coding notify <text> | list", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
