"""Safe deterministic classification for retrieval feedback utterances."""

from __future__ import annotations

import re
from dataclasses import dataclass

FEEDBACK_CLASSIFIER_VERSION = "retrieval-feedback-intent-v2"

_RELEVANCE = re.compile(
    r"\b(?:irrelevant|not\s+relevant|unrelated|wrong\s+(?:one|result|memory)|"
    r"not\s+(?:what|who)\s+i\s+meant|does(?:n['’]?t|\s+not)\s+answer)\b",
    re.IGNORECASE,
)
_FACTUAL = re.compile(
    r"\b(?:actually|fact(?:ual)?(?:ly)?\s+wrong|incorrect|outdated|stale|"
    r"should\s+(?:be|say)|(?:the|that)\s+(?:date|fact|name|number|time)\s+is\s+wrong|"
    r"wrong\s+(?:date|fact|name|number|time)|"
    r"correct(?:ion|ed)?|replace\s+it\s+with)\b",
    re.IGNORECASE,
)
_BARE_WRONG = re.compile(r"^\s*(?:that(?:'s|\s+is)\s+)?wrong[.!?\s]*$", re.IGNORECASE)
_REVOKE = re.compile(
    r"\b(?:undo|revoke|take\s+back|remove)\b.*\b(?:feedback|irrelevant|relevance)\b|"
    r"\brestore\b.*\brelevance\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class FeedbackIntent:
    kind: str
    label: str
    rule: str
    classifier_version: str = FEEDBACK_CLASSIFIER_VERSION


def classify_feedback(text: str, *, corrected_content: str = "") -> FeedbackIntent | None:
    """Classify only explicit user language, with bare ``wrong`` kept non-mutating."""

    clean = " ".join(str(text or "").split())
    correction = " ".join(str(corrected_content or "").split())
    if not clean:
        return None
    if _REVOKE.search(clean):
        return FeedbackIntent("revoke", "revoked", "explicit_revoke")
    if _BARE_WRONG.fullmatch(clean):
        return FeedbackIntent("relevance", "irrelevant", "bare_wrong_safe_default")
    if correction and (_FACTUAL.search(clean) or "wrong" in clean.casefold()):
        return FeedbackIntent("factual_correction", "incorrect_fact", "corrected_content")
    if _RELEVANCE.search(clean):
        return FeedbackIntent("relevance", "irrelevant", "explicit_relevance")
    if _FACTUAL.search(clean):
        return FeedbackIntent("factual_correction", "incorrect_fact", "explicit_factual")
    return None


__all__ = [
    "FEEDBACK_CLASSIFIER_VERSION",
    "FeedbackIntent",
    "classify_feedback",
]
