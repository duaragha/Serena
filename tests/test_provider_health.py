"""Continuity modes, the capability matrix, and durable resume.

The behaviour under test is what Serena is still allowed to claim when the
subscriptions are gone. Two failures are treated as serious here: saying a
capability survives an outage when it does not, and letting a result claim it
came from a provider that did not produce it.
"""

from __future__ import annotations

import pytest

from core.local_model_fallback import LocalModelProfile, LocalModelStatus
from core.provider_health import (
    CAPABILITY_MATRIX,
    DEGRADED,
    FULL,
    MODES,
    OFFLINE,
    ContinuityError,
    ContinuityStore,
    assert_not_claiming_cloud,
    assess_continuity,
    describe,
    route_brain_turn,
)

NOW = 1_786_000_000.0


def _capacity(*, claude=True, codex=True, claude_reason="", codex_reason=""):
    return {
        "claude": {
            "provider": "claude",
            "status": "available" if claude else "unavailable",
            "usable": claude,
            "reason": claude_reason or ("ok" if claude else "usage limit reached"),
            "source": "test",
        },
        "codex": {
            "provider": "codex",
            "status": "available" if codex else "unavailable",
            "usable": codex,
            "reason": codex_reason or ("ok" if codex else "usage limit reached"),
            "source": "test",
        },
    }


_PROFILE = LocalModelProfile(
    role="conversation",
    model_id="qwen2.5:14b-instruct-q4_K_M",
    parameters_b=14.0,
    quantization="Q4_K_M",
    approx_vram_gb=9.6,
    context_tokens=16_384,
)


def _local(available: bool, reason: str = "served locally"):
    return _PROFILE, LocalModelStatus(available, reason, base_url="http://127.0.0.1:11434/v1")


@pytest.fixture
def store(tmp_path):
    return ContinuityStore(tmp_path / "continuity.sqlite3")


# ---- routing the resident brain -------------------------------------------


def test_routing_picks_the_cloud_provider_while_one_has_capacity():
    routing = route_brain_turn(
        _capacity(), now=NOW, probe_local=False, cloud_model_for=lambda _p: "claude-opus-5"
    )
    assert routing.provider == "claude"
    assert routing.is_local is False
    assert routing.should_queue is False


def test_routing_falls_to_the_local_model_when_both_subscriptions_are_out():
    routing = route_brain_turn(
        _capacity(claude=False, codex=False), local=_local(True), now=NOW
    )
    assert routing.provider == "local"
    assert routing.is_local is True
    assert routing.model == "qwen2.5:14b-instruct-q4_K_M"
    assert routing.should_queue is False
    assert routing.mode == DEGRADED


def test_routing_queues_the_turn_when_nothing_can_answer_it():
    routing = route_brain_turn(
        _capacity(claude=False, codex=False), local=_local(False, "nothing loaded"), now=NOW
    )
    assert routing.provider == ""
    assert routing.should_queue is True
    assert routing.mode == OFFLINE


def test_asking_for_local_when_no_weights_are_loaded_queues_instead_of_pretending():
    routing = route_brain_turn(
        _capacity(),
        override="local",
        local=_local(False, "qwen2.5:14b-instruct-q4_K_M is not loaded"),
        now=NOW,
        probe_local=True,
    )
    assert routing.provider == ""
    assert routing.should_queue is True
    assert "is not loaded" in routing.reason


def test_an_explicit_cloud_override_that_is_out_of_capacity_is_not_silently_swapped():
    routing = route_brain_turn(
        _capacity(claude=False), override="claude", local=_local(False), now=NOW
    )
    assert routing.provider == ""
    assert routing.should_queue is True
    assert "out of capacity" in routing.reason


def test_routing_can_reuse_an_assessment_without_probing_again():
    state = assess_continuity(
        _capacity(claude=False, codex=False), local=_local(True), now=NOW
    )
    routing = route_brain_turn(state=state)
    assert routing.provider == "local"
    assert routing.mode == DEGRADED


# ---- mode classification --------------------------------------------------


def test_the_modes_are_the_three_declared_ones():
    assert MODES == (FULL, DEGRADED, OFFLINE)
    assert set(CAPABILITY_MATRIX) == set(MODES)


def test_a_healthy_subscription_is_full_mode_and_does_not_touch_the_gpu():
    state = assess_continuity(
        _capacity(),
        now=NOW,
        cloud_model_for=lambda provider: "claude-opus-5",
        probe_local=False,
    )
    assert state.mode == FULL
    assert state.selected_provider == "claude"
    assert state.selected_model == "claude-opus-5"
    assert state.fallback_reason == ""
    assert state.local_available is False
    assert state.allows("frontier_reasoning")
    assert state.allows("coding_jobs")


