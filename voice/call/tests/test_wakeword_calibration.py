from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from voice.call.wakeword_calibration import (
    CalibrationPolicy,
    CalibrationStore,
    SessionMetadata,
    analyze_calibration,
    build_acceptance_manifest,
    load_acceptance_manifest,
    threshold_grid,
    write_report_atomic,
)


def metadata() -> SessionMetadata:
    return SessionMetadata(
        model_path="/models/hey_serena.onnx",
        model_sha256="a" * 64,
        score_label="hey_serena",
        threshold=0.5,
        device="test-mic",
        environment="normal-desk",
        package_version="0.6.0",
    )


def relaxed_policy() -> CalibrationPolicy:
    return CalibrationPolicy(
        minimum_background_hours=0,
        minimum_attempts=2,
        minimum_span_days=0,
        minimum_active_days=0,
        minimum_daily_background_hours=0,
        minimum_attempt_days=0,
        maximum_miss_rate=0,
        maximum_environment_miss_rate=0,
        minimum_environment_attempts=1,
        minimum_environments=2,
        minimum_environment_background_hours=0,
        minimum_contiguous_segment_seconds=0,
        conditional_background_hours=0,
        conditional_false_accepts=0,
    )


def test_store_has_no_audio_or_transcript_columns(tmp_path: Path) -> None:
    db = tmp_path / "calibration.sqlite3"
    with CalibrationStore(db) as store:
        columns = {
            str(row[1])
            for table in ("sessions", "scores", "intents", "detections")
            for row in store.connection.execute(f"PRAGMA table_info({table})")
        }
    assert not {"audio", "pcm", "transcript", "embedding"} & columns
    assert db.stat().st_mode & 0o777 == 0o600


def test_store_records_scores_intents_and_detections(tmp_path: Path) -> None:
    db = tmp_path / "calibration.sqlite3"
    with CalibrationStore(db) as store:
        session_pk, session_id = store.begin_session(
            metadata(), wall_ns=1_000_000_000, mono_ns=2_000_000_000
        )
        store.record_scores(
            session_pk,
            [(0, 1_000_000_000, 2_000_000_000, 0.2, -40.0)],
        )
        intent_id = store.add_intent(
            now_wall_ns=1_000_000_000,
            lead_seconds=0,
            window_seconds=5,
        )
        detection_id = store.add_detection(
            session_pk,
            wall_ns=1_100_000_000,
            mono_ns=2_100_000_000,
            score=0.8,
            threshold=0.5,
            classification="intended",
            environment="quiet-near",
        )
        store.finish_session(session_pk, dropped_frames=0, status="complete")

        assert session_id
        assert intent_id
        assert detection_id
        assert store.intent_at(1_100_000_000)["intent_id"] == intent_id
        assert store.connection.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1


def test_analysis_selects_highest_zero_false_threshold_with_full_recall(
    tmp_path: Path,
) -> None:
    db = tmp_path / "calibration.sqlite3"
    wall = 1_700_000_000_000_000_000
    mono = 10_000_000_000
    with CalibrationStore(db) as store:
        session_pk, _ = store.begin_session(metadata(), wall_ns=wall, mono_ns=mono)
        first_intent = store.add_intent(
            now_wall_ns=wall + 1_000_000_000,
            lead_seconds=0,
            window_seconds=2,
            environment="quiet-near",
        )
        second_intent = store.add_intent(
            now_wall_ns=wall + 10_000_000_000,
            lead_seconds=0,
            window_seconds=2,
            environment="far-field",
        )
        store.confirm_intent(first_intent, valid=True, wall_ns=wall + 4_000_000_000)
        store.confirm_intent(second_intent, valid=True, wall_ns=wall + 14_000_000_000)
        rows = [
            (0, wall, mono, 0.40, -35.0),
            (1, wall + 1_000_000_000, mono + 1_000_000_000, 0.60, -20.0),
            (2, wall + 10_000_000_000, mono + 10_000_000_000, 0.60, -22.0),
            (3, wall + 20_000_000_000, mono + 20_000_000_000, 0.10, -40.0),
        ]
        store.record_scores(session_pk, rows)
        store.finish_session(session_pk, dropped_frames=0, status="complete")

    report = analyze_calibration(
        db,
        thresholds=[0.3, 0.5, 0.7],
        cooldown_seconds=0,
        policy=relaxed_policy(),
    )

    by_threshold = {row["threshold"]: row for row in report["candidates"]}
    assert by_threshold[0.3]["false_accepts"] == 1
    assert by_threshold[0.5]["status"] == "pass"
    assert by_threshold[0.7]["misses"] == 2
    assert report["recommendation"]["threshold"] == 0.5
    assert report["recommendation"]["provisional"] is True
    assert report["evaluation_mode"] == "development"
    assert report["acceptance_claim"] is False
    attempts = {row["intent_id"]: row for row in report["attempts"]}
    assert attempts[first_intent]["detected"] is True
    assert attempts[second_intent]["detected"] is True
    assert report["privacy"]["raw_audio_stored"] is False


def test_real_policy_remains_inconclusive_without_a_week_of_data(tmp_path: Path) -> None:
    db = tmp_path / "calibration.sqlite3"
    with CalibrationStore(db):
        pass
    report = analyze_calibration(db, thresholds=[0.5])
    assert report["candidates"][0]["status"] == "inconclusive"
    assert report["recommendation"]["provisional"] is True
    assert report["acceptance_claim"] is False


