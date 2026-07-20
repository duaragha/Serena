from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from core import frontdoor


class _BrainSocket:
    def __init__(self, *, cwd: Path) -> None:
        self.cwd = cwd
        self.sent = b""
        self.closed = False

    def sendall(self, value: bytes) -> None:
        self.sent += value

    def makefile(self, _mode: str) -> io.BytesIO:
        request = json.loads(self.sent)
        request_id = request["request_id"]
        rows = [
            {"type": "response.start", "request_id": request_id},
            {
                "type": "response.delta",
                "request_id": request_id,
                "delta": '{"say":"hello',
            },
            {
                "type": "response.delta",
                "request_id": request_id,
                "delta": '\\nr \\"good',
            },
            {
                "type": "response.delta",
                "request_id": request_id,
                "delta": (
                    '\\"","spawn":{"agents":["codex"],"cwd":'
                    + json.dumps(str(self.cwd))
                    + ',"seed":"brief"}}'
                ),
            },
            {
                "type": "response.done",
                "request_id": request_id,
                "say": 'hello\nr "good"',
                "spawn": {
                    "agents": ["codex"],
                    "cwd": str(self.cwd),
                    "seed": "brief",
                },
                "meta": {"first_delta": 0.42},
            },
        ]
        return io.BytesIO(
            b"".join(
                (json.dumps(row, separators=(",", ":")) + "\n").encode()
                for row in rows
            )
        )

    def close(self) -> None:
        self.closed = True


def test_say_decoder_streams_only_decoded_json_value() -> None:
    decoder = frontdoor._SayDeltaDecoder()

    chunks = [
        decoder.feed('{"say":"there'),
        decoder.feed(' you\\n\\"hi'),
        decoder.feed('\\"","spawn":null}'),
    ]

    assert "".join(chunks) == 'there you\n"hi"'
    assert decoder.complete is True


def test_say_decoder_ignores_nested_say_and_accepts_reordered_members() -> None:
    decoder = frontdoor._SayDeltaDecoder()

    assert decoder.feed('{"meta":{"say":"wrong"},') == ""
    assert decoder.feed('"spawn":null,"say":"right"}') == "right"
    assert decoder.decoded == "right"
    assert decoder.complete is True


def test_say_decoder_holds_fragmented_surrogate_pair_until_stable() -> None:
    decoder = frontdoor._SayDeltaDecoder()
    raw = '{"say":"x\\ud83d\\ude00y","spawn":null}'

    visible = "".join(decoder.feed(char) for char in raw)

    assert visible == "x😀y"
    assert decoder.decoded == "x😀y"
    assert decoder.complete is True


def test_say_decoder_counts_data_after_say_closes(monkeypatch) -> None:
    decoder = frontdoor._SayDeltaDecoder()

    assert decoder.feed('{"say":"done"}') == "done"
    monkeypatch.setattr(frontdoor, "_STREAM_RAW_LIMIT", len(decoder._raw) + 1)

    with pytest.raises(frontdoor._BrainStreamError, match="JSON limit"):
        decoder.feed("xx")


def test_resident_stream_emits_visible_say_and_exact_final_spawn(
    monkeypatch, tmp_path: Path
) -> None:
    connection = _BrainSocket(cwd=tmp_path)
    monkeypatch.setattr(frontdoor, "_spawn_cwd_allowed", lambda _cwd: True)
    monkeypatch.setattr(
        frontdoor,
        "_open_brain_stream",
        lambda: (connection, None, "brain.sock"),
    )

    events = list(
        frontdoor._resident_stream_turn(
            [{"role": "user", "text": "open the coding pane"}]
        )
    )
    request = json.loads(connection.sent)

    assert request["protocol"] == "frontdoor"
    assert request["stream"] is True
    assert "open the coding pane" in request["text"]
    assert events[0]["type"] == "start"
    assert "".join(
        event["delta"] for event in events if event["type"] == "delta"
    ) == 'hello\nr "good"'
    assert events[-1] == {
        "type": "done",
        "say": 'hello\nr "good"',
        "spawn": {
            "agents": ["codex"],
            "cwd": str(tmp_path),
            "seed": "brief",
        },
        "meta": {"first_delta": 0.42},
        "backend": "brain.sock",
    }
    assert connection.closed is True


def test_closing_outer_stream_closes_resident_connection(
    monkeypatch, tmp_path: Path
) -> None:
    connection = _BrainSocket(cwd=tmp_path)
    monkeypatch.setattr(
        frontdoor,
        "_open_brain_stream",
        lambda: (connection, None, "brain.sock"),
    )
    stream = frontdoor.stream_turn([{"role": "user", "text": "hello"}])

    assert next(stream)["type"] == "start"
    stream.close()

    assert connection.closed is True


def test_resident_rejects_foreign_request_id(monkeypatch) -> None:
    class Connection:
        def __init__(self) -> None:
            self.closed = False

        def sendall(self, _value: bytes) -> None:
            pass

        def makefile(self, _mode: str) -> io.BytesIO:
            row = {"type": "response.start", "request_id": "not-ours"}
            return io.BytesIO((json.dumps(row) + "\n").encode())

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(
        frontdoor,
        "_open_brain_stream",
        lambda: (connection, None, "brain.sock"),
    )

    events = list(frontdoor.stream_turn([{"role": "user", "text": "hello"}]))

    assert events == [
        {
            "type": "error",
            "error": "resident brain response request_id does not match",
        }
    ]
    assert connection.closed is True


