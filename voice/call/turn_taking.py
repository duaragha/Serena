"""Rule-based utterance completeness and adaptive end-of-utterance policy.

The desk runtime commits a hands-free turn adaptively: the VAD child keeps a
long silence ceiling while the parent watches per-frame trailing silence and a
speculative transcript. A transcript that reads complete commits early, an
obviously unfinished one extends the pause budget up to the child's ceiling.
No model is involved here; everything is cheap string heuristics so the
decision can run on every mic frame.
"""

from __future__ import annotations

from dataclasses import dataclass

COMPLETE = "complete"
INCOMPLETE = "incomplete"
UNKNOWN = "unknown"

_TERMINAL_PUNCTUATION = (".", "!", "?")
_ELLIPSES = ("...", "…")
_DANGLING_PUNCTUATION = (",", ";", ":", "-", "–", "—")

# Conjunction / preposition / filler tails that promise a continuation.
_DANGLING_WORDS = frozenset(
    {
        # tail markers named by the design
        "and", "but", "or", "so", "because", "with", "to", "for", "of",
        "like", "um", "uh", "uhh", "the", "a", "an",
        # obviously unfinished constructions: dangling prepositions,
        # subordinators, and possessives
        "in", "on", "at", "into", "onto", "about", "from", "by", "over",
        "under", "if", "unless", "until", "than", "whether", "while",
        "my", "your", "his", "her", "their", "our", "its",
        "gonna", "wanna",
    }
)

_DANGLING_BIGRAMS = frozenset({("i", "mean"), ("you", "know"), ("kind", "of"), ("sort", "of")})

_QUESTION_STARTERS = frozenset(
    {
        "what", "what's", "whats", "where", "where's", "when", "why", "how",
        "who", "who's", "which", "is", "are", "was", "were", "am", "do",
        "does", "did", "can", "could", "will", "would", "should", "shall",
        "have", "has", "had",
    }
)

_IMPERATIVE_STARTERS = frozenset(
    {
        "play", "stop", "pause", "resume", "skip", "open", "close", "turn",
        "set", "start", "send", "call", "text", "tell", "show", "read",
        "search", "check", "remind", "add", "remove", "delete", "cancel",
        "mute", "unmute", "repeat", "go", "give", "put", "make", "find",
        "look", "list", "write", "run", "restart", "switch", "lower",
        "raise", "increase", "decrease",
    }
)

# Single words that form a finished command on their own.
_STANDALONE_COMMANDS = frozenset(
    {"stop", "pause", "resume", "skip", "repeat", "cancel", "mute", "unmute",
     "next", "go", "yes", "no", "okay", "ok", "thanks", "nevermind"}
)

_WORD_TRIM_CHARS = "\"'.,;:!?()[]-–—…"


def assess_completeness(text: str) -> str:
    """Classify a (possibly punctuation-free) STT transcript tail.

    Returns "complete", "incomplete", or "unknown". Case-insensitive and
    tolerant of STT output without punctuation: trailing content words with
    no dangling markers are "unknown", never "incomplete".
    """
    stripped = text.strip()
    if not stripped:
        return UNKNOWN
    if stripped.endswith(_ELLIPSES):
        return INCOMPLETE
    if stripped.endswith(_DANGLING_PUNCTUATION):
        return INCOMPLETE
    terminal = stripped.endswith(_TERMINAL_PUNCTUATION)
    words = [word.strip(_WORD_TRIM_CHARS) for word in stripped.lower().split()]
    words = [word for word in words if word]
    if not words:
        return COMPLETE if terminal else UNKNOWN
    if len(words) >= 2 and (words[-2], words[-1]) in _DANGLING_BIGRAMS:
        return INCOMPLETE
    if words[-1] in _DANGLING_WORDS:
        return INCOMPLETE
    if terminal:
        return COMPLETE
    first = words[0]
    if first in _QUESTION_STARTERS and len(words) >= 2:
        return COMPLETE
    if first in _IMPERATIVE_STARTERS and len(words) >= 2:
        return COMPLETE
    if len(words) == 1 and first in _STANDALONE_COMMANDS:
        return COMPLETE
    return UNKNOWN


@dataclass(frozen=True, slots=True)
class AdaptiveEouPolicy:
    """Parent-side adaptive commit thresholds, all in milliseconds.

    The VAD child holds the ceiling (``max_ms``); the parent commits earlier
    based on trailing silence plus the speculative transcript's completeness:

    - complete transcript: commit at ``min_ms`` of trailing silence
    - unknown (no or unhelpful transcript): commit at ``base_ms``
    - incomplete or in-flight transcript: wait until ``max_ms``
    """

    min_ms: int = 480
    base_ms: int = 800
    max_ms: int = 2400
    partial_trigger_ms: int = 240

    def __post_init__(self) -> None:
        if self.partial_trigger_ms <= 0 or self.min_ms <= 0:
            raise ValueError("adaptive EOU durations must be positive")
        if not self.min_ms <= self.base_ms <= self.max_ms:
            raise ValueError("adaptive EOU thresholds must satisfy min <= base <= max")

    def decide(
        self,
        trailing_ms: int,
        completeness: str,
        *,
        transcript_pending: bool = False,
    ) -> str:
        """Return "commit" or "wait" for the current trailing-silence run."""
        if completeness not in (COMPLETE, INCOMPLETE, UNKNOWN):
            raise ValueError(f"unknown completeness {completeness!r}")
        if trailing_ms >= self.max_ms:
            return "commit"
        if transcript_pending:
            return "wait"
        if completeness == COMPLETE and trailing_ms >= self.min_ms:
            return "commit"
        if completeness != INCOMPLETE and trailing_ms >= self.base_ms:
            return "commit"
        return "wait"
