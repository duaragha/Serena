from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.codex_brain import CodexBrainClient
from core.codex_brain_tools import CodexBrainToolRegistry

FAKE_SERVER = r'''#!/usr/bin/env python3
import json
import os
import sys

log_path = os.environ["FAKE_CODEX_LOG"]

def complete_turn(text="ready"):
    print(json.dumps({
        "method": "item/agentMessage/delta",
        "params": {
            "delta": text,
            "itemId": "message-1",
            "threadId": "thread-1",
            "turnId": "turn-1"
        }
    }), flush=True)
    print(json.dumps({
        "method": "turn/completed",
        "params": {
            "threadId": "thread-1",
            "turn": {
                "id": "turn-1",
                "items": [{
                    "id": "message-1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": text
                }],
                "status": "completed"
            }
        }
    }), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(message, separators=(",", ":")) + "\n")
    method = message.get("method")
    request_id = message.get("id")
    if request_id == 900 and method is None:
        complete_turn("tool complete")
        continue
    if request_id is None:
        continue
    if method == "initialize":
        result = {"userAgent": "fake"}
    elif method in {"thread/start", "thread/resume"}:
        result = {"thread": {"id": "thread-1"}}
    elif method == "turn/start":
        result = {"turn": {"id": "turn-1", "items": [], "status": "inProgress"}}
    elif method == "turn/interrupt":
        result = {}
    else:
        result = {}
    print(json.dumps({"id": request_id, "result": result}), flush=True)
    if method == "turn/start":
        text = message["params"]["input"][0]["text"]
        if "invoke_dynamic_tool" in text:
            print(json.dumps({
                "id": 900,
                "method": "item/tool/call",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "callId": "call-1",
                    "namespace": "serena_work",
                    "tool": "coding_job_status",
                    "arguments": {"reference": "last"}
                }
            }), flush=True)
        else:
            complete_turn()
'''


def _fake_codex(tmp_path: Path) -> tuple[Path, Path]:
    binary = tmp_path / "codex"
    log = tmp_path / "requests.jsonl"
    binary.write_text(FAKE_SERVER, encoding="utf-8")
    binary.chmod(0o700)
    return binary, log


def test_app_server_fallback_is_read_only_and_streams_final_text(tmp_path: Path) -> None:
    async def scenario() -> None:
        binary, log = _fake_codex(tmp_path)
        state = tmp_path / "codex-brain.json"
        environment = dict(os.environ)
        environment["FAKE_CODEX_LOG"] = str(log)
        environment["OPENAI_API_KEY"] = "must-not-reach-child"
        deltas: list[str] = []
        client = CodexBrainClient(
            cwd=tmp_path / "brain-cwd",
            developer_instructions="private persona marker",
            state_path=state,
            binary=str(binary),
            environ=environment,
        )

        result = await client.turn("reply exactly ready", on_delta=deltas.append)
        snapshot = client.snapshot()
        await client.close()

        assert result == {
            "text": "ready",
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "tool_calls": [],
        }
        assert deltas == ["ready"]
        assert snapshot["running"] is True
        assert client.command[-2:] == ["--disable", "shell_tool"]
        assert "private persona marker" not in " ".join(client.command)
        assert json.loads(state.read_text(encoding="utf-8"))["thread_id"] == "thread-1"

        messages = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        thread = next(item for item in messages if item.get("method") == "thread/start")
        turn = next(item for item in messages if item.get("method") == "turn/start")
        assert thread["params"]["model"] == "gpt-5.6-terra"
        assert thread["params"]["approvalPolicy"] == "never"
        assert thread["params"]["sandbox"] == "read-only"
        assert thread["params"]["config"]["features"]["shell_tool"] is False
        assert thread["params"]["config"]["web_search"] == "disabled"
        assert thread["params"]["config"]["mcp_servers"] == {}
        assert turn["params"]["effort"] == "high"
        assert turn["params"]["approvalPolicy"] == "never"
        assert turn["params"]["sandboxPolicy"] == {
            "type": "readOnly",
            "networkAccess": False,
        }
        assert "OPENAI_API_KEY" not in client.environ

    asyncio.run(scenario())


