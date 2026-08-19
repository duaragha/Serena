"""Deterministic capability routing for Serena's resident brain.

The router changes models inside the one live SDK session.  It never creates a
second identity or provider session, and every decision includes a reason that
can be exposed in telemetry.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

ROUTE_CLASSES = (
    "reflex",
    "conversation",
    "planning",
    "research",
    "coding",
    "review",
    "documents",
    "laptop",
    "fleet",
)

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
_DOCUMENTS = re.compile(
    r"\b(document|docx|word file|pdf|presentation|powerpoint|pptx|"
    r"spreadsheet|excel|xlsx|report|proposal|deck)\b"
)
_FLEET = re.compile(r"\b(?:serena\s+)?fleet\b")
_LAPTOP = re.compile(
    r"\b(open|close|launch|restart|stop|kill|pause|resume|click|type|"
    r"volume|mute|window|screen|service|daemon|systemd|laptop|computer)\b"
)
_PLANNING = re.compile(
    r"\b(plan|architecture|design|strategy|trade[- ]?offs?|roadmap|"
    r"reason through|think deeply)\b"
)
_CASUAL_CONVERSATION = re.compile(
    r"^(?:(?:hey|hi)[,!]?\s+)?(?:serena[,!]?\s+)?(?:"
    r"how (?:are|r) you(?: doing)?|how(?:'s| is) it going|"
    r"what are you up to|tell me (?:a joke|something funny)|"
    r"say something funny|talk to me|keep me company|"
    r"i missed you|did you miss me)"
    r"[?.!,\s]*$"
)
# A greeting routes to the reflex lane only when the turn IS a greeting.
# Anchoring on the first word alone sent "hey can you open the calculator"
# to the smallest model, which answered "Opening calculator now" without
# calling any tool: a claimed action, no action.
_REFLEX = re.compile(
    r"^(?:hey|hi|hello|thanks|thank you|good morning|good night|yes|no|okay|ok)"
    r"(?:[,!.]?\s+serena)?[,!.\s]*$|"
    r"\b(volume up|volume down|mute|unmute|play|pause|next track|"
    r"previous track|what(?:'s| is) on screen|active window)\b"
)


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route_class: str
    model: str
    reason: str
    provider: str = "claude"
    runtime_model: str = ""
    effort: str = "high"
    profile: str = "brain"
    role: str = "execute"
    activity: str = "chat"
    complexity: str = ""
    risk: str = "normal"
    lane: str = "casual"
    manual_override: str = "auto"
    fallback_reason: str = ""
    policy_version: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "class": self.route_class,
            "model": self.model,
            "provider": self.provider,
            "runtime_model": self.runtime_model or self.model,
            "effort": self.effort,
            "profile": self.profile,
            "role": self.role,
            "activity": self.activity,
            "complexity": self.complexity,
            "risk": self.risk,
            "lane": self.lane,
            "manual_override": self.manual_override,
            "fallback_reason": self.fallback_reason,
            "policy_version": self.policy_version,
            "reason": self.reason,
        }


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _is_casual_conversation(payload: Mapping[str, object], route_class: str) -> bool:
    """Limit the small model to unmistakably light social voice turns."""

    return (
        route_class == "conversation"
        and not _clean_text(payload.get("route_class"))
        and bool(_CASUAL_CONVERSATION.match(_clean_text(payload.get("text"))))
    )


def classify_turn(payload: Mapping[str, object]) -> tuple[str, str]:
    """Return a stable route class and an inspectable reason."""

    explicit = _clean_text(payload.get("route_class"))
    if explicit in ROUTE_CLASSES:
        return explicit, "trusted surface supplied an explicit route class"

    text = _clean_text(payload.get("text"))
    if _FLEET.search(text):
        return "fleet", "request explicitly names Serena Fleet"
    if _DOCUMENTS.search(text):
        return "documents", "request concerns a document artifact"
    if _REVIEW.search(text):
        return "review", "voice request contains independent review language"
    if _CODING.search(text):
        return "coding", "voice request contains implementation language"
    if _RESEARCH.search(text):
        return "research", "voice request needs current or comparative research"
    if _PLANNING.search(text):
        return "planning", "request needs complex reasoning or planning"

    word_count = len(_WORD.findall(text))
    if word_count <= 14 and _REFLEX.search(text):
        return "reflex", "short acknowledgement or local control request"
    if _LAPTOP.search(text):
        return "laptop", "request concerns a local laptop action"
    return "conversation", "ordinary resident Serena conversation"


def route_turn(
    payload: Mapping[str, object],
    *,
    conversation_model: str | None = None,
    voice_model: str | None = None,
    reflex_model: str | None = None,
    capacity: Mapping[str, object] | None = None,
    manual_override: object = "auto",
    policy: Mapping[str, object] | None = None,
) -> RouteDecision:
    route_class, reason = classify_turn(payload)
    protocol = _clean_text(payload.get("protocol")) or "plain"
    if any(value is not None for value in (conversation_model, voice_model, reflex_model)):
        conversation = str(conversation_model or "sonnet")
        voice = str(voice_model or conversation)
        reflex = str(reflex_model or voice)
        if protocol != "voice":
            model = conversation
        elif route_class == "reflex":
            model = reflex
        elif route_class in {"research", "coding", "review", "planning"}:
            model = conversation
        else:
            model = voice
        provider = "codex" if model.startswith("gpt-") else "claude"
        return RouteDecision(
            route_class=route_class,
            model=model,
            runtime_model=model,
            provider=provider,
            activity="chat" if route_class == "conversation" else route_class,
            lane="legacy",
            reason=reason,
        )

    from core.serena_policy import classify_risk, resolve_policy

    casual_voice_chat = protocol == "voice" and _is_casual_conversation(
        payload, route_class
    )
    fast_voice_chat = (
        casual_voice_chat
        and payload.get("_fast_model_available", True) is not False
    )
    activity = (
        "voice_chat"
        if fast_voice_chat
        else "chat" if route_class == "conversation" else route_class
    )
    risk, risk_reason = classify_risk(
        payload.get("text"),
        paths=payload.get("paths") if isinstance(payload.get("paths"), list) else (),
        explicit=payload.get("risk"),
        policy=policy,
    )
    complexity = _clean_text(payload.get("complexity"))
    if not complexity and route_class in {"planning", "coding", "review"}:
        complexity = "normal"
    override = payload.get("model_override") or manual_override
    selected = resolve_policy(
        "brain",
        activity=activity,
        complexity=complexity,
        risk=risk,
        role="execute",
        manual_override=override,
        capacity=capacity,
        policy=policy,
    )
    fast_fallback_reason = (
        "fast conversational model was unavailable; restored the standard chat lane"
        if casual_voice_chat and not fast_voice_chat
        else ""
    )
    fallback_reason = "; ".join(
        part for part in (fast_fallback_reason, selected.fallback_reason) if part
    )
    return RouteDecision(
        route_class=route_class,
        model=selected.model,
        provider=selected.provider,
        runtime_model=selected.runtime_model,
        effort=selected.effort,
        activity=selected.activity,
        complexity=selected.complexity,
        risk=selected.risk,
        lane=selected.lane,
        manual_override=selected.manual_override,
        fallback_reason=fallback_reason,
        policy_version=selected.policy_version,
        reason="; ".join(
            part
            for part in (reason, risk_reason, fast_fallback_reason, selected.reason)
            if part
        ),
    )
