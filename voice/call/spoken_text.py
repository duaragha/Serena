"""Turn model prose into text that local speech engines pronounce cleanly."""

from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_URL = re.compile(r"https?://[^\s)>]+")
_CODE_FENCE = re.compile(r"```(?:[A-Za-z0-9_+-]+)?|```")
_LIST_PREFIX = re.compile(r"(?:^|\s)(?:[-*+]\s+|\d+[.)]\s+)")
_SPACE = re.compile(r"\s+")
_WORD_JOINER = re.compile(r"(?<=[A-Za-z])[_/](?=[A-Za-z])")
# She read a Windows worktree path, a 32-hex run id and "agent:a" down the
# phone one character at a time. None of it is speech. A caller needs the
# shape of the thing, not its address, and the full text is in his messages.
_WINDOWS_PATH = re.compile(r"'?[A-Za-z]:[\\/][^\s'\"]+'?")
_POSIX_PATH = re.compile(r"'?(?:/[A-Za-z0-9._~@+-]+){2,}/?'?")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_LONG_HEX = re.compile(r"\b[0-9a-f]{12,}\b", re.I)
_SHORT_HEX = re.compile(r"\b[0-9a-f]{7,11}\b", re.I)
_AGENT_TOKEN = re.compile(r"\b(agent):([a-z0-9]+)\b", re.I)
# "fleet 8ba9ee7a" and "duaragha__locket.task-1054" are how the machine names
# work. He names it by its number, so that is what she says.
_FLEET_RUN = re.compile(r"\bfleet\s+[0-9a-f]{7,}\b", re.I)
_TASK_SLUG = re.compile(r"\b[\w-]+__([\w-]+)\.task-(\d+)\b")


def _speak_path(match: re.Match[str]) -> str:
    """A path becomes the one part he could act on: its last name."""

    raw = match.group(0).strip("'\"")
    tail = raw.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if not tail or _UUID.fullmatch(tail) or _LONG_HEX.fullmatch(tail):
        return "a folder"
    return tail


def _speak_task(match: re.Match[str]) -> str:
    """A repository slug becomes the number he actually uses for it."""

    return f"task {match.group(2)} in {match.group(1)}"


def _speak_url(match: re.Match[str]) -> str:
    parsed = urlsplit(match.group(0).rstrip(".,!?"))
    host = (parsed.hostname or "").removeprefix("www.")
    if not host:
        return "the link"
    return host.replace(".", " dot ")


def prepare_spoken_text(text: str) -> str:
    """Remove visual syntax without rewriting the substance of the reply."""

    clean = html.unescape(str(text))
    clean = _MARKDOWN_LINK.sub(r"\1", clean)
    clean = _URL.sub(_speak_url, clean)
    clean = _CODE_FENCE.sub("", clean)
    clean = _WINDOWS_PATH.sub(_speak_path, clean)
    clean = _POSIX_PATH.sub(_speak_path, clean)
    clean = _TASK_SLUG.sub(_speak_task, clean)
    clean = _FLEET_RUN.sub("the fleet run", clean)
    clean = _UUID.sub("that run", clean)
    clean = _LONG_HEX.sub("that run", clean)
    clean = _SHORT_HEX.sub("that run", clean)
    clean = _AGENT_TOKEN.sub(r"\1 \2", clean)
    clean = clean.replace("`", "")
    clean = clean.replace("**", "").replace("__", "")
    clean = _LIST_PREFIX.sub(" ", clean)
    clean = _WORD_JOINER.sub(" ", clean)
    clean = re.sub(r"(?<=\d)%", " percent", clean)
    clean = clean.replace(" & ", " and ")
    return _SPACE.sub(" ", clean).strip()
