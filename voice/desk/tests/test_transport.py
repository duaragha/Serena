from __future__ import annotations

import json
import sys
import threading
from types import SimpleNamespace

from voice.call.protocol import MIC_FRAME_BYTES, parse_audio_frame
from voice.desk.transport import DeskTransport, MicFramePacker


class _Socket:
    def __init__(self) -> None:
        self.sent: list[tuple[object, object | None]] = []

    def send(self, payload, opcode=None):
        self.sent.append((payload, opcode))


def test_mic_packer_preserves_stream_across_80_to_200_ms_boundary() -> None:
    packer = MicFramePacker()
    source = b"".join(bytes([index]) * 2_560 for index in range(5))
    output: list[bytes] = []

    for index in range(5):
        output.extend(packer.feed(bytes([index]) * 2_560))

    assert len(output) == 2
    assert all(len(frame) == MIC_FRAME_BYTES for frame in output)
    assert b"".join(output) == source
    assert packer.buffered_bytes == 0


def test_transport_sends_valid_binary_mic_frames_and_gap_control() -> None:
    transport = DeskTransport("ws://localhost/ws/desk", "secret")
    socket = _Socket()
    transport._socket = socket
    transport._ready.set()
    transport.generation = 3

    transport.send_mic_frame(b"\x01\x00" * 3_200)
    transport.report_output_gap(4, 6)

    frame = parse_audio_frame(socket.sent[0][0], inbound=True)
    assert frame.header.sequence == 0
    assert frame.header.sample_rate == 16_000
    assert len(frame.pcm) == MIC_FRAME_BYTES
    control = json.loads(socket.sent[1][0])
    assert control == {
        "type": "sequence.gap",
        "generation": 3,
        "expected": 4,
        "received": 6,
        "direction": "output",
    }


def test_transport_rejects_partial_mic_frame() -> None:
    transport = DeskTransport("ws://localhost/ws/desk", "secret")
    transport._socket = _Socket()
    transport._ready.set()

    try:
        transport.send_mic_frame(b"\x00" * (MIC_FRAME_BYTES - 2))
    except ValueError as exc:
        assert "6400 bytes" in str(exc)
    else:
        raise AssertionError("partial desk mic frame was accepted")


def test_close_during_connect_cannot_resurrect_socket(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()

    class _LateSocket(_Socket):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False

        def settimeout(self, _value) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    socket = _LateSocket()

    def create_connection(*_args, **_kwargs):
        entered.set()
        release.wait(1)
        return socket

    monkeypatch.setitem(
        sys.modules,
        "websocket",
        SimpleNamespace(create_connection=create_connection),
    )
    transport = DeskTransport("ws://localhost/ws/desk", "secret")
    thread = threading.Thread(target=transport.connect)
    thread.start()
    assert entered.wait(1)

    transport.close()
    release.set()
    thread.join(1)

    assert not thread.is_alive()
    assert socket.closed is True
    assert socket.sent == []
    assert transport._closed.is_set()
    assert transport._socket is None
