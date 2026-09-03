from __future__ import annotations

import asyncio
import base64
import json

import pytest

from core import brain_daemon
from core.image_input import MAX_IMAGE_BYTES, clean_image_input
from voice import brain_bridge

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
IMAGE = {"media_type": "image/png", "data": base64.b64encode(PNG).decode("ascii")}


def test_bridge_accepts_text_with_an_image_and_an_image_by_itself() -> None:
    combined = brain_bridge.parse_client_message(
        json.dumps({"type": "typed", "text": "  what is this?  ", "image": IMAGE})
    )
    image_only = brain_bridge.parse_client_message(
        json.dumps({"type": "typed", "text": "", "image": IMAGE})
    )

    assert json.loads(combined or "null") == {
        "type": "typed",
        "text": "what is this?",
        "image": IMAGE,
    }
    assert json.loads(image_only or "null") == {
        "type": "typed",
        "text": "",
        "image": IMAGE,
    }


@pytest.mark.parametrize(
    "image",
    [
        {"media_type": "image/bmp", "data": IMAGE["data"]},
        {"media_type": "image/jpeg", "data": IMAGE["data"]},
        {"media_type": "image/png", "data": "not base64"},
    ],
)
def test_bridge_rejects_invalid_image_content(image: dict[str, str]) -> None:
    message = json.dumps({"type": "typed", "text": "look", "image": image})
    assert brain_bridge.parse_client_message(message) is None


def test_oversized_non_typed_message_is_rejected_before_json_decode(monkeypatch) -> None:
    def unexpected_decode(_raw: str):
        raise AssertionError("oversized non-typed input reached json.loads")

    monkeypatch.setattr(brain_bridge.json, "loads", unexpected_decode)
    raw = json.dumps({"type": "response", "text": "x" * 70_000})

    assert brain_bridge.parse_client_message(raw) is None


def test_large_typed_message_does_not_depend_on_json_key_order() -> None:
    large_png = b"\x89PNG\r\n\x1a\n" + b"x" * 70_000
    image = {
        "media_type": "image/png",
        "data": base64.b64encode(large_png).decode("ascii"),
    }
    wire = json.dumps({"image": image, "text": "inspect this", "type": "typed"})

    assert len(wire.encode("utf-8")) > 60_000
    assert json.loads(brain_bridge.parse_client_message(wire) or "null") == {
        "type": "typed",
        "text": "inspect this",
        "image": image,
    }


def test_bridge_reports_a_readable_error_for_a_rejected_typed_frame(monkeypatch) -> None:
    async def scenario() -> None:
        class MemoryWebSocket:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.messages = iter(
                    [json.dumps({"type": "typed", "text": "keep this", "image": {}})]
                )

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.messages)
                except StopIteration as error:
                    raise StopAsyncIteration from error

            async def send(self, value: str) -> None:
                self.sent.append(value)

        monkeypatch.setattr(brain_bridge, "current_durable_job_snapshot", lambda: None)
        ws = MemoryWebSocket()

        await brain_bridge.handler(ws)

        replies = [json.loads(value) for value in ws.sent]
        assert replies[-1]["type"] == "typed_input_error"
        assert "still here" in replies[-1]["error"]

    asyncio.run(scenario())


def test_backend_rejects_an_oversized_image_before_a_provider_sees_it() -> None:
    oversized = {
        "media_type": "image/png",
        "data": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * MAX_IMAGE_BYTES).decode(
            "ascii"
        ),
    }
    with pytest.raises(ValueError, match="under 5 MB"):
        clean_image_input(oversized)


def test_claude_receives_a_native_image_content_block() -> None:
    async def scenario() -> None:
        payload = brain_daemon._normalise_turn_payload(
            {"protocol": "voice", "text": "what is this?", "images": [IMAGE]}
        )
        query = brain_daemon._claude_query_input(payload)
        message = await anext(query)
        content = message["message"]["content"]

        assert content[0]["type"] == "text"
        assert content[1] == {
            "type": "image",
            "source": {"type": "base64", **IMAGE},
        }

    asyncio.run(scenario())


def test_installed_claude_sdk_serializes_the_native_image_message() -> None:
    from claude_agent_sdk import ClaudeSDKClient

    async def scenario() -> None:
        payload = brain_daemon._normalise_turn_payload(
            {"protocol": "voice", "text": "what is this?", "images": [IMAGE]}
        )
        written: list[str] = []

        class MemoryTransport:
            async def write(self, value: str) -> None:
                written.append(value)

        client = ClaudeSDKClient.__new__(ClaudeSDKClient)
        client._query = object()
        client._transport = MemoryTransport()
        await client.query(brain_daemon._claude_query_input(payload))

        assert len(written) == 1
        message = json.loads(written[0])
        assert message["session_id"] == "default"
        assert message["message"]["content"][1] == {
            "type": "image",
            "source": {"type": "base64", **IMAGE},
        }

    asyncio.run(scenario())


def test_claude_text_only_input_keeps_the_existing_string_contract(monkeypatch) -> None:
    monkeypatch.setattr(brain_daemon, "_state_block", lambda force=False: "")
    monkeypatch.setattr(brain_daemon, "_clock_block", lambda: "")
    monkeypatch.setattr(brain_daemon, "_recalled_voice_history_block", lambda _text: "")

    assert brain_daemon._claude_query_input(
        {"protocol": "voice", "text": "ordinary typed text"}
    ).endswith("ordinary typed text")


