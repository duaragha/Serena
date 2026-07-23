import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import tomllib
from mcp import types

from core import brain_daemon, brain_laptop_tools, brain_tools


class CapturedOptions:
    def __init__(self, **values):
        self.__dict__.update(values)


def test_billing_guard_has_no_api_key_override(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-run")
    monkeypatch.setenv("SERENA_BRAIN_ALLOW_API_KEY", "1")

    try:
        brain_daemon._guard_billing()
    except SystemExit as exc:
        assert exc.code == 78
    else:
        raise AssertionError("brain daemon accepted a pay-per-token API key")

    assert os.environ["ANTHROPIC_API_KEY"] == "must-not-run"


def test_billing_guard_rejects_non_oauth_provider_overrides(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")

    try:
        brain_daemon._guard_billing()
    except SystemExit as exc:
        assert exc.code == 78
    else:
        raise AssertionError("brain daemon accepted a metered provider override")


def test_billing_guard_rejects_anthropic_aws_route(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_ANTHROPIC_AWS", "1")

    try:
        brain_daemon._guard_billing()
    except SystemExit as exc:
        assert exc.code == 78
    else:
        raise AssertionError("brain daemon accepted the Anthropic AWS provider route")


def test_brain_instance_lock_is_process_exclusive(tmp_path: Path):
    lock_path = tmp_path / "brain.lock"
    first = brain_daemon._lock_file(lock_path)
    try:
        try:
            brain_daemon._lock_file(lock_path)
        except OSError:
            pass
        else:
            raise AssertionError("a second brain acquired the process lock")
        assert lock_path.read_text(encoding="ascii").strip() == str(os.getpid())
    finally:
        brain_daemon._unlock_file(first)


def test_runtime_declares_the_claude_agent_sdk_dependency():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    dependencies = project["project"]["dependencies"]
    assert any(value.startswith("claude-agent-sdk>=0.2.120") for value in dependencies)


def test_windows_acl_principal_prefers_resolvable_whoami(monkeypatch):
    monkeypatch.setenv("USERDOMAIN", "WORKGROUP")
    monkeypatch.setenv("USERNAME", "raghav")
    monkeypatch.setattr(
        brain_daemon.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="RAGHAVSGAMINGPC\\raghav\n",
        ),
    )

    assert brain_daemon._windows_current_principal() == ("RAGHAVSGAMINGPC\\raghav")


def test_brain_options_are_unattended_and_read_only(monkeypatch, tmp_path: Path):
    prompt_path = tmp_path / "brain-system-prompt.md"
    monkeypatch.setattr(brain_daemon, "_persona_context", lambda: "persona")
    monkeypatch.setattr(brain_daemon, "BRAIN_SYSTEM_PROMPT_FILE", prompt_path)
    server = brain_tools.brain_tools_server()
    response = asyncio.run(
        server["instance"].request_handlers[types.ListToolsRequest](
            types.ListToolsRequest(method="tools/list")
        )
    )
    exposed_names = [item.name for item in response.root.tools]
    allowed_names = [f"mcp__serena-ro__{name}" for name in exposed_names]
    options = brain_daemon._build_agent_options(
        CapturedOptions,
        server,
        brain_tools.BRAIN_TOOL_NAMES,
    )

    assert options.permission_mode == "dontAsk"
    assert options.system_prompt == {"type": "preset", "preset": "claude_code"}
    assert options.extra_args == {
        "append-system-prompt-file": str(prompt_path.resolve())
    }
    assert prompt_path.read_text(encoding="utf-8") == "persona\n"
    if os.name != "nt":
        assert prompt_path.stat().st_mode & 0o777 == 0o600
    assert options.strict_mcp_config is True
    assert options.mcp_servers == {"serena-ro": server}
    assert options.tools == []
    assert exposed_names == [
        "git_latest",
        "github_activity",
        "recall_chats",
        "read_ledger",
    ]
    assert options.allowed_tools == allowed_names == brain_tools.BRAIN_TOOL_NAMES
    assert options.setting_sources == []
    assert options.skills == []
    assert options.env["SERENA_BRAIN_PROCESS_ROLE"] == "resident-sdk"
    assert options.env["SERENA_BRAIN_PROCESS_TOKEN"] == "unassigned"
    assert {
        "Bash",
        "Edit",
        "Write",
        "Read",
        "Grep",
        "Glob",
        "ToolSearch",
    }.isdisjoint(options.tools)
    assert {
        "Bash",
        "Edit",
        "Write",
        "Read",
        "Grep",
        "Glob",
        "ToolSearch",
    }.isdisjoint(options.allowed_tools)


def test_laptop_tools_are_separate_and_capability_brokered(
    monkeypatch, tmp_path: Path
):
    prompt_path = tmp_path / "brain-system-prompt.md"
    monkeypatch.setattr(brain_daemon, "_persona_context", lambda: "persona")
    monkeypatch.setattr(brain_daemon, "BRAIN_SYSTEM_PROMPT_FILE", prompt_path)
    read_server = brain_tools.brain_tools_server()
    laptop_server = brain_laptop_tools.laptop_tools_server()

    options = brain_daemon._build_agent_options(
        CapturedOptions,
        read_server,
        brain_tools.BRAIN_TOOL_NAMES,
        laptop_tools=laptop_server,
        laptop_tool_names=brain_laptop_tools.LAPTOP_TOOL_NAMES,
    )

    assert set(options.mcp_servers) == {"serena-ro", "serena-laptop"}
    assert options.permission_mode == "dontAsk"
    assert options.allowed_tools == [
        *brain_tools.BRAIN_TOOL_NAMES,
        *brain_laptop_tools.LAPTOP_TOOL_NAMES,
    ]
    assert "mcp__serena-laptop__laptop_action" in options.allowed_tools


def test_sdk_command_keeps_private_prompt_out_of_process_arguments(
    monkeypatch, tmp_path: Path
):
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    prompt_path = tmp_path / "brain-system-prompt.md"
    private_marker = "private-serena-context-must-not-reach-argv"
    monkeypatch.setattr(brain_daemon, "_persona_context", lambda: private_marker)
    monkeypatch.setattr(brain_daemon, "BRAIN_SYSTEM_PROMPT_FILE", prompt_path)
    options = brain_daemon._build_agent_options(
        ClaudeAgentOptions,
        {},
        [],
    )
    transport = SubprocessCLITransport(prompt="", options=options)
    transport._cli_path = "/trusted/claude"

    command = transport._build_command()

    assert "--append-system-prompt" not in command
    flag = command.index("--append-system-prompt-file")
    assert command[flag + 1] == str(prompt_path.resolve())
    assert all(private_marker not in argument for argument in command)


def test_surface_roles_are_scoped_to_their_own_turns(monkeypatch):
    monkeypatch.setattr(brain_daemon, "_state_block", lambda: "")
    monkeypatch.setattr(
        "core.frontdoor._ROLE",
        "FRONTDOOR_ONLY_RULES",
    )

    voice = brain_daemon._compose_message(
        {
            "protocol": "voice",
            "text": "hello",
            "call_id": "call-7",
            "turn_id": "turn-3",
        }
    )
    frontdoor = brain_daemon._compose_message({"protocol": "frontdoor", "text": "hello"})

    assert "FRONTDOOR_ONLY_RULES" not in voice
    assert "spoken aloud" in voice
    assert "first useful point as a complete sentence" in voice
    assert "roughly six to sixteen words" in voice
    assert "You own coding work personally" in voice
    assert "Never mention panes" in voice
    assert '<voice-turn-context>{"call_id":"call-7","turn_id":"turn-3"}' in voice
    assert "FRONTDOOR_ONLY_RULES" in frontdoor
    assert "STRICT front-door JSON" in frontdoor


def test_tool_use_assistant_blocks_keep_a_spoken_word_boundary():
    assert brain_daemon._join_assistant_chunks(
        ["i'll check that now.", "you've got Chats open."]
    ) == "i'll check that now. you've got Chats open."
    assert brain_daemon._join_assistant_chunks(["already ", "spaced"]) == (
        "already spaced"
    )


def test_state_block_keeps_full_ledger_grounding_after_digest_is_unchanged(
    monkeypatch,
):
    from core import brain_state

    record = brain_state._clean_record(
        {
            "id": 396,
            "type": "ledger",
            "ledger_key": "gideon",
            "goal": "build the brain",
            "facts": "voice is local",
            "decision": "keep sonnet",
            "risk": "latency p90 is red",
            "next_action": "finish shorter gates",
        }
    )
    state = brain_state.ActiveState(records=(record,), source="local")
    monkeypatch.setattr(brain_state, "active_state", lambda: state)
    monkeypatch.setattr(brain_daemon, "_last_state_fingerprint", "")

    first = brain_daemon._state_block()
    second = brain_daemon._state_block()

    assert "# Active ledgers" in first
    assert "# His open tasks" not in second
    assert "decision: keep sonnet" in second
    assert "risk: latency p90 is red" in second


def test_voice_model_routing_reuses_one_sdk_session(monkeypatch):
    async def scenario() -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.models = []

            async def set_model(self, model):
                self.models.append(model)

        client = FakeClient()
        monkeypatch.setattr(brain_daemon, "MODEL", "sonnet")
        monkeypatch.setattr(brain_daemon, "VOICE_MODEL", "haiku")
        monkeypatch.setattr(brain_daemon, "_active_model", "sonnet")

        assert await brain_daemon._select_model(client, "voice") == "haiku"
        assert await brain_daemon._select_model(client, "voice") == "haiku"
        assert await brain_daemon._select_model(client, "frontdoor") == "sonnet"
        assert client.models == ["haiku", "sonnet"]

    asyncio.run(scenario())


def test_stream_rejects_bad_token_before_running_turn(monkeypatch):
    async def scenario() -> None:
        called = False

        async def run_turn(*args, **kwargs):
            nonlocal called
            called = True
            return {"ok": True, "say": "wrong", "turns": 1}

        monkeypatch.setattr(brain_daemon, "_run_turn", run_turn)
        brain_daemon._turn_lock = asyncio.Lock()

        async def handler(reader, writer):
            await brain_daemon._handle_stream_connection(
                object(), reader, writer, auth_token="right"
            )

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                (
                    json.dumps(
                        {
                            "type": "turn",
                            "request_id": "bad-auth",
                            "auth": "wrong",
                            "text": "hello",
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            response = json.loads(await reader.readline())
            assert response["error"] == "unauthorized"
            assert not called
            writer.close()
            await writer.wait_closed()

    asyncio.run(scenario())


def test_stream_turn_sends_start_and_done(monkeypatch):
    async def scenario() -> None:
        async def run_turn(*args, **kwargs):
            return {
                "ok": True,
                "say": "hello",
                "elapsed": 0.1,
                "first_delta": 0.05,
                "turns": 2,
            }

        monkeypatch.setattr(brain_daemon, "_run_turn", run_turn)
        brain_daemon._turn_lock = asyncio.Lock()

        async def handler(reader, writer):
            await brain_daemon._handle_stream_connection(
                object(), reader, writer, auth_token="secret"
            )

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                (
                    json.dumps(
                        {
                            "type": "turn",
                            "request_id": "success",
                            "auth": "secret",
                            "text": "hello",
                            "stream": True,
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            assert json.loads(await reader.readline())["type"] == "response.start"
            done = json.loads(await reader.readline())
            assert done["type"] == "response.done"
            assert done["say"] == "hello"
            assert done["meta"]["turns"] == 2
            writer.close()
            await writer.wait_closed()

    asyncio.run(scenario())


def test_stream_next_turn_waits_intact_while_first_response_rotates(monkeypatch):
    async def scenario() -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.delivery_started = asyncio.Event()
                self.release_delivery = asyncio.Event()
                self.deliveries = 0

            async def response_delivered(self, _out) -> None:
                self.deliveries += 1
                if self.deliveries == 1:
                    self.delivery_started.set()
                    await self.release_delivery.wait()

        client = FakeClient()
        texts = []

        async def run_turn(_client, payload, **_kwargs):
            texts.append(payload["text"])
            return {
                "ok": True,
                "say": payload["text"],
                "elapsed": 0.1,
                "turns": len(texts),
            }

        monkeypatch.setattr(brain_daemon, "_run_turn", run_turn)
        brain_daemon._turn_lock = asyncio.Lock()

        async def handler(reader, writer):
            await brain_daemon._handle_stream_connection(
                client, reader, writer, auth_token="secret"
            )

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)

            def request(request_id: str, text: str) -> bytes:
                return (
                    json.dumps(
                        {
                            "type": "turn",
                            "request_id": request_id,
                            "auth": "secret",
                            "text": text,
                            "stream": True,
                        }
                    )
                    + "\n"
                ).encode()

            writer.write(request("one", "first"))
            await writer.drain()
            assert json.loads(await reader.readline())["request_id"] == "one"
            assert json.loads(await reader.readline())["request_id"] == "one"
            await client.delivery_started.wait()

            writer.write(request("two", "second"))
            await writer.drain()
            client.release_delivery.set()
            second_start = json.loads(await asyncio.wait_for(reader.readline(), timeout=1))
            second_done = json.loads(await asyncio.wait_for(reader.readline(), timeout=1))
            assert second_start["request_id"] == "two"
            assert second_done["request_id"] == "two"
            assert texts == ["first", "second"]
            writer.close()
            await writer.wait_closed()

    asyncio.run(scenario())


def test_http_turn_rejects_missing_token_with_401(monkeypatch):
    async def scenario() -> None:
        called = False

        async def run_turn(*args, **kwargs):
            nonlocal called
            called = True
            return {"ok": True, "say": "wrong", "turns": 1}

        monkeypatch.setattr(brain_daemon, "_run_turn", run_turn)
        monkeypatch.setattr(brain_daemon, "BRAIN_TOKEN", "right")
        brain_daemon._turn_lock = asyncio.Lock()

        async def handler(reader, writer):
            await brain_daemon._handle_http_connection(object(), reader, writer)

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps({"text": "hello"}).encode()
            writer.write(
                b"POST /turn HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n" + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
            )
            await writer.drain()
            status = (await reader.readline()).decode()
            while await reader.readline() not in (b"\r\n", b"\n", b""):
                pass
            response = json.loads(await reader.read())
            assert status.startswith("HTTP/1.1 401 ")
            assert response["error"] == "unauthorized"
            assert not called
            writer.close()
            await writer.wait_closed()

    asyncio.run(scenario())


def test_http_peer_close_after_body_does_not_cancel_rotation(monkeypatch):
    async def scenario() -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.delivery_started = asyncio.Event()
                self.release_delivery = asyncio.Event()
                self.delivery_finished = asyncio.Event()

            async def response_delivered(self, _out) -> None:
                self.delivery_started.set()
                await self.release_delivery.wait()
                self.delivery_finished.set()

        client = FakeClient()

        async def run_turn(*_args, **_kwargs):
            return {"ok": True, "say": "done", "elapsed": 0.1, "turns": 1}

        monkeypatch.setattr(brain_daemon, "_run_turn", run_turn)
        monkeypatch.setattr(brain_daemon, "BRAIN_TOKEN", "right")
        brain_daemon._turn_lock = asyncio.Lock()

        async def handler(reader, writer):
            await brain_daemon._handle_http_connection(client, reader, writer)

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps({"text": "hello"}).encode()
            writer.write(
                b"POST /turn HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Authorization: Bearer right\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            assert (await reader.readline()).startswith(b"HTTP/1.1 200")
            content_length = 0
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                if line.lower().startswith(b"content-length:"):
                    content_length = int(line.split(b":", 1)[1])
            response = json.loads(await reader.readexactly(content_length))
            assert response["say"] == "done"
            await client.delivery_started.wait()
            writer.close()
            await writer.wait_closed()
            client.release_delivery.set()
            await asyncio.wait_for(client.delivery_finished.wait(), timeout=1)

    asyncio.run(scenario())


def test_http_reply_is_committed_before_response_bytes(monkeypatch):
    async def scenario() -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.committed = False

            async def response_committed(self, _out) -> None:
                self.committed = True

            async def response_delivered(self, _out) -> None:
                assert self.committed

        client = FakeClient()

        async def run_turn(*_args, **_kwargs):
            return {"ok": True, "say": "durable", "elapsed": 0.1, "turns": 1}

        original_write = brain_daemon._write_http_json

        async def checked_write(writer, status, reason, payload):
            if payload.get("say") == "durable":
                assert client.committed
            await original_write(writer, status, reason, payload)

        monkeypatch.setattr(brain_daemon, "_run_turn", run_turn)
        monkeypatch.setattr(brain_daemon, "_write_http_json", checked_write)
        monkeypatch.setattr(brain_daemon, "BRAIN_TOKEN", "right")
        brain_daemon._turn_lock = asyncio.Lock()

        async def handler(reader, writer):
            await brain_daemon._handle_http_connection(client, reader, writer)

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps({"text": "hello"}).encode()
            writer.write(
                b"POST /turn HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Authorization: Bearer right\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            assert (await reader.readline()).startswith(b"HTTP/1.1 200")
            while await reader.readline() not in (b"\r\n", b"\n", b""):
                pass
            response = json.loads(await reader.read())
            assert response["say"] == "durable"
            assert client.committed
            writer.close()
            await writer.wait_closed()

    asyncio.run(scenario())


def test_http_disconnect_interrupts_sdk_turn_and_releases_lock(monkeypatch):
    async def scenario() -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.interrupted = asyncio.Event()

            async def interrupt(self) -> None:
                self.interrupted.set()

        client = FakeClient()

        async def run_turn(*args, **kwargs):
            await client.interrupted.wait()
            return {"ok": True, "say": "late", "turns": 1}

        monkeypatch.setattr(brain_daemon, "_run_turn", run_turn)
        monkeypatch.setattr(brain_daemon, "BRAIN_TOKEN", "right")
        brain_daemon._turn_lock = asyncio.Lock()

        async def handler(reader, writer):
            await brain_daemon._handle_http_connection(client, reader, writer)

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps({"text": "hello"}).encode()
            writer.write(
                b"POST /turn HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Authorization: Bearer right\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            for _ in range(100):
                if brain_daemon._turn_lock.locked():
                    break
                await asyncio.sleep(0.01)
            assert brain_daemon._turn_lock.locked()
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(client.interrupted.wait(), timeout=1)
            for _ in range(100):
                if not brain_daemon._turn_lock.locked():
                    break
                await asyncio.sleep(0.01)
            assert not brain_daemon._turn_lock.locked()

    asyncio.run(scenario())


def test_queued_http_disconnect_does_not_interrupt_another_turn(monkeypatch):
    async def scenario() -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.interrupts = 0

            async def interrupt(self) -> None:
                self.interrupts += 1

        client = FakeClient()
        called = False

        async def run_turn(*args, **kwargs):
            nonlocal called
            called = True
            return {"ok": True, "say": "wrong", "turns": 1}

        monkeypatch.setattr(brain_daemon, "_run_turn", run_turn)
        monkeypatch.setattr(brain_daemon, "BRAIN_TOKEN", "right")
        brain_daemon._turn_lock = asyncio.Lock()
        await brain_daemon._turn_lock.acquire()

        async def handler(reader, writer):
            await brain_daemon._handle_http_connection(client, reader, writer)

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps({"text": "hello"}).encode()
            writer.write(
                b"POST /turn HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Authorization: Bearer right\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            await asyncio.sleep(0.02)
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.05)
            assert client.interrupts == 0
            assert not called
            brain_daemon._turn_lock.release()

    asyncio.run(scenario())


def test_stream_disconnect_interrupts_sdk_turn_and_releases_lock(monkeypatch):
    async def scenario() -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.interrupted = asyncio.Event()

            async def interrupt(self) -> None:
                self.interrupted.set()

        client = FakeClient()

        async def run_turn(*args, **kwargs):
            await client.interrupted.wait()
            return {"ok": True, "say": "late", "turns": 1}

        monkeypatch.setattr(brain_daemon, "_run_turn", run_turn)
        brain_daemon._turn_lock = asyncio.Lock()

        async def handler(reader, writer):
            await brain_daemon._handle_stream_connection(
                client, reader, writer, auth_token="secret"
            )

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                (
                    json.dumps(
                        {
                            "type": "turn",
                            "request_id": "disconnect",
                            "auth": "secret",
                            "text": "hello",
                            "stream": True,
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            assert json.loads(await reader.readline())["type"] == "response.start"
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(client.interrupted.wait(), timeout=1)
            for _ in range(50):
                if not brain_daemon._turn_lock.locked():
                    break
                await asyncio.sleep(0.01)
            assert not brain_daemon._turn_lock.locked()

    asyncio.run(scenario())


def test_queued_stream_disconnect_does_not_run_or_interrupt(monkeypatch):
    async def scenario() -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.interrupts = 0

            async def interrupt(self) -> None:
                self.interrupts += 1

        client = FakeClient()
        called = False

        async def run_turn(*args, **kwargs):
            nonlocal called
            called = True
            return {"ok": True, "say": "wrong", "turns": 1}

        monkeypatch.setattr(brain_daemon, "_run_turn", run_turn)
        brain_daemon._turn_lock = asyncio.Lock()
        await brain_daemon._turn_lock.acquire()

        async def handler(reader, writer):
            await brain_daemon._handle_stream_connection(
                client, reader, writer, auth_token="secret"
            )

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                (
                    json.dumps(
                        {
                            "type": "turn",
                            "request_id": "queued-disconnect",
                            "auth": "secret",
                            "text": "hello",
                            "stream": True,
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            await asyncio.sleep(0.02)
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.05)
            assert client.interrupts == 0
            assert not called
            brain_daemon._turn_lock.release()

    asyncio.run(scenario())


def test_discovery_token_file_is_user_only_on_posix(monkeypatch, tmp_path):
    if os.name == "nt":
        return
    path = tmp_path / "config" / "brain.json"
    monkeypatch.setattr(brain_daemon, "BRAIN_FILE", path)
    brain_daemon._write_discovery({"token": "secret"})
    assert json.loads(path.read_text())["token"] == "secret"
    assert path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0
