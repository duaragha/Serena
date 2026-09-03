from __future__ import annotations

import asyncio

import pytest

from voice.call.endpoint import EndpointResult
from voice.call.orchestrator import CallRuntime, CallSession
from voice.call.turn_taking import AdaptiveEouPolicy, assess_completeness


class TestAssessCompleteness:
    @pytest.mark.parametrize(
        "text",
        [
            "play the next song.",
            "That works for me!",
            "is it going to rain today?",
            "  turn off the lights.  ",
        ],
    )
    def test_terminal_punctuation_is_complete(self, text: str) -> None:
        assert assess_completeness(text) == "complete"

    @pytest.mark.parametrize(
        "text",
        [
            "what time is it",
            "how long until the meeting",
            "can you check the weather",
            "PLAY THE NEXT SONG",
            "turn off the lights",
            "set a timer for ten minutes",
        ],
    )
    def test_unpunctuated_question_or_imperative_is_complete(self, text: str) -> None:
        assert assess_completeness(text) == "complete"

    @pytest.mark.parametrize("text", ["stop", "pause", "skip", "cancel"])
    def test_standalone_command_is_complete(self, text: str) -> None:
        assert assess_completeness(text) == "complete"

    @pytest.mark.parametrize(
        "text",
        [
            "check the weather,",
            "so I was thinking,",
            "and",
            "i want to",
            "look for the",
            "send it to",
            "a message for",
            "It was kind of",
            "I mean",
            "you know",
            "do it because",
            "um",
            "uh",
            "open my",
            "I was going to say...",
            "set the volume to like",
        ],
    )
    def test_dangling_tail_is_incomplete(self, text: str) -> None:
        assert assess_completeness(text) == "incomplete"

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "the meeting notes from yesterday",
            "yesterday's standup recording",
            "turn",
        ],
    )
    def test_trailing_content_without_signal_is_unknown(self, text: str) -> None:
        assert assess_completeness(text) == "unknown"

    def test_case_insensitive_dangling_word(self) -> None:
        assert assess_completeness("AND") == "incomplete"
        assert assess_completeness("I Mean") == "incomplete"


class TestAdaptiveEouPolicy:
    def _policy(self) -> AdaptiveEouPolicy:
        return AdaptiveEouPolicy(
            min_ms=480, base_ms=800, max_ms=2400, partial_trigger_ms=240
        )

    def test_complete_commits_at_min(self) -> None:
        policy = self._policy()
        assert policy.decide(479, "complete") == "wait"
        assert policy.decide(480, "complete") == "commit"

    def test_unknown_commits_at_base_not_min(self) -> None:
        policy = self._policy()
        assert policy.decide(480, "unknown") == "wait"
        assert policy.decide(799, "unknown") == "wait"
        assert policy.decide(800, "unknown") == "commit"

    def test_incomplete_waits_until_the_long_pause_ceiling(self) -> None:
        policy = self._policy()
        for trailing in (480, 800, 1400, 2399):
            assert policy.decide(trailing, "incomplete") == "wait"
        assert policy.decide(2400, "incomplete") == "commit"

    def test_inflight_partial_blocks_the_base_commit(self) -> None:
        policy = self._policy()
        assert (
            policy.decide(800, "unknown", transcript_pending=True)
            == "wait"
        )
        assert (
            policy.decide(2399, "unknown", transcript_pending=True)
            == "wait"
        )
        assert (
            policy.decide(2400, "unknown", transcript_pending=True)
            == "commit"
        )

    def test_defaults_match_design_knobs(self) -> None:
        policy = AdaptiveEouPolicy()
        assert (policy.min_ms, policy.base_ms, policy.max_ms, policy.partial_trigger_ms) == (
            480,
            800,
            2400,
            240,
        )

    def test_invalid_thresholds_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            AdaptiveEouPolicy(min_ms=700, base_ms=650, max_ms=1400)
        with pytest.raises(ValueError):
            AdaptiveEouPolicy(min_ms=480, base_ms=1500, max_ms=1400)
        with pytest.raises(ValueError):
            AdaptiveEouPolicy(partial_trigger_ms=0)

    def test_unknown_completeness_label_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            self._policy().decide(1000, "maybe")


