"""Inspectable context budgeting and secret filtering for Fleet prompts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any

DEFAULT_CONTEXT_CHARS = 96_000
MIN_EXCERPT_CHARS = 1_000

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL)),
    ("authorization", re.compile(r"(?i)\b(?:authorization|proxy-authorization)\s*:\s*(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:[A-Z][A-Z0-9_.-]*[_-])?"
            r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY)"
            r"\s*=\s*(?:\"[^\"\r\n]+\"|'[^'\r\n]+'|[^\s,;]+)"
        ),
    ),
    (
        "provider_api_key",
        re.compile(
            r"\b(?:sk-(?:(?:proj|svcacct)-|ant-(?:api\d{2}-)?)?"
            r"[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,})\b"
        ),
    ),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SENSITIVE_FIELD_SUFFIXES = (
    "authorization",
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "private_key",
)


@dataclass(frozen=True, slots=True)
class ContextReceipt:
    strategy: str
    source_chars: int
    delivered_chars: int
    omitted_chars: int
    source_count: int
    redaction_count: int
    full_history_preserved: bool
    source_sha256: str
    budget_chars: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def redact_text(value: object) -> tuple[str, int]:
    """Redact common credential forms before text crosses a worker boundary."""

    text = str(value or "")
    count = 0
    for label, pattern in _SECRET_PATTERNS:
        text, matches = pattern.subn(f"[redacted:{label}]", text)
        count += matches
    return text, count


def redact_value(value: Any) -> tuple[Any, int]:
    """Recursively redact strings in an operator or cross-surface payload."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        total = 0
        for key, item in value.items():
            if _sensitive_field_name(key):
                result[str(key)] = "[redacted:sensitive_field]"
                total += 1
                continue
            clean, count = redact_value(item)
            result[str(key)] = clean
            total += count
        return result, total
    if isinstance(value, list):
        result_list: list[Any] = []
        total = 0
        for item in value:
            clean, count = redact_value(item)
            result_list.append(clean)
            total += count
        return result_list, total
    if isinstance(value, tuple):
        clean, count = redact_value(list(value))
        return tuple(clean), count
    return value, 0


def _sensitive_field_name(value: object) -> bool:
    normalized = _CAMEL_BOUNDARY.sub("_", str(value)).replace("-", "_").lower()
    return any(
        normalized == suffix or normalized.endswith("_" + suffix)
        for suffix in _SENSITIVE_FIELD_SUFFIXES
    )


def budget_context(
    sources: list[tuple[str, str]],
    *,
    budget_chars: int = DEFAULT_CONTEXT_CHARS,
    weights: list[float] | None = None,
) -> tuple[str, ContextReceipt]:
    """Build a deterministic, inspectable context without rewriting history.

    Native provider transcripts remain authoritative. Fleet keeps their
    sanitized result text, and never replaces it with a summary. When the prompt
    budget is exceeded, bounded source excerpts are delivered and a receipt
    records the omitted size and hash of that preserved sanitized source text.
    """

    safe_budget = max(MIN_EXCERPT_CHARS, int(budget_chars))
    rendered: list[str] = []
    rendered_weights: list[float] = []
    redactions = 0
    raw_weights = list(weights or [])
    for index, (label, raw_text) in enumerate(sources):
        safe_label, label_redactions = redact_text(label)
        safe_text, text_redactions = redact_text(raw_text)
        redactions += label_redactions + text_redactions
        if safe_text.strip():
            rendered.append(f"[{safe_label}]\n{safe_text.strip()}")
            weight = raw_weights[index] if index < len(raw_weights) else 1.0
            rendered_weights.append(max(0.0, float(weight)) or 1.0)
    full = "\n\n".join(rendered)
    source_chars = len(full)
    digest = hashlib.sha256(full.encode("utf-8", errors="replace")).hexdigest()
    if source_chars <= safe_budget:
        receipt = ContextReceipt(
            strategy="full",
            source_chars=source_chars,
            delivered_chars=source_chars,
            omitted_chars=0,
            source_count=len(rendered),
            redaction_count=redactions,
            full_history_preserved=True,
            source_sha256=digest,
            budget_chars=safe_budget,
        )
        return full, receipt

    # Weighted share, not an equal split. A peer whose work this worker actually
    # depends on or reviews is worth more of the window than one it will never
    # touch, and an equal split spent the same budget on both.
    total_weight = sum(rendered_weights) or float(len(rendered))
    excerpts: list[str] = []
    for section, weight in zip(rendered, rendered_weights, strict=True):
        per_source = max(
            MIN_EXCERPT_CHARS, int(safe_budget * (weight / total_weight))
        )
        if len(section) <= per_source:
            excerpts.append(section)
            continue
        marker = "\n[context excerpt omitted; full source preserved in Fleet history]\n"
        available = max(0, per_source - len(marker))
        head = available // 2
        tail = available - head
        excerpts.append(section[:head] + marker + section[-tail:] if tail else section[:head] + marker)
    delivered = "\n\n".join(excerpts)
    if len(delivered) > safe_budget:
        delivered = delivered[:safe_budget]
    receipt = ContextReceipt(
        strategy="bounded_excerpts",
        source_chars=source_chars,
        delivered_chars=len(delivered),
        omitted_chars=max(0, source_chars - len(delivered)),
        source_count=len(rendered),
        redaction_count=redactions,
        full_history_preserved=True,
        source_sha256=digest,
        budget_chars=safe_budget,
    )
    return delivered, receipt
