"""Collect and calibrate Serena's local "hey serena" wake-word model.

Examples:
    python -m voice.call.wakeword_harness doctor
    python -m voice.call.wakeword_harness collect --environment normal-desk
    python -m voice.call.wakeword_harness attempt --environment quiet-near
    python -m voice.call.wakeword_harness report --output wake-report.json

The collector writes scores and coarse RMS only. Microphone PCM never leaves the
process and is never written to disk.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import json
import os
import queue
import signal
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np

from voice.call.wakeword import (
    FRAME_SAMPLES,
    SAMPLE_RATE,
    OpenWakeWordScorer,
    WakeGate,
    WakeWordConfigurationError,
    WakeWordModelSpec,
    rms_dbfs,
    sha256_file,
)
from voice.call.wakeword_calibration import (
    DEFAULT_DB,
    CalibrationStore,
    SessionMetadata,
    analyze_calibration,
    build_acceptance_manifest,
    load_acceptance_manifest,
    threshold_grid,
    write_report_atomic,
)
from voice.call.wakeword_model import (
    DEFAULT_PROVENANCE_ATTESTATION_KEY,
    verify_installation_attestation,
)

DEFAULT_MODEL = Path("~/.config/serena/models/hey_serena.onnx").expanduser()
DEFAULT_REPORT = Path("~/.local/state/serena/wakeword-report.json").expanduser()
DEFAULT_ACCEPTANCE_MANIFEST = Path(
    "~/.config/serena/wakeword-acceptance.json"
).expanduser()
_QUEUE_FRAMES = 128
_WRITE_BATCH = 50


def _json(value: Any) -> None:
    print(json.dumps(value, sort_keys=True), flush=True)


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _device(value: str | None) -> int | str | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _spec(
    args: argparse.Namespace,
    manifest: dict[str, Any] | None = None,
) -> WakeWordModelSpec:
    if manifest is not None:
        return WakeWordModelSpec(
            model_path=_path(str(manifest["model_path"])),
            score_label=str(manifest["score_label"]),
            verifier_path=_path(str(manifest["verifier_path"]))
            if manifest.get("verifier_path")
            else None,
            verifier_threshold=float(manifest["verifier_threshold"]),
            vad_threshold=float(manifest["vad_threshold"]),
            enable_speex_noise_suppression=bool(
                manifest["speex_noise_suppression"]
            ),
        )
    return WakeWordModelSpec(
        model_path=_path(args.model),
        score_label=args.score_label,
        verifier_path=_path(args.verifier) if args.verifier else None,
        verifier_threshold=args.verifier_threshold,
        vad_threshold=args.oww_vad_threshold,
        enable_speex_noise_suppression=args.speex,
    )


def _acceptance_manifest(args: argparse.Namespace) -> dict[str, Any] | None:
    raw = getattr(args, "manifest", None)
    return load_acceptance_manifest(_path(raw)) if raw else None


def _verify_manifest_files(manifest: dict[str, Any]) -> None:
    model = _path(str(manifest["model_path"]))
    if sha256_file(model) != str(manifest["model_sha256"]):
        raise WakeWordConfigurationError(
            "frozen hey serena model hash does not match the acceptance manifest"
        )
    if manifest.get("verifier_path"):
        verifier = _path(str(manifest["verifier_path"]))
        if sha256_file(verifier) != str(manifest.get("verifier_sha256") or ""):
            raise WakeWordConfigurationError(
                "frozen verifier hash does not match the acceptance manifest"
            )


def _resolve_device_identity(sd: Any, raw: str | None) -> tuple[int | str | None, str]:
    selector = _device(raw)
    resolved = sd.default.device[0] if selector is None else selector
    info = sd.query_devices(resolved, "input")
    identity = ":".join(
        (
            str(resolved),
            str(info.get("name") or "unknown"),
            str(info.get("hostapi") or "unknown"),
            str(info.get("max_input_channels") or "unknown"),
        )
    )
    return selector, identity


def _openwakeword_version() -> str:
    try:
        return importlib.metadata.version("openwakeword")
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _warm_scorer(scorer: OpenWakeWordScorer) -> None:
    silence = np.zeros(FRAME_SAMPLES, dtype=np.int16)
    for _ in range(5):
        scorer.score_frame(silence)


def _validated_model_provenance(
    path: Path,
    *,
    model: Path,
    model_hash: str,
    attestation_key: Path,
) -> str:
    """Require a runtime-promoted training artifact before acceptance freeze."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WakeWordConfigurationError(f"cannot read model provenance: {exc}") from exc
    if not isinstance(value, dict):
        raise WakeWordConfigurationError("model provenance must be a JSON object")
    try:
        verify_installation_attestation(value, attestation_key)
    except ValueError as exc:
        raise WakeWordConfigurationError(str(exc)) from exc
    expected = {
        "target_phrase": "hey serena",
        "model_name": "hey_serena",
        "model_sha256": model_hash,
        "installed_model": str(model.resolve()),
        "validated_for_runtime": True,
    }
    for key, wanted in expected.items():
        if value.get(key) != wanted:
            raise WakeWordConfigurationError(
                f"model provenance has invalid or missing {key}"
            )
    runtime_validation = value.get("runtime_validation")
    if not isinstance(runtime_validation, dict) or runtime_validation.get(
        "score_label"
    ) != "hey_serena":
        raise WakeWordConfigurationError(
            "model provenance has no successful hey_serena runtime validation"
        )
    return sha256_file(path)


