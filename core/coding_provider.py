"""Pick who writes the code and who reviews it, from live account capacity.

The implementer used to be a constant. On 2026-08-04 Codex reported 100% of
its rate limit consumed, and Raghav asked Serena to change the coding workflow
so it would stop depending on Codex. That job was dispatched to Codex, which
could not run it, and sat at "working" until it was cancelled by hand. The
system already knew: read_fleet_capacity() said codex unusable, claude 43%
used. Nothing asked it.

So this asks. Both providers healthy keeps the pairing that was chosen for a
reason, one model writes and a different model reviews, because a reviewer
that shares the author's blind spots is decoration. When only one provider is
usable it does both passes, which is worth strictly more than refusing to
work, and the review runs in a fresh session so it at least re-reads the diff
cold. When neither is usable nothing is dispatched and the reason is said out
loud, rather than a job hanging forever with no log line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.coding_job_contract import (
    CLAUDE_REVIEW_EFFORT,
    CLAUDE_REVIEW_MODEL,
    CODEX_EFFORT,
    CODEX_MODEL,
)

CLAUDE_IMPLEMENT_MODEL = CLAUDE_REVIEW_MODEL
CLAUDE_IMPLEMENT_EFFORT = CLAUDE_REVIEW_EFFORT
CODEX_REVIEW_MODEL = CODEX_MODEL
CODEX_REVIEW_EFFORT = "high"

_MODELS = {
    ("codex", "implement"): (CODEX_MODEL, CODEX_EFFORT),
    ("codex", "review"): (CODEX_REVIEW_MODEL, CODEX_REVIEW_EFFORT),
    ("claude", "implement"): (CLAUDE_IMPLEMENT_MODEL, CLAUDE_IMPLEMENT_EFFORT),
    ("claude", "review"): (CLAUDE_REVIEW_MODEL, CLAUDE_REVIEW_EFFORT),
}


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
    capacity: dict[str, Any] = field(default_factory=dict)

    @property
    def same_provider_reviews_itself(self) -> bool:
        return bool(
            self.implement_provider
            and self.implement_provider == self.review_provider
        )

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
            "self_review": self.same_provider_reviews_itself,
            "capacity": self.capacity,
        }


def _pair(implement: str, review: str) -> tuple[str, str, str, str, str, str]:
    im, ie = _MODELS[(implement, "implement")]
    rm, re_ = _MODELS[(review, "review")]
    return implement, im, ie, review, rm, re_


def choose_providers(
    capacity: dict[str, Any] | None = None,
    *,
    preferred_implementer: str = "codex",
) -> CodingAssignment:
    """Assign the implement and review passes from live provider capacity.

    An unknown or stale reading never disables a provider, matching
    fleet_capacity's own rule: only a positive, unexpired exhaustion signal
    counts against someone. Being unsure is not the same as being out.
    """
    if capacity is None:
        from core.fleet_capacity import read_fleet_capacity

        capacity = read_fleet_capacity()

    snapshot: dict[str, Any] = {}
    usable: dict[str, bool] = {}
    for name in ("codex", "claude"):
        entry = capacity.get(name) if hasattr(capacity, "get") else None
        as_dict = entry.to_dict() if hasattr(entry, "to_dict") else dict(entry or {})
        snapshot[name] = as_dict
        # Absent reading means unknown, and unknown never blocks a provider.
        usable[name] = bool(as_dict.get("usable", True))

    other = {"codex": "claude", "claude": "codex"}
    first = preferred_implementer if preferred_implementer in other else "codex"
    second = other[first]

    if usable[first] and usable[second]:
        chosen = _pair(first, second)
        reason = f"both providers usable, {first} implements and {second} reviews"
    elif usable[first]:
        chosen = _pair(first, first)
        reason = (
            f"{second} is out of capacity, {first} implements and reviews itself"
        )
    elif usable[second]:
        chosen = _pair(second, second)
        reason = (
            f"{first} is out of capacity, {second} implements and reviews itself"
        )
    else:
        return CodingAssignment(
            usable=False,
            reason=_no_capacity_reason(snapshot),
            capacity=snapshot,
        )

    implement, im, ie, review, rm, re_ = chosen
    return CodingAssignment(
        usable=True,
        reason=reason,
        implement_provider=implement,
        implement_model=im,
        implement_effort=ie,
        review_provider=review,
        review_model=rm,
        review_effort=re_,
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
