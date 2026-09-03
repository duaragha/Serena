"""Consent-bound, local visual context with deterministic adapter seams.

There is deliberately no watcher or polling loop here. Every screenshot needs
one fresh consent envelope and a visible capture indicator. Raw pixels live in
memory only and disappear from the service registry when their short expiry
passes. OCR and accessibility are adapters so production can use local tools
while tests never touch the real desktop.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024
MAX_CONTEXT_CHARS = 40_000
MAX_CAPTURE_TTL_SECONDS = 300.0
CAPTURE_SCOPE = "screen.capture"


class VisualContextError(RuntimeError):
    pass


class ConsentRequired(VisualContextError):
    pass


class PrivateAppExcluded(VisualContextError):
    pass


class CaptureExpired(VisualContextError):
    pass


@dataclass(frozen=True, slots=True)
class CaptureConsent:
    request_id: str
    actor_id: str
    source: str
    session_id: str
    scopes: tuple[str, ...]
    granted_at: float
    expires_at: float
    authority_receipt_id: str
    consented: bool = True

    def validate(self, *, now: float) -> None:
        if not self.consented:
            raise ConsentRequired("screen capture was not consented")
        if not all(
            (
                self.request_id,
                self.actor_id,
                self.source,
                self.session_id,
                self.authority_receipt_id,
            )
        ):
            raise ConsentRequired("capture consent is missing identity or session evidence")
        if CAPTURE_SCOPE not in self.scopes:
            raise ConsentRequired("capture consent does not include screen.capture")
        # Consent is valid on the half-open interval [granted_at, expires_at).
        if not self.granted_at <= now < self.expires_at:
            raise ConsentRequired("capture consent is not currently valid")
        if self.expires_at - self.granted_at > MAX_CAPTURE_TTL_SECONDS:
            raise ConsentRequired("capture consent lasts longer than five minutes")


@dataclass(frozen=True, slots=True)
class DesktopContext:
    active_app: str = ""
    window_title: str = ""
    active_document: str = ""
    current_task: str = ""

    def bounded(self) -> DesktopContext:
        return DesktopContext(
            active_app=_bounded(self.active_app, 256),
            window_title=_bounded(self.window_title, 500),
            active_document=_bounded(self.active_document, 1_000),
            current_task=_bounded(self.current_task, 1_000),
        )


@dataclass(frozen=True, slots=True)
class ScreenshotFrame:
    data: bytes
    media_type: str = "image/png"
    width: int | None = None
    height: int | None = None

    def validate(self) -> None:
        if not self.data:
            raise VisualContextError("screenshot adapter returned an empty frame")
        if len(self.data) > MAX_SCREENSHOT_BYTES:
            raise VisualContextError("screenshot is larger than 20 MB")
        if self.media_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise VisualContextError("screenshot adapter returned an unsupported media type")


@dataclass(frozen=True, slots=True)
class CaptureProvenance:
    capture_id: str
    request_id: str
    authority_receipt_sha256: str
    actor_id: str
    source: str
    session_id: str
    screenshot_adapter: str
    ocr_adapter: str
    accessibility_adapter: str
    image_sha256: str
    captured_at: float
    expires_at: float
    action_receipt_id: str = ""
    redactions: int = 0


@dataclass(frozen=True, slots=True)
class VisualSnapshot:
    context: DesktopContext
    ocr_text: str
    accessibility_tree: dict[str, Any]
    provenance: CaptureProvenance
    image: ScreenshotFrame = field(repr=False)

    def assert_fresh(self, *, now: float | None = None) -> None:
        moment = time.time() if now is None else float(now)
        if moment >= self.provenance.expires_at:
            raise CaptureExpired("visual context has expired")

    def provider_payload(self, *, now: float | None = None) -> dict[str, Any]:
        """Return bounded text metadata. Pixels stay on the image field."""

        self.assert_fresh(now=now)
        return {
            "context": asdict(self.context),
            "ocr_text": self.ocr_text,
            "accessibility_tree": self.accessibility_tree,
            "provenance": asdict(self.provenance),
            "image": {
                "media_type": self.image.media_type,
                "bytes": len(self.image.data),
                "width": self.image.width,
                "height": self.image.height,
            },
        }


@dataclass(frozen=True, slots=True)
class VisualPolicy:
    private_apps: tuple[str, ...] = (
        "1password",
        "bitwarden",
        "keepass",
        "seahorse",
        "secret service",
    )
    private_title_patterns: tuple[str, ...] = (
        r"\bprivate browsing\b",
        r"\bincognito\b",
        r"\bpassword manager\b",
    )
    redact_patterns: tuple[str, ...] = (
        r"(?i)\b(?:password|passcode|api[_ -]?key|secret|token)\s*[:=]\s*\S+",
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        r"\b(?:\d[ -]*?){13,19}\b",
    )
    capture_ttl_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not 1 <= self.capture_ttl_seconds <= MAX_CAPTURE_TTL_SECONDS:
            raise ValueError("capture_ttl_seconds must be between 1 and 300")
        for pattern in (*self.private_title_patterns, *self.redact_patterns):
            re.compile(pattern)


class ScreenshotAdapter(Protocol):
    name: str

    def capture(self) -> ScreenshotFrame: ...


class OCRAdapter(Protocol):
    name: str

    def extract(self, frame: ScreenshotFrame) -> str: ...


class AccessibilityAdapter(Protocol):
    name: str

    def snapshot(self) -> Mapping[str, Any]: ...


class CaptureIndicator(Protocol):
    def begin(self, request_id: str) -> None: ...

    def end(self, request_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class VerificationResult:
    verified: bool
    action_receipt_id: str
    capture_id: str
    checks: dict[str, bool]
    detail: str


class VisualContextService:
    def __init__(
        self,
        *,
        screenshot: ScreenshotAdapter,
        ocr: OCRAdapter,
        accessibility: AccessibilityAdapter,
        context_reader: Callable[[], DesktopContext],
        indicator: CaptureIndicator,
        consent_consumer: Callable[[CaptureConsent], bool],
        policy: VisualPolicy | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.screenshot = screenshot
        self.ocr = ocr
        self.accessibility = accessibility
        self.context_reader = context_reader
        self.indicator = indicator
        self.consent_consumer = consent_consumer
        self.policy = policy or VisualPolicy()
        self.now = now
        self._snapshots: dict[str, VisualSnapshot] = {}
        self._consumed_requests: dict[str, float] = {}

    def capture(
        self,
        consent: CaptureConsent,
        *,
        action_receipt_id: str = "",
    ) -> VisualSnapshot:
        moment = self.now()
        consent.validate(now=moment)
        self._consumed_requests = {
            request_id: expiry
            for request_id, expiry in self._consumed_requests.items()
            if expiry > moment
        }
        if consent.request_id in self._consumed_requests:
            raise ConsentRequired("capture consent was already used")
        if not self.consent_consumer(consent):
            raise ConsentRequired("capture consent was not issued by local authority")
        # Consume before reading desktop state. A failed capture still requires a
        # fresh visible grant rather than silently retrying old permission.
        self._consumed_requests[consent.request_id] = consent.expires_at

        raw_context = self.context_reader().bounded()
        self._assert_not_private(raw_context)
        context, context_count = _redact_context(
            raw_context, self.policy.redact_patterns
        )

        indicator_started = False
        try:
            self.indicator.begin(consent.request_id)
            indicator_started = True
            frame = self.screenshot.capture()
            frame.validate()
            raw_ocr = _bounded(self.ocr.extract(frame), MAX_CONTEXT_CHARS)
            raw_tree = _bounded_mapping(self.accessibility.snapshot())
        finally:
            if indicator_started:
                self.indicator.end(consent.request_id)

        ocr_text, ocr_count = _redact_text(raw_ocr, self.policy.redact_patterns)
        tree, tree_count = _redact_mapping(raw_tree, self.policy.redact_patterns)
        expiry = min(consent.expires_at, moment + self.policy.capture_ttl_seconds)
        provenance = CaptureProvenance(
            capture_id=str(uuid.uuid4()),
            request_id=consent.request_id,
            authority_receipt_sha256=hashlib.sha256(
                consent.authority_receipt_id.encode("utf-8")
            ).hexdigest(),
            actor_id=consent.actor_id,
            source=consent.source,
            session_id=consent.session_id,
            screenshot_adapter=_adapter_name(self.screenshot),
            ocr_adapter=_adapter_name(self.ocr),
            accessibility_adapter=_adapter_name(self.accessibility),
            image_sha256=hashlib.sha256(frame.data).hexdigest(),
            captured_at=moment,
            expires_at=expiry,
            action_receipt_id=_bounded(action_receipt_id, 256),
            redactions=ocr_count + tree_count + context_count,
        )
        snapshot = VisualSnapshot(context, ocr_text, tree, provenance, frame)
        self.purge_expired(now=moment)
        self._snapshots[provenance.capture_id] = snapshot
        return snapshot

    def get(self, capture_id: str, *, now: float | None = None) -> VisualSnapshot:
        snapshot = self._snapshots.get(str(capture_id))
        if snapshot is None:
            raise KeyError(capture_id)
        snapshot.assert_fresh(now=self.now() if now is None else now)
        return snapshot

    def purge_expired(self, *, now: float | None = None) -> int:
        moment = self.now() if now is None else float(now)
        expired = [
            capture_id
            for capture_id, snapshot in self._snapshots.items()
            if snapshot.provenance.expires_at <= moment
        ]
        for capture_id in expired:
            del self._snapshots[capture_id]
        return len(expired)

    def verify_post_action(
        self,
        snapshot: VisualSnapshot,
        *,
        action_receipt_id: str,
        expected: Mapping[str, str],
        now: float | None = None,
    ) -> VerificationResult:
        snapshot.assert_fresh(now=self.now() if now is None else now)
        receipt = str(action_receipt_id or "").strip()
        if not receipt or snapshot.provenance.action_receipt_id != receipt:
            raise VisualContextError(
                "post-action verification requires the matching action receipt"
            )
        allowed = {"active_app", "window_title", "active_document", "current_task", "text_contains"}
        unknown = sorted(set(expected) - allowed)
        if unknown:
            raise ValueError("unknown verification checks: " + ", ".join(unknown))
        checks: dict[str, bool] = {}
        for key, wanted in expected.items():
            needle = str(wanted).casefold()
            if key == "text_contains":
                haystack = snapshot.ocr_text.casefold()
            else:
                haystack = str(getattr(snapshot.context, key)).casefold()
            checks[key] = bool(needle) and needle in haystack
        verified = bool(checks) and all(checks.values())
        return VerificationResult(
            verified=verified,
            action_receipt_id=receipt,
            capture_id=snapshot.provenance.capture_id,
            checks=checks,
            detail="postconditions observed"
            if verified
            else "one or more postconditions were not observed",
        )

    def _assert_not_private(self, context: DesktopContext) -> None:
        app = context.active_app.casefold()
        title = context.window_title
        if any(entry.casefold() in app for entry in self.policy.private_apps):
            raise PrivateAppExcluded("the active application is excluded from capture")
        if any(
            re.search(pattern, title, flags=re.IGNORECASE)
            for pattern in self.policy.private_title_patterns
        ):
            raise PrivateAppExcluded("the active window is excluded from capture")


def _adapter_name(adapter: object) -> str:
    return _bounded(getattr(adapter, "name", type(adapter).__name__), 128)


def _bounded(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _bounded_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    def clean(item: Any, depth: int = 0) -> Any:
        if depth > 8:
            return "[depth-limited]"
        if isinstance(item, Mapping):
            return {
                str(key)[:128]: clean(child, depth + 1) for key, child in list(item.items())[:500]
            }
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            return [clean(child, depth + 1) for child in list(item)[:500]]
        if isinstance(item, (bool, int, float)) or item is None:
            return item
        return _bounded(item, 2_000)

    result = clean(value)
    if not isinstance(result, dict):
        raise VisualContextError("accessibility adapter must return an object")
    return result


def _redact_text(value: str, patterns: Sequence[str]) -> tuple[str, int]:
    text = value
    count = 0
    for pattern in patterns:
        text, substitutions = re.subn(pattern, "[redacted]", text)
        count += substitutions
    return text, count


def _redact_mapping(
    value: Mapping[str, Any], patterns: Sequence[str]
) -> tuple[dict[str, Any], int]:
    count = 0

    def redact(item: Any) -> Any:
        nonlocal count
        if isinstance(item, Mapping):
            return {key: redact(child) for key, child in item.items()}
        if isinstance(item, list):
            return [redact(child) for child in item]
        if isinstance(item, str):
            text, substitutions = _redact_text(item, patterns)
            count += substitutions
            return text
        return item

    return dict(redact(value)), count


def _redact_context(
    context: DesktopContext, patterns: Sequence[str]
) -> tuple[DesktopContext, int]:
    values: list[str] = []
    substitutions = 0
    for value in (
        context.active_app,
        context.window_title,
        context.active_document,
        context.current_task,
    ):
        scrubbed, count = _redact_text(value, patterns)
        values.append(scrubbed)
        substitutions += count
    return DesktopContext(*values), substitutions
