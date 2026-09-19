"""Drop-before-capture denylist for ambient sensing.

Unlike `core.visual_context.VisualPolicy`, which redacts after a consented
capture, this denylist is consulted by the sensor *before* anything is
recorded. A denied app, title, or URL produces zero rows: nothing to redact,
nothing to leak, nothing to forget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class AmbientDenylist:
    """Substring app matches plus regex title and URL patterns."""

    apps: tuple[str, ...] = ()
    title_patterns: tuple[str, ...] = ()
    url_patterns: tuple[str, ...] = ()
    _title_compiled: tuple = field(default=(), repr=False, compare=False)
    _url_compiled: tuple = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_title_compiled",
            tuple(re.compile(p, re.IGNORECASE) for p in self.title_patterns),
        )
        object.__setattr__(
            self,
            "_url_compiled",
            tuple(re.compile(p, re.IGNORECASE) for p in self.url_patterns),
        )

    @classmethod
    def default(cls) -> AmbientDenylist:
        from core.visual_context import VisualPolicy

        policy = VisualPolicy()
        return cls(
            apps=policy.private_apps
            + (
                "keepassxc",
                "tor browser",
                "torbrowser",
            ),
            title_patterns=policy.private_title_patterns
            + (
                r"\binprivate\b",
                r"\btor browser\b",
            ),
            url_patterns=(
                r"\.onion(/|$)",
                r"://([^/]*\.)?(chase|wellsfargo|bankofamerica|citi|capitalone|amex|discover|paypal|venmo|coinbase|kraken)\.",
                r"/(login|log-in|signin|sign-in|account|checkout|payment|billing|password|2fa|mfa|otp)\b",
            ),
        )

    def denies(self, *, app: str = "", title: str = "", url: str = "") -> str:
        """Return a reason when this activity must not be recorded at all."""

        lowered_app = str(app or "").casefold()
        if lowered_app and any(
            entry.casefold() in lowered_app for entry in self.apps
        ):
            return f"denylisted app {app!r}"
        lowered_title = str(title or "")
        if lowered_title and any(
            pattern.search(lowered_title) for pattern in self._title_compiled
        ):
            return "denylisted window title"
        lowered_url = str(url or "")
        if lowered_url and any(
            pattern.search(lowered_url) for pattern in self._url_compiled
        ):
            return "denylisted url"
        return ""
