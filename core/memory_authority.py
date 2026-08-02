"""Broker for Serena editing her own memory and knowledge on Raghav's word.

He asked for this plainly: "she should be able to add, delete, edit memories,
knowledge, everything honestly if I tell her to." The judgement about WHAT to
save or remove is hers; this layer only proves the action is grounded in a
live spoken or desk turn he actually produced, and that destructive actions
were asked for rather than volunteered.

Same shape as core.work_authority, deliberately: the tool call arrives with
her arguments, and the broker independently re-reads the originating turn.
She cannot rewrite her own memory out of enthusiasm, from a hallucinated
instruction, or on a turn he never spoke.
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

DEFAULT_AUDIT_PATH = Path.home() / ".local" / "state" / "serena" / "memory-authority.jsonl"
MAX_CONTENT_CHARS = 4_000
SPOKEN_PROTOCOLS = frozenset({"voice", "desk"})

# Destructive actions need him to have actually asked for removal or
# replacement, in his own words. Broad on purpose; her judgement narrows it.
_DESTRUCTIVE_SIGNAL = re.compile(
    r"\b(?:delete|remove|forget|drop|erase|clear|scrap|get\s+rid|wipe|clean\s+up|"
    r"prune|purge|update|change|edit|fix|correct|replace|rewrite|rename|wrong|"
    r"outdated|stale|merge)\b",
    re.IGNORECASE,
)
# Saving something new only needs evidence he asked her to keep or note it.
_SAVE_SIGNAL = re.compile(
    r"\b(?:remember|save|note|keep|add|write\s+down|store|track|log|record|"
    r"memoriz[es]?|update|jot|put)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MemoryAuthorityResult:
    allowed: bool
    reason: str
    action: str
    detail: str = ""


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def authority_denial(
    action: str,
    origin: Mapping[str, object],
    *,
    destructive: bool,
) -> str | None:
    """Why this action may not run, or None when it may."""

    spoken = _clean(origin.get("text"))
    protocol = str(origin.get("protocol") or "").strip().lower()
    if not spoken:
        return "no originating spoken turn is bound to this tool call"
    if protocol not in SPOKEN_PROTOCOLS:
        return (
            "memory changes happen only on a live spoken turn, "
            f"not a {protocol or 'unknown'} turn"
        )
    signal = _DESTRUCTIVE_SIGNAL if destructive else _SAVE_SIGNAL
    if not signal.search(spoken):
        kind = "removing or changing" if destructive else "saving"
        return f"the spoken turn did not ask for {kind} anything"
    return None


def authorize(
    action: str,
    *,
    origin: Mapping[str, object],
    destructive: bool,
    detail: str = "",
    audit_path: Path = DEFAULT_AUDIT_PATH,
) -> MemoryAuthorityResult:
    detail = _clean(detail)[:MAX_CONTENT_CHARS]
    denial = authority_denial(action, origin, destructive=destructive)
    if denial is not None:
        result = MemoryAuthorityResult(False, denial, action, detail)
    else:
        result = MemoryAuthorityResult(True, "authorized", action, detail)
    _append_audit(result, origin=origin, audit_path=audit_path)
    return result


def _append_audit(
    result: MemoryAuthorityResult,
    *,
    origin: Mapping[str, object],
    audit_path: Path,
) -> None:
    """Best effort, and raw speech is never persisted, only its digest."""
    try:
        audit_path = Path(audit_path).expanduser()
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
                (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
            )
        finally:
            os.close(descriptor)
    except OSError:
        pass
