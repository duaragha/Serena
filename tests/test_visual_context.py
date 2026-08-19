from __future__ import annotations

import pytest

from core.visual_context import (
    CaptureConsent,
    CaptureExpired,
    ConsentRequired,
    DesktopContext,
    PrivateAppExcluded,
    ScreenshotFrame,
    VisualContextError,
    VisualContextService,
    VisualPolicy,
)


class FakeScreenshot:
    name = "fake-screenshot"

    def __init__(self):
        self.calls = 0

    def capture(self):
        self.calls += 1
        return ScreenshotFrame(b"not-real-pixels", "image/png", 100, 50)


class FakeOCR:
    name = "fake-ocr"

    def extract(self, _frame):
        return "email me at private@example.com token=secret-value task complete"


class FakeAccessibility:
    name = "fake-accessibility"

    def snapshot(self):
        return {"role": "window", "children": [{"name": "password: swordfish"}]}


class FakeIndicator:
    def __init__(self):
        self.events = []

    def begin(self, request_id):
        self.events.append(("begin", request_id))

    def end(self, request_id):
        self.events.append(("end", request_id))


def consent(*, now=100.0, allowed=True, receipt="capture-grant-1"):
    return CaptureConsent(
        request_id="capture-1",
        actor_id="raghav",
        source="desktop",
        session_id="session-1",
        scopes=("screen.capture",),
        granted_at=now - 1,
        expires_at=now + 30,
        authority_receipt_id=receipt,
        consented=allowed,
    )


def service(*, now, context=None, consent_consumer=None):
    screenshot = FakeScreenshot()
    indicator = FakeIndicator()
    visual = VisualContextService(
        screenshot=screenshot,
        ocr=FakeOCR(),
        accessibility=FakeAccessibility(),
        context_reader=lambda: (
            context or DesktopContext("code", "tests", "visual_context.py", "implement vision")
        ),
        indicator=indicator,
        consent_consumer=consent_consumer
        or (lambda item: item.authority_receipt_id == "capture-grant-1"),
        policy=VisualPolicy(capture_ttl_seconds=20),
        now=lambda: now[0],
    )
    return visual, screenshot, indicator


def test_capture_requires_fresh_explicit_consent_before_pixels_are_read():
    now = [100.0]
    visual, screenshot, indicator = service(now=now)

    with pytest.raises(ConsentRequired):
        visual.capture(consent(now=100, allowed=False))

    assert screenshot.calls == 0
    assert indicator.events == []


def test_capture_requires_an_authority_issued_receipt_before_desktop_access():
    now = [100.0]
    context_reads = []
    visual, screenshot, indicator = service(now=now)
    visual.context_reader = lambda: context_reads.append(True) or DesktopContext()

    with pytest.raises(ConsentRequired, match="local authority"):
        visual.capture(consent(receipt="forged"))

    assert context_reads == []
    assert screenshot.calls == 0
    assert indicator.events == []


def test_capture_consent_is_single_use_even_when_the_first_capture_succeeds():
    now = [100.0]
    visual, screenshot, indicator = service(now=now)
    grant = consent()

    visual.capture(grant)
    with pytest.raises(ConsentRequired, match="already used"):
        visual.capture(grant)

    assert screenshot.calls == 1
    assert indicator.events == [("begin", "capture-1"), ("end", "capture-1")]


def test_capture_consent_expires_at_the_declared_boundary():
    now = [130.0]
    visual, screenshot, _ = service(now=now)

    with pytest.raises(ConsentRequired, match="not currently valid"):
        visual.capture(consent(now=100.0))

    assert screenshot.calls == 0


def test_private_apps_are_excluded_before_indicator_or_capture():
    now = [100.0]
    visual, screenshot, indicator = service(
        now=now,
        context=DesktopContext("Bitwarden", "Vault", "", ""),
    )

    with pytest.raises(PrivateAppExcluded):
        visual.capture(consent())

    assert screenshot.calls == 0
    assert indicator.events == []


def test_capture_is_visible_redacted_provenanced_and_bounded():
    now = [100.0]
    visual, screenshot, indicator = service(
        now=now,
        context=DesktopContext(
            "code",
            "tests",
            "private@example.com",
            "token=context-secret",
        ),
    )

    snapshot = visual.capture(consent())
    payload = snapshot.provider_payload(now=100.0)

    assert screenshot.calls == 1
    assert indicator.events == [("begin", "capture-1"), ("end", "capture-1")]
    assert "private@example.com" not in snapshot.ocr_text
    assert "secret-value" not in snapshot.ocr_text
    assert "swordfish" not in str(snapshot.accessibility_tree)
    assert "private@example.com" not in snapshot.context.active_document
    assert "context-secret" not in snapshot.context.current_task
    assert snapshot.provenance.redactions == 5
    assert len(snapshot.provenance.authority_receipt_sha256) == 64
    assert "capture-grant-1" not in str(payload["provenance"])
    assert snapshot.provenance.image_sha256
    assert payload["image"] == {"media_type": "image/png", "bytes": 15, "width": 100, "height": 50}


def test_capture_expiry_removes_raw_frame_from_registry():
    now = [100.0]
    visual, _, _ = service(now=now)
    snapshot = visual.capture(consent())

    now[0] = 121.0

    with pytest.raises(CaptureExpired):
        snapshot.provider_payload(now=now[0])
    assert visual.purge_expired() == 1
    with pytest.raises(KeyError):
        visual.get(snapshot.provenance.capture_id)


def test_post_action_verification_is_tied_to_the_action_receipt():
    now = [100.0]
    visual, _, _ = service(now=now)
    snapshot = visual.capture(consent(), action_receipt_id="action-123")

    result = visual.verify_post_action(
        snapshot,
        action_receipt_id="action-123",
        expected={"active_app": "code", "text_contains": "task complete"},
    )

    assert result.verified is True
    assert result.checks == {"active_app": True, "text_contains": True}
    with pytest.raises(VisualContextError, match="matching action receipt"):
        visual.verify_post_action(
            snapshot,
            action_receipt_id="wrong",
            expected={"active_app": "code"},
        )


def test_indicator_is_cleared_when_a_local_adapter_fails():
    now = [100.0]
    visual, _, indicator = service(now=now)
    visual.ocr.extract = lambda _frame: (_ for _ in ()).throw(RuntimeError("ocr unavailable"))

    with pytest.raises(RuntimeError, match="ocr unavailable"):
        visual.capture(consent())

    assert indicator.events == [("begin", "capture-1"), ("end", "capture-1")]
