"""Privacy-preserving wake-word calibration storage and analysis.

The database stores model scores, timestamps, and coarse signal level. It never
stores PCM, transcripts, embeddings, or any other reconstructable speech data.
"""

from __future__ import annotations

import bisect
import contextlib
import hashlib
import hmac
import json
import math
import os
import socket
import sqlite3
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from voice.call.wakeword import (
    FRAME_MILLISECONDS,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    WakeGate,
)

SCHEMA_VERSION = 2
NANOSECONDS = 1_000_000_000
DEFAULT_DB = Path("~/.local/state/serena/wakeword-calibration.sqlite3").expanduser()
MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SessionMetadata:
    model_path: str
    model_sha256: str
    score_label: str
    threshold: float
    device: str
    environment: str
    verifier_path: str | None = None
    verifier_sha256: str | None = None
    verifier_threshold: float = 0.1
    vad_threshold: float = 0.0
    speex_noise_suppression: bool = False
    patience_frames: int = 1
    cooldown_seconds: float = 3.0
    phase: str = "development"
    configuration_sha256: str | None = None
    package_version: str | None = None
    notes: str | None = None
    host: str = socket.gethostname()


@dataclass(frozen=True, slots=True)
class CalibrationPolicy:
    # This is a passive week-long livability check, not a training gate. The
    # model can be frozen and used after same-day threshold calibration.
    minimum_background_hours: float = 7.0
    minimum_attempts: int = 20
    minimum_span_days: float = 6.0
    minimum_active_days: int = 7
    minimum_daily_background_hours: float = 1.0
    minimum_attempt_days: int = 1
    maximum_miss_rate: float = 0.05
    maximum_environment_miss_rate: float = 0.10
    minimum_environment_attempts: int = 5
    minimum_environments: int = 1
    minimum_environment_background_hours: float = 1.0
    maximum_drop_rate: float = 0.01
    minimum_contiguous_segment_seconds: float = 60.0
    maximum_frame_gap_seconds: float = 0.2
    maximum_clock_drift_seconds: float = 1.0
    conditional_background_hours: float = 48.0
    conditional_false_accepts: int = 1


def manifest_sha256(manifest: dict[str, Any]) -> str:
    """Hash the immutable acceptance configuration, excluding its own digest."""

    payload = dict(manifest)
    payload.pop("configuration_sha256", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_acceptance_manifest(
    *,
    model_path: Path,
    model_sha256: str,
    score_label: str,
    threshold: float,
    patience_frames: int,
    cooldown_seconds: float,
    device: str,
    package_version: str,
    device_selector: str | None = None,
    verifier_path: Path | None = None,
    verifier_sha256: str | None = None,
    verifier_threshold: float = 0.1,
    vad_threshold: float = 0.0,
    speex_noise_suppression: bool = False,
    created_wall_ns: int | None = None,
    model_provenance_sha256: str | None = None,
) -> dict[str, Any]:
    if not model_sha256 or len(model_sha256) != 64:
        raise ValueError("acceptance manifest needs a SHA-256 model identity")
    if verifier_path is None and verifier_sha256 is not None:
        raise ValueError("verifier hash cannot be set without a verifier path")
    if verifier_path is not None and (
        verifier_sha256 is None or len(verifier_sha256) != 64
    ):
        raise ValueError("acceptance manifest needs a SHA-256 verifier identity")
    if not score_label.strip() or not device.strip() or not package_version.strip():
        raise ValueError("score label, microphone identity, and package version are required")
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("wake threshold must be between 0 and 1")
    if not 0.0 <= verifier_threshold <= 1.0:
        raise ValueError("verifier threshold must be between 0 and 1")
    if not 0.0 <= vad_threshold <= 1.0:
        raise ValueError("VAD threshold must be between 0 and 1")
    if patience_frames < 1 or cooldown_seconds < 0:
        raise ValueError("invalid wake gate configuration")
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "created_wall_ns": time.time_ns() if created_wall_ns is None else created_wall_ns,
        "target_phrase": "hey serena",
        "sample_rate": SAMPLE_RATE,
        "frame_samples": FRAME_SAMPLES,
        "model_path": str(model_path.expanduser().resolve()),
        "model_sha256": model_sha256,
        "model_provenance_sha256": model_provenance_sha256,
        "score_label": score_label.strip(),
        "verifier_path": str(verifier_path.expanduser().resolve())
        if verifier_path is not None
        else None,
        "verifier_sha256": verifier_sha256,
        "verifier_threshold": verifier_threshold,
        "vad_threshold": vad_threshold,
        "speex_noise_suppression": bool(speex_noise_suppression),
        "threshold": threshold,
        "patience_frames": patience_frames,
        "cooldown_seconds": cooldown_seconds,
        "device": device.strip(),
        "device_selector": (device_selector or device).strip(),
        "package_version": package_version.strip(),
    }
    manifest["configuration_sha256"] = manifest_sha256(manifest)
    return manifest


