import json
import os
import stat
import subprocess
import threading
from pathlib import Path


def test_mobile_bootstrap_does_not_disclose_bearer_token():
    from ui.web import app

    response = app.test_client().get("/app")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "window.SERENA_BOOT" in html
    assert "'/ws/chat'" in html
    assert "token:" not in html


def test_mobile_static_route_does_not_escape_dist():
    from ui.web import app, serve_mobile_app

    with app.test_request_context("/app/../package.json"):
        response = serve_mobile_app("../package.json")
    html = response.get_data(as_text=True)
    assert "window.SERENA_BOOT" in html


def test_call_websocket_rejects_query_token(monkeypatch):
    from ui import web

    with web.app.test_request_context("/ws/call?token=secret"):
        assert not web._call_websocket_is_authorized("secret")


def test_call_websocket_accepts_authorization_header(monkeypatch):
    from ui import web

    with web.app.test_request_context(
        "/ws/call", headers={"Authorization": "Bearer secret"}
    ):
        assert web._call_websocket_is_authorized("secret")


def test_browser_call_auth_issues_secure_one_use_cookie(monkeypatch):
    from core import chat_daemon
    from ui import web
    from voice.call.browser_auth import CALL_SOCKET_COOKIE, BrowserCallTickets

    tickets = BrowserCallTickets(ttl_seconds=45)
    monkeypatch.setattr(chat_daemon, "get_or_create_token", lambda: "secret")
    monkeypatch.setattr(web, "browser_call_tickets", tickets)
    client = web.app.test_client()

    rejected = client.post("/api/call/socket-auth")
    assert rejected.status_code == 401

    response = client.post(
        "/api/call/socket-auth",
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    cookie = response.headers["Set-Cookie"]
    assert cookie.startswith(f"{CALL_SOCKET_COOKIE}=")
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Path=/ws/call" in cookie

    cookie_header = cookie.split(";", 1)[0]
    with web.app.test_request_context(
        "/ws/call",
        headers={"Cookie": cookie_header},
    ):
        assert web._call_websocket_is_authorized("secret")
    with web.app.test_request_context(
        "/ws/call",
        headers={"Cookie": cookie_header},
    ):
        assert not web._call_websocket_is_authorized("secret")


def test_call_websocket_passes_remote_tailnet_peer(monkeypatch):
    from core import chat_daemon
    from ui import web

    captured = {}
    monkeypatch.setattr(chat_daemon, "get_or_create_token", lambda: "secret")
    monkeypatch.setattr(
        web,
        "handle_websocket",
        lambda ws, *, peer_host=None: captured.update(ws=ws, peer_host=peer_host),
    )
    socket = object()

    with web.app.test_request_context(
        "/ws/call",
        headers={"Authorization": "Bearer secret"},
        environ_base={"REMOTE_ADDR": "100.64.0.2"},
    ):
        web._serve_call_websocket(socket)

    assert captured == {"ws": socket, "peer_host": "100.64.0.2"}


def test_desk_websocket_uses_dedicated_runtime(monkeypatch):
    from core import chat_daemon
    from ui import web

    captured = {}
    runtime = object()
    monkeypatch.setattr(chat_daemon, "get_or_create_token", lambda: "secret")
    monkeypatch.setattr(web, "get_desk_runtime", lambda: runtime)
    monkeypatch.setattr(
        web,
        "handle_websocket",
        lambda ws, *, runtime=None, peer_host=None: captured.update(
            ws=ws,
            runtime=runtime,
            peer_host=peer_host,
        ),
    )
    socket = object()

    with web.app.test_request_context(
        "/ws/desk",
        headers={"Authorization": "Bearer secret"},
        environ_base={"REMOTE_ADDR": "100.64.0.3"},
    ):
        web._serve_call_websocket(socket, desk=True)

    assert captured == {
        "ws": socket,
        "runtime": runtime,
        "peer_host": "100.64.0.3",
    }


def test_desk_greeting_route_is_authenticated_and_returns_pcm(monkeypatch):
    from core import chat_daemon
    from ui import web
    from voice.desk.greetings import GreetingAudio

    class Pool:
        def take(self):
            return GreetingAudio("greeting-1", 24_000, b"\x01\x00", "hey", 1.0)

    monkeypatch.setattr(chat_daemon, "get_or_create_token", lambda: "secret")
    monkeypatch.setattr(web, "_get_desk_greeting_pool", Pool)
    client = web.app.test_client()

    assert client.get("/desk/greeting").status_code == 401
    response = client.get(
        "/desk/greeting", headers={"Authorization": "Bearer secret"}
    )

    assert response.status_code == 200
    assert response.data == b"\x01\x00"
    assert response.headers["X-Serena-Greeting-Id"] == "greeting-1"
    assert response.headers["X-Serena-Sample-Rate"] == "24000"
    assert response.headers["Cache-Control"] == "no-store"


def test_frontdoor_rejects_non_loopback_callers(monkeypatch):
    from ui import web

    called = False

    def turn(_history):
        nonlocal called
        called = True
        return {"ok": True, "say": "wrong", "spawn": None}

    monkeypatch.setattr("core.frontdoor.turn", turn)
    response = web.app.test_client().post(
        "/api/frontdoor",
        json={"history": [{"role": "user", "text": "read secrets"}]},
        environ_base={"REMOTE_ADDR": "100.64.0.2"},
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == "front door is local-only"
    assert not called


def test_frontdoor_allows_loopback_callers(monkeypatch):
    from ui import web

    monkeypatch.setattr(
        "core.frontdoor.turn",
        lambda history: {"ok": True, "say": history[-1]["text"], "spawn": None},
    )
    response = web.app.test_client().post(
        "/api/frontdoor",
        json={"history": [{"role": "user", "text": "hello"}]},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert response.get_json()["say"] == "hello"


def test_frontdoor_streams_ndjson_to_loopback_callers(monkeypatch):
    from ui import web

    observed = []

    def stream_turn(history):
        observed.append(history)
        yield {"type": "start"}
        yield {"type": "delta", "delta": "hel"}
        yield {"type": "delta", "delta": "lo"}
        yield {"type": "done", "say": "hello", "spawn": None, "meta": {}}

    monkeypatch.setattr("core.frontdoor.stream_turn", stream_turn)
    response = web.app.test_client().post(
        "/api/frontdoor",
        json={
            "history": [{"role": "user", "text": "hello"}],
            "stream": True,
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        buffered=True,
    )

    assert response.status_code == 200
    assert response.mimetype == "application/x-ndjson"
    assert response.headers["Cache-Control"] == "no-store"
    events = [json.loads(line) for line in response.get_data().splitlines()]
    assert [event["type"] for event in events] == [
        "start",
        "delta",
        "delta",
        "done",
    ]
    assert observed == [[{"role": "user", "text": "hello"}]]


def test_frontdoor_releases_stream_before_turn_finishes(monkeypatch):
    from ui import web

    release = threading.Event()

    def stream_turn(_history):
        yield {"type": "start"}
        if not release.wait(timeout=1.0):
            raise AssertionError("front-door response was buffered")
        yield {"type": "delta", "delta": "hello"}
        yield {"type": "done", "say": "hello", "spawn": None, "meta": {}}

    monkeypatch.setattr("core.frontdoor.stream_turn", stream_turn)
    response = web.app.test_client().post(
        "/api/frontdoor",
        json={"history": [{"role": "user", "text": "hello"}], "stream": True},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        buffered=False,
    )

    first = next(response.response)
    assert json.loads(first)["type"] == "start"
    release.set()
    rest = b"".join(response.response)
    assert [json.loads(line)["type"] for line in rest.splitlines()] == [
        "delta",
        "done",
    ]


def test_frontdoor_client_close_closes_stream_generator(monkeypatch):
    from ui import web

    closed = False

    def stream_turn(_history):
        nonlocal closed
        try:
            yield {"type": "start"}
            yield {"type": "delta", "delta": "never consumed"}
        finally:
            closed = True

    monkeypatch.setattr("core.frontdoor.stream_turn", stream_turn)
    response = web.app.test_client().post(
        "/api/frontdoor",
        json={"history": [{"role": "user", "text": "hello"}], "stream": True},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        buffered=False,
    )

    assert json.loads(next(response.response))["type"] == "start"
    response.close()

    assert closed is True


def test_frontdoor_rejects_non_object_history_entry(monkeypatch):
    from ui import web

    called = False

    def stream_turn(_history):
        nonlocal called
        called = True
        yield {"type": "done", "say": "wrong", "spawn": None}

    monkeypatch.setattr("core.frontdoor.stream_turn", stream_turn)
    response = web.app.test_client().post(
        "/api/frontdoor",
        json={"history": ["not-an-object"], "stream": True},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "history entries must be objects"
    assert called is False


def test_frontdoor_bounds_history_count_and_text():
    from ui import web

    client = web.app.test_client()
    too_many = client.post(
        "/api/frontdoor",
        json={"history": [{"role": "user", "text": "x"}] * 65},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    too_large = client.post(
        "/api/frontdoor",
        json={"history": [{"role": "user", "text": "x" * 32769}]},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert too_many.status_code == 413
    assert too_many.get_json()["error"] == "history is too long"
    assert too_large.status_code == 413
    assert too_large.get_json()["error"] == "history text is too large"


def test_mobile_claude_context_uses_a_private_prompt_file(
    monkeypatch, tmp_path: Path
):
    from core import chat_daemon

    marker = "private-mobile-serena-context"
    prompt_path = tmp_path / "prompts" / "turn.md"
    monkeypatch.setattr(chat_daemon, "_injected_context", lambda: marker)

    command = chat_daemon._agent_command(
        "claude",
        "00000000-0000-0000-0000-000000000000",
        prompt_path=prompt_path,
    )

    assert "--append-system-prompt" not in command
    flag = command.index("--append-system-prompt-file")
    assert command[flag + 1] == str(prompt_path.resolve())
    assert all(marker not in argument for argument in command)
    assert prompt_path.read_text(encoding="utf-8") == marker + "\n"
    if os.name != "nt":
        assert stat.S_IMODE(prompt_path.stat().st_mode) == 0o600


def test_mobile_agent_turns_and_context_stay_out_of_argv(
    monkeypatch, tmp_path: Path
):
    from core import chat_daemon

    private_context = "private-system-context"
    private_turn = "private user turn"
    captured: dict[str, object] = {}

    class Input:
        value = ""
        closed = False

        def write(self, value: str) -> None:
            self.value += value

        def close(self) -> None:
            self.closed = True

    class Process:
        def __init__(self, command, **kwargs) -> None:
            captured["command"] = command
            captured["kwargs"] = kwargs
            prompt_flag = command.index("--append-system-prompt-file")
            prompt_path = Path(command[prompt_flag + 1])
            captured["prompt"] = prompt_path.read_text(encoding="utf-8")
            captured["prompt_mode"] = stat.S_IMODE(prompt_path.stat().st_mode)
            self.stdin = Input()
            captured["stdin"] = self.stdin
            self.stdout = iter(())

        def wait(self, timeout: int) -> int:
            captured["timeout"] = timeout
            return 0

    monkeypatch.setattr(chat_daemon, "_injected_context", lambda: private_context)
    monkeypatch.setattr(chat_daemon, "_orig_session_file", lambda _sid: None)
    monkeypatch.setattr(chat_daemon, "_canonicalize_session", lambda _sid: None)
    monkeypatch.setattr(chat_daemon, "CHAT_PROMPT_DIR", tmp_path / "prompts")
    monkeypatch.setattr(chat_daemon.subprocess, "Popen", Process)
    events: list[dict] = []

    chat_daemon.run_agent(
        "00000000-0000-0000-0000-000000000000",
        "claude",
        private_turn,
        None,
        events.append,
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert private_context not in command
    assert private_turn not in command
    assert captured["prompt"] == private_context + "\n"
    if os.name != "nt":
        assert captured["prompt_mode"] == 0o600
    process_kwargs = captured["kwargs"]
    assert isinstance(process_kwargs, dict)
    assert process_kwargs["stdin"] == subprocess.PIPE
    process_input = captured["stdin"]
    assert isinstance(process_input, Input)
    assert process_input.value == private_turn
    assert process_input.closed is True
    assert not any((tmp_path / "prompts").iterdir())
    assert not any(event.get("type") == "error" for event in events)


def test_mobile_codex_turn_uses_stdin_marker() -> None:
    from core import chat_daemon

    command = chat_daemon._agent_command(
        "codex",
        "00000000-0000-0000-0000-000000000000",
    )

    assert command[-1] == "-"


def test_mobile_private_prompt_is_removed_when_spawn_fails(
    monkeypatch, tmp_path: Path
) -> None:
    from core import chat_daemon

    monkeypatch.setattr(chat_daemon, "_injected_context", lambda: "private")
    monkeypatch.setattr(chat_daemon, "_orig_session_file", lambda _sid: None)
    monkeypatch.setattr(chat_daemon, "_canonicalize_session", lambda _sid: None)
    monkeypatch.setattr(chat_daemon, "CHAT_PROMPT_DIR", tmp_path / "prompts")

    def fail_spawn(*_args, **_kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(chat_daemon.subprocess, "Popen", fail_spawn)
    events: list[dict] = []

    chat_daemon.run_agent(
        "00000000-0000-0000-0000-000000000000",
        "claude",
        "private turn",
        None,
        events.append,
    )

    assert not any((tmp_path / "prompts").iterdir())
    assert any(
        event.get("type") == "error" and "spawn failed" in event.get("message", "")
        for event in events
    )
