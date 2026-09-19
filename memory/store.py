"""Filesystem-backed memory storage.

Memories live as Markdown files with YAML frontmatter under
``MEMORY_DIR/{type}/NNN-<slug>.md``. This is the single source of truth —
the web UI, the TUI, and the ``chats memory`` CLI all read and write the
same files.

Tasks count in their own sequence, everything else in another. His todo list
is the one surface he reads numbers off out loud, and sharing a counter with
every passing reference note pushed task #3 to #1093 without three digits of
work behind it. Both sequences are monotonic for the same reason: a
scheduler's dispatch receipt outlives the task it names, so finishing #53
never frees #53 — the next task is still #74. Because the two spaces now
overlap, an id alone no longer identifies a file, and ``_find_path`` refuses
an ambiguous one rather than guessing which of the two the caller meant.

Task ownership uses the voice inbox's discipline: serialize selection and
transition, expire abandoned claims, and fence acknowledgements by owner/token.
Here a bounded process lock and atomic replacement protect Markdown itself;
there is no second task database to drift from the files. A lease only fences
local writes. Dispatch must separately persist intent before starting Fleet.
"""

import math
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import suppress
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from core.config import MEMORY_DIR
from core.file_lock import exclusive_lock

MEMORY_TYPES = ["task", "ledger", "feedback", "user", "project", "reference", "general"]

# One counter per id space. Tasks are their own; every other type shares the
# original one, so existing memory ids keep meaning what they always meant.
TASK_COUNTER = ".task-next-id"
MEMORY_COUNTER = ".memory-next-id"


class AmbiguousMemoryId(LookupError):
    """An id that names both a task and a memory, with no type to choose by.

    Raised instead of returning either file: the two id spaces overlap by
    design, so a bare number is a question, not an address.
    """

# "backlog" is human-owned work: anything filed as a note, by an agent or by
# hand, and every task that predates the queue. Only enqueue_task, the phone
# and webhook boundary, can make work "ready", so the dispatcher never picks up
# a to-do list it was not handed.
TASK_STATES = frozenset({"backlog", "needs_triage", "ready", "claimed", "running", "blocked", "done"})
TASK_PRIORITIES = ("low", "normal", "high", "critical")
MAX_TASKS = 10000
TASK_LEASE_SECONDS = 30
TASK_WORK_SECONDS = 86400
_TASK_FIELDS = ("state", "assignee", "priority", "project_hint", "source_id",
                "lease_token", "lease_until", "run_id", "asked_at", "result")
# Frontmatter is parsed with str.splitlines(), which breaks on far more than
# LF and CR. Any of these inside a metadata value would be read back as a new
# key, so a caller-supplied hint could forge its own state and skip triage.
# Every separator str.splitlines() honours is listed here; the round-trip test
# re-derives this set over the whole Unicode range.
_LINE_BREAKS = frozenset("\n\v\f\r\x1c\x1d\x1e\x85\u2028\u2029")
_WRITE_LOCK = threading.RLock()
_WRITE_LOCAL = threading.local()


