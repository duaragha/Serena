"""Pick who writes the code and who reviews it, from live account capacity.

The implementer used to be a constant. On 2026-08-04 Codex reported 100% of
its rate limit consumed, and Raghav asked Serena to change the coding workflow
so it would stop depending on Codex. That job was dispatched to Codex, which
could not run it, and sat at "working" until it was cancelled by hand. The
system already knew: read_fleet_capacity() said codex unusable, claude 43%
used. Nothing asked it.

So this asks, then applies the shared Serena policy. The policy may use a
lighter model for review or a different provider for critical work. When one
provider is exhausted, each pass takes the first allowed model on the provider
that remains. When neither is usable nothing is dispatched and the reason is
said out loud, rather than a job hanging forever with no log line.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from core.coding_model_preferences import (
    AUTO_MODEL,
    CLAUDE_MODEL,
    SONNET_MODEL,
    normalise_coding_model,
)
from core.serena_policy import SerenaPolicyError, classify_risk, resolve_policy

CLAUDE_IMPLEMENT_MODEL = CLAUDE_MODEL
CLAUDE_IMPLEMENT_EFFORT = "xhigh"


@dataclass(frozen=True, slots=True)
class CodingAssignment:
    """Who runs each pass, and the evidence for choosing them."""

    usable: bool
    reason: str
    implement_provider: str = ""
    implement_model: str = ""
    implement_effort: str = ""
    review_provider: str = ""
    review_model: str = ""
    review_effort: str = ""
    lane: str = ""
    risk: str = "normal"
    selected_model: str = AUTO_MODEL
    fallback_reason: str = ""
    policy_version: int = 0
    implement_decision: dict[str, Any] = field(default_factory=dict)
    review_decision: dict[str, Any] = field(default_factory=dict)
    capacity: dict[str, Any] = field(default_factory=dict)

    @property
    def same_provider_reviews_itself(self) -> bool:
        return bool(
            self.implement_provider
            and self.implement_provider == self.review_provider
        )

    def with_implement_effort(self, effort: str) -> CodingAssignment:
        """Same providers, the implement pass tiered to this job's complexity.

        Who implements is a capacity question and stays here. How hard they
        deliberate is a property of the job, which this object cannot see, so
        the caller applies it once the accepted brief is in hand. Review is
        deliberately untouched: the reviewer's whole value is a second opinion
        at full depth on work it did not write.
        """

        effort = str(effort or "").strip()
        if not effort or not self.usable or effort == self.implement_effort:
            return self
        return replace(self, implement_effort=effort)

    def to_dict(self) -> dict[str, Any]:
        return {
            "usable": self.usable,
            "reason": self.reason,
            "implement": {
                "provider": self.implement_provider,
                "model": self.implement_model,
                "effort": self.implement_effort,
            },
            "review": {
                "provider": self.review_provider,
                "model": self.review_model,
                "effort": self.review_effort,
            },
            "lane": self.lane,
            "risk": self.risk,
            "selected_model": self.selected_model,
            "fallback_reason": self.fallback_reason,
            "policy_version": self.policy_version,
            "implement_decision": self.implement_decision,
            "review_decision": self.review_decision,
            "self_review": self.same_provider_reviews_itself,
            "capacity": self.capacity,
        }

def choose_providers(
    capacity: dict[str, Any] | None = None,
    *,
    preferred_implementer: str = "codex",
    preferred_model: object = AUTO_MODEL,
    complexity: object = "ordinary",
    risk: object = "normal",
    request: object = "",
) -> CodingAssignment:
    """Resolve and freeze the coding lane against live provider capacity.

    An unknown or stale reading never disables a provider, matching
    fleet_capacity's own rule: only a positive, unexpired exhaustion signal
    counts against someone. Being unsure is not the same as being out.
    """
    if capacity is None:
        from fleet.capacity import read_fleet_capacity

        capacity = read_fleet_capacity()

    snapshot: dict[str, Any] = {}
    for name in ("codex", "claude"):
        entry = capacity.get(name) if hasattr(capacity, "get") else None
        as_dict = entry.to_dict() if hasattr(entry, "to_dict") else dict(entry or {})
        snapshot[name] = as_dict
    try:
        selected_model = normalise_coding_model(preferred_model, strict=True)
    except ValueError:
        selected_model = AUTO_MODEL
    # Keep the old provider-only API truthful for legacy callers. New accepted
    # jobs pass a concrete preferred_model instead.
    if selected_model == AUTO_MODEL and preferred_implementer == "claude":
        selected_model = SONNET_MODEL
    elif selected_model == AUTO_MODEL and preferred_implementer not in {"codex", "claude"}:
        preferred_implementer = "codex"
    risk_level, _risk_reason = classify_risk(
        request,
        explicit=risk,
    )
    try:
        implement = resolve_policy(
            "coding",
            activity="implement",
            complexity=complexity,
            risk=risk_level,
            role="implement",
            manual_override=selected_model,
            capacity=capacity,
        )
        review = resolve_policy(
            "coding",
            activity="review",
            complexity=complexity,
            risk=risk_level,
            role="review",
            capacity=capacity,
        )
    except SerenaPolicyError as error:
        return CodingAssignment(
            usable=False,
            reason=(
                _no_capacity_reason(snapshot)
                if "no provider has capacity" in str(error)
                else f"shared Serena coding policy could not resolve: {error}"
            ),
            capacity=snapshot,
        )
    fallbacks = [
        value
        for value in (implement.fallback_reason, review.fallback_reason)
        if value
    ]
    reason = (
        f"{implement.lane} policy selected {implement.model} to implement and "
        f"{review.model} to review"
    )
    unavailable = [
        provider
        for provider in ("codex", "claude")
        if snapshot.get(provider, {}).get("usable") is False
    ]
    if len(unavailable) == 1:
        reason += f"; {unavailable[0]} is out of capacity"
    if fallbacks:
        reason += "; " + "; ".join(fallbacks)
    return CodingAssignment(
        usable=True,
        reason=reason,
        implement_provider=implement.provider,
        implement_model=implement.model,
        implement_effort=implement.effort,
        review_provider=review.provider,
        review_model=review.model,
        review_effort=review.effort,
        lane=implement.lane,
        risk=implement.risk,
        selected_model=selected_model,
        fallback_reason="; ".join(fallbacks),
        policy_version=implement.policy_version,
        implement_decision=implement.as_dict(),
        review_decision=review.as_dict(),
        capacity=snapshot,
    )


def _no_capacity_reason(snapshot: dict[str, Any]) -> str:
    """Say which account is out and when it returns, not just that it failed.

    A job that dies for this reason is not a bug he needs to debug, it is a
    wait, and he can only tell the difference if the message says so.
    """
    parts = []
    for name in ("codex", "claude"):
        entry = snapshot.get(name) or {}
        detail = str(entry.get("reason") or "no capacity reading").strip()
        parts.append(f"{name}: {detail}")
    return "no coding provider has capacity right now (" + "; ".join(parts) + ")"