def test_one_exhausted_provider_still_means_full_but_names_the_fallback():
    state = assess_continuity(
        _capacity(claude=False, claude_reason="resets 1:40pm"),
        now=NOW,
        probe_local=False,
    )
    assert state.mode == FULL
    assert state.selected_provider == "codex"
    assert "claude is out of capacity" in state.fallback_reason
    assert "resets 1:40pm" in state.fallback_reason


def test_unknown_capacity_is_not_treated_as_exhausted():
    state = assess_continuity({}, now=NOW, probe_local=False)
    assert state.mode == FULL
    assert state.usable_cloud == ("claude", "codex")


def test_both_out_with_a_local_model_is_degraded():
    state = assess_continuity(
        _capacity(claude=False, codex=False),
        local=_local(True),
        now=NOW,
    )
    assert state.mode == DEGRADED
    assert state.selected_provider == "local"
    assert state.selected_model == "qwen2.5:14b-instruct-q4_K_M"
    assert "both subscriptions are out" in state.fallback_reason
    assert state.local_available is True


def test_both_out_with_no_local_model_is_offline():
    state = assess_continuity(
        _capacity(claude=False, codex=False),
        local=(None, LocalModelStatus(False, "no local model server answering")),
        now=NOW,
    )
    assert state.mode == OFFLINE
    assert state.selected_provider == ""
    assert state.selected_model == ""
    assert "no local model server answering" in state.fallback_reason


def test_a_probe_failure_never_raises_out_of_assessment():
    class Boom:
        def get(self, _name):
            raise RuntimeError("capacity file is corrupt")

    state = assess_continuity(Boom(), local=_local(False), now=NOW)
    # Unreadable capacity is unknown, and unknown stays usable.
    assert state.mode in MODES


# ---- what survives an outage ---------------------------------------------


def test_the_local_only_capabilities_survive_every_mode():
    for mode in MODES:
        matrix = CAPABILITY_MATRIX[mode]
        assert matrix["memory_recall"] is True, mode
        assert matrix["briefings"] is True, mode
        assert matrix["queued_work"] is True, mode
        assert matrix["safe_local_actions"] is True, mode


def test_cloud_only_capabilities_are_refused_when_cloud_is_gone():
    for mode in (DEGRADED, OFFLINE):
        assert CAPABILITY_MATRIX[mode]["frontier_reasoning"] is False, mode
        assert CAPABILITY_MATRIX[mode]["coding_jobs"] is False, mode


def test_offline_does_not_claim_local_reasoning():
    state = assess_continuity(
        _capacity(claude=False, codex=False),
        local=(None, LocalModelStatus(False, "nothing loaded")),
        now=NOW,
    )
    assert not state.allows("local_reasoning")
    assert state.allows("briefings")


# ---- honest description ---------------------------------------------------


def test_degraded_says_the_real_local_model_and_the_real_reason():
    state = assess_continuity(
        _capacity(claude=False, codex=False), local=_local(True), now=NOW
    )
    line = describe(state)
    assert "degraded" in line
    assert "qwen2.5:14b-instruct-q4_K_M" in line
    assert "no cloud reasoning" in line
    # It must not imply a frontier model answered.
    assert "opus" not in line.lower()


def test_offline_does_not_pretend_a_model_is_answering():
    state = assess_continuity(
        _capacity(claude=False, codex=False),
        local=(None, LocalModelStatus(False, "nothing loaded")),
        now=NOW,
    )
    line = describe(state)
    assert line.startswith("offline")
    assert "memory, commitments" in line


def test_full_mode_names_the_provider_actually_selected():
    state = assess_continuity(
        _capacity(), now=NOW, cloud_model_for=lambda _p: "claude-opus-5", probe_local=False
    )
    assert "claude" in describe(state)
    assert "claude-opus-5" in describe(state)


# ---- provenance -----------------------------------------------------------


def test_a_degraded_result_may_not_claim_a_cloud_provider():
    state = assess_continuity(
        _capacity(claude=False, codex=False), local=_local(True), now=NOW
    )
    assert_not_claiming_cloud(state, {"provider": "local", "model": "qwen2.5:14b"})
    with pytest.raises(AssertionError):
        assert_not_claiming_cloud(state, {"provider": "claude", "model": "claude-opus-5"})


def test_a_full_mode_result_may_not_claim_to_be_local():
    state = assess_continuity(_capacity(), now=NOW, probe_local=False)
    assert_not_claiming_cloud(state, {"provider": "claude", "model": "claude-opus-5"})
    with pytest.raises(AssertionError):
        assert_not_claiming_cloud(state, {"provider": "local", "model": "qwen2.5:14b"})


# ---- deferred work and durable resume ------------------------------------


