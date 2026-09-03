"""Provider selection and usage-limit recognition for the resident brain."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


class BrainProviderUnavailable(RuntimeError):
    """No configured subscription provider can accept a brain turn."""


class BrainProviderUsageLimit(RuntimeError):
    """A provider positively reported that its subscription is exhausted."""

    def __init__(self, provider: str, detail: object) -> None:
        self.provider = provider
        self.detail = str(detail)
        super().__init__(f"{provider} subscription usage is exhausted: {self.detail}")


def _capacity_value(
    capacity: Mapping[str, Any] | None,
    provider: str,
    field: str,
) -> Any:
    if not capacity:
        return None
    state = capacity.get(provider)
    if isinstance(state, Mapping):
        return state.get(field)
    return getattr(state, field, None)


def provider_is_usable(
    capacity: Mapping[str, Any] | None,
    provider: str,
) -> bool:
    """Treat missing and stale capacity as unknown, not exhausted."""

    usable = _capacity_value(capacity, provider, "usable")
    if usable is not None:
        return bool(usable)
    return str(_capacity_value(capacity, provider, "status") or "unknown") != "unavailable"


def choose_brain_provider(
    capacity: Mapping[str, Any] | None,
    *,
    override: str | None = None,
    preferred_provider: str = "claude",
) -> str:
    """Honor an explicit provider, otherwise follow the policy preference."""

    selected = str(
        override
        if override is not None
        else os.environ.get("SERENA_BRAIN_PROVIDER", "auto")
    ).strip().lower()
    if selected not in {"auto", "claude", "codex"}:
        raise ValueError("SERENA_BRAIN_PROVIDER must be auto, claude, or codex")
    if selected != "auto":
        if not provider_is_usable(capacity, selected):
            raise BrainProviderUnavailable(f"{selected} subscription usage is exhausted")
        return selected
    preferred = str(preferred_provider or "claude").strip().lower()
    if preferred not in {"claude", "codex"}:
        raise ValueError("preferred_provider must be claude or codex")
    fallback = "codex" if preferred == "claude" else "claude"
    if provider_is_usable(capacity, preferred):
        return preferred
    if provider_is_usable(capacity, fallback):
        return fallback
    raise BrainProviderUnavailable("Claude and Codex subscription usage are exhausted")


def capacity_reset_time(
    capacity: Mapping[str, Any] | None,
    provider: str,
) -> float | None:
    value = _capacity_value(capacity, provider, "resets_at")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def is_usage_limit_error(value: object) -> bool:
    """Recognize provider exhaustion in exceptions and normal reply text."""

    text = str(value or "").strip().lower().replace("’", "'")
    if not text:
        return False
    if any(
        phrase in text
        for phrase in (
            "you've hit your session limit",
            "you have hit your session limit",
            "hit your session limit",
            "session usage limit reached",
        )
    ):
        return True
    if "usage limit" in text and any(
        marker in text for marker in ("hit", "reached", "exceeded", "reset", "resets")
    ):
        return True
    return "rate limit" in text and any(
        marker in text
        for marker in ("hit", "reached", "exceeded", "429", "too many requests")
    )
