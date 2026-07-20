from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from core.work_jobs import WorkJobEvent
from voice.call.brain import BrainDoneMeta, BrainEvent
from voice.call.orchestrator import CallRuntime, CallSession
from voice.call.protocol import ProtocolError
from voice.call.tts import DeterministicTTSStub


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str | bytes] = []

    def send(self, payload: str | bytes) -> None:
        self.sent.append(payload)


class StubDispatcher:
    def __init__(self) -> None:
        self.submissions = []
        self.links = []
        self.events = []
        self.consumed_receipts = set()
        self.live_submissions = []
        self.workspace_controls = []

    def submit_if_explicit(self, text: str, *, call_id: str, turn_id: str):
        self.submissions.append((text, call_id, turn_id))
        if "send me a link" not in text:
            return None
        return SimpleNamespace(job_id="job-1", state="accepted")

    def link_origin_session(self, job_id: str, session_id: str) -> None:
        self.links.append((job_id, session_id))

    def submit_live_work_if_explicit(
        self,
        text: str,
        *,
        call_id: str,
        turn_id: str,
        context=None,
    ):
        self.live_submissions.append((text, call_id, turn_id, context or []))
        if not text.startswith("fix"):
            return None
        return SimpleNamespace(item_id="voice-item-1", state="queued")

    def control_coding_workspace(self, text: str, *, action: str):
        self.workspace_controls.append((text, action))
        return {"ok": True}

    def events_for_call(self, call_id: str, *, after: int = 0):
        return [
            event for event in self.events if event.call_id == call_id and event.event_seq > after
        ]

    def event_for_call(self, call_id: str, event_seq: int):
        return next(
            (
                event
                for event in self.events
                if event.call_id == call_id and event.event_seq == event_seq
            ),
            None,
        )

    def consume_artifact_receipt(self, receipt, *, job_id, artifact_id, sha256):
        valid = (
            receipt == "v1.server.receipt"
            and job_id == "job-1"
            and artifact_id == "artifact-1"
            and sha256 == "a" * 64
            and receipt not in self.consumed_receipts
        )
        if valid:
            self.consumed_receipts.add(receipt)
        return valid


class DoneBrain:
    async def stream_turn(
        self,
        text,
        *,
        call_id,
        turn_id,
        request_id=None,
        journal=True,
    ):
        request_id = request_id or "request"
        yield BrainEvent("start", request_id)
        yield BrainEvent(
            "done",
            request_id,
            say="i'm drafting it now.",
            meta=BrainDoneMeta(
                turns=1,
                session_turns=2,
                extra={"session_id": "origin-session", "turn_id": turn_id},
            ),
        )


def _runtime(tmp_path, dispatcher, *, brain=None) -> CallRuntime:
    return CallRuntime(
        stt=object(),
        brain=brain or object(),
        tts=DeterministicTTSStub(samples_per_sentence=80),
        endpoint_factory=object,
        task_dispatcher=dispatcher,
        metrics_path=tmp_path / "metrics.jsonl",
    )


def test_explicit_task_submission_uses_exact_call_and_turn(tmp_path) -> None:
    async def scenario() -> None:
        dispatcher = StubDispatcher()
        session = CallSession(FakeSocket(), _runtime(tmp_path, dispatcher))
        session.call_id = "call-task"
        session.telemetry = session.runtime.make_telemetry(session.call_id)

        accepted = await session._submit_call_task(4, "draft a note and send me a link")
        ignored = await session._submit_call_task(5, "just brainstorm this")

        assert dispatcher.submissions == [
            ("draft a note and send me a link", "call-task", "call-task:4"),
            ("just brainstorm this", "call-task", "call-task:5"),
        ]
        assert session._task_job_ids == {4: "job-1"}
        assert accepted == "job-1"
        assert ignored is None
        metrics = [
            json.loads(line) for line in (tmp_path / "metrics.jsonl").read_text().splitlines()
        ]
        assert [row["event"] for row in metrics] == ["task.accepted"]

    asyncio.run(scenario())


def test_spoken_build_request_is_queued_for_serenas_private_coding_work(tmp_path) -> None:
    async def scenario() -> None:
        dispatcher = StubDispatcher()
        session = CallSession(FakeSocket(), _runtime(tmp_path, dispatcher))
        session.call_id = "call-live"
        session.telemetry = session.runtime.make_telemetry(session.call_id)

        accepted = await session._submit_live_work(
            3,
            "fix the voice pacing",
            context=["the answer pauses between sentences"],
        )

        assert accepted == "voice-item-1"
        assert dispatcher.live_submissions == [
            (
                "fix the voice pacing",
                "call-live",
                "call-live:3",
                ["the answer pauses between sentences"],
            )
        ]
        metrics = [
            json.loads(line)
            for line in (tmp_path / "metrics.jsonl").read_text().splitlines()
        ]
        assert metrics[-1]["event"] == "voice_inbox.accepted"

    asyncio.run(scenario())


def test_spoken_code_panel_command_is_applied_before_reply(tmp_path) -> None:
    async def scenario() -> None:
        session = CallSession(FakeSocket(), _runtime(tmp_path, StubDispatcher()))
        session.call_id = "call-panel"
        session.current_generation = 2
        session.telemetry = session.runtime.make_telemetry(session.call_id)

        action = await session._apply_code_panel_intent(
            2,
            "Can you open a coding panel now?",
        )

        assert action == "open"
        assert [json.loads(payload) for payload in session.ws.sent] == [
            {"type": "code.panel", "generation": 2, "action": "open"}
        ]

    asyncio.run(scenario())


