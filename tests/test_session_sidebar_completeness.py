from __future__ import annotations

from ui import web


def _session(number: int) -> dict:
    return {
        "session_id": f"session-{number}",
        "project_dir": "-home-raghav",
        "last_timestamp": f"2026-01-{(number % 28) + 1:02d}T12:00:00+00:00",
    }


def test_sidebar_does_not_truncate_chat_history_at_500(monkeypatch) -> None:
    sessions = [_session(number) for number in range(750)]
    observed_limits: list[int] = []

    def fake_list_sessions(*, limit: int, **_filters) -> list[dict]:
        observed_limits.append(limit)
        return sessions[:limit]

    monkeypatch.setattr(web, "list_sessions", fake_list_sessions)
    monkeypatch.setattr(web, "_decorate_sessions", lambda rows: rows)
    monkeypatch.setattr(web, "_include_permanent_serena_session", lambda rows: rows)

    response = web.app.test_client().get("/api/sessions")

    assert response.status_code == 200
    assert len(response.get_json()) == 750
    assert observed_limits == [100_000]


def test_project_sidebar_merges_all_sessions_without_the_old_cap(monkeypatch) -> None:
    first = [_session(number) for number in range(400)]
    second = [_session(number) for number in range(350, 750)]
    by_project = {"first": first, "second": second}
    observed_limits: list[int] = []

    def fake_list_sessions(*, project: str, limit: int) -> list[dict]:
        observed_limits.append(limit)
        return by_project[project][:limit]

    monkeypatch.setattr(web, "list_sessions", fake_list_sessions)
    monkeypatch.setattr(web, "_decorate_sessions", lambda rows: rows)
    monkeypatch.setattr(web, "_include_permanent_serena_session", lambda rows: rows)

    response = web.app.test_client().get("/api/sessions?projects=first,second")

    assert response.status_code == 200
    assert len(response.get_json()) == 750
    assert observed_limits == [100_000, 100_000]


def test_hot_ui_can_expand_a_stale_backend_without_restarting_live_ptys() -> None:
    assert "async function _fetchSidebarSessions(params, dirs)" in web.HTML
    assert "if ((dirs && dirs.length) || primary.length !== 500) return primary" in web.HTML
    assert "fetch('/api/search?q=a')" in web.HTML
    assert "delete clean.search_snippet" in web.HTML
    assert "allSessions = await _fetchSidebarSessions(params, dirs)" in web.HTML
