from __future__ import annotations

import struct

import pytest

from voice.call.protocol import (
    FLAG_FINAL,
    HEADER_SIZE,
    MAGIC,
    MAX_TTS_FRAME_MS,
    MIC_FRAME_BYTES,
    AudioFrame,
    AudioHeader,
    AudioKind,
    ProtocolError,
    SequenceTracker,
    parse_audio_frame,
    parse_control,
)


def test_header_is_exactly_24_bytes_and_big_endian() -> None:
    header = AudioHeader(
        kind=AudioKind.MIC_PCM16,
        flags=FLAG_FINAL,
        sequence=0x01020304,
        sample_rate=16_000,
        timestamp_us=0x0102030405060708,
    )
    encoded = header.pack()
    assert len(encoded) == HEADER_SIZE == 24
    assert encoded == struct.pack(
        "!IBBHIIQ",
        MAGIC,
        1,
        1,
        FLAG_FINAL,
        0x01020304,
        16_000,
        0x0102030405060708,
    )


def test_exact_mic_frame_round_trip() -> None:
    frame = AudioFrame(
        AudioHeader(AudioKind.MIC_PCM16, 0, 7, 16_000, 123),
        b"\x01\x00" * 3_200,
    )
    decoded = parse_audio_frame(frame.pack())
    assert decoded == frame
    assert len(decoded.pcm) == MIC_FRAME_BYTES


def test_unsupported_tts_rate_is_rejected() -> None:
    frame = AudioFrame(AudioHeader(AudioKind.TTS_PCM16, 0, 0, 12_345, 0), b"\x00\x00")
    with pytest.raises(ProtocolError, match="TTS sample rate"):
        parse_audio_frame(frame.header.pack() + frame.pcm, inbound=False)