@contextlib.contextmanager
def _collector_lock(db_path: Path) -> Iterator[None]:
    """Keep one microphone scorer attached to a calibration database."""

    try:
        import fcntl
    except ImportError as exc:
        raise WakeWordConfigurationError(
            "wake-word collection currently requires a POSIX desk host"
        ) from exc
    lock_path = db_path.with_suffix(db_path.suffix + ".collector.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WakeWordConfigurationError(
                f"another wake-word collector already owns {db_path}"
            ) from exc
        descriptor.seek(0)
        descriptor.truncate()
        descriptor.write(str(os.getpid()) + "\n")
        descriptor.flush()
        with contextlib.suppress(OSError):
            lock_path.chmod(0o600)
        yield
    finally:
        try:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)
        finally:
            descriptor.close()


def command_doctor(args: argparse.Namespace) -> int:
    result: dict[str, Any] = {
        "ok": False,
        "model": str(_path(args.model)),
        "verifier": str(_path(args.verifier)) if args.verifier else None,
        "openwakeword_version": _openwakeword_version(),
        "sample_rate": SAMPLE_RATE,
        "frame_samples": FRAME_SAMPLES,
        "privacy": "no audio persistence",
    }
    try:
        scorer = OpenWakeWordScorer(_spec(args))
        _warm_scorer(scorer)
        result["score_label"] = scorer.score_label
        result["model_sha256"] = sha256_file(scorer.spec.model_path)
        if scorer.spec.verifier_path is not None:
            result["verifier_sha256"] = sha256_file(scorer.spec.verifier_path)
        try:
            import sounddevice as sd

            device = _device(args.device)
            sd.check_input_settings(
                device=device,
                channels=1,
                dtype="int16",
                samplerate=SAMPLE_RATE,
            )
            result["device"] = str(device if device is not None else sd.default.device[0])
        except (ImportError, OSError, ValueError) as exc:
            raise WakeWordConfigurationError(f"microphone path is unavailable: {exc}") from exc
        result["ok"] = True
    except WakeWordConfigurationError as exc:
        result["error"] = str(exc)
    _json(result)
    return 0 if result["ok"] else 2


