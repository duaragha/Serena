"""What she says out loud while a coding job is running.

Raghav asked for the thing he liked in ChatGPT's voice mode: "she updates as
she's coding, so there's always something going on". The material already
exists, the coding worker streams an agent_message every time it decides
something, which is exactly the "I thought of doing this, so I did that" line.

This is only the pipe between the two processes. The supervisor writes here;
the voice session tails it and decides whether the floor is free to speak.
Deliberately a plain append-only file rather than a socket: the supervisor must
never block or die because nothing is listening, and a restart on either side
must not lose or replay the backlog.

The speaking rules live in the reader, not here, but one rule is enforced at
the source because it cannot be recovered later: ROUTINE narration is allowed
to be dropped, while blockers and completion are not. Anything written with
kind="milestone" may be skipped when he is busy; kind="blocker" and
kind="done" must survive to be spoken.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path.home() / ".local" / "state" / "serena" / "work-narration.jsonl"
MAX_TEXT = 240
# A narration older than this is history, not news. Speaking "I'm about to run
# the tests" a minute after they passed is worse than silence.
ROUTINE_STALE_SECONDS = 45.0
DROPPABLE_KINDS = frozenset({"milestone"})


@dataclass(frozen=True, slots=True)
class NarrationLine:
    offset: int
    created_at: float
    job_id: str
    kind: str
    text: str

    @property
    def droppable(self) -> bool:
        return self.kind in DROPPABLE_KINDS

    def stale(self, *, now: float) -> bool:
        return self.droppable and (now - self.created_at) > ROUTINE_STALE_SECONDS


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())[:MAX_TEXT]


def append(
    text: str,
    *,
    job_id: str = "",
    kind: str = "milestone",
    path: Path | None = None,
) -> bool:
    """Record one spoken-shaped update. Best effort, never raises."""

    line = _clean(text)
    if not line:
        return False
    payload = {
        "created_at": time.time(),
        "job_id": str(job_id or "")[:128],
        "kind": str(kind or "milestone")[:32],
        "text": line,
    }
    try:
        path = Path(path or DEFAULT_PATH).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(
                descriptor,
                (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"),
            )
        finally:
            os.close(descriptor)
    except OSError:
        return False
    return True


def read_since(
    offset: int, *, path: Path | None = None
) -> tuple[list[NarrationLine], int]:
    """Return whatever was appended after offset, plus the new offset.

    A byte offset rather than a line count so a reader that restarts mid-file
    resumes where it stopped, and a truncated or rotated file rewinds to the
    start instead of silently going deaf.
    """

    path = Path(path or DEFAULT_PATH).expanduser()
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset
    if size < offset:
        offset = 0
    if size == offset:
        return [], offset
    lines: list[NarrationLine] = []
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            raw = handle.read()
            consumed = offset + len(raw)
            # A partial trailing line means the writer is mid-append. Leave it
            # for the next read rather than speaking half a sentence.
            if not raw.endswith(b"\n"):
                cut = raw.rfind(b"\n")
                if cut < 0:
                    return [], offset
                consumed = offset + cut + 1
                raw = raw[: cut + 1]
    except OSError:
        return [], offset
    decoded = raw.decode("utf-8", errors="replace").splitlines()
    parsed: list[dict] = []
    unreadable = 0
    for chunk in decoded:
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            payload = json.loads(chunk)
        except json.JSONDecodeError:
            unreadable += 1
            continue
        if isinstance(payload, dict):
            parsed.append(payload)
    if offset and unreadable and not parsed:
        # Every record was garbage, so the file was rotated or truncated under
        # us and the offset now points into the middle of a record. Rewinding
        # re-speaks at worst a little history; not rewinding goes deaf forever.
        return read_since(0, path=path)
    for payload in parsed:
        text = _clean(payload.get("text"))
        if not text:
            continue
        lines.append(
            NarrationLine(
                offset=consumed,
                created_at=float(payload.get("created_at") or 0.0),
                job_id=str(payload.get("job_id") or ""),
                kind=str(payload.get("kind") or "milestone"),
                text=text,
            )
        )
    return lines, consumed


def current_offset(path: Path | None = None) -> int:
    """Where the file ends now, so a fresh call ignores old jobs' backlog."""

    try:
        return Path(path or DEFAULT_PATH).expanduser().stat().st_size
    except OSError:
        return 0


def collapse(lines: list[NarrationLine], *, now: float | None = None) -> list[NarrationLine]:
    """Reduce a backlog to what is still worth saying.

    Three updates that piled up while he was talking must not be read out one
    after another. Everything that must be heard is kept in order, and of the
    droppable ones only the newest survives.
    """

    moment = time.time() if now is None else now
    fresh = [line for line in lines if not line.stale(now=moment)]
    must_speak = [line for line in fresh if not line.droppable]
    # "I'm about to run the tests" is moot once the job has finished. Anything
    # routine older than a completion is dropped rather than narrated as news.
    finished_at = max(
        (line.created_at for line in must_speak if line.kind == "done"), default=None
    )
    droppable = [
        line
        for line in fresh
        if line.droppable and (finished_at is None or line.created_at > finished_at)
    ]
    if droppable:
        must_speak.append(droppable[-1])
    return sorted(must_speak, key=lambda line: line.created_at)
