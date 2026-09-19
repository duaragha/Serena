"""One shape for tool results, so a failure cannot be mistaken for an answer.

Every brain tool used to hand back plain text whatever happened: a real result
and "(memory unavailable: ...)" arrived identically, as content. A model reading
content is entitled to summarise it, and hers did -- asked for a macOS version
after the tool errored, she answered "Sequoia 15" for a Big Sur guest, and asked
for a project folder she could not list she produced "atrium", which does not
exist.

MCP has a flag for this. The SDK maps ``is_error`` onto ``isError``, and an
errored result is delivered to the model as a failure rather than as something
it read. That is the difference between her saying "the tool failed" and her
filling the hole with something plausible.

Use ``failed`` for anything that did not produce the thing that was asked for:
a missing binary, a refused call, a timeout, an empty index. "I could not look"
is a different sentence from "there is nothing there", and only the tool knows
which one is true.
"""

from __future__ import annotations

from typing import Any


def ok(text: str) -> dict[str, Any]:
    """A real answer."""

    return {"content": [{"type": "text", "text": text}]}


def failed(text: str) -> dict[str, Any]:
    """A failure, flagged so it reaches the model as one.

    The text still carries the detail, because "it failed" is not useful to him
    without the reason.
    """

    return {"content": [{"type": "text", "text": text}], "is_error": True}