def _serialized_write(function):
    """All local writers cooperate, including legacy edits and ID allocation."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not _WRITE_LOCK.acquire(timeout=5):
            raise TimeoutError("memory writer is still owned")
        callbacks = []
        try:
            if getattr(_WRITE_LOCAL, "held", False):
                return function(*args, **kwargs)
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            with (MEMORY_DIR / ".task-queue.lock").open("a+b") as handle, exclusive_lock(handle, timeout=5):
                _WRITE_LOCAL.held = True
                _WRITE_LOCAL.callbacks = callbacks
                try:
                    result = function(*args, **kwargs)
                finally:
                    _WRITE_LOCAL.held = False
                    _WRITE_LOCAL.callbacks = None
        finally:
            _WRITE_LOCK.release()
        # Phone synchronization may perform network I/O. It must never hold
        # the local claim lock, including nested set_locket_id callbacks.
        for callback, call_args, call_kwargs in callbacks:
            with suppress(Exception):
                callback(*call_args, **call_kwargs)
        return result
    return wrapped


def _after_write(callback, *args, **kwargs):
    _WRITE_LOCAL.callbacks.append((callback, args, kwargs))


def _atomic_text(path: Path, text: str) -> None:
    """Publish a complete file, retaining the previous version on write failure."""
    fd, name = tempfile.mkstemp(prefix=".memory-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _one_line(value: str) -> bool:
    """A value safe to serialize as one frontmatter line and read back unchanged."""
    return not (_LINE_BREAKS & set(value)) and "---" not in value


def _flatten(value: str) -> str:
    """Free-text frontmatter: keep the prose, never a second parsed line."""
    for separator in _LINE_BREAKS:
        value = value.replace(separator, " ")
    return value


def _task_text(value, name: str, limit: int, *, optional=False) -> str:
    if optional and value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValueError(f"invalid task {name}")
    value = value.strip()
    if not value and not optional:
        raise ValueError(f"task {name} is required")
    if name != "text" and not _one_line(value):
        raise ValueError(f"invalid task {name}")
    return value


# Words that name nothing to act on: grammar, politeness, urgency, bare
# symptoms, and the action verbs themselves. A brief built only from these is
# padding, however long it runs. Words of two characters or fewer are dropped
# outright, which covers the rest of the closed-class vocabulary.
_PADDING_VOCABULARY = """
the this that these those there here and but for nor yet with from into onto about after
before while when because since than then also just only even still again very really quite
please pls plz can could would will shall should must may might need needs needed want wants
you your yours our ours their theirs his her hers its who whom whose what which
was were are been being have has had does did doing done get gets got
now today tonight tomorrow yesterday soon later asap urgent urgently immediately
any all some more most much many anything something everything nothing thing things stuff
broke broken breaks breaking break bad wrong weird strange odd off funny annoying
issue issues problem problems bug bugs error errors fail fails failing failure failures
work works working sometimes randomly random lately recently often always never
fix fixes fixed add adds added implement build builds update updates remove removes repair
repairs test tests create creates investigate diagnose refactor replace replaces resolve
"""
_TASK_PADDING = frozenset(_PADDING_VOCABULARY.split())


def classify_task(text: str, project_hint: str | None = None) -> str:
    """Conservative, pure triage; nothing outside the brief can supply a target.

    A brief is actionable only when it names an action AND enough substantive
    words to identify what to act on. A project hint is deliberately no
    evidence at all: it says where to work, never what to do, so "please fix
    it, it is still broken" stays needs_triage no matter which repository it
    arrives with. MAST attributes ~41.8% of multi-agent failures to
    specification issues, so an underspecified brief waits for one question
    rather than opening a run. This is a classification boundary, not
    permission to execute prose; ingress approval and reviewed dispatch remain
    separate.
    """
    text = _task_text(text, "text", 4000)
    _task_text(project_hint, "project", 512, optional=True)  # validated, never evidence
    words = re.findall(r"\b\w+\b", text.lower())
    # He writes the verb he means, not the verb a triager expects. "enable
    # workouts in Locket" and "research why X happens" are as actionable as
    # "fix X"; leaving them out parked real work as needs_triage.
    action = re.search(r"\b(fix|add|implement|build|update|remove|repair|test|create|"
                       r"investigate|diagnose|refactor|replace|resolve|enable|disable|"
                       r"research|change|make|migrate|integrate|connect|wire|rename|"
                       r"ship|support)\b", text, re.I)
    substantive = {word for word in words if len(word) > 2 and word not in _TASK_PADDING}
    return "ready" if action and len(words) >= 8 and len(substantive) >= 4 else "needs_triage"


def _task_rows() -> list[dict]:
    rows = []
    ids = set()
    for index, path in enumerate((MEMORY_DIR / "task").glob("*.md")):
        if index >= MAX_TASKS:
            raise ValueError("task queue capacity exceeded")
        row = _parse_file(path)
        if row and row["type"] == "task":
            if row["id"] in ids:
                raise ValueError("duplicate task id requires reconciliation")
            ids.add(row["id"])
            rows.append(row)
    return rows


@_serialized_write
def enqueue_task(text: str, project_hint: str | None = None,
                 priority: str = "normal", source_id: str | None = None) -> dict:
    """Persist approved work, independently of the legacy MemoryV2 proposal UI.

    This is only a local queue write, never approval or dispatch. Receipt keys
    deduplicate retries under the same lock as ID allocation and publication.
    """
    text = _task_text(text, "text", 4000)
    project = _task_text(project_hint, "project", 512, optional=True)
    source = _task_text(source_id, "source_id", 512, optional=True)
    if not isinstance(priority, str) or priority not in TASK_PRIORITIES:
        raise ValueError("invalid task priority")
    rows = _task_rows()
    for row in rows:
        if source and row["source_id"] == source:
            return _clean(row)
    if len(rows) >= MAX_TASKS:
        raise ValueError("task queue capacity exceeded")
    path = _write_file(_next_id("task"), "task", text, task_fields={
        "state": classify_task(text, project), "assignee": "", "priority": priority,
        "project_hint": project, "source_id": source or f"queue:{uuid.uuid4().hex}",
    })
    return _clean(_parse_file(path))


def _moment(now=None) -> float:
    value = time.time() if now is None else float(now)
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid task timestamp")
    return value


def _lease_seconds(value) -> float:
    value = float(value)
    if not math.isfinite(value) or not 1 <= value <= TASK_WORK_SECONDS:
        raise ValueError("task lease must be between 1 and 86400 seconds")
    return value


def _lease_alive(row: dict, now: float) -> bool:
    try:
        until = float(row.get("lease_until") or 0)
        return math.isfinite(until) and until > now
    except (ValueError, TypeError):
        return False


def _task_metadata(row: dict, **fields) -> dict:
    """Called only while locked; retain body, filename and unknown frontmatter.

    Transitions serialize the same way creation does, so the same one-line rule
    applies: a forged separator here would outrank the state we just decided.
    """
    path = row["_path"]
    _start, frontmatter, body = path.read_text(encoding="utf-8").split("---", 2)
    fields["updated"] = _now()
    for key, value in fields.items():
        if not _one_line(str(key)) or not _one_line(str(value)):
            raise ValueError(f"invalid task {key}")
    lines = [line for line in frontmatter.strip().splitlines()
             if line.partition(":")[0].strip() not in fields]
    lines.extend(f"{key}: {value}" for key, value in fields.items())
    _atomic_text(path, "---\n" + "\n".join(lines) + "\n---" + body)
    return _parse_file(path)


@_serialized_write
def claim_next_task(owner: str, now=None, lease_seconds=TASK_LEASE_SECONDS) -> dict | None:
    """Claim one highest-priority unsnoozed task; stale claim tokens lose ownership.

    Mirrors voice_inbox.claim_next: expire, select, transition in one critical
    section. Running expiry blocks rather than retrying an uncertain side effect.
    """
    owner = _task_text(owner, "assignee", 256)
    moment = _moment(now)
    duration = _lease_seconds(lease_seconds)
    ready = []
    for row in _task_rows():
        if row["state"] in {"claimed", "running"} and not _lease_alive(row, moment):
            state = "ready" if row["state"] == "claimed" else "blocked"
            row = _task_metadata(row, state=state, assignee="", lease_token="", lease_until="")
        # Only enqueue_task stamps a source id. Older writers defaulted plain
        # notes to "ready"; without a source they are never dispatch input.
        if row["state"] == "ready" and row["source_id"] and not _is_snoozed(row):
            ready.append(row)
    if not ready:
        return None
    row = min(ready, key=lambda item: (-TASK_PRIORITIES.index(item["priority"]), item["id"]))
    return _clean(_task_metadata(row, state="claimed", assignee=owner,
                                lease_token=uuid.uuid4().hex, lease_until=moment + duration))


def _owned_task(task_id: int, owner: str, token: str, now: float) -> dict | None:
    path = _find_task_path(task_id)
    row = _parse_file(path) if path else None
    if (not row or row["type"] != "task" or row["state"] not in {"claimed", "running"}
            or not owner or not token or row["assignee"] != owner
            or row["lease_token"] != token or not _lease_alive(row, now)):
        return None
    return row


@_serialized_write
def mark_task_running(task_id: int, owner: str, token: str, run_id: str, *, now=None) -> bool:
    run_id = _task_text(run_id, "run_id", 256)
    moment = _moment(now)
    row = _owned_task(task_id, owner, token, moment)
    if not row or row["state"] != "claimed":
        return False
    _task_metadata(row, state="running", run_id=run_id, lease_until=moment + TASK_WORK_SECONDS)
    return True


@_serialized_write
def release_task_claim(task_id: int, owner: str, token: str, *, state="ready", now=None) -> bool:
    if state not in {"ready", "blocked", "needs_triage", "done"}:
        raise ValueError("invalid task release state")
    row = _owned_task(task_id, owner, token, _moment(now))
    if not row or (row["state"] == "running" and state == "ready"):
        return False
    _task_metadata(row, state=state, assignee="", lease_token="", lease_until="")
    return True


@_serialized_write
def renew_task_claim(task_id: int, owner: str, token: str, *, now=None,
                     lease_seconds=TASK_LEASE_SECONDS) -> bool:
    moment = _moment(now)
    duration = _lease_seconds(lease_seconds)
    row = _owned_task(task_id, owner, token, moment)
    if not row:
        return False
    _task_metadata(row, lease_until=moment + duration)
    return True


def tasks_in_state(*states: str) -> list[dict]:
    """Queue rows in any of the given states, oldest first."""
    wanted = set(states)
    return [_clean(row) for row in sorted(_task_rows(), key=lambda row: row["id"])
            if row["state"] in wanted]


@_serialized_write
def finish_task_run(task_id: int, run_id: str, state: str, result: str = "") -> bool:
    """Close a dispatched task once its Fleet run is terminal.

    The run id is the credential here, not a lease: the dispatcher that opened
    the run is long gone by the time it finishes, and the reservation ledger
    guarantees one task maps to one run.
    """
    if state not in {"done", "blocked"}:
        raise ValueError("invalid task finish state")
    run_id = _task_text(run_id, "run_id", 256)
    path = _find_task_path(task_id)
    row = _parse_file(path) if path else None
    if not row or row["type"] != "task" or row["state"] != "running" or row["run_id"] != run_id:
        return False
    _task_metadata(row, state=state, assignee="", lease_token="", lease_until="",
                   result=_flatten(str(result or ""))[:500])
    return True


@_serialized_write
def reopen_task_run(task_id: int, run_id: str, *, now=None) -> bool:
    """Put a blocked dispatched task back on its (retried) run."""
    run_id = _task_text(run_id, "run_id", 256)
    path = _find_task_path(task_id)
    row = _parse_file(path) if path else None
    if not row or row["type"] != "task" or row["state"] != "blocked" or row["run_id"] != run_id:
        return False
    _task_metadata(row, state="running", assignee="phone-retry",
                   lease_token=uuid.uuid4().hex,
                   lease_until=_moment(now) + TASK_WORK_SECONDS, result="")
    return True


@_serialized_write
def mark_task_asked(task_id: int, now=None) -> bool:
    """Record that the one triage question went out, so it is asked once."""
    path = _find_task_path(task_id)
    row = _parse_file(path) if path else None
    if not row or row["type"] != "task" or row["state"] != "needs_triage" or row["asked_at"]:
        return False
    _task_metadata(row, asked_at=int(_moment(now)))
    return True


@_serialized_write
def answer_triage(task_id: int, answer: str) -> dict | None:
    """Fold his answer into the brief and triage it again.

    The combined brief goes through the same classifier as a fresh one, so an
    answer that still says nothing actionable leaves the task waiting instead
    of opening a run. A second question is not sent automatically.
    """
    answer = _task_text(_flatten(str(answer)) if isinstance(answer, str) else answer,
                        "answer", 2000)
    path = _find_task_path(task_id)
    row = _parse_file(path) if path else None
    if not row or row["type"] != "task" or row["state"] != "needs_triage":
        return None
    brief = _task_text(f"{row['content']}\n\nClarification: {answer}", "text", 6000)
    state = classify_task(brief[:4000], row.get("project_hint") or None)
    new_path = _write_file(task_id, "task", brief, created=row["created_at"], snooze=row["snooze_until"],
                locket_id=row["locket_id"], source_session_id=row["source_session_id"],
                source_agent=row["source_agent"], source_title=row["source_title"],
                source_message_timestamp=row["source_message_timestamp"],
                task_fields={"state": state})
    if new_path != path:
        path.unlink(missing_ok=True)
    return _clean(_parse_file(new_path))


_V2_TYPE_MAP = {
    "task": "commitment",
    "ledger": "commitment",
    "feedback": "correction",
    "user": "semantic_fact",
    "project": "semantic_fact",
    "reference": "procedure",
    "general": "episode",
}

# Order + human headers for the session digest. Tasks lead so every chat opens
# like someone who remembers what we were doing, not a rules dump. Tasks =
# anything owed, whether Raghav owns it or I do; ledgers = structured
# current-state cards for active threads (one per thread, updated in place —
# the thing either agent reads before answering and writes after acting, so a
# handoff doesn't mean re-guessing where things stand).
#
# There is deliberately no "loop" type. Work in progress is a task. Memory is
# for what we did, how things work, and who Raghav is, never for what is owed.
TYPE_ORDER = ["task", "ledger", "user", "feedback", "project", "reference", "general"]
TYPE_HEADERS = {
    "task": "Raghav's tasks — nudge him on these",
    "ledger": "Active ledgers — exact state of live threads",
    "user": "About Raghav",
    "feedback": "How to work with him",
    "project": "Projects & context",
    "reference": "Reference",
    "general": "Other",
}

# Ledger fields, in the fixed order they're always shown. Each ledger is one
# mutable card per active thread (keyed by ledger_key), not an append-only log.
LEDGER_FIELDS = ["goal", "facts", "decision", "promise", "risk", "next_action"]


def _slugify(text: str, max_len: int = 50) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    return slug[:max_len].rstrip("-")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ago(ts: str) -> str:
    """Human relative time for open loops ('2d ago'). Empty on bad input."""
    try:
        then = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ""
    secs = (datetime.now() - then).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _parse_file(fpath: Path) -> dict | None:
    try:
        text = fpath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    meta: dict = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    body = parts[2].strip()
    try:
        mid = int(meta.get("id", 0))
    except ValueError:
        return None
    out = {
        "id": mid,
        "type": meta.get("type", "general"),
        "content": body,
        "created_at": meta.get("created", ""),
        "updated_at": meta.get("updated", ""),
        "snooze_until": meta.get("snooze", ""),
        "locket_id": meta.get("locket_id", ""),
        "source_session_id": meta.get("source_session_id", ""),
        "source_agent": meta.get("source_agent", ""),
        "source_title": meta.get("source_title", ""),
        "source_message_timestamp": meta.get("source_message_timestamp", ""),
        "_path": fpath,
        "filename": fpath.name,
    }
    if meta.get("type") == "ledger":
        out["ledger_key"] = meta.get("ledger_key", "")
        for f in LEDGER_FIELDS:
            out[f] = meta.get(f, "")
    if out["type"] == "task":
        out.update({key: meta.get(key, "") for key in _TASK_FIELDS})
        # Missing fields are legacy work, which stays human-owned; malformed
        # explicit values fail closed.
        out["state"] = meta.get("state", "backlog")
        if out["state"] not in TASK_STATES:
            out["state"] = "needs_triage"
        elif out["state"] == "ready" and not out["source_id"]:
            # Older writers defaulted every note to "ready". Only enqueue_task
            # stamps a source, so anything else is still human-owned backlog.
            out["state"] = "backlog"
        out["priority"] = meta.get("priority", "normal")
        if out["priority"] not in TASK_PRIORITIES:
            out["priority"] = "normal"
            out["state"] = "needs_triage"
    return out


def _is_snoozed(m: dict) -> bool:
    """True if this item is snoozed past now (deferred, don't surface it)."""
    su = m.get("snooze_until", "")
    if not su:
        return False
    try:
        return datetime.strptime(su, "%Y-%m-%d %H:%M:%S") > datetime.now()
    except (ValueError, TypeError):
        return False


def _scan_all() -> list[dict]:
    out = []
    for t in MEMORY_TYPES:
        d = MEMORY_DIR / t
        if not d.exists():
            continue
        for f in d.glob("*.md"):
            m = _parse_file(f)
            if m:
                out.append(m)
    return out


def _find_path(memory_id: int, mem_type: str | None = None) -> Path | None:
    """Locate one memory file. ``mem_type`` picks the id space to look in.

    Without it both spaces are searched, and an id living in both raises
    rather than resolving to whichever type sorts first.
    """
    types = [mem_type] if mem_type else MEMORY_TYPES
    prefix = f"{memory_id:03d}-"
    hits: list[Path] = []
    for t in types:
        d = MEMORY_DIR / t
        if not d.exists():
            continue
        for f in d.glob(f"{prefix}*.md"):
            hits.append(f)
            break
    if not hits:
        # Frontmatter is the authority when a filename was hand-renamed.
        hits = [m["_path"] for m in _scan_all()
                if m["id"] == memory_id and (not mem_type or m["type"] == mem_type)]
    if len(hits) > 1:
        kinds = sorted({h.parent.name for h in hits})
        raise AmbiguousMemoryId(
            f"#{memory_id} names more than one thing ({', '.join(kinds)}); "
            f"pass a type to say which")
    return hits[0] if hits else None


def _find_task_path(task_id: int) -> Path | None:
    return _find_path(task_id, "task")


@_serialized_write
def _write_file(mem_id: int, mem_type: str, content: str,
                created: str = "", updated: str = "", snooze: str = "",
                locket_id: str = "", source_session_id: str = "",
                source_agent: str = "", source_title: str = "",
                source_message_timestamp: str = "",
                ledger_key: str = "", ledger_fields: dict | None = None,
                task_fields: dict | None = None) -> Path:
    if not created:
        created = _now()
    if not updated:
        updated = _now()
    type_dir = MEMORY_DIR / mem_type
    type_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify(ledger_key if mem_type == "ledger" and ledger_key else content) or "memory"
    fpath = type_dir / f"{mem_id:03d}-{slug}.md"
    fm = f"---\nid: {mem_id}\ntype: {mem_type}\ncreated: {created}\nupdated: {updated}\n"
    if snooze:
        fm += f"snooze: {snooze}\n"
    if locket_id:
        # Link to the always-on Locket serena_memories row, so bot edits
        # and deletes from the phone reconcile to the right local file.
        fm += f"locket_id: {locket_id}\n"
    if source_session_id:
        fm += f"source_session_id: {source_session_id}\n"
    if source_agent:
        fm += f"source_agent: {source_agent}\n"
    if source_title:
        # Captured from a chat title, so it is free text on a task's own
        # frontmatter. Flatten every separator, not just LF.
        fm += f"source_title: {_flatten(source_title)[:500]}\n"
    if source_message_timestamp:
        fm += f"source_message_timestamp: {source_message_timestamp}\n"
    if mem_type == "ledger":
        fm += f"ledger_key: {ledger_key}\n"
        for f in LEDGER_FIELDS:
            v = (ledger_fields or {}).get(f, "")
            fm += f"{f}: {_flatten(v).strip()}\n"
    if mem_type == "task":
        previous_path = _find_path(mem_id, "task")
        previous = _parse_file(previous_path) if previous_path else {}
        fields = {key: (previous or {}).get(key, "") for key in _TASK_FIELDS}
        fields.update(task_fields or {})
        fields["state"] = fields.get("state") or "backlog"
        fields["priority"] = fields.get("priority") or "normal"
        if fields["state"] not in TASK_STATES or fields["priority"] not in TASK_PRIORITIES:
            raise ValueError("invalid task state or priority")
        for key in _TASK_FIELDS:
            value = str(fields.get(key, ""))
            if not _one_line(value):
                raise ValueError(f"invalid task {key}")
            fm += f"{key}: {value}\n"
    fm += "---\n"
    _atomic_text(fpath, f"{fm}\n{content}\n")
    return fpath


@_serialized_write
def set_locket_id(memory_id: int, locket_id: int, mem_type: str | None = None) -> None:
    """Stamp an existing local memory with its Locket row id (rewrites the
    file in place, preserving everything else)."""
    fpath = _find_path(memory_id, mem_type)
    if not fpath:
        return
    m = _parse_file(fpath)
    if not m:
        return
    new_path = _write_file(memory_id, m["type"], m["content"],
                created=m["created_at"], updated=m["updated_at"],
                snooze=m.get("snooze_until", ""), locket_id=str(locket_id),
                source_session_id=m.get("source_session_id", ""),
                source_agent=m.get("source_agent", ""),
                source_title=m.get("source_title", ""),
                source_message_timestamp=m.get("source_message_timestamp", ""))
    if new_path != fpath:
        fpath.unlink()


@_serialized_write
def _next_id(mem_type: str = "general") -> int:
    # A scheduler's durable dispatch receipt outlives a deleted task, so the
    # counter only moves forward: a crash may leave a harmless gap, but two
    # tasks can never share a receipt. The counter is the authority on what
    # has been handed out; the scan is a fallback for when it is missing, and
    # a stray high id is stepped over rather than dragging every later id up
    # to meet it. One task imported from a machine that predated the counter
    # used to pin his whole todo list in the 1100s.
    is_task = mem_type == "task"
    counter = MEMORY_DIR / (TASK_COUNTER if is_task else MEMORY_COUNTER)
    taken = {m["id"] for m in _scan_all() if (m["type"] == "task") is is_task}
    mid = (int(counter.read_text(encoding="utf-8")) if counter.exists()
           else max(taken, default=0) + 1)
    while mid in taken:
        mid += 1
    _atomic_text(counter, str(mid + 1))
    return mid


def _rewrite_index():
    """Regenerate MEMORY_DIR/INDEX.md grouped by type."""
    memories = sorted(_scan_all(), key=lambda m: (m["type"], m["id"]))
    by_type: dict[str, list[dict]] = {}
    for m in memories:
        by_type.setdefault(m["type"], []).append(m)
    lines = ["# Memory", "", "Persistent memories grouped by type. Each file is one memory.", ""]
    for t in MEMORY_TYPES:
        mems = by_type.get(t, [])
        if not mems:
            continue
        lines.append(f"## {t.title()} ({len(mems)})")
        lines.append("")
        for m in mems:
            if t == "ledger":
                summary = f"[{m.get('ledger_key', '?')}] {m.get('goal', '').strip()}"[:80]
            else:
                summary = m["content"].split("\n")[0][:80]
            lines.append(f"- [#{m['id']}](./{m['type']}/{m['filename']}) \u2014 {summary}")
        lines.append("")
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    (MEMORY_DIR / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _clean(m: dict) -> dict:
    return {k: v for k, v in m.items() if not k.startswith("_")}


def list_memories(type_filter: str | None = None) -> list[dict]:
    memories = _scan_all()
    if type_filter:
        memories = [m for m in memories if m["type"] == type_filter]
    memories.sort(key=lambda m: (m["type"], -m["id"]))
    return [_clean(m) for m in memories]


def _source_context() -> tuple[str, str, str, str]:
    sid = (
        os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CODEX_THREAD_ID")
        or ""
    ).strip()
    if not sid:
        return "", "", "", ""
    agent = "claude" if os.environ.get("CLAUDE_CODE_SESSION_ID") else "codex"
    title = ""
    try:
        from core.indexer import get_session

        row = get_session(sid)
        if row:
            title = str(row.get("display_title") or row.get("title") or "")
    except Exception:
        pass
    return sid, agent, title, datetime.now().astimezone().isoformat()


def _active_v2_store():
    from memory.v2 import MemoryV2Store

    return MemoryV2Store() if MemoryV2Store.authority_is_active() else None


def _v2_source(
    *,
    action: str,
    content: str,
    source_session_id: str = "",
    source_agent: str = "",
) -> dict:
    from memory.v2 import source_receipt

    if not source_session_id:
        source_session_id, source_agent, _title, _timestamp = _source_context()
    locator = f"{source_agent or 'operator'}:{source_session_id or 'local'}:{action}"
    return source_receipt(
        kind="legacy_memory_surface",
        locator=locator,
        source_text=content,
        session_id=source_session_id,
        surface="memory-store",
    )


def _flush_v2_outbox(store, proposal: dict) -> dict:
    try:
        store.flush_control_outbox()
    except Exception:
        proposal["control_event_pending"] = True
    return proposal


@_serialized_write
def add_memory(
    content: str,
    mem_type: str = "general",
    _no_mirror: bool = False,
    source_session_id: str = "",
    source_agent: str = "",
    source_title: str = "",
    source_message_timestamp: str = "",
) -> int | str:
    if mem_type not in MEMORY_TYPES:
        mem_type = "general"
    if not source_session_id and not _no_mirror:
        (
            source_session_id,
            source_agent,
            source_title,
            source_message_timestamp,
        ) = _source_context()
    if not _no_mirror and (v2 := _active_v2_store()) is not None:
        proposal = v2.propose_candidate(
            content=content,
            record_type=_V2_TYPE_MAP[mem_type],
            source=_v2_source(
                action="add",
                content=content,
                source_session_id=source_session_id,
                source_agent=source_agent,
            ),
            sensitivity="personal",
        )
        return str(_flush_v2_outbox(v2, proposal)["proposal_id"])
    mid = _next_id(mem_type)
    _write_file(
        mid,
        mem_type,
        content,
        source_session_id=source_session_id,
        source_agent=source_agent,
        source_title=source_title,
        source_message_timestamp=source_message_timestamp,
    )
    _rewrite_index()
    # Keep phone-Serena's brain in sync (fail-soft; Locket down = local only).
    # _no_mirror=True when the row is being created BY a pull from Locket,
    # so we don't echo it straight back and duplicate.
    if not _no_mirror:
        try:
            from memory.locket_mirror import mirror_add
            _after_write(mirror_add,
                content,
                mem_type,
                mid,
                source_session_id=source_session_id,
                source_agent=source_agent,
                source_title=source_title,
                source_message_timestamp=source_message_timestamp,
            )
        except Exception:
            pass
    return mid


@_serialized_write
def update_memory(
    memory_id: int, content: str | None = None, mem_type: str | None = None,
    find_type: str | None = None
) -> str | None:
    fpath = _find_path(memory_id, find_type)
    if not fpath:
        return
    existing = _parse_file(fpath)
    if not existing:
        return
    new_content = content if content is not None else existing["content"]
    new_type = mem_type if mem_type is not None else existing["type"]
    if new_type not in MEMORY_TYPES:
        new_type = existing["type"]
    if (v2 := _active_v2_store()) is not None:
        target = f"legacy:{existing['type']}:{memory_id}"
        current = v2.get_record(target)
        if current is None:
            raise RuntimeError(f"Memory v2 has no canonical record for legacy memory #{memory_id}")
        proposal = v2.create_proposal(
            operation="update",
            target_record_id=target,
            candidate={
                "record_type": _V2_TYPE_MAP[new_type],
                "content": new_content,
                "confidence": current.confidence,
                "sensitivity": current.sensitivity,
                "project": current.project,
                "people": list(current.people),
                "valid_from": current.valid_from,
                "valid_until": current.valid_until,
                "retention_until": current.retention_until,
            },
            source=_v2_source(action="update", content=new_content),
        )
        return str(_flush_v2_outbox(v2, proposal)["proposal_id"])
    # Move to new type folder (or rename slug) by writing fresh and removing old
    new_path = _write_file(
        memory_id, new_type, new_content,
        created=existing["created_at"],
        updated=_now(),
        snooze=existing.get("snooze_until", ""),
        locket_id=existing.get("locket_id", ""),
        source_session_id=existing.get("source_session_id", ""),
        source_agent=existing.get("source_agent", ""),
        source_title=existing.get("source_title", ""),
        source_message_timestamp=existing.get("source_message_timestamp", ""),
    )
    if fpath != new_path:
        with suppress(OSError):
            fpath.unlink()
    _rewrite_index()
    try:
        if new_type == existing["type"]:
            from memory.locket_mirror import mirror_update
            _after_write(mirror_update,
                existing["content"],
                new_content,
                existing.get("locket_id", ""),
            )
        else:
            from memory.locket_mirror import mirror_add, mirror_delete
            _after_write(mirror_delete, existing["content"], existing.get("locket_id", ""))
            _after_write(mirror_add,
                new_content,
                new_type,
                memory_id,
                source_session_id=existing.get("source_session_id", ""),
                source_agent=existing.get("source_agent", ""),
                source_title=existing.get("source_title", ""),
                source_message_timestamp=existing.get("source_message_timestamp", ""),
            )
    except Exception:
        pass


@_serialized_write
def delete_memory(memory_id: int, mem_type: str | None = None) -> bool | str:
    fpath = _find_path(memory_id, mem_type)
    if not fpath:
        return False
    existing = _parse_file(fpath)
    if existing and (v2 := _active_v2_store()) is not None:
        target = f"legacy:{existing['type']}:{memory_id}"
        if v2.get_record(target) is None:
            raise RuntimeError(f"Memory v2 has no canonical record for legacy memory #{memory_id}")
        proposal = v2.create_proposal(
            operation="forget",
            target_record_id=target,
            source=_v2_source(action="forget", content=existing["content"]),
        )
        return str(_flush_v2_outbox(v2, proposal)["proposal_id"])
    try:
        fpath.unlink()
    except OSError:
        return False
    _rewrite_index()
    # Completed tasks and loops stay in Locket's history. Other memory types
    # retain their explicit hard-delete behavior.
    if existing:
        try:
            if existing["type"] == "task":
                from memory.locket_mirror import mirror_archive
                _after_write(mirror_archive, existing["content"], existing.get("locket_id", ""))
            else:
                from memory.locket_mirror import mirror_delete
                _after_write(mirror_delete, existing["content"], existing.get("locket_id", ""))
        except Exception:
            pass
    return True


@_serialized_write
def snooze_memory(memory_id: int, days: float = 7,
                  mem_type: str | None = None) -> bool | str:
    """Defer an item (task/loop): hide it from the nudge rail until `days`
    from now, so a different one surfaces instead. Content is untouched."""
    fpath = _find_path(memory_id, mem_type)
    if not fpath:
        return False
    existing = _parse_file(fpath)
    if not existing:
        return False
    until = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    if (v2 := _active_v2_store()) is not None:
        target = f"legacy:{existing['type']}:{memory_id}"
        current = v2.get_record(target)
        if current is None:
            raise RuntimeError(f"Memory v2 has no canonical record for legacy memory #{memory_id}")
        proposal = v2.create_proposal(
            operation="update",
            target_record_id=target,
            candidate={
                "record_type": current.record_type,
                "content": current.content,
                "confidence": current.confidence,
                "sensitivity": current.sensitivity,
                "project": current.project,
                "people": list(current.people),
                "valid_from": datetime.strptime(until, "%Y-%m-%d %H:%M:%S").timestamp(),
                "valid_until": current.valid_until,
                "retention_until": current.retention_until,
            },
            source=_v2_source(action="snooze", content=existing["content"]),
        )
        return str(_flush_v2_outbox(v2, proposal)["proposal_id"])
    new_path = _write_file(
        memory_id, existing["type"], existing["content"],
        created=existing["created_at"], updated=existing["updated_at"],
        snooze=until, locket_id=existing.get("locket_id", ""),
        source_session_id=existing.get("source_session_id", ""),
        source_agent=existing.get("source_agent", ""),
        source_title=existing.get("source_title", ""),
        source_message_timestamp=existing.get("source_message_timestamp", ""),
    )
    if fpath != new_path:
        with suppress(OSError):
            fpath.unlink()
    return True


def find_ledger(key: str) -> dict | None:
    """Find the ledger card for this key, if one exists."""
    key = key.strip()
    for m in _scan_all():
        if m["type"] == "ledger" and m.get("ledger_key") == key:
            return _clean(m)
    return None


@_serialized_write
def upsert_ledger(key: str, **fields) -> int:
    """Create or update the ledger card for `key`. Only fields actually
    passed are changed — omitted fields keep their current value. `key`
    should be a short stable slug for the thread (e.g. 'persona-tuning'),
    not a free-text description. Fields: goal, facts, decision, promise,
    risk, next_action."""
    key = key.strip()
    if not key:
        raise ValueError("ledger key required")
    existing = None
    for m in _scan_all():
        if m["type"] == "ledger" and m.get("ledger_key") == key:
            existing = m
            break
    merged = {f: fields.get(f, existing.get(f, "") if existing else "") for f in LEDGER_FIELDS}
    if existing:
        mid = existing["id"]
        fpath = existing["_path"]
        new_path = _write_file(
            mid, "ledger", "", created=existing["created_at"], updated=_now(),
            source_session_id=existing.get("source_session_id", ""),
            source_agent=existing.get("source_agent", ""),
            source_title=existing.get("source_title", ""),
            ledger_key=key, ledger_fields=merged,
        )
        if fpath != new_path:
            with suppress(OSError):
                fpath.unlink()
    else:
        mid = _next_id("ledger")
        source_session_id, source_agent, source_title, _ts = _source_context()
        _write_file(
            mid, "ledger", "", ledger_key=key, ledger_fields=merged,
            source_session_id=source_session_id, source_agent=source_agent,
            source_title=source_title,
        )
    _rewrite_index()
    return mid


def get_memory(memory_id: int, mem_type: str | None = None) -> dict | None:
    fpath = _find_path(memory_id, mem_type)
    if not fpath:
        return None
    m = _parse_file(fpath)
    return _clean(m) if m else None


def search_memories(query: str) -> list[dict]:
    """Search memory through the one retrieval authority.

    This used to be its own substring scan over every file, which made it the
    only search surface that did not go through `retrieve_memory`: it ignored
    the active authority, scored nothing, and returned rows with no record_id,
    so a hit found here could not be matched against a hit found anywhere else.
    The surface stays the default private one: this is a local search over his
    own memory, and the old scan read everything. Imported lazily because
    retrieval reads this module.
    """

    from memory.retrieval import search_memory_records

    return search_memory_records(query)


def format_loops() -> str:
    """Retired. Always empty.

    The "loop" memory type is gone: a parallel backlog of half-tracked threads
    grew to 69 entries, most of them either already shipped or actively wrong,
    and a rail that size stops being read. Anything owed is a task now. Kept as
    a no-op so older callers and the `chats memory loops` command do not break.
    """
    return ""


def format_ledgers() -> str:
    """Structured current-state cards for active threads. Check these before
    answering on a thread that has one, and update via `chats memory ledger`
    after deciding/promising/acting — this is what a handoff actually reads
    from, whichever agent picks the thread up next. Empty string if none."""
    ledgers = [m for m in _scan_all() if m["type"] == "ledger" and not _is_snoozed(m)]
    if not ledgers:
        return ""
    ledgers.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
    lines = ["[active ledgers — read the relevant one before answering on that thread, "
             "update it (`chats memory ledger <key> --field ...`) after you decide, promise, "
             "or act. Whichever agent, claude or codex, picks this thread up next reads this, "
             "not a re-guess:]"]
    for m in ledgers:
        age = _ago(m.get("updated_at", ""))
        suffix = f"  (updated {age})" if age else ""
        lines.append(f"- [{m['id']}] ledger: {m.get('ledger_key', '?')}{suffix}")
        for f in LEDGER_FIELDS:
            v = (m.get(f) or "").strip()
            if v:
                lines.append(f"    {f}: {v}")
    return "\n".join(lines)


def format_tasks() -> str:
    """Raghav's deliberate todo list, freshest first, with age (so I can tell
    what's gone stale and nudge harder). Empty string if none."""
    tasks = [m for m in _scan_all() if m["type"] == "task"
             and m.get("state") != "done" and not _is_snoozed(m)]
    if not tasks:
        return ""
    tasks.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
    lines = ["[Raghav's tasks. Surface the most relevant ONE when a chat opens and STEER him on "
             "it: tell him to do it, or give a strict this-or-that, never an open-ended 'what do "
             "you want to work on'. If he defers ('later', 'not now', 'skip it'), run "
             "`chats memory snooze <id>` so it goes quiet for ~a week and a different task surfaces "
             "next time. Don't pile on multiple tasks at once. Age = how stale:]"]
    for m in tasks:
        age = _ago(m.get("updated_at", ""))
        suffix = f"  ({age})" if age else ""
        lines.append(f"- [{m['id']}] {m['content'].strip()}{suffix}")
    return "\n".join(lines)


def format_active() -> str:
    """Tasks + ledgers — the per-turn payload injected by the
    UserPromptSubmit hook so I stay grounded on what's live every single turn,
    even deep into a long chat after the session digest compacts away."""
    parts = [format_tasks(), format_ledgers()]
    return "\n\n".join(p for p in parts if p)


def format_for_claude() -> str:
    memories = list_memories()
    if not memories:
        return ('No memories yet. Save one with '
                '`chats memory add "..." --type task|user|feedback|project|reference`.')
    by_type: dict[str, list[dict]] = {}
    for m in memories:
        by_type.setdefault(m["type"], []).append(m)

    lines = ["# Memory"]
    rendered = set()
    for t in TYPE_ORDER + sorted(k for k in by_type if k not in TYPE_ORDER):
        mems = by_type.get(t)
        if not mems or t in rendered:
            continue
        rendered.add(t)
        lines.append(f"\n## {TYPE_HEADERS.get(t, t.title())}")
        if t == "ledger":
            mems = sorted(mems, key=lambda m: m.get("updated_at", ""), reverse=True)
            for m in mems:
                age = _ago(m.get("updated_at", ""))
                suffix = f"  (updated {age})" if age else ""
                lines.append(f"- [{m['id']}] ledger: {m.get('ledger_key', '?')}{suffix}")
                for f in LEDGER_FIELDS:
                    v = (m.get(f) or "").strip()
                    if v:
                        lines.append(f"    {f}: {v}")
            continue
        for m in mems:
            lines.append(f"- [{m['id']}] {m['content'].strip()}")
    return "\n".join(lines)