def command_freeze(args: argparse.Namespace) -> int:
    """Freeze one fully identified runtime before passive field observation."""

    try:
        import sounddevice as sd
    except ImportError as exc:
        raise WakeWordConfigurationError("sounddevice is not installed") from exc
    scorer = OpenWakeWordScorer(_spec(args))
    _warm_scorer(scorer)
    selector, device_identity = _resolve_device_identity(sd, args.device)
    sd.check_input_settings(
        device=selector,
        channels=1,
        dtype="int16",
        samplerate=SAMPLE_RATE,
    )
    provenance_path = _path(args.model_provenance)
    model_hash = sha256_file(scorer.spec.model_path)
    provenance_sha256 = _validated_model_provenance(
        provenance_path,
        model=scorer.spec.model_path,
        model_hash=model_hash,
        attestation_key=_path(args.provenance_key),
    )
    manifest = build_acceptance_manifest(
        model_path=scorer.spec.model_path,
        model_sha256=model_hash,
        score_label=scorer.score_label or scorer.spec.model_name,
        verifier_path=scorer.spec.verifier_path,
        verifier_sha256=sha256_file(scorer.spec.verifier_path)
        if scorer.spec.verifier_path
        else None,
        verifier_threshold=scorer.spec.verifier_threshold,
        vad_threshold=scorer.spec.vad_threshold,
        speex_noise_suppression=scorer.spec.enable_speex_noise_suppression,
        threshold=args.threshold,
        patience_frames=args.patience_frames,
        cooldown_seconds=args.cooldown_seconds,
        device=device_identity,
        device_selector=str(
            selector if selector is not None else sd.default.device[0]
        ),
        package_version=_openwakeword_version(),
        model_provenance_sha256=provenance_sha256,
    )
    output = _path(args.output)
    write_report_atomic(manifest, output)
    _json(
        {
            "ok": True,
            "event": "acceptance.frozen",
            "output": str(output),
            "configuration_sha256": manifest["configuration_sha256"],
            "instruction": "use a fresh database for passive week-long observation",
        }
    )
    return 0


def command_devices(_: argparse.Namespace) -> int:
    try:
        import sounddevice as sd
    except ImportError:
        _json({"ok": False, "error": "sounddevice is not installed"})
        return 2
    devices = []
    for index, info in enumerate(sd.query_devices()):
        if int(info["max_input_channels"]) > 0:
            devices.append(
                {
                    "index": index,
                    "name": str(info["name"]),
                    "inputs": int(info["max_input_channels"]),
                    "default_sample_rate": float(info["default_samplerate"]),
                }
            )
    _json({"ok": True, "devices": devices, "default": sd.default.device[0]})
    return 0