def test_ordered_job_events_replay_on_a_new_call_socket(tmp_path) -> None:
    async def collect(session: CallSession) -> list[dict]:
        task = asyncio.create_task(session._watch_job_events())
        for _ in range(50):
            if len(session.ws.sent) == 3:
                break
            await asyncio.sleep(0.01)
        session._closed = True
        await task
        return [json.loads(item) for item in session.ws.sent]

    async def scenario() -> None:
        dispatcher = StubDispatcher()
        dispatcher.events = [
            WorkJobEvent(
                event_seq=5,
                job_id="job-1",
                call_id="call-replay",
                type="job.accepted",
                payload={"state": "accepted"},
            ),
            WorkJobEvent(
                event_seq=6,
                job_id="job-1",
                call_id="call-replay",
                type="job.progress",
                payload={"state": "running", "message": "drafting"},
            ),
            WorkJobEvent(
                event_seq=7,
                job_id="job-1",
                call_id="call-replay",
                type="artifact.ready",
                payload={
                    "state": "artifact_ready",
                    "name": "draft.md",
                    "url": "/artifacts/signed",
                },
            ),
        ]
        first = CallSession(FakeSocket(), _runtime(tmp_path, dispatcher))
        first.call_id = "call-replay"
        first.telemetry = first.runtime.make_telemetry(first.call_id)
        reconnect = CallSession(FakeSocket(), first.runtime)
        reconnect.call_id = "call-replay"
        reconnect.telemetry = reconnect.runtime.make_telemetry(reconnect.call_id)

        first_events = await collect(first)
        replayed = await collect(reconnect)
        assert first_events == replayed
        assert [event["type"] for event in first_events] == [
            "job.accepted",
            "job.progress",
            "artifact.ready",
        ]
        assert [event["event_seq"] for event in first_events] == [5, 6, 7]

    asyncio.run(scenario())


def test_phone_ack_cursor_and_in_app_open_are_recorded(tmp_path) -> None:
    async def scenario() -> None:
        dispatcher = StubDispatcher()
        ready = WorkJobEvent(
            event_seq=7,
            job_id="job-1",
            call_id="call-ack",
            type="artifact.ready",
            payload={
                "state": "artifact_ready",
                "artifact_id": "artifact-1",
                "name": "draft.md",
                "url": "/artifacts/signed",
                "sha256": "a" * 64,
            },
        )
        dispatcher.events = [ready]
        session = CallSession(FakeSocket(), _runtime(tmp_path, dispatcher))
        session.call_id = "call-ack"
        session._call_started = True
        session.telemetry = session.runtime.make_telemetry(session.call_id)

        await session._handle_control({"type": "job.ack", "event_seq": 7})
        await session._handle_control(
            {
                "type": "artifact.opened",
                "event_seq": 7,
                "job_id": "job-1",
                "receipt": "v1.server.receipt",
            }
        )
        await session._accept_job_cursor(7)

        rows = [json.loads(line) for line in (tmp_path / "metrics.jsonl").read_text().splitlines()]
        assert [row["event"] for row in rows] == [
            "task.event_acknowledged",
            "task.artifact_opened",
        ]
        assert rows[0]["source"] == "phone_ack"
        assert rows[1]["receipt_verified"] is True
        assert session._job_event_cursor == 7

        replay = CallSession(FakeSocket(), session.runtime)
        replay.call_id = "call-ack"
        replay._call_started = True
        replay.telemetry = replay.runtime.make_telemetry(replay.call_id)
        with pytest.raises(ProtocolError, match="not issued by the server"):
            await replay._handle_control(
                {
                    "type": "artifact.opened",
                    "event_seq": 7,
                    "job_id": "job-1",
                    "receipt": "v1.server.receipt",
                }
            )

    asyncio.run(scenario())


def test_phone_cannot_claim_open_without_a_server_fetch_receipt(tmp_path) -> None:
    async def scenario() -> None:
        dispatcher = StubDispatcher()
        dispatcher.events = [
            WorkJobEvent(
                event_seq=7,
                job_id="job-1",
                call_id="call-forged-open",
                type="artifact.ready",
                payload={
                    "state": "artifact_ready",
                    "artifact_id": "artifact-1",
                    "name": "draft.md",
                    "url": "/artifacts/signed",
                    "sha256": "a" * 64,
                },
            )
        ]
        session = CallSession(FakeSocket(), _runtime(tmp_path, dispatcher))
        session.call_id = "call-forged-open"
        session._call_started = True
        session.telemetry = session.runtime.make_telemetry(session.call_id)

        with pytest.raises(ProtocolError, match="not issued by the server"):
            await session._handle_control(
                {
                    "type": "artifact.opened",
                    "event_seq": 7,
                    "job_id": "job-1",
                    "receipt": "v1.forged.receipt",
                }
            )
        assert not (tmp_path / "metrics.jsonl").exists()

    asyncio.run(scenario())


def test_brain_done_links_job_to_resident_session(tmp_path) -> None:
    async def scenario() -> None:
        dispatcher = StubDispatcher()
        session = CallSession(FakeSocket(), _runtime(tmp_path, dispatcher, brain=DoneBrain()))
        session.call_id = "call-link"
        session.current_generation = 2
        session._task_job_ids[2] = "job-2"

        await session._brain_to_tts(2, "draft it")

        assert dispatcher.links == [("job-2", "origin-session")]

    asyncio.run(scenario())
