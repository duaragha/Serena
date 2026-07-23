"""Deterministic capability routing for Serena's resident brain.

The router changes models inside the one live SDK session.  It never creates a
second identity or provider session, and every decision includes a reason that
can be exposed in telemetry.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

ROUTE_CLASSES = ("reflex", "conversation", "research", "coding", "review")

_WORD = re.compile(r"[a-z0-9']+")
_RESEARCH = re.compile(
    r"\b(research|investigate|look up|search for|latest|current|compare|"
    r"fact[- ]?check|find out|what changed|deep dive)\b"
)
_CODING = re.compile(
    r"\b(build|implement|code|fix|debug|refactor|migrate|write tests?|"
    r"change the repo|edit the repo|make a commit)\b"
)
_REVIEW = re.compile(
    r"\b(review|audit|double[- ]?check|sanity[- ]?check|inspect the diff|"
    r"check the implementation)\b"
)
_REFLEX = re.compile(
    r"^(hey|hi|hello|thanks|thank you|good morning|good night|yes|no|okay|ok)\b|"
    r"\b(volume up|volume down|mute|unmute|play|pause|next track|"
    r"previous track|what(?:'s| is) on screen|active window)\b"
)


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route_class: str
    model: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "class": self.route_class,
            "model": self.model,
            "reason": self.reason,
        }


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def classify_turn(payload: Mapping[str, object]) -> tuple[str, str]:
    """Return a stable route class and an inspectable reason."""

    explicit = _clean_text(payload.get("route_class"))
    if explicit in ROUTE_CLASSES:
        return explicit, "trusted surface supplied an explicit route class"

    protocol = _clean_text(payload.get("protocol")) or "plain"
    text = _clean_text(payload.get("text"))
    if protocol != "voice":
        return "conversation", f"{protocol} uses the resident conversation lane"
    if _REVIEW.search(text):
        return "review", "voice request contains independent review language"
    if _CODING.search(text):
        return "coding", "voice request contains implementation language"
    if _RESEARCH.search(text):
        return "research", "voice request needs current or comparative research"

    word_count = len(_WORD.findall(text))
    if word_count <= 14 and _REFLEX.search(text):
        return "reflex", "short acknowledgement or local control request"
    return "conversation", "ordinary resident Serena conversation"


def route_turn(
    payload: Mapping[str, object],
    *,
    conversation_model: str,
    voice_model: str,
    reflex_model: str,
) -> RouteDecision:
    route_class, reason = classify_turn(payload)
    protocol = _clean_text(payload.get("protocol")) or "plain"
    if protocol != "voice":
        model = conversation_model
    elif route_class == "reflex":
        model = reflex_model
    elif route_class in {"research", "coding", "review"}:
        model = conversation_model
    else:
        model = voice_model
    return RouteDecision(route_class=route_class, model=model, reason=reason)