def command_collect(args: argparse.Namespace) -> int:
    try:
        import sounddevice as sd
    except ImportError:
        _json({"ok": False, "error": "sounddevice is not installed"})
        return 2

    manifest = _acceptance_manifest(args)
    try:
        if manifest is not None:
            _verify_manifest_files(manifest)
        scorer = OpenWakeWordScorer(_spec(args, manifest))
        _warm_scorer(scorer)
    except WakeWordConfigurationError as exc:
        _json({"ok": False, "error": str(exc)})
        return 2

    packet_queue: queue.Queue[tuple[bytes, int, int]] = queue.Queue(
        maxsize=_QUEUE_FRAMES
    )
    stop = threading.Event()
    dropped = [0]
    status_messages: queue.SimpleQueue[str] = queue.SimpleQueue()

    def callback(indata: Any, frames: int, _: Any, status: Any) -> None:
        if status:
            status_messages.put(str(status))
            if bool(getattr(status, "input_overflow", False)):
                dropped[0] += 1
        if frames != FRAME_SAMPLES:
            dropped[0] += 1
            status_messages.put(
                f"unexpected frame size {frames}; expected {FRAME_SAMPLES}"
            )
            return
        packet = (bytes(indata), time.time_ns(), time.monotonic_ns())
        try:
            packet_queue.put_nowait(packet)
        except queue.Full:
            dropped[0] += 1

    def stop_signal(_signum: int, _frame: Any) -> None:
        stop.set()

    previous_handlers: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, stop_signal)

    spec = scorer.spec
    threshold = float(manifest["threshold"]) if manifest else args.threshold
    patience_frames = (
        int(manifest["patience_frames"]) if manifest else args.patience_frames
    )
    cooldown_seconds = (
        float(manifest["cooldown_seconds"]) if manifest else args.cooldown_seconds
    )
    gate = WakeGate(
        threshold,
        patience_frames=patience_frames,
        cooldown_seconds=cooldown_seconds,
    )
    selector_raw = str(manifest["device_selector"]) if manifest else args.device
    device, device_identity = _resolve_device_identity(sd, selector_raw)
    if manifest is not None and device_identity != str(manifest["device"]):
        _json(
            {
                "ok": False,
                "error": "microphone identity changed after acceptance freeze",
                "expected": manifest["device"],
                "actual": device_identity,
            }
        )
        return 2
    metadata = SessionMetadata(
        model_path=str(spec.model_path),
        model_sha256=sha256_file(spec.model_path),
        verifier_path=str(spec.verifier_path) if spec.verifier_path else None,
        verifier_sha256=sha256_file(spec.verifier_path) if spec.verifier_path else None,
        score_label=scorer.score_label or spec.model_name,
        verifier_threshold=spec.verifier_threshold,
        vad_threshold=spec.vad_threshold,
        speex_noise_suppression=spec.enable_speex_noise_suppression,
        patience_frames=patience_frames,
        cooldown_seconds=cooldown_seconds,
        phase="acceptance" if manifest else "development",
        configuration_sha256=str(manifest["configuration_sha256"])
        if manifest
        else None,
        threshold=threshold,
        device=device_identity,
        environment=args.environment,
        package_version=_openwakeword_version(),
        notes=args.notes,
    )

    session_pk: int | None = None
    session_id: str | None = None
    batch: list[tuple[int, int, int, float, float]] = []
    frame_index = 0
    started = time.monotonic()
    final_status = "complete"
    db_path = _path(args.db)

    try:
        with _collector_lock(db_path), CalibrationStore(db_path) as store:
            abandoned = store.abandon_running_sessions()
            session_pk, session_id = store.begin_session(metadata)
            _json(
                {
                    "event": "collector.started",
                    "session_id": session_id,
                    "db": str(db_path),
                    "model_sha256": metadata.model_sha256,
                    "score_label": metadata.score_label,
                    "threshold": threshold,
                    "phase": metadata.phase,
                    "configuration_sha256": metadata.configuration_sha256,
                    "environment": args.environment,
                    "raw_audio_stored": False,
                    "abandoned_sessions_recovered": abandoned,
                }
            )
            with sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=FRAME_SAMPLES,
                device=device,
                channels=1,
                dtype="int16",
                callback=callback,
            ):
                while not stop.is_set():
                    if args.duration > 0 and time.monotonic() - started >= args.duration:
                        break
                    try:
                        raw, wall_ns, mono_ns = packet_queue.get(timeout=0.5)
                    except queue.Empty:
                        while not status_messages.empty():
                            _json(
                                {
                                    "event": "collector.audio_status",
                                    "status": status_messages.get(),
                                }
                            )
                        continue

                    frame = np.frombuffer(raw, dtype=np.int16)
                    if frame.shape[0] != FRAME_SAMPLES:
                        dropped[0] += 1
                        continue
                    score = scorer.score_frame(frame)
                    level = rms_dbfs(frame)
                    batch.append((frame_index, wall_ns, mono_ns, score, level))
                    frame_index += 1
                    if gate.observe(score, monotonic_ns=mono_ns):
                        intent = store.intent_at(wall_ns)
                        classification = "intended" if intent is not None else "background"
                        detection_id = store.add_detection(
                            session_pk,
                            wall_ns=wall_ns,
                            mono_ns=mono_ns,
                            score=score,
                            threshold=threshold,
                            classification=classification,
                            environment=args.environment,
                        )
                        _json(
                            {
                                "event": "wake.detected",
                                "detection_id": detection_id,
                                "session_id": session_id,
                                "score": round(score, 6),
                                "threshold": threshold,
                                "classification": classification,
                                "intent_id": str(intent["intent_id"])
                                if intent is not None
                                else None,
                            }
                        )
                    if len(batch) >= _WRITE_BATCH:
                        store.record_scores(session_pk, batch)
                        batch.clear()

                    while not status_messages.empty():
                        _json(
                            {
                                "event": "collector.audio_status",
                                "status": status_messages.get(),
                            }
                        )
            if batch:
                store.record_scores(session_pk, batch)
                batch.clear()
            if stop.is_set():
                final_status = "interrupted"
            store.finish_session(
                session_pk,
                dropped_frames=dropped[0],
                status=final_status,
            )
    except Exception as exc:
        final_status = "error"
        if session_pk is not None:
            try:
                with CalibrationStore(db_path) as recovery:
                    if batch:
                        recovery.record_scores(session_pk, batch)
                    recovery.finish_session(
                        session_pk,
                        dropped_frames=dropped[0],
                        status=final_status,
                    )
            except Exception:
                pass
        _json(
            {
                "event": "collector.error",
                "session_id": session_id,
                "error": str(exc),
            }
        )
        return 1
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)

    _json(
        {
            "event": "collector.stopped",
            "session_id": session_id,
            "status": final_status,
            "frames": frame_index,
            "dropped_frames": dropped[0],
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    )
    return 0


def command_attempt(args: argparse.Namespace) -> int:
    db_path = _path(args.db)
    manifest = _acceptance_manifest(args)
    with CalibrationStore(db_path) as store:
        latest = store.connection.execute(
            "SELECT MAX(wall_ns) FROM scores"
        ).fetchone()[0]
        age_seconds = None if latest is None else (time.time_ns() - int(latest)) / 1e9
        if not args.allow_offline and (age_seconds is None or age_seconds > 2.0):
            _json(
                {
                    "ok": False,
                    "error": "wake-word collector is not receiving live microphone frames",
                    "latest_frame_age_seconds": round(age_seconds, 3)
                    if age_seconds is not None
                    else None,
                }
            )
            return 2
        intent_id = store.add_intent(
            window_seconds=args.window,
            lead_seconds=args.lead,
            environment=args.environment,
            speaker=args.speaker,
            distance=args.distance,
            phrase_variant=args.phrase_variant,
            configuration_sha256=str(manifest["configuration_sha256"])
            if manifest
            else None,
            note=args.note,
        )
    _json(
        {
            "ok": True,
            "event": "intent.opened",
            "intent_id": intent_id,
            "lead_seconds": args.lead,
            "window_seconds": args.window,
            "environment": args.environment,
            "instruction": (
                "say hey serena once now, then confirm this intent as valid or invalid"
            ),
        }
    )
    return 0


def command_confirm(args: argparse.Namespace) -> int:
    with CalibrationStore(_path(args.db)) as store:
        store.confirm_intent(
            args.intent_id,
            valid=args.status == "valid",
            note=args.note,
        )
    _json(
        {
            "ok": True,
            "event": "intent.confirmed",
            "intent_id": args.intent_id,
            "status": args.status,
        }
    )
    return 0


def command_report(args: argparse.Namespace) -> int:
    manifest = _acceptance_manifest(args)
    thresholds = (
        [float(manifest["threshold"])]
        if manifest
        else threshold_grid(
            args.threshold_start,
            args.threshold_stop,
            args.threshold_step,
        )
    )
    report = analyze_calibration(
        _path(args.db),
        thresholds=thresholds,
        patience_frames=int(manifest["patience_frames"])
        if manifest
        else args.patience_frames,
        cooldown_seconds=float(manifest["cooldown_seconds"])
        if manifest
        else args.cooldown_seconds,
        acceptance_manifest=manifest,
    )
    output = _path(args.output)
    write_report_atomic(report, output)
    recommendation = report["recommendation"]
    _json(
        {
            "ok": True,
            "event": "report.written",
            "output": str(output),
            "coverage": report["coverage"],
            "recommendation": recommendation,
            "acceptance_claim": report["acceptance_claim"],
        }
    )
    if args.require_pass and not report["acceptance_claim"]:
        return 3
    return 0


def command_status(args: argparse.Namespace) -> int:
    db_path = _path(args.db)
    with CalibrationStore(db_path) as store:
        session = store.connection.execute(
            "SELECT * FROM sessions ORDER BY started_wall_ns DESC LIMIT 1"
        ).fetchone()
        score_summary = store.connection.execute(
            "SELECT COUNT(*) AS frames, MAX(wall_ns) AS latest FROM scores"
        ).fetchone()
        attempts = int(store.connection.execute("SELECT COUNT(*) FROM intents").fetchone()[0])
        detections = store.connection.execute(
            "SELECT classification, COUNT(*) AS count FROM detections GROUP BY classification"
        ).fetchall()
    latest_age = (
        None
        if score_summary["latest"] is None
        else (time.time_ns() - int(score_summary["latest"])) / 1e9
    )
    _json(
        {
            "ok": True,
            "db": str(db_path),
            "frames": int(score_summary["frames"]),
            "audio_hours": round(
                int(score_summary["frames"]) * FRAME_SAMPLES / SAMPLE_RATE / 3600,
                6,
            ),
            "latest_frame_age_seconds": round(latest_age, 3)
            if latest_age is not None
            else None,
            "collector_live": latest_age is not None and latest_age <= 2.0,
            "attempts": attempts,
            "detections": {str(row["classification"]): int(row["count"]) for row in detections},
            "latest_session": dict(session) if session is not None else None,
        }
    )
    return 0


def _add_model_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--score-label")
    parser.add_argument("--verifier")
    parser.add_argument("--verifier-threshold", type=float, default=0.1)
    parser.add_argument("--oww-vad-threshold", type=float, default=0.0)
    parser.add_argument("--speex", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="validate model, runtime, and microphone")
    _add_model_arguments(doctor)
    doctor.add_argument("--device")
    doctor.set_defaults(function=command_doctor)

    freeze = subparsers.add_parser(
        "freeze",
        help="freeze one immutable configuration before passive field observation",
    )
    _add_model_arguments(freeze)
    freeze.add_argument("--device")
    freeze.add_argument("--threshold", type=float, required=True)
    freeze.add_argument("--patience-frames", type=int, default=1)
    freeze.add_argument("--cooldown-seconds", type=float, default=3.0)
    freeze.add_argument("--model-provenance", required=True)
    freeze.add_argument(
        "--provenance-key", default=str(DEFAULT_PROVENANCE_ATTESTATION_KEY)
    )
    freeze.add_argument("--output", default=str(DEFAULT_ACCEPTANCE_MANIFEST))
    freeze.set_defaults(function=command_freeze)

    devices = subparsers.add_parser("devices", help="list available microphones")
    devices.set_defaults(function=command_devices)

    collect = subparsers.add_parser("collect", help="collect continuous score telemetry")
    _add_model_arguments(collect)
    collect.add_argument("--db", default=str(DEFAULT_DB))
    collect.add_argument("--manifest")
    collect.add_argument("--device")
    collect.add_argument("--environment", default="normal-desk")
    collect.add_argument("--notes")
    collect.add_argument("--threshold", type=float, default=0.5)
    collect.add_argument("--patience-frames", type=int, default=1)
    collect.add_argument("--cooldown-seconds", type=float, default=3.0)
    collect.add_argument("--duration", type=float, default=0.0)
    collect.set_defaults(function=command_collect)

    attempt = subparsers.add_parser("attempt", help="open a labeled intentional-wake window")
    attempt.add_argument("--db", default=str(DEFAULT_DB))
    attempt.add_argument("--manifest")
    attempt.add_argument("--environment", default="quiet-near")
    attempt.add_argument("--speaker", default="raghav")
    attempt.add_argument("--distance", default="unspecified")
    attempt.add_argument("--phrase-variant", default="hey serena")
    attempt.add_argument("--note")
    attempt.add_argument("--lead", type=float, default=0.25)
    attempt.add_argument("--window", type=float, default=5.0)
    attempt.add_argument("--allow-offline", action="store_true", help=argparse.SUPPRESS)
    attempt.set_defaults(function=command_attempt)

    confirm = subparsers.add_parser(
        "confirm",
        help="confirm whether one labeled attempt was actually spoken",
    )
    confirm.add_argument("intent_id")
    confirm.add_argument("--db", default=str(DEFAULT_DB))
    confirm.add_argument("--status", choices=("valid", "invalid"), required=True)
    confirm.add_argument("--note")
    confirm.set_defaults(function=command_confirm)

    report = subparsers.add_parser("report", help="score every candidate threshold")
    report.add_argument("--db", default=str(DEFAULT_DB))
    report.add_argument("--manifest")
    report.add_argument("--output", default=str(DEFAULT_REPORT))
    report.add_argument("--threshold-start", type=float, default=0.30)
    report.add_argument("--threshold-stop", type=float, default=0.90)
    report.add_argument("--threshold-step", type=float, default=0.05)
    report.add_argument("--patience-frames", type=int, default=1)
    report.add_argument("--cooldown-seconds", type=float, default=3.0)
    report.add_argument("--require-pass", action="store_true")
    report.set_defaults(function=command_report)

    status = subparsers.add_parser("status", help="show collection progress")
    status.add_argument("--db", default=str(DEFAULT_DB))
    status.set_defaults(function=command_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.function(args))
    except (RuntimeError, ValueError, WakeWordConfigurationError) as exc:
        _json({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