def test_resident_rejects_done_without_say_after_visible_delta(monkeypatch) -> None:
    class Connection:
        def __init__(self) -> None:
            self.sent = b""

        def sendall(self, value: bytes) -> None:
            self.sent += value

        def makefile(self, _mode: str) -> io.BytesIO:
            request_id = json.loads(self.sent)["request_id"]
            rows = [
                {"type": "response.start", "request_id": request_id},
                {
                    "type": "response.delta",
                    "request_id": request_id,
                    "delta": '{"say":"hello',
                },
                {"type": "response.done", "request_id": request_id},
            ]
            return io.BytesIO(
                b"".join((json.dumps(row) + "\n").encode() for row in rows)
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        frontdoor,
        "_open_brain_stream",
        lambda: (Connection(), None, "brain.sock"),
    )

    events = list(frontdoor.stream_turn([{"role": "user", "text": "hello"}]))

    assert [event["type"] for event in events] == ["start", "delta", "error"]
    assert events[-1] == {
        "type": "error",
        "error": "resident brain final reply has no say",
    }


def test_resident_rejects_invalid_final_spawn(monkeypatch) -> None:
    class Connection:
        def __init__(self) -> None:
            self.sent = b""

        def sendall(self, value: bytes) -> None:
            self.sent += value

        def makefile(self, _mode: str) -> io.BytesIO:
            request_id = json.loads(self.sent)["request_id"]
            rows = [
                {"type": "response.start", "request_id": request_id},
                {
                    "type": "response.done",
                    "request_id": request_id,
                    "say": "opening it",
                    "spawn": {"agents": ["codex"], "cwd": "/", "seed": "brief"},
                },
            ]
            return io.BytesIO(
                b"".join((json.dumps(row) + "\n").encode() for row in rows)
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        frontdoor,
        "_open_brain_stream",
        lambda: (Connection(), None, "brain.sock"),
    )

    events = list(frontdoor.stream_turn([{"role": "user", "text": "open it"}]))

    assert events == [
        {"type": "start", "request_id": events[0]["request_id"], "backend": "brain.sock"},
        {"type": "delta", "delta": "opening it"},
        {"type": "error", "error": "resident brain final spawn is invalid"},
    ]


def test_stream_falls_back_only_when_resident_was_never_reached(monkeypatch) -> None:
    def unavailable(_history):
        raise frontdoor._BrainStreamUnavailable("offline")
        yield

    monkeypatch.setattr(frontdoor, "_resident_stream_turn", unavailable)
    monkeypatch.setattr(
        frontdoor,
        "turn",
        lambda *_args, **_kwargs: {
            "ok": True,
            "say": "cold but present",
            "spawn": None,
            "error": "",
        },
    )

    events = list(
        frontdoor.stream_turn([{"role": "user", "text": "hello"}])
    )

    assert [event["type"] for event in events] == ["start", "delta", "done"]
    assert events[1]["delta"] == "cold but present"


def test_stream_does_not_duplicate_a_resident_turn_after_midstream_failure(
    monkeypatch,
) -> None:
    fallback_called = False

    def broken(_history):
        yield {"type": "start"}
        raise frontdoor._BrainStreamError("lost after delivery")

    def fallback(*_args, **_kwargs):
        nonlocal fallback_called
        fallback_called = True
        return {"ok": True, "say": "duplicate", "spawn": None, "error": ""}

    monkeypatch.setattr(frontdoor, "_resident_stream_turn", broken)
    monkeypatch.setattr(frontdoor, "turn", fallback)

    events = list(
        frontdoor.stream_turn([{"role": "user", "text": "one turn only"}])
    )

    assert events == [
        {"type": "start"},
        {"type": "error", "error": "lost after delivery"},
    ]
    assert fallback_called is False


def test_send_failure_never_retries_a_possibly_delivered_turn(monkeypatch) -> None:
    fallback_called = False

    class Connection:
        def sendall(self, _value: bytes) -> None:
            raise OSError("uncertain send")

        def close(self) -> None:
            pass

    def fallback(*_args, **_kwargs):
        nonlocal fallback_called
        fallback_called = True
        return {"ok": True, "say": "duplicate", "spawn": None, "error": ""}

    monkeypatch.setattr(
        frontdoor,
        "_open_brain_stream",
        lambda: (Connection(), None, "brain.sock"),
    )
    monkeypatch.setattr(frontdoor, "turn", fallback)

    events = list(frontdoor.stream_turn([{"role": "user", "text": "once"}]))

    assert events == [
        {"type": "error", "error": "resident brain stream failed mid-turn"}
    ]
    assert fallback_called is False


def test_streamed_greeting_is_cached_for_the_next_open(
    monkeypatch, tmp_path: Path
) -> None:
    cache = tmp_path / "greeting.json"
    monkeypatch.setattr(frontdoor, "GREETING_CACHE", cache)
    monkeypatch.setattr(
        frontdoor,
        "_resident_stream_turn",
        lambda _history: iter(
            [
                {"type": "start"},
                {"type": "delta", "delta": "morning raghav"},
                {
                    "type": "done",
                    "say": "morning raghav",
                    "spawn": None,
                    "meta": {},
                },
            ]
        ),
    )

    assert list(frontdoor.stream_turn([]))[-1]["say"] == "morning raghav"
    assert json.loads(cache.read_text(encoding="utf-8"))["say"] == "morning raghav"
