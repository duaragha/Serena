"""Conservative project hints shared by queue intake and repository resolution."""

from __future__ import annotations

import re

_NAMES = {
    "locket": r"\blocket\b",
    "unified": r"\bunified(?:[ -]inbox)?\b",
    "serena": r"\bserena\b",
    "atrium": r"\batrium\b",
    "vantage": r"\bvantage\b",
    "openwhispr": r"\bopenwhispr\b",
}
_DOMAINS = {
    "locket": r"\b(workouts?|exercises?|routines?|vault|nutrition|calories)\b|\bsets\b.*\breps\b",
    "unified": r"\b(inbox|whatsapp|matrix|message search)\b",
}


def named_task_projects(text: str) -> set[str]:
    """Recognized explicit names, including conflicts that must stay visible."""

    return {name for name, pattern in _NAMES.items() if re.search(pattern, text, re.I)}


def infer_task_project(text: str, *, domains: bool = True) -> str:
    """Return one recognized name; ambiguous briefs keep their question.

    Explicit project names outrank feature vocabulary. Hints locate work;
    they never make a vague brief actionable or authorize a new operation.
    """

    named = named_task_projects(text)
    if named:
        return next(iter(named)) if len(named) == 1 else ""
    matches = ({name for name, pattern in _DOMAINS.items() if re.search(pattern, text, re.I)}
               if domains else set())
    return next(iter(matches)) if len(matches) == 1 else ""