def load_acceptance_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read wake-word acceptance manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("wake-word acceptance manifest must be a JSON object")
    if value.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported wake-word acceptance manifest schema")
    expected = str(value.get("configuration_sha256") or "")
    if not expected or not hmac.compare_digest(expected, manifest_sha256(value)):
        raise ValueError("wake-word acceptance manifest digest does not match")
    return value


@dataclass(slots=True)
class _CandidateState:
    threshold: float
    patience_frames: int
    cooldown_seconds: float
    false_accepts: int = 0
    false_accepts_by_environment: dict[str, int] | None = None
    attempt_detected: dict[str, bool] | None = None
    attempt_peaks: dict[str, float] | None = None
    attempt_detection_ns: dict[str, int | None] | None = None
    gate: WakeGate | None = None

    def __post_init__(self) -> None:
        self.false_accepts_by_environment = defaultdict(int)
        self.attempt_detected = {}
        self.attempt_peaks = {}
        self.attempt_detection_ns = {}
        self.gate = WakeGate(
            self.threshold,
            patience_frames=self.patience_frames,
            cooldown_seconds=self.cooldown_seconds,
        )


class CalibrationStore:
    """SQLite store that is safe for a collector and marker CLI to share."""

    def __init__(self, path: Path = DEFAULT_DB) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            self.path.parent.chmod(0o700)
        self.connection = sqlite3.connect(self.path, timeout=10.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=10000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self._initialize()
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    def __enter__(self) -> CalibrationStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_pk INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL UNIQUE,
                started_wall_ns INTEGER NOT NULL,
                started_mono_ns INTEGER NOT NULL,
                ended_wall_ns INTEGER,
                ended_mono_ns INTEGER,
                model_path TEXT NOT NULL,
                model_sha256 TEXT NOT NULL,
                verifier_path TEXT,
                verifier_sha256 TEXT,
                verifier_threshold REAL NOT NULL,
                vad_threshold REAL NOT NULL,
                speex_noise_suppression INTEGER NOT NULL,
                patience_frames INTEGER NOT NULL,
                cooldown_seconds REAL NOT NULL,
                phase TEXT NOT NULL,
                configuration_sha256 TEXT,
                score_label TEXT NOT NULL,
                threshold REAL NOT NULL,
                device TEXT NOT NULL,
                environment TEXT NOT NULL,
                host TEXT NOT NULL,
                package_version TEXT,
                notes TEXT,
                dropped_frames INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running'
            );
            CREATE TABLE IF NOT EXISTS scores (
                session_pk INTEGER NOT NULL REFERENCES sessions(session_pk),
                frame_index INTEGER NOT NULL,
                wall_ns INTEGER NOT NULL,
                mono_ns INTEGER NOT NULL,
                score REAL NOT NULL CHECK(score >= 0.0 AND score <= 1.0),
                rms_dbfs REAL NOT NULL,
                PRIMARY KEY (session_pk, frame_index)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS scores_wall_idx ON scores(wall_ns);
            CREATE TABLE IF NOT EXISTS intents (
                intent_id TEXT PRIMARY KEY,
                created_wall_ns INTEGER NOT NULL,
                start_wall_ns INTEGER NOT NULL,
                end_wall_ns INTEGER NOT NULL,
                environment TEXT NOT NULL,
                speaker TEXT NOT NULL,
                distance TEXT NOT NULL,
                phrase_variant TEXT NOT NULL,
                configuration_sha256 TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                confirmed_wall_ns INTEGER,
                note TEXT
            );
            CREATE INDEX IF NOT EXISTS intents_window_idx
                ON intents(start_wall_ns, end_wall_ns);
            CREATE TABLE IF NOT EXISTS detections (
                detection_id TEXT PRIMARY KEY,
                session_pk INTEGER NOT NULL REFERENCES sessions(session_pk),
                wall_ns INTEGER NOT NULL,
                mono_ns INTEGER NOT NULL,
                score REAL NOT NULL,
                threshold REAL NOT NULL,
                classification TEXT NOT NULL,
                environment TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS detections_wall_idx ON detections(wall_ns);
            """
        )
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            self.connection.execute(
                "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif int(row["value"]) != SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported wake calibration schema {row['value']}; "
                f"expected {SCHEMA_VERSION}"
            )
        self.connection.commit()

    def begin_session(
        self,
        metadata: SessionMetadata,
        *,
        wall_ns: int | None = None,
        mono_ns: int | None = None,
        session_id: str | None = None,
    ) -> tuple[int, str]:
        wall_ns = time.time_ns() if wall_ns is None else wall_ns
        mono_ns = time.monotonic_ns() if mono_ns is None else mono_ns
        session_id = session_id or str(uuid.uuid4())
        values = asdict(metadata)
        cursor = self.connection.execute(
            """
            INSERT INTO sessions(
                session_id, started_wall_ns, started_mono_ns,
                model_path, model_sha256, verifier_path, verifier_sha256,
                verifier_threshold, vad_threshold, speex_noise_suppression,
                patience_frames, cooldown_seconds, phase, configuration_sha256,
                score_label, threshold, device, environment, host,
                package_version, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                wall_ns,
                mono_ns,
                values["model_path"],
                values["model_sha256"],
                values["verifier_path"],
                values["verifier_sha256"],
                values["verifier_threshold"],
                values["vad_threshold"],
                int(values["speex_noise_suppression"]),
                values["patience_frames"],
                values["cooldown_seconds"],
                values["phase"],
                values["configuration_sha256"],
                values["score_label"],
                values["threshold"],
                values["device"],
                values["environment"],
                values["host"],
                values["package_version"],
                values["notes"],
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid), session_id

    def abandon_running_sessions(
        self,
        *,
        wall_ns: int | None = None,
        mono_ns: int | None = None,
    ) -> int:
        """Close sessions left running by a crash before a new collector starts."""

        cursor = self.connection.execute(
            """
            UPDATE sessions
            SET ended_wall_ns=COALESCE(ended_wall_ns, ?),
                ended_mono_ns=COALESCE(ended_mono_ns, ?),
                status='abandoned'
            WHERE status='running'
            """,
            (
                time.time_ns() if wall_ns is None else wall_ns,
                time.monotonic_ns() if mono_ns is None else mono_ns,
            ),
        )
        self.connection.commit()
        return int(cursor.rowcount)

    def record_scores(
        self,
        session_pk: int,
        rows: Iterable[tuple[int, int, int, float, float]],
    ) -> None:
        self.connection.executemany(
            """
            INSERT INTO scores(
                session_pk, frame_index, wall_ns, mono_ns, score, rms_dbfs
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ((session_pk, *row) for row in rows),
        )
        self.connection.commit()

    def finish_session(
        self,
        session_pk: int,
        *,
        dropped_frames: int,
        status: str,
        wall_ns: int | None = None,
        mono_ns: int | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE sessions
            SET ended_wall_ns=?, ended_mono_ns=?, dropped_frames=?, status=?
            WHERE session_pk=?
            """,
            (
                time.time_ns() if wall_ns is None else wall_ns,
                time.monotonic_ns() if mono_ns is None else mono_ns,
                dropped_frames,
                status,
                session_pk,
            ),
        )
        self.connection.commit()

    def add_intent(
        self,
        *,
        window_seconds: float = 5.0,
        lead_seconds: float = 0.25,
        environment: str = "desk",
        speaker: str = "raghav",
        distance: str = "unspecified",
        phrase_variant: str = "hey serena",
        configuration_sha256: str | None = None,
        note: str | None = None,
        now_wall_ns: int | None = None,
    ) -> str:
        if window_seconds <= 0:
            raise ValueError("intent window must be positive")
        if lead_seconds < 0:
            raise ValueError("intent lead cannot be negative")
        now_wall_ns = time.time_ns() if now_wall_ns is None else now_wall_ns
        intent_id = str(uuid.uuid4())
        start = now_wall_ns + int(lead_seconds * NANOSECONDS)
        end = start + int(window_seconds * NANOSECONDS)
        overlap = self.connection.execute(
            """
            SELECT intent_id FROM intents
            WHERE start_wall_ns <= ? AND end_wall_ns >= ? LIMIT 1
            """,
            (end, start),
        ).fetchone()
        if overlap is not None:
            raise ValueError(
                "intent window overlaps an existing attempt; wait for it to close"
            )
        self.connection.execute(
            """
            INSERT INTO intents(
                intent_id, created_wall_ns, start_wall_ns, end_wall_ns,
                environment, speaker, distance, phrase_variant,
                configuration_sha256, status, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                intent_id,
                now_wall_ns,
                start,
                end,
                environment,
                speaker,
                distance,
                phrase_variant,
                configuration_sha256,
                note,
            ),
        )
        self.connection.commit()
        return intent_id

    def confirm_intent(
        self,
        intent_id: str,
        *,
        valid: bool,
        note: str | None = None,
        wall_ns: int | None = None,
    ) -> None:
        row = self.connection.execute(
            "SELECT status, note FROM intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown wake-word intent {intent_id}")
        if str(row["status"]) != "pending":
            raise ValueError(f"wake-word intent {intent_id} is already confirmed")
        merged_note = note if note is not None else row["note"]
        self.connection.execute(
            """
            UPDATE intents
            SET status=?, confirmed_wall_ns=?, note=?
            WHERE intent_id=?
            """,
            (
                "valid" if valid else "invalid",
                time.time_ns() if wall_ns is None else wall_ns,
                merged_note,
                intent_id,
            ),
        )
        self.connection.commit()

    def intent_at(self, wall_ns: int) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM intents
            WHERE start_wall_ns <= ? AND end_wall_ns >= ?
            ORDER BY start_wall_ns DESC LIMIT 1
            """,
            (wall_ns, wall_ns),
        ).fetchone()

    def add_detection(
        self,
        session_pk: int,
        *,
        wall_ns: int,
        mono_ns: int,
        score: float,
        threshold: float,
        classification: str,
        environment: str,
    ) -> str:
        detection_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO detections(
                detection_id, session_pk, wall_ns, mono_ns, score,
                threshold, classification, environment
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                detection_id,
                session_pk,
                wall_ns,
                mono_ns,
                score,
                threshold,
                classification,
                environment,
            ),
        )
        self.connection.commit()
        return detection_id


def threshold_grid(start: float = 0.30, stop: float = 0.90, step: float = 0.05) -> list[float]:
    if step <= 0 or stop < start:
        raise ValueError("invalid threshold grid")
    values: list[float] = []
    current = start
    while current <= stop + 1e-9:
        values.append(round(current, 6))
        current += step
    if not values or values[0] < 0.0 or values[-1] > 1.0:
        raise ValueError("threshold grid must stay between 0 and 1")
    return values


def analyze_calibration(
    path: Path,
    *,
    thresholds: list[float] | None = None,
    patience_frames: int | None = None,
    cooldown_seconds: float | None = None,
    policy: CalibrationPolicy | None = None,
    acceptance_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Analyze development data or one pre-frozen acceptance configuration."""

    policy = policy or CalibrationPolicy()
    acceptance_mode = acceptance_manifest is not None
    if acceptance_manifest is not None:
        expected_digest = str(acceptance_manifest.get("configuration_sha256") or "")
        if not expected_digest or not hmac.compare_digest(
            expected_digest,
            manifest_sha256(acceptance_manifest),
        ):
            raise ValueError("wake-word acceptance manifest digest does not match")
        fixed_threshold = float(acceptance_manifest["threshold"])
        fixed_patience = int(acceptance_manifest["patience_frames"])
        fixed_cooldown = float(acceptance_manifest["cooldown_seconds"])
        if thresholds is not None and sorted(set(thresholds)) != [fixed_threshold]:
            raise ValueError("acceptance report must use the frozen wake threshold")
        if patience_frames is not None and patience_frames != fixed_patience:
            raise ValueError("acceptance report must use the frozen patience")
        if cooldown_seconds is not None and cooldown_seconds != fixed_cooldown:
            raise ValueError("acceptance report must use the frozen cooldown")
        thresholds = [fixed_threshold]
        patience_frames = fixed_patience
        cooldown_seconds = fixed_cooldown
    else:
        thresholds = thresholds or threshold_grid()
        patience_frames = 1 if patience_frames is None else patience_frames
        cooldown_seconds = 3.0 if cooldown_seconds is None else cooldown_seconds
    if not thresholds:
        raise ValueError("at least one candidate threshold is required")
    candidates = [
        _CandidateState(value, patience_frames, cooldown_seconds)
        for value in sorted(set(thresholds))
    ]

    with CalibrationStore(path) as store:
        sessions = store.connection.execute(
            "SELECT * FROM sessions ORDER BY started_wall_ns"
        ).fetchall()
        intents = store.connection.execute(
            "SELECT * FROM intents ORDER BY start_wall_ns"
        ).fetchall()
        valid_intents = [row for row in intents if str(row["status"]) == "valid"]
        pending_intents = [row for row in intents if str(row["status"]) == "pending"]
        starts = [int(row["start_wall_ns"]) for row in valid_intents]
        intent_by_id = {str(row["intent_id"]): row for row in valid_intents}
        for candidate in candidates:
            candidate.attempt_detected = {
                intent_id: False for intent_id in intent_by_id
            }
            candidate.attempt_peaks = {
                intent_id: 0.0 for intent_id in intent_by_id
            }
            candidate.attempt_detection_ns = {
                intent_id: None for intent_id in intent_by_id
            }

        scoring_identities = {
            (
                str(row["model_sha256"]),
                str(row["verifier_sha256"] or ""),
                float(row["verifier_threshold"]),
                float(row["vad_threshold"]),
                bool(row["speex_noise_suppression"]),
                str(row["score_label"]),
                str(row["package_version"] or ""),
            )
            for row in sessions
        }
        if len(scoring_identities) > 1:
            raise ValueError(
                "calibration database mixes scoring identities; use a fresh database "
                "after changing model, verifier, VAD, Speex, label, or runtime"
            )

        manifest_match = not acceptance_mode
        manifest_created_before_sessions = not acceptance_mode
        if acceptance_manifest is not None:
            digest = str(acceptance_manifest["configuration_sha256"])
            expected_session = {
                "model_sha256": str(acceptance_manifest["model_sha256"]),
                "verifier_sha256": str(
                    acceptance_manifest.get("verifier_sha256") or ""
                ),
                "verifier_threshold": float(
                    acceptance_manifest["verifier_threshold"]
                ),
                "vad_threshold": float(acceptance_manifest["vad_threshold"]),
                "speex_noise_suppression": bool(
                    acceptance_manifest["speex_noise_suppression"]
                ),
                "score_label": str(acceptance_manifest["score_label"]),
                "threshold": float(acceptance_manifest["threshold"]),
                "patience_frames": int(acceptance_manifest["patience_frames"]),
                "cooldown_seconds": float(acceptance_manifest["cooldown_seconds"]),
                "device": str(acceptance_manifest["device"]),
                "package_version": str(acceptance_manifest["package_version"]),
            }
            manifest_match = bool(sessions) and all(
                str(row["phase"]) == "acceptance"
                and str(row["configuration_sha256"] or "") == digest
                and str(row["model_sha256"]) == expected_session["model_sha256"]
                and str(row["verifier_sha256"] or "")
                == expected_session["verifier_sha256"]
                and float(row["verifier_threshold"])
                == expected_session["verifier_threshold"]
                and float(row["vad_threshold"]) == expected_session["vad_threshold"]
                and bool(row["speex_noise_suppression"])
                == expected_session["speex_noise_suppression"]
                and str(row["score_label"]) == expected_session["score_label"]
                and float(row["threshold"]) == expected_session["threshold"]
                and int(row["patience_frames"])
                == expected_session["patience_frames"]
                and float(row["cooldown_seconds"])
                == expected_session["cooldown_seconds"]
                and str(row["device"]) == expected_session["device"]
                and str(row["package_version"] or "")
                == expected_session["package_version"]
                for row in sessions
            ) and all(
                str(row["configuration_sha256"] or "") == digest for row in intents
            )
            manifest_created_before_sessions = bool(sessions) and int(
                acceptance_manifest["created_wall_ns"]
            ) <= min(int(row["started_wall_ns"]) for row in sessions)
            if sessions and not manifest_match:
                raise ValueError(
                    "acceptance database does not match the frozen wake configuration"
                )

        raw_background_frames = 0
        valid_background_frames = 0
        valid_background_by_day: dict[str, int] = defaultdict(int)
        valid_background_by_environment: dict[str, int] = defaultdict(int)
        segment_background_by_day: dict[str, int] = defaultdict(int)
        segment_background_by_environment: dict[str, int] = defaultdict(int)
        segment_frames = 0
        frame_gaps = 0
        timing_violations = 0
        maximum_observed_gap_seconds = 0.0
        first_wall_ns: int | None = None
        last_wall_ns: int | None = None
        current_session: int | None = None
        current_environment = "unknown"
        previous_frame_index: int | None = None
        previous_wall_ns: int | None = None
        previous_mono_ns: int | None = None
        session_environment = {
            int(row["session_pk"]): str(row["environment"]) for row in sessions
        }

        def flush_segment() -> None:
            nonlocal segment_frames, valid_background_frames
            segment_seconds = segment_frames * FRAME_MILLISECONDS / 1000.0
            if segment_seconds >= policy.minimum_contiguous_segment_seconds:
                valid = sum(segment_background_by_day.values())
                valid_background_frames += valid
                for day, count in segment_background_by_day.items():
                    valid_background_by_day[day] += count
                for environment, count in segment_background_by_environment.items():
                    valid_background_by_environment[environment] += count
            segment_frames = 0
            segment_background_by_day.clear()
            segment_background_by_environment.clear()

        query = store.connection.execute(
            """
            SELECT session_pk, frame_index, wall_ns, mono_ns, score
            FROM scores ORDER BY session_pk, frame_index
            """
        )
        for row in query:
            session_pk = int(row["session_pk"])
            if session_pk != current_session:
                flush_segment()
                current_session = session_pk
                current_environment = session_environment.get(session_pk, "unknown")
                previous_frame_index = None
                previous_wall_ns = None
                previous_mono_ns = None
                for candidate in candidates:
                    assert candidate.gate is not None
                    candidate.gate.reset()

            frame_index = int(row["frame_index"])
            wall_ns = int(row["wall_ns"])
            mono_ns = int(row["mono_ns"])
            score = float(row["score"])
            split_segment = False
            if previous_mono_ns is not None and previous_wall_ns is not None:
                mono_delta = (mono_ns - previous_mono_ns) / NANOSECONDS
                wall_delta = (wall_ns - previous_wall_ns) / NANOSECONDS
                maximum_observed_gap_seconds = max(
                    maximum_observed_gap_seconds,
                    mono_delta,
                )
                if (
                    mono_delta <= 0
                    or wall_delta <= 0
                    or previous_frame_index is None
                    or frame_index != previous_frame_index + 1
                    or abs(wall_delta - mono_delta) > policy.maximum_clock_drift_seconds
                ):
                    timing_violations += 1
                    split_segment = True
                elif mono_delta > policy.maximum_frame_gap_seconds:
                    frame_gaps += 1
                    split_segment = True
            if split_segment:
                flush_segment()
                for candidate in candidates:
                    assert candidate.gate is not None
                    candidate.gate.reset()
            previous_frame_index = frame_index
            previous_wall_ns = wall_ns
            previous_mono_ns = mono_ns
            segment_frames += 1
            first_wall_ns = wall_ns if first_wall_ns is None else min(first_wall_ns, wall_ns)
            last_wall_ns = wall_ns if last_wall_ns is None else max(last_wall_ns, wall_ns)

            intent = _intent_at(valid_intents, starts, wall_ns)
            if intent is None:
                raw_background_frames += 1
                local_day = time.strftime(
                    "%Y-%m-%d",
                    time.localtime(wall_ns / NANOSECONDS),
                )
                segment_background_by_day[local_day] += 1
                segment_background_by_environment[current_environment] += 1
                intent_id = None
            else:
                intent_id = str(intent["intent_id"])

            for candidate in candidates:
                assert candidate.gate is not None
                assert candidate.attempt_peaks is not None
                assert candidate.attempt_detected is not None
                assert candidate.attempt_detection_ns is not None
                assert candidate.false_accepts_by_environment is not None
                if intent_id is not None:
                    candidate.attempt_peaks[intent_id] = max(
                        candidate.attempt_peaks[intent_id], score
                    )
                if not candidate.gate.observe(score, monotonic_ns=mono_ns):
                    continue
                if intent_id is None:
                    candidate.false_accepts += 1
                    candidate.false_accepts_by_environment[current_environment] += 1
                else:
                    candidate.attempt_detected[intent_id] = True
                    if candidate.attempt_detection_ns[intent_id] is None:
                        candidate.attempt_detection_ns[intent_id] = wall_ns

        flush_segment()

        raw_background_hours = (
            raw_background_frames * FRAME_MILLISECONDS / 1000.0 / 3600.0
        )
        background_hours = (
            valid_background_frames * FRAME_MILLISECONDS / 1000.0 / 3600.0
        )
        span_days = 0.0
        if first_wall_ns is not None and last_wall_ns is not None:
            span_days = max(0.0, (last_wall_ns - first_wall_ns) / NANOSECONDS / 86400.0)
        background_hours_by_day = {
            day: count * FRAME_MILLISECONDS / 1000.0 / 3600.0
            for day, count in valid_background_by_day.items()
        }
        background_hours_by_environment = {
            environment: count * FRAME_MILLISECONDS / 1000.0 / 3600.0
            for environment, count in valid_background_by_environment.items()
        }
        active_days = sum(
            hours >= policy.minimum_daily_background_hours
            for hours in background_hours_by_day.values()
        )
        attempt_days = len(
            {
                time.strftime(
                    "%Y-%m-%d",
                    time.localtime(int(row["start_wall_ns"]) / NANOSECONDS),
                )
                for row in valid_intents
            }
        )
        model_hashes = sorted({str(row["model_sha256"]) for row in sessions})
        verifier_identities = sorted(
            {str(row["verifier_sha256"] or "") for row in sessions}
        )
        verifier_hashes = [value for value in verifier_identities if value]
        dropped_frames = sum(int(row["dropped_frames"]) for row in sessions)
        total_score_frames = int(
            store.connection.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
        )
        session_statuses: dict[str, int] = defaultdict(int)
        for row in sessions:
            session_statuses[str(row["status"])] += 1
        sessions_closed = bool(sessions) and all(
            row["ended_wall_ns"] is not None
            and row["ended_mono_ns"] is not None
            and str(row["status"]) != "running"
            for row in sessions
        )
        devices = sorted({str(row["device"]) for row in sessions})
        hosts = sorted({str(row["host"]) for row in sessions})

    observed_frames = total_score_frames + dropped_frames
    drop_rate = dropped_frames / observed_frames if observed_frames else 0.0
    timing_valid = timing_violations == 0

    results = [
        _candidate_result(
            candidate,
            intent_by_id,
            background_hours=background_hours,
            background_hours_by_environment=background_hours_by_environment,
            span_days=span_days,
            active_days=active_days,
            attempt_days=attempt_days,
            drop_rate=drop_rate,
            timing_valid=timing_valid,
            sessions_closed=sessions_closed,
            policy=policy,
        )
        for candidate in candidates
    ]
    recommended = _recommend(results, policy)
    if recommended is not None and not acceptance_mode:
        recommended["provisional"] = True
    attempt_details: list[dict[str, Any]] = []
    if recommended is not None:
        state = next(
            candidate
            for candidate in candidates
            if candidate.threshold == recommended["threshold"]
        )
        assert state.attempt_detected is not None
        assert state.attempt_peaks is not None
        assert state.attempt_detection_ns is not None
        for intent in intents:
            intent_id = str(intent["intent_id"])
            valid = intent_id in intent_by_id
            detected = state.attempt_detected.get(intent_id, False)
            detection_ns = state.attempt_detection_ns.get(intent_id)
            attempt_details.append(
                {
                    "intent_id": intent_id,
                    "created_at": _iso_timestamp(int(intent["created_wall_ns"])),
                    "environment": str(intent["environment"]),
                    "speaker": str(intent["speaker"]),
                    "distance": str(intent["distance"]),
                    "phrase_variant": str(intent["phrase_variant"]),
                    "status": str(intent["status"]),
                    "peak_score": round(state.attempt_peaks.get(intent_id, 0.0), 6),
                    "detected": detected if valid else None,
                    "missed": (not detected) if valid else None,
                    "detection_latency_ms": round(
                        (detection_ns - int(intent["start_wall_ns"])) / 1_000_000,
                        3,
                    )
                    if valid and detection_ns is not None
                    else None,
                    "note": intent["note"],
                }
            )

    acceptance_claim = bool(
        acceptance_mode
        and manifest_match
        and manifest_created_before_sessions
        and not pending_intents
        and recommended
        and recommended["status"] == "pass"
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "evaluation_mode": "acceptance" if acceptance_mode else "development",
        "configuration_sha256": acceptance_manifest.get("configuration_sha256")
        if acceptance_manifest
        else None,
        "generated_at": _iso_timestamp(time.time_ns()),
        "database": str(path.expanduser().resolve()),
        "privacy": {
            "raw_audio_stored": False,
            "transcripts_stored": False,
            "stored_signal_data": ["wake score", "rms dbfs", "timestamps"],
        },
        "coverage": {
            "sessions": len(sessions),
            "background_hours": round(background_hours, 6),
            "raw_background_hours": round(raw_background_hours, 6),
            "background_hours_by_day": {
                key: round(value, 6)
                for key, value in sorted(background_hours_by_day.items())
            },
            "background_hours_by_environment": {
                key: round(value, 6)
                for key, value in sorted(background_hours_by_environment.items())
            },
            "attempts": len(valid_intents),
            "pending_attempts": len(pending_intents),
            "invalid_attempts": sum(
                str(row["status"]) == "invalid" for row in intents
            ),
            "attempt_days": attempt_days,
            "span_days": round(span_days, 6),
            "active_days": active_days,
            "frame_gaps": frame_gaps,
            "timing_violations": timing_violations,
            "maximum_observed_gap_seconds": round(maximum_observed_gap_seconds, 6),
            "sessions_closed": sessions_closed,
            "session_statuses": dict(sorted(session_statuses.items())),
            "score_frames": total_score_frames,
            "dropped_frames": dropped_frames,
            "drop_rate": round(drop_rate, 6),
            "model_sha256": model_hashes,
            "verifier_sha256": verifier_hashes,
            "devices": devices,
            "hosts": hosts,
        },
        "acceptance_evidence": {
            "manifest_match": manifest_match,
            "manifest_created_before_sessions": manifest_created_before_sessions,
            "fixed_single_threshold": acceptance_mode and len(candidates) == 1,
            "timing_valid": timing_valid,
        },
        "policy": asdict(policy),
        "patience_frames": patience_frames,
        "cooldown_seconds": cooldown_seconds,
        "candidates": results,
        "recommendation": recommended,
        "attempts": attempt_details,
        "acceptance_claim": acceptance_claim,
    }


def write_report_atomic(report: dict[str, Any], path: Path) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _intent_at(
    intents: list[sqlite3.Row], starts: list[int], wall_ns: int
) -> sqlite3.Row | None:
    index = bisect.bisect_right(starts, wall_ns) - 1
    while index >= 0:
        intent = intents[index]
        if int(intent["end_wall_ns"]) >= wall_ns:
            return intent
        if int(intent["start_wall_ns"]) < wall_ns:
            break
        index -= 1
    return None


def _candidate_result(
    candidate: _CandidateState,
    intent_by_id: dict[str, sqlite3.Row],
    *,
    background_hours: float,
    background_hours_by_environment: dict[str, float],
    span_days: float,
    active_days: int,
    attempt_days: int,
    drop_rate: float,
    timing_valid: bool,
    sessions_closed: bool,
    policy: CalibrationPolicy,
) -> dict[str, Any]:
    assert candidate.attempt_detected is not None
    assert candidate.false_accepts_by_environment is not None
    attempts = len(candidate.attempt_detected)
    misses = sum(not detected for detected in candidate.attempt_detected.values())
    miss_rate = misses / attempts if attempts else None
    by_environment: dict[str, dict[str, Any]] = {}
    environments = sorted(
        {str(row["environment"]) for row in intent_by_id.values()}
        | set(candidate.false_accepts_by_environment)
        | set(background_hours_by_environment)
    )
    environment_ok = True
    for environment in environments:
        ids = [
            intent_id
            for intent_id, row in intent_by_id.items()
            if str(row["environment"]) == environment
        ]
        environment_misses = sum(
            not candidate.attempt_detected[intent_id] for intent_id in ids
        )
        environment_rate = environment_misses / len(ids) if ids else None
        if (
            len(ids) >= policy.minimum_environment_attempts
            and environment_rate is not None
            and environment_rate > policy.maximum_environment_miss_rate
        ):
            environment_ok = False
        by_environment[environment] = {
            "background_hours": round(
                background_hours_by_environment.get(environment, 0.0),
                6,
            ),
            "attempts": len(ids),
            "misses": environment_misses,
            "miss_rate": round(environment_rate, 6) if environment_rate is not None else None,
            "false_accepts": candidate.false_accepts_by_environment.get(environment, 0),
        }

    enough_data = (
        background_hours >= policy.minimum_background_hours
        and attempts >= policy.minimum_attempts
        and span_days >= policy.minimum_span_days
        and active_days >= policy.minimum_active_days
        and attempt_days >= policy.minimum_attempt_days
        and sum(
            row["attempts"] >= policy.minimum_environment_attempts
            and row["background_hours"]
            >= policy.minimum_environment_background_hours
            for row in by_environment.values()
        )
        >= policy.minimum_environments
        and drop_rate <= policy.maximum_drop_rate
        and timing_valid
        and sessions_closed
    )
    recall_ok = (
        miss_rate is not None
        and miss_rate <= policy.maximum_miss_rate
        and environment_ok
    )
    if not enough_data:
        status = "inconclusive"
    elif candidate.false_accepts == 0 and recall_ok:
        status = "pass"
    elif (
        background_hours >= policy.conditional_background_hours
        and candidate.false_accepts <= policy.conditional_false_accepts
        and recall_ok
    ):
        status = "conditional"
    else:
        status = "fail"

    return {
        "threshold": candidate.threshold,
        "status": status,
        "false_accepts": candidate.false_accepts,
        "false_accepts_per_hour": round(
            candidate.false_accepts / background_hours, 6
        )
        if background_hours > 0
        else None,
        "false_accept_upper_95_per_hour": round(
            -math.log(0.05) / background_hours,
            6,
        )
        if candidate.false_accepts == 0 and background_hours > 0
        else None,
        "attempts": attempts,
        "misses": misses,
        "miss_rate": round(miss_rate, 6) if miss_rate is not None else None,
        "environment_ok": environment_ok,
        "by_environment": by_environment,
    }


def _recommend(
    results: list[dict[str, Any]], policy: CalibrationPolicy
) -> dict[str, Any] | None:
    if not results:
        return None
    for status in ("pass", "conditional"):
        eligible = [row for row in results if row["status"] == status]
        if eligible:
            chosen = max(eligible, key=lambda row: row["threshold"])
            return {**chosen, "provisional": False}

    recall_eligible = [
        row
        for row in results
        if row["miss_rate"] is not None
        and row["miss_rate"] <= policy.maximum_miss_rate
    ]
    if recall_eligible:
        chosen = min(
            recall_eligible,
            key=lambda row: (
                row["false_accepts"],
                -row["threshold"],
            ),
        )
    else:
        chosen = min(
            results,
            key=lambda row: (
                row["miss_rate"] if row["miss_rate"] is not None else 1.0,
                row["false_accepts"],
                -row["threshold"],
            ),
        )
    return {**chosen, "provisional": True}


def _iso_timestamp(wall_ns: int) -> str:
    seconds, remainder = divmod(wall_ns, NANOSECONDS)
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(seconds))
    offset = time.strftime("%z", time.localtime(seconds))
    return f"{base}.{remainder // 1_000_000:03d}{offset}"
