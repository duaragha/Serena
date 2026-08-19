from __future__ import annotations

import sqlite3

import pytest

from core.supportive_mode import (
    SupportiveModeDisabled,
    SupportiveModeStore,
    support_boundary,
)


def test_support_store_declares_schema_version(tmp_path):
    path = tmp_path / "support.sqlite3"
    SupportiveModeStore(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_mode_is_off_by_default_and_has_an_easy_off_switch(tmp_path):
    store = SupportiveModeStore(tmp_path / "support.sqlite3")

    assert store.settings().enabled is False
    assert store.context_for_provider() == ""
    assert store.checkin_due(now=100.0) is False
    with pytest.raises(SupportiveModeDisabled):
        store.add_reflection("private thought", source="chat", now=100.0)

    store.configure(
        enabled=True,
        allow_checkins=True,
        allow_pattern_insights=True,
        relationship_context="Use the stable Serena voice across providers.",
        now=100.0,
    )
    assert store.settings().enabled is True
    assert store.disable(now=101.0).enabled is False
    assert store.context_for_provider(now=101.0) == ""
    assert store.checkin_due(now=1_000.0) is False


def test_private_reflections_are_durable_and_retention_is_enforced(tmp_path):
    path = tmp_path / "support.sqlite3"
    store = SupportiveModeStore(path)
    store.configure(enabled=True, retention_days=2, now=0.0)
    entry = store.add_reflection(
        "I got outside and felt steadier.",
        source="desk:turn-1",
        mood="steady",
        tags=("movement",),
        now=10.0,
        retention_days=1,
    )

    reopened = SupportiveModeStore(path)
    assert reopened.reflections(now=20.0)[0] == entry
    assert reopened.purge_expired(now=86_411.0) == 1
    assert reopened.reflections(now=86_411.0) == []
    assert all("felt steadier" not in str(event) for event in reopened.audit_events())


def test_lowering_retention_applies_to_existing_reflections_immediately(tmp_path):
    path = tmp_path / "support.sqlite3"
    store = SupportiveModeStore(path)
    store.configure(enabled=True, retention_days=90, now=0.0)
    entry = store.add_reflection(
        "content that should age out under the new policy",
        source="journal",
        now=1.0,
    )

    store.configure(enabled=True, retention_days=1, now=2 * 86_400)

    assert store.reflections(now=2 * 86_400) == []
    events = store.audit_events()
    assert any(
        event["event_type"] == "reflection.retention_shortened"
        and event["entry_id"] == entry.entry_id
        for event in events
    )
    assert all("content that should age out" not in str(event) for event in events)


def test_corrupted_tag_metadata_fails_empty_without_hiding_reflections(tmp_path):
    path = tmp_path / "support.sqlite3"
    store = SupportiveModeStore(path)
    store.configure(enabled=True, allow_pattern_insights=True, now=0.0)
    entry = store.add_reflection(
        "the body remains readable",
        source="journal",
        tags=("steady",),
        now=1.0,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE support_reflections SET tags_json = ? WHERE entry_id = ?",
            ("{broken", entry.entry_id),
        )

    loaded = store.reflections(now=2.0)

    assert len(loaded) == 1
    assert loaded[0].body == "the body remains readable"
    assert loaded[0].tags == ()
    assert store.patterns(now=2.0) == []


def test_patterns_are_local_counts_with_explainable_provenance(tmp_path):
    store = SupportiveModeStore(tmp_path / "support.sqlite3")
    store.configure(enabled=True, allow_pattern_insights=True, now=0.0)
    first = store.add_reflection(
        "A walk helped today.", source="journal", mood="steady", tags=("movement",), now=1.0
    )
    second = store.add_reflection(
        "Stretching made the afternoon easier.",
        source="journal",
        mood="steady",
        tags=("movement",),
        now=2.0,
    )

    insights = store.patterns(now=3.0)
    movement = next(item for item in insights if item.kind == "tag")

    assert movement.value == "movement"
    assert movement.count == 2
    assert set(movement.entry_ids) == {first.entry_id, second.entry_id}
    assert all(len(digest) == 64 for digest in movement.source_sha256)
    assert "not a diagnosis" in movement.explanation
    context = store.context_for_provider(now=3.0)
    assert "not a therapist" in context
    assert "A walk helped" not in context


def test_patterns_and_checkins_need_separate_consent(tmp_path):
    store = SupportiveModeStore(tmp_path / "support.sqlite3")
    store.configure(enabled=True, allow_checkins=False, allow_pattern_insights=False, now=0.0)
    store.add_reflection("one", source="journal", mood="steady", tags=("movement",), now=1.0)
    store.add_reflection("two", source="journal", mood="steady", tags=("movement",), now=2.0)

    assert store.patterns(now=3.0) == []
    assert store.checkin_due(now=3.0) is False
    with pytest.raises(SupportiveModeDisabled):
        store.record_checkin(now=3.0)


def test_checkins_are_consent_bound_and_rate_limited(tmp_path):
    store = SupportiveModeStore(tmp_path / "support.sqlite3")
    store.configure(enabled=True, allow_checkins=True, checkin_interval_hours=12, now=0.0)

    assert store.checkin_due(now=100.0) is True
    store.record_checkin(now=100.0)
    assert store.checkin_due(now=101.0) is False
    assert store.checkin_due(now=100.0 + 12 * 3_600) is True


def test_deletion_controls_remove_content_without_hiding_lifecycle(tmp_path):
    store = SupportiveModeStore(tmp_path / "support.sqlite3")
    store.configure(enabled=True, now=0.0)
    first = store.add_reflection("first private entry", source="journal", now=1.0)
    store.add_reflection("second private entry", source="journal", now=2.0)

    assert store.forget_reflection(first.entry_id, now=3.0) is True
    with pytest.raises(ValueError, match="explicit confirmation"):
        store.delete_all_reflections(confirmed=False)
    assert store.delete_all_reflections(confirmed=True, now=4.0) == 1
    assert store.reflections(now=4.0) == []
    audit = str(store.audit_events())
    assert "first private entry" not in audit
    assert "second private entry" not in audit


def test_medical_and_crisis_boundaries_are_fixed_and_never_claim_outreach():
    crisis = support_boundary("I might hurt myself and cannot stay safe")
    medical = support_boundary("can you diagnose whether I am bipolar")
    ordinary = support_boundary("I had a hard day")

    assert crisis.level == "crisis"
    assert "emergency services" in crisis.guidance
    assert crisis.external_action_taken is False
    assert medical.level == "medical"
    assert "Do not diagnose" in medical.guidance
    assert ordinary.level == "supportive"
    assert ordinary.external_action_taken is False