def test_empty_images_list_keeps_a_valid_text_turn(monkeypatch) -> None:
    monkeypatch.setattr(brain_daemon, "_state_block", lambda force=False: "")
    monkeypatch.setattr(brain_daemon, "_clock_block", lambda: "")
    monkeypatch.setattr(brain_daemon, "_recalled_voice_history_block", lambda _text: "")

    payload = brain_daemon._normalise_turn_payload(
        {"protocol": "voice", "text": "ordinary text", "images": []}
    )

    assert "images" not in payload
    assert brain_daemon._compose_message(payload).endswith("ordinary text")


def test_empty_non_image_message_never_invents_an_image_prompt(monkeypatch) -> None:
    monkeypatch.setattr(brain_daemon, "_state_block", lambda force=False: "")
    monkeypatch.setattr(brain_daemon, "_clock_block", lambda: "")
    monkeypatch.setattr(brain_daemon, "_recalled_voice_history_block", lambda _text: "")

    assert "look at this image" not in brain_daemon._compose_message(
        {"protocol": "voice", "text": ""}
    )


def test_large_typed_image_crosses_the_overlay_and_provider_contracts() -> None:
    large_png = b"\x89PNG\r\n\x1a\n" + b"x" * 70_000
    image = {
        "media_type": "image/png",
        "data": base64.b64encode(large_png).decode("ascii"),
    }
    wire = json.dumps({"type": "typed", "text": "inspect this", "image": image})

    assert len(wire.encode("utf-8")) > 60_000
    bridged = json.loads(brain_bridge.parse_client_message(wire) or "null")
    payload = brain_daemon._normalise_turn_payload(
        {"protocol": "voice", "text": bridged["text"], "images": [bridged["image"]]}
    )

    async def claude_blocks() -> list[dict]:
        return (await anext(brain_daemon._claude_query_input(payload)))["message"]["content"]

    blocks = asyncio.run(claude_blocks())
    assert blocks[1]["source"]["data"] == image["data"]
    assert len(base64.b64decode(blocks[1]["source"]["data"])) == len(large_png)


def test_large_image_crosses_the_brain_stream_handler(monkeypatch) -> None:
    async def scenario() -> None:
        large_png = b"\x89PNG\r\n\x1a\n" + b"x" * 70_000
        image = {
            "media_type": "image/png",
            "data": base64.b64encode(large_png).decode("ascii"),
        }
        captured: dict = {}

        async def run_turn(_client, payload, on_delta=None):
            captured.update(payload)
            return {"ok": True, "say": "i can see it"}

        monkeypatch.setattr(brain_daemon, "_run_turn", run_turn)
        monkeypatch.setattr(brain_daemon, "_turn_lock", asyncio.Lock())

        class MemoryWriter:
            def __init__(self) -> None:
                self.data = bytearray()
                self.done = asyncio.Event()

            def write(self, data: bytes) -> None:
                self.data.extend(data)
                if b'"response.done"' in self.data:
                    self.done.set()

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                return None

            async def wait_closed(self) -> None:
                return None

        reader = asyncio.StreamReader(limit=8 * 1024 * 1024)
        writer = MemoryWriter()
        handler = asyncio.create_task(
            brain_daemon._handle_stream_connection(object(), reader, writer)
        )
        request = {
            "type": "turn",
            "request_id": "large-image",
            "protocol": "voice",
            "text": "inspect this",
            "images": [image],
        }
        encoded = (json.dumps(request) + "\n").encode("utf-8")
        assert len(encoded) > 60_000
        reader.feed_data(encoded)
        await asyncio.wait_for(writer.done.wait(), timeout=1)
        reader.feed_eof()
        await handler
        started, done = [
            json.loads(line)
            for line in bytes(writer.data).decode("utf-8").splitlines()
        ]

        assert started == {"type": "response.start", "request_id": "large-image"}
        assert done["type"] == "response.done"
        assert captured["images"] == [image]

    asyncio.run(scenario())


def test_typed_turn_forwards_the_image_to_the_normal_voice_brain_path(monkeypatch) -> None:
    async def scenario() -> None:
        captured: dict = {}
        messages: list[dict] = []

        async def broadcast(message, *, exclude=None) -> None:
            messages.append(json.loads(message))

        async def speak_stream(clauses) -> None:
            while await clauses.get() is not None:
                pass

        async def stream_turn(text, **kwargs) -> str:
            captured.update({"text": text, **kwargs})
            await kwargs["on_sentence"]("i can see it.")
            return "i can see it."

        monkeypatch.setattr(brain_bridge, "broadcast", broadcast)
        monkeypatch.setattr(brain_bridge, "_typed_turn", None)
        monkeypatch.setattr("voice.desk.say.set_state", lambda _state: None)
        monkeypatch.setattr("voice.desk.say.speak_stream", speak_stream)
        monkeypatch.setattr("voice.desk.say.stream_turn", stream_turn)

        await brain_bridge.run_typed_turn("", image=IMAGE)

        assert captured["text"] == "look at this image"
        assert captured["images"] == [IMAGE]
        assert messages[0] == {"type": "transcription", "text": "image attached"}
        assert messages[-1] == {"type": "response", "text": "i can see it."}
        brain_bridge._typed_turn = None

    asyncio.run(scenario())
