from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from voice.call.brain import (
    BrainDiscoveryClient,
    BrainDoneMeta,
    BrainError,
    BrainHttpFallback,
    BrainSocketClient,
)
from voice.call.sentences import IncrementalSentenceSplitter


def test_brain_socket_matches_request_and_preserves_done_meta(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "brain.sock"
        captured = {}

        async def handler(reader, writer) -> None:
            request = json.loads(await reader.readline())
            captured.update(request)
            writer.write(
                (json.dumps({"type": "response.delta", "request_id": "other", "delta": "x"}) + "\n").encode()
            )
            for payload in (
                {"type": "response.start", "request_id": request["request_id"]},
                {
                    "type": "response.delta",
                    "request_id": request["request_id"],
                    "delta": "hello. ",
                },
                {
                    "type": "response.done",
                    "request_id": request["request_id"],
                    "say": "hello.",
                    "meta": {
                        "elapsed": 0.42,
                        "turns": 9,
                        "session_turns": 4,
                        "first_delta": 0.1,
                        "session_id": "session-voice",
                        "turn_id": "turn-1",
                    },
                },
            ):
                writer.write((json.dumps(payload) + "\n").encode())
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(handler, path=str(socket_path))
        client = BrainSocketClient(socket_path)
        events = []
        async with server:
            async for event in client.stream_turn(
                "hi", call_id="call-1", turn_id="turn-1", request_id="request-1"
            ):
                events.append(event)
        assert [event.type for event in events] == ["start", "delta", "done"]
        assert events[-1].meta is not None
        assert events[-1].meta.as_dict() == {
            "elapsed": 0.42,
            "turns": 9,
            "session_turns": 4,
            "first_delta": 0.1,
            "session_id": "session-voice",
            "turn_id": "turn-1",
        }
        assert captured == {
            "type": "turn",
            "request_id": "request-1",
            "protocol": "voice",
            "text": "hi",
            "stream": True,
            "call_id": "call-1",
            "turn_id": "turn-1",
        }

    asyncio.run(scenario())


def test_brain_socket_can_exclude_a_synthetic_probe_from_continuity(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "brain.sock"
        captured = {}

        async def handler(reader, writer) -> None:
            captured.update(json.loads(await reader.readline()))
            for payload in (
                {"type": "response.start", "request_id": "probe"},
                {
                    "type": "response.done",
                    "request_id": "probe",
                    "say": "done.",
                },
            ):
                writer.write((json.dumps(payload) + "\n").encode())
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(handler, path=str(socket_path))
        async with server:
            events = [
                event
                async for event in BrainSocketClient(socket_path).stream_turn(
                    "probe",
                    call_id="probe-call",
                    turn_id="probe-turn",
                    request_id="probe",
                    journal=False,
                )
            ]

        assert [event.type for event in events] == ["start", "done"]
        assert captured["journal"] is False

    asyncio.run(scenario())


def test_absent_brain_meta_fields_stay_absent() -> None:
    assert BrainDoneMeta.from_payload({"elapsed": 1.0}).as_dict() == {
        "elapsed": 1.0
    }


def test_brain_discovery_uses_loopback_tcp_stream(tmp_path: Path) -> None:
    async def scenario() -> None:
        captured = {}

        async def handler(reader, writer) -> None:
            request = json.loads(await reader.readline())
            captured.update(request)
            for payload in (
                {"type": "response.start", "request_id": request["request_id"]},
                {
                    "type": "response.delta",
                    "request_id": request["request_id"],
                    "delta": "ready.",
                },
                {
                    "type": "response.done",
                    "request_id": request["request_id"],
                    "say": "ready.",
                    "meta": {"elapsed": 0.2, "turns": 2},
                },
            ):
                writer.write((json.dumps(payload) + "\n").encode())
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        discovery = tmp_path / "brain.json"
        discovery.write_text(
            json.dumps(
                {
                    "port": 8377,
                    "stream": {
                        "transport": "tcp",
                        "host": "127.0.0.1",
                        "port": port,
                        "token": "test-secret",
                    },
                }
            )
        )
        client = BrainDiscoveryClient(
            discovery_path=discovery,
            socket_path=tmp_path / "missing.sock",
        )
        events = []
        async with server:
            async for event in client.stream_turn(
                "hi",
                call_id="call-tcp",
                turn_id="turn-tcp",
                request_id="request-tcp",
            ):
                events.append(event)
        assert [event.type for event in events] == ["start", "delta", "done"]
        assert {event.backend for event in events} == {"brain.tcp"}
        assert captured["protocol"] == "voice"
        assert captured["call_id"] == "call-tcp"
        assert captured["auth"] == "test-secret"

    asyncio.run(scenario())


def test_brain_discovery_rejects_non_loopback_stream(tmp_path: Path) -> None:
    async def scenario() -> None:
        discovery = tmp_path / "brain.json"
        discovery.write_text(
            json.dumps(
                {
                    "stream": {
                        "transport": "tcp",
                        "host": "example.com",
                        "port": 8378,
                        "token": "secret",
                    }
                }
            )
        )
        client = BrainDiscoveryClient(
            discovery_path=discovery,
            socket_path=tmp_path / "missing.sock",
        )
        with pytest.raises(BrainError, match="discovery is unavailable"):
            async for _ in client.stream_turn(
                "hi", call_id="call", turn_id="turn"
            ):
                pass

    asyncio.run(scenario())


def test_http_fallback_cancellation_closes_transport(tmp_path: Path) -> None:
    async def scenario() -> None:
        request_received = asyncio.Event()
        peer_closed = asyncio.Event()

        async def handler(reader, writer) -> None:
            await reader.readline()
            length = 0
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("latin-1").partition(":")
                if key.strip().lower() == "content-length":
                    length = int(value.strip())
            await reader.readexactly(length)
            request_received.set()
            await reader.read()
            peer_closed.set()
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        discovery = tmp_path / "brain.json"
        discovery.write_text(
            json.dumps({"port": port, "token": "secret"}),
            encoding="utf-8",
        )
        client = BrainHttpFallback(discovery_path=discovery, timeout=5)

        async def consume() -> None:
            async for _ in client.stream_turn(
                "hello", call_id="call", turn_id="turn"
            ):
                pass

        async with server:
            task = asyncio.create_task(consume())
            await asyncio.wait_for(request_received.wait(), timeout=1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.wait_for(peer_closed.wait(), timeout=1)

    asyncio.run(scenario())


def test_sentence_splitter_streams_complete_sentences_and_flushes_tail() -> None:
    splitter = IncrementalSentenceSplitter()
    assert splitter.feed("there you") == []
    assert splitter.feed(" are. keep") == ["there you are."]
    assert splitter.feed(" going! last") == ["keep going!"]
    assert splitter.flush() == ["last"]


def test_sentence_splitter_does_not_split_common_abbreviation() -> None:
    splitter = IncrementalSentenceSplitter()
    assert splitter.feed("ask dr.") == []
    assert splitter.feed(" singh. next.") == ["ask dr. singh.", "next."]


def test_sentence_splitter_flushes_first_safe_clause_for_voice_latency() -> None:
    splitter = IncrementalSentenceSplitter()
    assert splitter.feed("Same status, nothing") == ["Same status,"]
    assert splitter.feed(" new since I last checked") == []
    assert splitter.feed(": still uncommitted") == []
    assert splitter.pending == "nothing new since I last checked: still uncommitted"


def test_sentence_splitter_releases_short_opening_after_context_arrives() -> None:
    splitter = IncrementalSentenceSplitter()
    assert splitter.feed("Quick one: your") == ["Quick one:"]
    assert splitter.feed(" ECU still needs checking") == []
    assert splitter.pending == "your ECU still needs checking"


def test_sentence_splitter_does_not_release_a_tiny_filler_clause() -> None:
    splitter = IncrementalSentenceSplitter()
    assert splitter.feed("yes, ") == []
    assert splitter.feed("that is the right call") == []
    assert splitter.flush() == ["yes, that is the right call"]


def test_sentence_splitter_hard_flushes_only_the_first_long_clause() -> None:
    splitter = IncrementalSentenceSplitter(
        first_clause_chars=32,
        first_clause_hard_chars=48,
    )
    first = "this opening keeps going without any useful punctuation at all"
    emitted = splitter.feed(first)
    assert emitted == ["this opening keeps going without any useful"]
    assert splitter.feed(" and the rest remains buffered") == []
    assert splitter.flush() == ["punctuation at all and the rest remains buffered"]


def test_sentence_splitter_prefers_early_voice_cut_over_distant_period() -> None:
    splitter = IncrementalSentenceSplitter(
        first_clause_chars=12,
        first_clause_hard_chars=32,
    )

    assert splitter.feed(
        "this opening arrives as one large model delta before its final period."
    ) == [
        "this opening arrives as one",
        "large model delta before its final period.",
    ]
    assert splitter.flush() == []


def test_sentence_splitter_prefers_short_clause_in_complete_model_delta() -> None:
    splitter = IncrementalSentenceSplitter()

    assert splitter.feed("same status, the longer explanation follows here.") == [
        "same status,",
        "the longer explanation follows here.",
    ]