def test_acceptance_requires_one_manifest_frozen_before_collection(
    tmp_path: Path,
) -> None:
    wall = 1_700_000_000_000_000_000
    mono = 10_000_000_000
    manifest = build_acceptance_manifest(
        model_path=tmp_path / "hey_serena.onnx",
        model_sha256="a" * 64,
        score_label="hey_serena",
        threshold=0.5,
        patience_frames=1,
        cooldown_seconds=0,
        device="0:test-mic:0:1",
        device_selector="0",
        package_version="0.6.0",
        created_wall_ns=wall - 1,
    )
    db = tmp_path / "acceptance.sqlite3"
    session = SessionMetadata(
        model_path=str(tmp_path / "hey_serena.onnx"),
        model_sha256="a" * 64,
        score_label="hey_serena",
        threshold=0.5,
        device="0:test-mic:0:1",
        environment="normal-desk",
        package_version="0.6.0",
        cooldown_seconds=0,
        phase="acceptance",
        configuration_sha256=manifest["configuration_sha256"],
    )
    with CalibrationStore(db) as store:
        session_pk, _ = store.begin_session(session, wall_ns=wall, mono_ns=mono)
        first = store.add_intent(
            now_wall_ns=wall + 1_000_000_000,
            lead_seconds=0,
            window_seconds=2,
            environment="quiet-near",
            configuration_sha256=manifest["configuration_sha256"],
        )
        second = store.add_intent(
            now_wall_ns=wall + 10_000_000_000,
            lead_seconds=0,
            window_seconds=2,
            environment="far-field",
            configuration_sha256=manifest["configuration_sha256"],
        )
        store.confirm_intent(first, valid=True, wall_ns=wall + 4_000_000_000)
        store.confirm_intent(second, valid=True, wall_ns=wall + 14_000_000_000)
        store.record_scores(
            session_pk,
            [
                (0, wall, mono, 0.1, -40.0),
                (1, wall + 1_000_000_000, mono + 1_000_000_000, 0.8, -20.0),
                (2, wall + 10_000_000_000, mono + 10_000_000_000, 0.8, -20.0),
                (3, wall + 20_000_000_000, mono + 20_000_000_000, 0.1, -40.0),
            ],
        )
        store.finish_session(session_pk, dropped_frames=0, status="complete")

    report = analyze_calibration(
        db,
        policy=relaxed_policy(),
        acceptance_manifest=manifest,
    )
    assert report["evaluation_mode"] == "acceptance"
    assert report["acceptance_evidence"]["manifest_match"] is True
    assert report["acceptance_evidence"]["fixed_single_threshold"] is True
    assert report["recommendation"]["provisional"] is False
    assert report["acceptance_claim"] is True


def test_manifest_tampering_and_mixed_scoring_identity_are_rejected(
    tmp_path: Path,
) -> None:
    manifest = build_acceptance_manifest(
        model_path=tmp_path / "hey_serena.onnx",
        model_sha256="a" * 64,
        score_label="hey_serena",
        threshold=0.5,
        patience_frames=1,
        cooldown_seconds=3,
        device="test-mic",
        package_version="0.6.0",
    )
    path = tmp_path / "manifest.json"
    write_report_atomic(manifest, path)
    assert load_acceptance_manifest(path)["threshold"] == 0.5
    tampered = dict(manifest)
    tampered["threshold"] = 0.7
    path.write_text(json.dumps(tampered), encoding="utf-8")
    try:
        load_acceptance_manifest(path)
    except ValueError as exc:
        assert "digest" in str(exc)
    else:
        raise AssertionError("tampered wake-word manifest was accepted")

    db = tmp_path / "mixed.sqlite3"
    with CalibrationStore(db) as store:
        store.begin_session(metadata())
        store.begin_session(replace(metadata(), vad_threshold=0.4))
    try:
        analyze_calibration(db, thresholds=[0.5])
    except ValueError as exc:
        assert "scoring identities" in str(exc)
    else:
        raise AssertionError("mixed wake-word scoring identities were accepted")


def test_sparse_frames_and_unconfirmed_windows_cannot_fake_background_evidence(
    tmp_path: Path,
) -> None:
    db = tmp_path / "sparse.sqlite3"
    wall = 1_700_000_000_000_000_000
    mono = 10_000_000_000
    with CalibrationStore(db) as store:
        session_pk, _ = store.begin_session(metadata(), wall_ns=wall, mono_ns=mono)
        store.add_intent(
            now_wall_ns=wall,
            lead_seconds=0,
            window_seconds=5,
            environment="quiet-near",
        )
        store.record_scores(
            session_pk,
            [
                (
                    day,
                    wall + day * 86_400_000_000_000,
                    mono + day * 86_400_000_000_000,
                    0.8 if day == 0 else 0.1,
                    -30.0,
                )
                for day in range(7)
            ],
        )
        store.finish_session(session_pk, dropped_frames=0, status="complete")

    report = analyze_calibration(db, thresholds=[0.5], cooldown_seconds=0)
    candidate = report["candidates"][0]
    assert report["coverage"]["raw_background_hours"] > 0
    assert report["coverage"]["background_hours"] == 0
    assert report["coverage"]["active_days"] == 0
    assert report["coverage"]["pending_attempts"] == 1
    assert candidate["false_accepts"] == 1
    assert report["acceptance_claim"] is False


def test_report_write_is_atomic_and_private(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    write_report_atomic({"ok": True}, output)
    assert json.loads(output.read_text()) == {"ok": True}
    assert output.stat().st_mode & 0o777 == 0o600


def test_threshold_grid_is_stable() -> None:
    assert threshold_grid(0.3, 0.4, 0.05) == [0.3, 0.35, 0.4]