class _BlockingPartialSTT:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def transcribe(
        self,
        pcm16: bytes,
        sample_rate: int,
        *,
        generation: int,
    ) -> str:
        del pcm16, sample_rate, generation
        self.started.set()
        await self.release.wait()
        return "i was going to continue because"


class _AdaptiveEndpoint:
    speech_active = True
    trailing_ms = 0

    def __init__(self) -> None:
        self.finishes: list[str | None] = []

    def finish(
        self,
        *,
        force: bool = False,
        reason: str | None = None,
    ) -> EndpointResult:
        del force
        self.finishes.append(reason)
        pcm = b"\x01\x00" * 1_600
        return EndpointResult(
            pcm=pcm,
            reason=reason or "ptt_end",
            sample_count=len(pcm) // 2,
            trailing_silence_samples=self.trailing_ms * 16,
        )


def test_runtime_waits_for_an_inflight_partial_before_base_commit(
    tmp_path,
) -> None:
    async def scenario() -> None:
        stt = _BlockingPartialSTT()
        endpoint = _AdaptiveEndpoint()
        runtime = CallRuntime(
            stt=stt,
            brain=object(),
            tts=object(),
            endpoint_factory=lambda: endpoint,
            eou_policy=AdaptiveEouPolicy(),
            metrics_path=tmp_path / "metrics.jsonl",
        )
        session = CallSession(object(), runtime)
        session.current_generation = 1
        pcm = b"\x01\x00" * 1_280

        endpoint.trailing_ms = 240
        assert await session._observe_adaptive_frame(1, pcm) is None
        await stt.started.wait()

        endpoint.trailing_ms = 800
        assert await session._observe_adaptive_frame(1, pcm) is None
        assert endpoint.finishes == []

        stt.release.set()
        assert session._speculative_task is not None
        await session._speculative_task

        endpoint.trailing_ms = 1_200
        assert await session._observe_adaptive_frame(1, pcm) is None
        assert endpoint.finishes == []

        endpoint.trailing_ms = 2_400
        result = await session._observe_adaptive_frame(1, pcm)
        assert result is not None
        assert result.reason == "silence"
        assert endpoint.finishes == ["silence"]
        session._reset_turn_mirror()

    asyncio.run(scenario())


def test_interrupting_her_keeps_only_what_he_actually_heard() -> None:
    """2026-08-20: cutting her off left _recent_dialogue with no record of the
    reply at all, because the cancel path raised before the transcript line was
    appended. She could restart an answer he had already heard most of. The
    handbook's rule: keep the spoken prefix, mark where it stopped."""
    import asyncio

    from voice.call import orchestrator as orch

    session = orch.CallSession.__new__(orch.CallSession)
    session._recent_dialogue = []
    session._spoken_sentences = {7: ["the deploy failed.", "it was the migration."]}

    class _Telemetry:
        def __init__(self): self.events = []
        def record(self, event, **kw): self.events.append(event)

    session.telemetry = _Telemetry()
    session._record_truncated_reply(7)

    assert session._recent_dialogue == [
        "Serena: the deploy failed. it was the migration. (cut off here by Raghav)"
    ]
    assert "turn.reply_truncated" in session.telemetry.events
    # the generation's buffer is consumed, never replayed into a later turn
    assert 7 not in session._spoken_sentences


def test_a_reply_he_never_heard_is_not_remembered_as_said() -> None:
    """Truncation must not invent speech: interrupted before any audio left
    the queue means she said nothing, and nothing is what gets recorded."""
    from voice.call import orchestrator as orch

    session = orch.CallSession.__new__(orch.CallSession)
    session._recent_dialogue = []
    session._spoken_sentences = {3: []}

    class _Telemetry:
        def record(self, event, **kw): pass

    session.telemetry = _Telemetry()
    session._record_truncated_reply(3)
    assert session._recent_dialogue == []
