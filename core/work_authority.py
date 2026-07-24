"""Broker for Serena starting coding work from her own judgment.

Why this exists: the spoken path used to decide by regex, before the brain
ever saw the words. Pattern matching cannot tell "fix the phev tracker" from
"why is the phev tracker broken", cannot check memory first, and cannot ask
one clarifying question. So ordinary asks silently never became jobs while
Serena sat there sounding willing.

The authority moves to her, with a broker underneath: she calls the tool when
she judges that Raghav asked for work, and this module independently re-reads
his ACTUAL spoken turn before anything is queued. She cannot invent a job out
of a question, a hypothetical, or her own enthusiasm, and she cannot queue
work from a turn he never spoke.

Deliberately NOT enforced here: which verb he used, or what order the words
came in. That judgement is hers now. This layer only proves the request is
grounded in something he really said, on a live spoken turn, once.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_AUDIT_PATH = Path.home() / ".local" / "state" / "serena" / "work-authority.jsonl"
MAX_REQUEST_CHARS = 4_000
MIN_REQUEST_CHARS = 3

# Spoken surfaces may start work. A front-door or plain text turn is not a
# live spoken authority and must go through the existing pane protocol.
SPOKEN_PROTOCOLS = frozenset({"voice", "desk"})

# She may not convert a pure question into a job. This mirrors the language
# the old gate refused, kept as a floor under her judgement rather than as
# the decision itself.
_INTERROGATIVE = re.compile(
    r"^\s*(?:what|why|when|where|who|which|how|is|are|was|were|do|does|did|"
    r"can|could|would|should|will|have|has|had)\b",
    re.IGNORECASE,
)
_QUESTION_ONLY = re.compile(r"\?\s*$")
_CANCELLATION = re.compile(
    r"\b(?:do\s+not|don'?t|never|cancel|stop|abort|hold\s+off|no\s+need\s+to|"
    r"nevermind|never\s+mind|forget\s+it|leave\s+it)\b",
    re.IGNORECASE,
)
# A spoken turn that authorizes work mentions doing something to something.
# Broad on purpose: her judgement narrows it, this only proves intent exists.
_WORK_SIGNAL = re.compile(
    r"\b(?:fix|build|implement|change|update|add|remove|replace|refactor|"
    r"debug|investigate|test|verify|review|finish|wire|hook|work|create|"
    r"write|code|start|continue|keep|going|look|dig|jump|tackle|sort|handle|"
    r"deal|clean|set|make|patch|repair|migrate|rename|polish|optimi[sz]e|"
    r"document|connect|move|split|merge|extract|do|done|sort(?:ed)?|"
    r"broken|bug|issue|error|failing|deploy|ship)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class WorkAuthorityResult:
    allowed: bool
    reason: str
    request: str
    item_id: str = ""


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def authority_denial(request: str, origin: Mapping[str, object]) -> str | None:
    """Return why this request may not start work, or None when it may."""

    request = _clean(request)
    spoken = _clean(origin.get("text"))
    protocol = str(origin.get("protocol") or "").strip().lower()

    if not request or len(request) < MIN_REQUEST_CHARS:
        return "the work request was empty"
    if len(request) > MAX_REQUEST_CHARS:
        return "the work request was too long to queue safely"
    if not spoken:
        return "no originating spoken turn is bound to this tool call"
    if protocol not in SPOKEN_PROTOCOLS:
        return (
            "coding work starts only from a live spoken turn, "
            f"not a {protocol or 'unknown'} turn"
        )
    if _CANCELLATION.search(spoken):
        return "the spoken turn withdrew or postponed the work"
    if _INTERROGATIVE.match(spoken) and _QUESTION_ONLY.search(spoken):
        return "the spoken turn was a question, not an instruction"
    if not _WORK_SIGNAL.search(spoken):
        return "the spoken turn did not ask for any work to be done"
    return None


def start_coding_work(
    request: str,
    *,
    origin: Mapping[str, object],
    inbox=None,
    audit_path: Path = DEFAULT_AUDIT_PATH,
) -> WorkAuthorityResult:
    """Queue one coding job for Serena's private worker, or explain refusal."""

    request = _clean(request)
    denial = authority_denial(request, origin)
    if denial is not None:
        result = WorkAuthorityResult(False, denial, request)
        _append_audit(result, origin=origin, audit_path=audit_path)
        return result

    if inbox is None:
        from core.voice_inbox import get_default_voice_inbox

        inbox = get_default_voice_inbox()
    if not inbox.resident_lease_active():
        result = WorkAuthorityResult(
            False, "my private coding runtime is not running", request
        )
        _append_audit(result, origin=origin, audit_path=audit_path)
        return result

    call_id = str(origin.get("call_id") or "").strip() or "desk"
    turn_id = str(origin.get("turn_id") or "").strip() or f"{call_id}:tool"
    # call_id + turn_id is the inbox's idempotency key, so the legacy spoken
    # fast path and this tool cannot double-queue the same turn.
    item = inbox.enqueue(request, call_id=call_id, turn_id=turn_id)
    result = WorkAuthorityResult(
        True, "queued for the private coding worker", request, item.item_id
    )
    _append_audit(result, origin=origin, audit_path=audit_path)
    return result


def _append_audit(
    result: WorkAuthorityResult,
    *,
    origin: Mapping[str, object],
    audit_path: Path,
) -> None:
    """Record every decision, including refusals, without storing raw speech.

    Best effort by design: the job is already durable in the inbox once
    enqueued, so a failed audit write must never turn a started job into a
    reported failure. Serena telling the truth about what happened outranks
    this file.
    """

    try:
        _write_audit(result, origin=origin, audit_path=Path(audit_path))
    except OSError:
        pass


def _write_audit(
    result: WorkAuthorityResult,
    *,
    origin: Mapping[str, object],
    audit_path: Path,
) -> None:
    audit_path = audit_path.expanduser()
    audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        **asdict(result),
        "created_at": time.time(),
        "protocol": str(origin.get("protocol") or ""),
        "call_id": str(origin.get("call_id") or ""),
        "turn_id": str(origin.get("turn_id") or ""),
        "origin_sha256": hashlib.sha256(
            str(origin.get("text") or "").encode("utf-8")
        ).hexdigest(),
    }
    descriptor = os.open(audit_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(
            descriptor,
            (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
