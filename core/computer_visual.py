"""Single-use Gideon visual capture backed by the shared desktop helper."""

from __future__ import annotations

import base64
import hashlib
import time

from core.action_authority import BASIS_ORIGIN_TURN, build_request
from core.computer_client import ComputerClient
from core.visual_context import (
    CaptureProvenance,
    ConsentRequired,
    DesktopContext,
    ScreenshotFrame,
    VisualSnapshot,
)


class ComputerVisualAdapter:
    def __init__(self, authority, *, client=None):
        self.authority = authority
        self.client = client or ComputerClient()

    def capture(self, consent):
        consent.validate(now=time.time())
        proof = self.authority.turn_proof(consent.authority_receipt_id)
        if not proof or (proof.identity, proof.source, proof.session_id) != (
            consent.actor_id,
            consent.source,
            consent.session_id,
        ):
            raise ConsentRequired("capture consent does not match its originating user turn")
        request = build_request(
            capability="screen.capture",
            target="active_screen",
            intent="capture the explicitly requested active window",
            effect="external",
            source=consent.source,
            identity=consent.actor_id,
            session_id=consent.session_id,
            turn_id=proof.turn_id,
            origin_proof=proof.proof_id,
            authorization_basis=BASIS_ORIGIN_TURN,
        )
        decision = self.authority.authorize(request)
        if not decision.allowed:
            raise ConsentRequired(decision.reason)
        session_id = None
        try:
            self.client.ensure_running()
            opened = self.client.call(
                "begin",
                mode="watch",
                target="active",
                request="inspect the requested active window once",
                seconds=max(1, min(60, consent.expires_at - time.time())),
                owner=consent.session_id,
                interactive=True,
            )
            session_id = opened["session"]["id"]
            frame = self.client.call("observe", session_id=session_id)
            data = base64.b64decode(frame["data"])
            snapshot = VisualSnapshot(
                # Raw titles can contain sensitive text; the image has the explicitly selected scope.
                DesktopContext(active_app=frame["context"].get("app", "")),
                "",
                {},
                CaptureProvenance(
                    capture_id=frame["frame_id"],
                    request_id=consent.request_id,
                    authority_receipt_sha256=hashlib.sha256(
                        consent.authority_receipt_id.encode()
                    ).hexdigest(),
                    actor_id=consent.actor_id,
                    source=consent.source,
                    session_id=consent.session_id,
                    screenshot_adapter="serena-computer",
                    ocr_adapter="none",
                    accessibility_adapter="none",
                    image_sha256=hashlib.sha256(data).hexdigest(),
                    captured_at=frame["captured_at"],
                    expires_at=min(frame["expires_at"], consent.expires_at),
                    action_receipt_id=request.request_id,
                ),
                ScreenshotFrame(data, frame["media_type"], frame["width"], frame["height"]),
            )
            self.authority.record_outcome(
                request.request_id,
                status="completed",
                detail="single-use screen capture",
                receipt={"capture_id": frame["frame_id"]},
            )
            return snapshot
        except Exception as exc:
            self.authority.record_outcome(
                request.request_id, status="failed", detail=str(exc)[:300]
            )
            raise
        finally:
            if session_id:
                self.client.call(
                    "stop", reason="single-use visual capture finished", session_id=session_id
                )
