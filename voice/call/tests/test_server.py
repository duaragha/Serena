from voice.call import server
from voice.desk.greetings import GreetingAudio


def test_call_host_requires_authorization_header() -> None:
    with server.app.test_request_context("/ws/call?token=secret"):
        assert not server.request_is_authorized("secret")
    with server.app.test_request_context(
        "/ws/call", headers={"Authorization": "Bearer secret"}
    ):
        assert server.request_is_authorized("secret")


def test_browser_socket_ticket_exchange_is_secure_and_one_use(monkeypatch) -> None:
    from voice.call.browser_auth import CALL_SOCKET_COOKIE, BrowserCallTickets

    tickets = BrowserCallTickets(ttl_seconds=45)
    monkeypatch.setattr(server, "get_or_create_token", lambda: "secret")
    monkeypatch.setattr(server, "browser_call_tickets", tickets)
    client = server.app.test_client()
    response = client.post(
        "/api/call/socket-auth",
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200
    cookie = response.headers["Set-Cookie"]
    assert cookie.startswith(f"{CALL_SOCKET_COOKIE}=")
    assert "Secure" in cookie
    assert "HttpOnly" in cookie

    cookie_header = cookie.split(";", 1)[0]
    with server.app.test_request_context(
        "/ws/call",
        headers={"Cookie": cookie_header},
    ):
        assert server.call_socket_is_authorized("secret")
    with server.app.test_request_context(
        "/ws/call",
        headers={"Cookie": cookie_header},
    ):
        assert not server.call_socket_is_authorized("secret")


def test_unauthorized_payload_is_fatal() -> None:
    payload = server.unauthorized_payload()
    assert payload["code"] == "unauthorized"
    assert payload["fatal"] is True


def test_authorized_socket_passes_remote_tailnet_peer(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(server, "get_or_create_token", lambda: "secret")
    monkeypatch.setattr(
        server,
        "handle_websocket",
        lambda ws, *, runtime=None, peer_host=None: captured.update(
            ws=ws, runtime=runtime, peer_host=peer_host
        ),
    )
    runtime = object()
    monkeypatch.setattr(server, "get_default_runtime", lambda: runtime)
    socket = object()

    with server.app.test_request_context(
        "/ws/call",
        headers={"Authorization": "Bearer secret"},
        environ_base={"REMOTE_ADDR": "100.64.0.2"},
    ):
        server._serve_call_socket(socket)

    assert captured == {
        "ws": socket,
        "runtime": runtime,
        "peer_host": "100.64.0.2",
    }


def test_desk_socket_uses_dedicated_runtime(monkeypatch) -> None:
    captured = {}
    runtime = object()
    monkeypatch.setattr(server, "get_or_create_token", lambda: "secret")
    monkeypatch.setattr(server, "get_desk_runtime", lambda: runtime)
    monkeypatch.setattr(
        server,
        "handle_websocket",
        lambda ws, *, runtime=None, peer_host=None: captured.update(
            ws=ws, runtime=runtime, peer_host=peer_host
        ),
    )
    socket = object()

    with server.app.test_request_context(
        "/ws/desk",
        headers={"Authorization": "Bearer secret"},
        environ_base={"REMOTE_ADDR": "100.64.0.3"},
    ):
        server._serve_call_socket(socket, desk=True)

    assert captured == {
        "ws": socket,
        "runtime": runtime,
        "peer_host": "100.64.0.3",
    }


def test_desk_greeting_requires_auth_and_returns_pcm(monkeypatch) -> None:
    class Pool:
        def take(self):
            return GreetingAudio("greeting-1", 24_000, b"\x01\x00", "hey", 1.0)

    monkeypatch.setattr(server, "get_or_create_token", lambda: "secret")
    monkeypatch.setattr(server, "get_desk_greeting_pool", Pool)
    client = server.app.test_client()

    assert client.get("/desk/greeting").status_code == 401
    response = client.get(
        "/desk/greeting", headers={"Authorization": "Bearer secret"}
    )

    assert response.status_code == 200
    assert response.data == b"\x01\x00"
    assert response.headers["X-Serena-Greeting-Id"] == "greeting-1"
    assert response.headers["X-Serena-Sample-Rate"] == "24000"
    assert response.headers["Cache-Control"] == "no-store"


def test_desk_greeting_reports_warming_cache(monkeypatch) -> None:
    class Pool:
        def take(self):
            return None

    monkeypatch.setattr(server, "get_or_create_token", lambda: "secret")
    monkeypatch.setattr(server, "get_desk_greeting_pool", Pool)
    response = server.app.test_client().get(
        "/desk/greeting", headers={"Authorization": "Bearer secret"}
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