def test_oversized_tts_frame_is_rejected() -> None:
    sample_rate = 16_000
    max_payload = sample_rate * 2 * MAX_TTS_FRAME_MS // 1000
    frame = AudioFrame(
        AudioHeader(AudioKind.TTS_PCM16, 0, 0, sample_rate, 0),
        b"\x00\x00" * (max_payload // 2 + 1),
    )
    with pytest.raises(ProtocolError, match="frame limit"):
        parse_audio_frame(frame.header.pack() + frame.pcm, inbound=False)


@pytest.mark.parametrize(
    "mutator,match",
    [
        (lambda frame: b"bad!" + frame[4:], "bad magic"),
        (lambda frame: frame[:4] + b"\x02" + frame[5:], "version"),
        (lambda frame: frame[:5] + b"\x09" + frame[6:], "audio kind"),
        (lambda frame: frame[:6] + b"\x80\x00" + frame[8:], "flags"),
        (
            lambda frame: frame[:12] + struct.pack("!I", 8_000) + frame[16:],
            "sample rate",
        ),
        (lambda frame: frame[:-2], "mic frame"),
    ],
)
def test_malformed_binary_is_rejected(mutator, match: str) -> None:
    good = AudioFrame(
        AudioHeader(AudioKind.MIC_PCM16, 0, 0, 16_000, 0),
        b"\x00\x00" * 3_200,
    ).pack()
    with pytest.raises(ProtocolError, match=match):
        parse_audio_frame(mutator(good))


def test_strict_json_controls() -> None:
    start = parse_control(
        '{"type":"call.start","call_id":"call-1","generation":0,"greeting":true,'
        '"job_cursor":42,"continuity":false}'
    )
    assert start["greeting"] is True
    assert start["job_cursor"] == 42
    assert start["continuity"] is False
    assert parse_control('{"type":"job.ack","event_seq":42}')["event_seq"] == 42
    assert (
        parse_control(
            '{"type":"artifact.opened","event_seq":42,"job_id":"job-1",'
            '"receipt":"v1.payload.signature"}'
        )["job_id"]
        == "job-1"
    )
    assert parse_control('{"type":"ptt.begin","generation":3}')["generation"] == 3
    assert (
        parse_control('{"type":"ptt.end","generation":3,"eou_monotonic_us":123456}')[
            "eou_monotonic_us"
        ]
        == 123456
    )
    playback = parse_control(
        '{"type":"playback.started","generation":3,"sequence":0,'
        '"timestamp_us":124000,"eou_to_playback_ms":1430.25,'
        '"play_return_to_head_ms":5.0,'
        '"measurement_point":"playback_head_advanced"}'
    )
    assert playback["eou_to_playback_ms"] == 1430.25
    content_playback = parse_control(
        '{"type":"playback.segment_started","generation":3,'
        '"sequence":4,"kind":"content","timestamp_us":125000,'
        '"eou_to_playback_ms":1810.5,'
        '"measurement_point":"playback_head_advanced",'
        '"call_hello":true,"cold_start":true,"app_uptime_ms":3900}'
    )
    assert content_playback["sequence"] == 4
    assert content_playback["kind"] == "content"
    assert content_playback["cold_start"] is True
    assert content_playback["app_uptime_ms"] == 3900
    assert (
        parse_control('{"type":"sequence.gap","generation":3,"expected":4,"received":7}')["type"]
        == "sequence.gap"
    )
    rtt = parse_control(
        '{"type":"rtt.report","rtt_ms":42.5,"path":"direct",'
        '"path_source":"client_config","sample_id":"ping-1"}'
    )
    assert rtt["sample_id"] == "ping-1"
    pong = parse_control(
        '{"type":"pong","nonce":"ping-2","sent_at_us":42,'
        '"sample_id":"server-sample-2","path":"relay",'
        '"path_source":"tailscale_probe",'
        '"server_processing_us":950}'
    )
    assert pong["path"] == "relay"
    assert pong["sample_id"] == "server-sample-2"
    assert pong["server_processing_us"] == 950
    long_uptime_ping = parse_control(
        '{"type":"ping","nonce":"long-uptime","sent_at_us":34892520726}'
    )
    assert long_uptime_ping["sent_at_us"] == 34_892_520_726
    with pytest.raises(ProtocolError, match="server sample_id"):
        parse_control(
            '{"type":"pong","nonce":"ping-2","path":"direct","path_source":"tailscale_probe"}'
        )
    with pytest.raises(ProtocolError, match="valid sample_id"):
        parse_control(
            '{"type":"rtt.report","rtt_ms":42.5,"path":"direct","path_source":"client_config"}'
        )
    with pytest.raises(ProtocolError, match="unsupported fields"):
        parse_control(
            '{"type":"ping","nonce":"ping-2","path":"direct","path_source":"tailscale_probe"}'
        )
    with pytest.raises(ProtocolError, match="unsupported fields"):
        parse_control('{"type":"ptt.begin","generation":3,"extra":true}')
    with pytest.raises(ProtocolError, match="greeting must be a boolean"):
        parse_control('{"type":"call.start","call_id":"call-1","greeting":"yes"}')
    with pytest.raises(ProtocolError, match="continuity must be a boolean"):
        parse_control('{"type":"call.start","call_id":"call-1","continuity":"no"}')
    with pytest.raises(ProtocolError, match="out of range"):
        parse_control('{"type":"job.ack","event_seq":0}')
    with pytest.raises(ProtocolError, match="valid job_id"):
        parse_control(
            '{"type":"artifact.opened","event_seq":1,"job_id":"","receipt":"v1.payload.signature"}'
        )
    with pytest.raises(ProtocolError, match="valid receipt"):
        parse_control('{"type":"artifact.opened","event_seq":1,"job_id":"job-1","receipt":""}')
    with pytest.raises(ProtocolError, match="unsupported fields"):
        parse_control('{"type":"ptt.begin","generation":3,"eou_monotonic_us":1}')
    with pytest.raises(ProtocolError, match="measurement point"):
        parse_control('{"type":"playback.started","generation":3,"measurement_point":"wall_clock"}')
    with pytest.raises(ProtocolError, match="requires call_hello"):
        parse_control(
            '{"type":"playback.segment_started","generation":3,'
            '"sequence":4,"kind":"content",'
            '"measurement_point":"playback_head_advanced",'
            '"cold_start":true,"app_uptime_ms":3900}'
        )
    with pytest.raises(ProtocolError, match="requires app_uptime_ms"):
        parse_control(
            '{"type":"playback.segment_started","generation":3,'
            '"sequence":4,"kind":"content",'
            '"measurement_point":"playback_head_advanced",'
            '"call_hello":true,"cold_start":true}'
        )
    with pytest.raises(ProtocolError, match="unsupported control"):
        parse_control('{"type":"listen.forever"}')
    with pytest.raises(ProtocolError, match="text frames"):
        parse_control(b'{"type":"hangup"}')


def test_sequence_tracker_reports_forward_and_reordered_gaps() -> None:
    tracker = SequenceTracker()
    assert tracker.observe(0) is None
    assert tracker.observe(1) is None
    gap = tracker.observe(4)
    assert gap is not None
    assert (gap.expected, gap.received, gap.missing) == (2, 4, 2)
    reordered = tracker.observe(3)
    assert reordered is not None
    assert (reordered.expected, reordered.received, reordered.missing, reordered.stale) == (
        5,
        3,
        0,
        True,
    )
    assert tracker.expected == 5
    tracker.reset()
    assert tracker.observe(0) is None


def test_sequence_gap_missing_count_handles_uint32_wrap() -> None:
    tracker = SequenceTracker()
    tracker.observe(0xFFFFFFFE)
    gap = tracker.observe(1)
    assert gap is not None
    assert gap.expected == 0xFFFFFFFF
    assert gap.missing == 2