def test_dynamic_tool_request_runs_the_registered_serena_handler(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls: list[dict] = []

        async def handler(arguments):
            calls.append(arguments)
            return {"content": [{"type": "text", "text": "JOB abc. running"}]}

        registry = CodexBrainToolRegistry(
            {
                "serena_work": (
                    "Serena work tools.",
                    [
                        SimpleNamespace(
                            name="coding_job_status",
                            description="Read a coding job.",
                            input_schema={"reference": str},
                            handler=handler,
                        )
                    ],
                )
            }
        )
        binary, log = _fake_codex(tmp_path)
        environment = dict(os.environ)
        environment["FAKE_CODEX_LOG"] = str(log)
        client = CodexBrainClient(
            cwd=tmp_path / "brain-cwd",
            developer_instructions="persona",
            state_path=tmp_path / "state.json",
            binary=str(binary),
            environ=environment,
            tool_registry=registry,
        )
        result = await client.turn("invoke_dynamic_tool")
        await client.close()

        assert result["text"] == "tool complete"
        assert result["tool_calls"] == ["serena_work.coding_job_status"]
        assert calls == [{"reference": "last"}]
        messages = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        initialize = next(item for item in messages if item.get("method") == "initialize")
        thread = next(item for item in messages if item.get("method") == "thread/start")
        tool_response = next(item for item in messages if item.get("id") == 900)
        assert initialize["params"]["capabilities"]["experimentalApi"] is True
        assert thread["params"]["dynamicTools"] == registry.specs()
        assert tool_response["result"] == {
            "success": True,
            "contentItems": [{"type": "inputText", "text": "JOB abc. running"}],
        }

    asyncio.run(scenario())


def test_saved_fallback_thread_is_resumed(tmp_path: Path) -> None:
    async def scenario() -> None:
        binary, log = _fake_codex(tmp_path)
        state = tmp_path / "codex-brain.json"
        state.write_text(
            json.dumps(
                {
                    "thread_id": "thread-1",
                    "model": "gpt-5.6-sol",
                    "tool_contract": "none-v1",
                }
            ),
            encoding="utf-8",
        )
        environment = dict(os.environ)
        environment["FAKE_CODEX_LOG"] = str(log)
        client = CodexBrainClient(
            cwd=tmp_path / "brain-cwd",
            developer_instructions="persona",
            state_path=state,
            binary=str(binary),
            environ=environment,
        )
        await client.start()
        await client.close()

        messages = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert any(item.get("method") == "thread/resume" for item in messages)
        assert not any(item.get("method") == "thread/start" for item in messages)

    asyncio.run(scenario())


def test_model_switch_changes_the_turn_without_splitting_the_thread(tmp_path: Path) -> None:
    async def scenario() -> None:
        binary, log = _fake_codex(tmp_path)
        environment = dict(os.environ)
        environment["FAKE_CODEX_LOG"] = str(log)
        client = CodexBrainClient(
            cwd=tmp_path / "brain-cwd",
            developer_instructions="persona",
            state_path=tmp_path / "state.json",
            binary=str(binary),
            environ=environment,
        )
        await client.start()
        thread_id = client.thread_id
        client.set_route("gpt-5.6-sol", "xhigh")
        await client.turn("hard planning turn")
        await client.close()

        messages = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        turn = next(item for item in messages if item.get("method") == "turn/start")
        assert turn["params"]["threadId"] == thread_id == "thread-1"
        assert turn["params"]["model"] == "gpt-5.6-sol"
        assert turn["params"]["effort"] == "xhigh"
        saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert saved["model"] == "gpt-5.6-sol"
        assert saved["effort"] == "xhigh"

    asyncio.run(scenario())


def test_closed_fallback_client_can_restart_cleanly(tmp_path: Path) -> None:
    async def scenario() -> None:
        binary, log = _fake_codex(tmp_path)
        environment = dict(os.environ)
        environment["FAKE_CODEX_LOG"] = str(log)
        client = CodexBrainClient(
            cwd=tmp_path / "brain-cwd",
            developer_instructions="persona",
            state_path=tmp_path / "state.json",
            binary=str(binary),
            environ=environment,
        )
        assert (await client.turn("first"))["text"] == "ready"
        await client.close()
        assert (await client.turn("second"))["text"] == "ready"
        await client.close()

        messages = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert sum(item.get("method") == "initialize" for item in messages) == 2
        assert sum(item.get("method") == "turn/start" for item in messages) == 2

    asyncio.run(scenario())


def test_image_turn_uses_the_native_codex_image_input(tmp_path: Path) -> None:
    async def scenario() -> None:
        binary, log = _fake_codex(tmp_path)
        environment = dict(os.environ)
        environment["FAKE_CODEX_LOG"] = str(log)
        client = CodexBrainClient(
            cwd=tmp_path / "brain-cwd",
            developer_instructions="persona",
            state_path=tmp_path / "state.json",
            binary=str(binary),
            environ=environment,
        )
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        await client.turn(
            "what is this?",
            images=[{"media_type": "image/png", "data": png}],
        )
        await client.close()

        messages = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        turn = next(item for item in messages if item.get("method") == "turn/start")
        assert turn["params"]["input"] == [
            {"type": "text", "text": "what is this?"},
            {"type": "image", "url": f"data:image/png;base64,{png}"},
        ]

    asyncio.run(scenario())


def test_installed_codex_schema_requires_url_for_image_input(tmp_path: Path) -> None:
    binary = shutil.which("codex")
    if not binary:
        return
    schema_dir = tmp_path / "schema"
    result = subprocess.run(
        [
            binary,
            "app-server",
            "generate-json-schema",
            "--experimental",
            "--out",
            str(schema_dir),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("installed codex does not expose experimental app-server schemas")
    schema = json.loads((schema_dir / "ClientRequest.json").read_text(encoding="utf-8"))
    image_variant = next(
        variant
        for variant in schema["definitions"]["UserInput"]["oneOf"]
        if variant.get("title") == "ImageUserInput"
    )

    assert set(image_variant["required"]) == {"type", "url"}
    assert "url" in image_variant["properties"]
    assert "image_url" not in image_variant["properties"]