def test_deferring_needs_a_kind_and_a_summary(store):
    with pytest.raises(ContinuityError):
        store.defer(kind="", summary="something", reason="no capacity")
    with pytest.raises(ContinuityError):
        store.defer(kind="brain_turn", summary="  ", reason="no capacity")


def test_queued_work_survives_reopening_the_store(tmp_path):
    path = tmp_path / "continuity.sqlite3"
    first = ContinuityStore(path)
    item = first.defer(
        kind="brain_turn",
        summary="explain the tax thing",
        reason="both subscriptions out",
        payload={"turn_id": "voice-9"},
        now=NOW,
    )
    reopened = ContinuityStore(path)
    pending = reopened.pending()
    assert [entry.work_id for entry in pending] == [item.work_id]
    assert pending[0].payload == {"turn_id": "voice-9"}
    assert pending[0].state == "queued"


def test_resume_refuses_while_still_degraded(store):
    store.defer(kind="brain_turn", summary="a question", reason="out", now=NOW)
    degraded = assess_continuity(
        _capacity(claude=False, codex=False), local=_local(True), now=NOW
    )

    def must_not_run(_item):  # pragma: no cover - the point is it is not called
        raise AssertionError("queued frontier work must not be run on the local model")

    report = store.resume(must_not_run, state=degraded, now=NOW)
    assert report["resumed"] == 0
    assert report["skipped"] == 1
    assert "still degraded" in report["reason"]
    assert len(store.pending()) == 1


def test_resume_drains_the_queue_once_cloud_is_back(store):
    store.defer(kind="brain_turn", summary="first", reason="out", now=NOW)
    store.defer(kind="brain_turn", summary="second", reason="out", now=NOW)
    healthy = assess_continuity(_capacity(), now=NOW, probe_local=False)

    seen = []

    def handler(item):
        seen.append(item.summary)
        return True

    report = store.resume(handler, state=healthy, now=NOW + 60)
    assert report["resumed"] == 2
    assert seen == ["first", "second"]
    assert store.pending() == []
    assert [entry.state for entry in store.list(state="resumed")] == ["resumed", "resumed"]


def test_a_failing_resume_is_retried_then_given_up_on(store):
    store.defer(kind="brain_turn", summary="doomed", reason="out", now=NOW)
    healthy = assess_continuity(_capacity(), now=NOW, probe_local=False)

    def failing(_item):
        return False

    for _ in range(5):
        store.resume(failing, state=healthy, max_attempts=5, now=NOW)
    assert store.pending() == []
    abandoned = store.list(state="abandoned")
    assert len(abandoned) == 1
    assert "gave up after" in abandoned[0].last_error


def test_a_raising_handler_is_recorded_not_swallowed(store):
    store.defer(kind="brain_turn", summary="explodes", reason="out", now=NOW)
    healthy = assess_continuity(_capacity(), now=NOW, probe_local=False)

    def exploding(_item):
        raise RuntimeError("worker died")

    report = store.resume(exploding, state=healthy, now=NOW)
    assert report["failed"] == 1
    assert "worker died" in store.pending()[0].last_error


# ---- mode history ---------------------------------------------------------


def test_only_mode_changes_are_recorded(store):
    healthy = assess_continuity(_capacity(), now=NOW, probe_local=False)
    degraded = assess_continuity(
        _capacity(claude=False, codex=False), local=_local(True), now=NOW + 60
    )

    assert store.record_mode(healthy) is True
    assert store.record_mode(healthy) is False  # unchanged, not written again
    assert store.record_mode(degraded) is True
    assert store.current_mode() == DEGRADED

    history = store.mode_history()
    assert [row["mode"] for row in history] == [DEGRADED, FULL]
    assert history[0]["selected_model"] == "qwen2.5:14b-instruct-q4_K_M"


def test_a_fresh_store_reports_full_until_told_otherwise(store):
    assert store.current_mode() == FULL


# ---- briefings keep working in an outage ---------------------------------


def test_briefings_are_generated_with_no_provider_at_all(tmp_path):
    """The ws-4 and ws-8 seam: an outage must not cost him his day."""

    from core.briefings import build_morning_briefing
    from core.commitments import CommitmentStore

    commitments = CommitmentStore(tmp_path / "commitments.sqlite3")
    commitments.propose(
        title="dentist",
        actor="raghav",
        source="voice",
        due_at=NOW + 3_600,
        state="accepted",
        now=NOW,
    )
    state = assess_continuity(
        _capacity(claude=False, codex=False),
        local=(None, LocalModelStatus(False, "nothing loaded")),
        now=NOW,
    )
    assert state.mode == OFFLINE
    assert state.allows("briefings")

    briefing = build_morning_briefing(commitments, now=NOW, mode=state.mode)
    assert not briefing.empty
    assert "dentist" in briefing.spoken
