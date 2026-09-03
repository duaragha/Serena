from __future__ import annotations

from ui import web


def test_page_reload_detaches_terminals_instead_of_killing_them() -> None:
    html = web.HTML
    start = html.index("function detachLiveTerminalsForReload()")
    end = html.index("window.addEventListener('beforeunload'", start)
    body = html[start:end]

    assert "s.giveUp = true" in body
    assert "s.ws.close()" in body
    assert "/api/kill-terminal/" not in body
    assert (
        "window.addEventListener('beforeunload', detachLiveTerminalsForReload)"
        in html
    )
    assert (
        "window.addEventListener('beforeunload', () => teardownLiveTerminal())"
        not in html
    )


def test_explicit_terminal_close_still_uses_the_kill_endpoint() -> None:
    html = web.HTML
    start = html.index("function teardownLiveTerminal(sid)")
    end = html.index("function detachLiveTerminalsForReload()", start)

    assert "/api/kill-terminal/" in html[start:end]
