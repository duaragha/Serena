"""Binary audio and JSON control protocol for Serena calls.

Only the 24-byte header is network byte order. PCM16 payloads are signed,
little-endian samples because that is the native Android AudioRecord format.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

MAGIC = 0x53524341  # SRCA
VERSION = 1
HEADER_SIZE = 24
MIC_SAMPLE_RATE = 16_000
MIC_FRAME_SAMPLES = 3_200
MIC_FRAME_BYTES = MIC_FRAME_SAMPLES * 2
TTS_SAMPLE_RATES = frozenset({16_000, 22_050, 24_000, 44_100, 48_000})
# Keep the phone-side work queue bounded in audio time, not merely item count.
# Every backend must re-chunk PCM before it reaches the transport.
MAX_TTS_FRAME_MS = 50
FLAG_FINAL = 0x0001
SUPPORTED_FLAGS = FLAG_FINAL
_HEADER = struct.Struct("!IBBHIIQ")


class ProtocolError(ValueError):
    """An input frame or control message violated the call contract."""


class AudioKind(IntEnum):
    MIC_PCM16 = 1
    TTS_PCM16 = 2


@dataclass(frozen=True, slots=True)
class AudioHeader:
    kind: AudioKind
    flags: int
    sequence: int
    sample_rate: int
    timestamp_us: int
    version: int = VERSION

    @property
    def final(self) -> bool:
        return bool(self.flags & FLAG_FINAL)

    def pack(self) -> bytes:
        _validate_uint("flags", self.flags, 16)
        _validate_uint("sequence", self.sequence, 32)
        _validate_uint("sample_rate", self.sample_rate, 32)
        _validate_uint("timestamp_us", self.timestamp_us, 64)
        if self.version != VERSION:
            raise ProtocolError(f"unsupported version {self.version}")
        if self.flags & ~SUPPORTED_FLAGS:
            raise ProtocolError(f"unsupported flags 0x{self.flags:04x}")
        if not isinstance(self.kind, AudioKind):
            raise ProtocolError(f"unsupported audio kind {self.kind!r}")
        return _HEADER.pack(
            MAGIC,
            self.version,
            int(self.kind),
            self.flags,
            self.sequence,
            self.sample_rate,
            self.timestamp_us,
        )

    @classmethod
    def unpack(cls, data: bytes | bytearray | memoryview) -> AudioHeader:
        if len(data) < HEADER_SIZE:
            raise ProtocolError(f"binary frame shorter than {HEADER_SIZE}-byte header")
        magic, version, raw_kind, flags, sequence, sample_rate, timestamp_us = _HEADER.unpack_from(
            data
        )
        if magic != MAGIC:
            raise ProtocolError(f"bad magic 0x{magic:08x}")
        if version != VERSION:
            raise ProtocolError(f"unsupported version {version}")
        try:
            kind = AudioKind(raw_kind)
        except ValueError as exc:
            raise ProtocolError(f"unsupported audio kind {raw_kind}") from exc
        if flags & ~SUPPORTED_FLAGS:
            raise ProtocolError(f"unsupported flags 0x{flags:04x}")
        if sample_rate <= 0:
            raise ProtocolError("sample rate must be positive")
        return cls(
            kind=kind,
            flags=flags,
            sequence=sequence,
            sample_rate=sample_rate,
            timestamp_us=timestamp_us,
            version=version,
        )


@dataclass(frozen=True, slots=True)
class AudioFrame:
    header: AudioHeader
    pcm: bytes

    def pack(self) -> bytes:
        validate_audio_frame(self, inbound=self.header.kind is AudioKind.MIC_PCM16)
        return self.header.pack() + self.pcm


@dataclass(frozen=True, slots=True)
class SequenceGap:
    expected: int
    received: int
    stale: bool = False

    @property
    def missing(self) -> int:
        if self.stale:
            return 0
        return (self.received - self.expected) & 0xFFFFFFFF


class SequenceTracker:
    """Tracks monotonic uint32 sequences within one JSON generation."""

    def __init__(self) -> None:
        self._next: int | None = None

    @property
    def expected(self) -> int | None:
        return self._next

    def reset(self) -> None:
        self._next = None

    def observe(self, sequence: int) -> SequenceGap | None:
        _validate_uint("sequence", sequence, 32)
        if self._next is None:
            self._next = (sequence + 1) & 0xFFFFFFFF
            return SequenceGap(0, sequence) if sequence != 0 else None
        if sequence == self._next:
            self._next = (sequence + 1) & 0xFFFFFFFF
            return None
        distance = (sequence - self._next) & 0xFFFFFFFF
        if 0 < distance < 0x80000000:
            gap = SequenceGap(self._next, sequence)
            self._next = (sequence + 1) & 0xFFFFFFFF
            return gap
        return SequenceGap(self._next, sequence, stale=True)


def parse_audio_frame(data: bytes | bytearray | memoryview, *, inbound: bool = True) -> AudioFrame:
    view = memoryview(data)
    header = AudioHeader.unpack(view)
    frame = AudioFrame(header=header, pcm=bytes(view[HEADER_SIZE:]))
    validate_audio_frame(frame, inbound=inbound)
    return frame


def validate_audio_frame(frame: AudioFrame, *, inbound: bool) -> None:
    header = frame.header
    if len(frame.pcm) % 2:
        raise ProtocolError("PCM16 payload must contain whole samples")
    if inbound:
        if header.kind is not AudioKind.MIC_PCM16:
            raise ProtocolError("client binary frames must be mic PCM16")
        if header.sample_rate != MIC_SAMPLE_RATE:
            raise ProtocolError(
                f"mic sample rate must be {MIC_SAMPLE_RATE}, got {header.sample_rate}"
            )
        if len(frame.pcm) != MIC_FRAME_BYTES:
            raise ProtocolError(
                f"mic frame must contain {MIC_FRAME_BYTES} PCM bytes, got {len(frame.pcm)}"
            )
    else:
        if header.kind is not AudioKind.TTS_PCM16:
            raise ProtocolError("server binary frames must be TTS PCM16")
        if header.sample_rate not in TTS_SAMPLE_RATES:
            raise ProtocolError(f"unsupported TTS sample rate {header.sample_rate}")
        if not frame.pcm:
            raise ProtocolError("TTS PCM16 payload cannot be empty")
        max_payload = header.sample_rate * 2 * MAX_TTS_FRAME_MS // 1000
        if len(frame.pcm) > max_payload:
            raise ProtocolError(f"TTS PCM16 payload exceeds {MAX_TTS_FRAME_MS} ms frame limit")


def parse_control(raw: str | bytes | bytearray) -> dict[str, Any]:
    if isinstance(raw, (bytes, bytearray)):
        raise ProtocolError("control messages must be text frames")
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ProtocolError("invalid JSON control message") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("control message must be a JSON object")
    kind = obj.get("type")
    if not isinstance(kind, str) or not kind:
        raise ProtocolError("control message requires a string type")
    validators = {
        "call.start": _validate_call_start,
        "ptt.begin": _validate_generation_control,
        "ptt.end": _validate_ptt_end,
        "cancel": _validate_optional_generation,
        "hangup": _validate_empty_control,
        "ping": _validate_ping,
        "pong": _validate_ping,
        "rtt.report": _validate_rtt,
        "playback.started": _validate_playback_started,
        "playback.segment_started": _validate_playback_segment_started,
        "playback.underrun": _validate_playback_underrun,
        "sequence.gap": _validate_sequence_gap,
        "job.ack": _validate_job_ack,
        "artifact.opened": _validate_artifact_opened,
    }
    validator = validators.get(kind)
    if validator is None:
        raise ProtocolError(f"unsupported control type {kind!r}")
    validator(obj)
    return obj


def encode_control(control_type: str, **fields: Any) -> str:
    return json.dumps({"type": control_type, **fields}, separators=(",", ":"))


def _validate_call_start(obj: dict[str, Any]) -> None:
    _only_keys(
        obj,
        "type",
        "call_id",
        "generation",
        "greeting",
        "job_cursor",
        "continuity",
    )
    call_id = obj.get("call_id")
    if not isinstance(call_id, str) or not call_id.strip() or len(call_id) > 128:
        raise ProtocolError("call.start requires a non-empty call_id")
    if "generation" in obj:
        _positive_int(obj, "generation", allow_zero=True)
    if "greeting" in obj and not isinstance(obj["greeting"], bool):
        raise ProtocolError("call.start greeting must be a boolean")
    if "continuity" in obj and not isinstance(obj["continuity"], bool):
        raise ProtocolError("call.start continuity must be a boolean")
    if "job_cursor" in obj:
        _positive_int(obj, "job_cursor", allow_zero=True, bits=64)


def _validate_job_ack(obj: dict[str, Any]) -> None:
    _only_keys(obj, "type", "event_seq")
    _positive_int(obj, "event_seq", bits=64)


def _validate_artifact_opened(obj: dict[str, Any]) -> None:
    _only_keys(obj, "type", "event_seq", "job_id", "receipt")
    _positive_int(obj, "event_seq", bits=64)
    job_id = obj.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 128:
        raise ProtocolError("artifact.opened requires a valid job_id")
    receipt = obj.get("receipt")
    if not isinstance(receipt, str) or not receipt.strip() or len(receipt) > 2_048:
        raise ProtocolError("artifact.opened requires a valid receipt")


def _validate_generation_control(obj: dict[str, Any]) -> None:
    _only_keys(obj, "type", "generation")
    _positive_int(obj, "generation")


def _validate_ptt_end(obj: dict[str, Any]) -> None:
    _only_keys(obj, "type", "generation", "eou_monotonic_us")
    _positive_int(obj, "generation")
    if "eou_monotonic_us" in obj:
        _positive_int(obj, "eou_monotonic_us", allow_zero=True, bits=64)


def _validate_optional_generation(obj: dict[str, Any]) -> None:
    _only_keys(obj, "type", "generation")
    if "generation" in obj:
        _positive_int(obj, "generation", allow_zero=True)


def _validate_empty_control(obj: dict[str, Any]) -> None:
    _only_keys(obj, "type")


def _validate_ping(obj: dict[str, Any]) -> None:
    fields = ["type", "nonce", "sent_at_us"]
    if obj.get("type") == "pong":
        fields.extend(("sample_id", "path", "path_source", "server_processing_us"))
    _only_keys(obj, *fields)
    nonce = obj.get("nonce")
    if not isinstance(nonce, (str, int)) or isinstance(nonce, bool):
        raise ProtocolError("ping requires a string or integer nonce")
    if isinstance(nonce, str) and (not nonce or len(nonce) > 128):
        raise ProtocolError("ping nonce is invalid")
    if isinstance(nonce, int) and not 0 <= nonce < 2**64:
        raise ProtocolError("ping nonce is invalid")
    if "sent_at_us" in obj:
        _positive_int(obj, "sent_at_us", allow_zero=True, bits=64)
    if "server_processing_us" in obj:
        _positive_int(obj, "server_processing_us", allow_zero=True, bits=64)
    if obj.get("type") == "pong":
        sample_id = obj.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id or len(sample_id) > 128:
            raise ProtocolError("pong requires a valid server sample_id")
    if obj.get("type") == "pong" and ("path" in obj or "path_source" in obj):
        if obj.get("path") not in {"direct", "relay", "unknown"}:
            raise ProtocolError("pong path must be direct, relay, or unknown")
        if obj.get("path_source") not in {"tailscale_probe", "unknown"}:
            raise ProtocolError("pong path source is invalid")


def _validate_rtt(obj: dict[str, Any]) -> None:
    _only_keys(obj, "type", "rtt_ms", "path", "path_source", "sample_id")
    value = obj.get("rtt_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError("rtt.report requires numeric rtt_ms")
    if not 0 <= float(value) <= 120_000:
        raise ProtocolError("rtt_ms is out of range")
    if obj.get("path") not in {"direct", "relay", "unknown"}:
        raise ProtocolError("rtt path must be direct, relay, or unknown")
    if obj.get("path_source", "unknown") not in {
        "client_config",
        "tailscale_probe",
        "unknown",
    }:
        raise ProtocolError("rtt path source is invalid")
    sample_id = obj.get("sample_id")
    if not isinstance(sample_id, str) or not sample_id or len(sample_id) > 128:
        raise ProtocolError("rtt.report requires a valid sample_id")


def _validate_playback_started(obj: dict[str, Any]) -> None:
    _only_keys(
        obj,
        "type",
        "generation",
        "sequence",
        "timestamp_us",
        "eou_to_playback_ms",
        "first_output_to_playback_ms",
        "first_pcm_write_to_playback_ms",
        "play_return_to_head_ms",
        "measurement_point",
    )
    _positive_int(obj, "generation", allow_zero=True)
    if "sequence" in obj:
        _positive_int(obj, "sequence", allow_zero=True, bits=32)
    if "timestamp_us" in obj:
        _positive_int(obj, "timestamp_us", allow_zero=True, bits=64)
    for key in (
        "eou_to_playback_ms",
        "first_output_to_playback_ms",
        "first_pcm_write_to_playback_ms",
        "play_return_to_head_ms",
    ):
        if key in obj:
            value = obj[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ProtocolError(f"{key} must be numeric")
            if not 0 <= float(value) <= 120_000:
                raise ProtocolError(f"{key} is out of range")
    if "measurement_point" in obj and obj["measurement_point"] not in {
        "audio_track_play_return",
        "playback_head_advanced",
    }:
        raise ProtocolError("unsupported playback measurement point")


def _validate_playback_segment_started(obj: dict[str, Any]) -> None:
    _only_keys(
        obj,
        "type",
        "generation",
        "sequence",
        "kind",
        "timestamp_us",
        "eou_to_playback_ms",
        "measurement_point",
        "call_hello",
        "cold_start",
        "app_uptime_ms",
    )
    _positive_int(obj, "generation", allow_zero=True)
    _positive_int(obj, "sequence", allow_zero=True, bits=32)
    if obj.get("kind") not in {"content", "acknowledgement"}:
        raise ProtocolError("playback segment kind is invalid")
    if "timestamp_us" in obj:
        _positive_int(obj, "timestamp_us", allow_zero=True, bits=64)
    if "eou_to_playback_ms" in obj:
        value = obj["eou_to_playback_ms"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProtocolError("eou_to_playback_ms must be numeric")
        if not 0 <= float(value) <= 120_000:
            raise ProtocolError("eou_to_playback_ms is out of range")
    for key in ("call_hello", "cold_start"):
        if key in obj and not isinstance(obj[key], bool):
            raise ProtocolError(f"{key} must be a boolean")
    if obj.get("cold_start") and not obj.get("call_hello"):
        raise ProtocolError("cold_start requires call_hello")
    if obj.get("call_hello") and "app_uptime_ms" not in obj:
        raise ProtocolError("call_hello requires app_uptime_ms")
    if "app_uptime_ms" in obj:
        _positive_int(obj, "app_uptime_ms", allow_zero=True, bits=64)
    if obj.get("measurement_point") != "playback_head_advanced":
        raise ProtocolError("unsupported playback measurement point")


def _validate_playback_underrun(obj: dict[str, Any]) -> None:
    _only_keys(obj, "type", "generation", "count", "timestamp_us")
    _positive_int(obj, "generation", allow_zero=True)
    if "count" in obj:
        _positive_int(obj, "count", allow_zero=True, bits=32)
    if "timestamp_us" in obj:
        _positive_int(obj, "timestamp_us", allow_zero=True, bits=64)


def _validate_sequence_gap(obj: dict[str, Any]) -> None:
    _only_keys(obj, "type", "generation", "expected", "received", "direction")
    _positive_int(obj, "generation", allow_zero=True)
    _positive_int(obj, "expected", allow_zero=True, bits=32)
    _positive_int(obj, "received", allow_zero=True, bits=32)
    if obj.get("direction", "output") not in {"input", "output"}:
        raise ProtocolError("sequence gap direction must be input or output")


def _positive_int(
    obj: dict[str, Any], key: str, *, allow_zero: bool = False, bits: int = 31
) -> int:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{key} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum or value > (1 << bits) - 1:
        raise ProtocolError(f"{key} is out of range")
    return value


def _validate_uint(name: str, value: int, bits: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{name} must be an integer")
    if value < 0 or value > (1 << bits) - 1:
        raise ProtocolError(f"{name} is out of range")


def _only_keys(obj: dict[str, Any], *allowed: str) -> None:
    extra = set(obj) - set(allowed)
    if extra:
        raise ProtocolError(f"unsupported fields: {', '.join(sorted(extra))}")
