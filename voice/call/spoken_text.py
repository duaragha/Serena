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
    clean = clean.replace("`", "")
    clean = clean.replace("**", "").replace("__", "")
    clean = _LIST_PREFIX.sub(" ", clean)
    clean = _WORD_JOINER.sub(" ", clean)
    clean = re.sub(r"(?<=\d)%", " percent", clean)
    clean = clean.replace(" & ", " and ")
    return _SPACE.sub(" ", clean).strip()
