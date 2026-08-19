"""Safe session selection for Serena's spoken coding work."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from core.work_session_router import (
    choose_work_route,
    discover_active_codex_sessions,
    discover_runtime_ports,
    discover_work_route,
    parse_route_preference,
)

PROJECT = "/projects/serena"


def _session(sid: str, **overrides) -> dict:
    record = {
        "session_id": sid,
        "agent": "codex",
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
        "work_project_root": PROJECT,
        "last_timestamp": "2026-08-03T12:00:00Z",
        "title": f"chat {sid}",
    }
    record.update(overrides)
    return record


def _context(focus_sid: str, *members: dict, **overrides) -> dict:
    record = {
        "bridge_port": 46747,
        "window_active": True,
        "focused_at": 10,
        "focused_session_id": focus_sid,
        "sessions": list(members),
    }
    record.update(overrides)
    return record


def _git_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("start a fresh coding chat for this", "new"),
        ("do not reuse the current chat", "new"),
        ("never use this chat, make a new chat", "new"),
        ("continue using this chat", "existing"),
        ("don't create a new chat, use the current chat", "existing"),
        ("use the currently open chat", "existing"),
        ("fix Serena's routing", "auto"),
    ],
)
def test_spoken_preference_requires_an_explicit_chat_instruction(
    spoken: str,
    expected: str,
) -> None:
    """Ordinary coding words must not be mistaken for routing commands."""

    assert parse_route_preference(spoken) == expected


def test_explicit_new_chat_never_reuses_the_focused_candidate() -> None:
    """A direct new-chat request must override even a perfect focused match."""

    focused = _session("focused")
    route = choose_work_route(
        PROJECT,
        "open a new chat and fix it",
        [_context("focused", focused)],
        [focused],
    )

    assert route.mode == "private"
    assert route.preference == "new"
    assert route.session_id == ""


def test_explicit_existing_without_a_safe_candidate_refuses() -> None:
    """Serena must not silently create a chat after promising to reuse one."""

    route = choose_work_route(
        PROJECT,
        "continue in the existing chat",
        [],
        [],
    )

    assert route.mode == "refused"
    assert route.preference == "existing"


def test_auto_without_a_safe_candidate_falls_back_to_private_work() -> None:
    """Missing live context must preserve the existing private worker path."""

    route = choose_work_route(PROJECT, "fix the bug", [], [])

    assert route.mode == "private"
    assert route.preference == "auto"


def test_focused_exact_project_sol_chat_wins_over_newer_history() -> None:
    """Recent history must not displace the exact chat Raghav is viewing."""

    focused = _session("focused", last_timestamp="2026-08-01T12:00:00Z")
    newer = _session("newer", last_timestamp="2026-08-03T12:00:00Z")
    route = choose_work_route(
        PROJECT,
        "fix the bug",
        [_context("focused", focused)],
        [focused, newer],
    )

    assert route.mode == "reuse"
    assert route.session_id == "focused"
    assert route.bridge_port == 46747
    assert route.bound_focus is False


def test_runtime_endpoint_shape_routes_the_focused_native_chat() -> None:
    """The live API field names must not silently disable focus-based reuse."""

    focused = _session("focused")
    context = {
        "port": 46747,
        "window_active": True,
        "focused_at": 10,
        "focused_sid": "focused",
        "split_pair": [],
        "runtimes": [focused],
    }

    route = choose_work_route(PROJECT, "fix the bug", [context], [focused])

    assert route.mode == "reuse"
    assert route.session_id == "focused"
    assert route.bridge_port == 46747


def test_focused_unbound_chat_can_be_bound_to_the_resolved_project() -> None:
    """A focused home or cache chat must remain usable once bound exactly."""

    focused = _session(
        "focused",
        work_project_root="",
        canonical_project_root="",
        cwd="/home/raghav",
    )
    route = choose_work_route(
        PROJECT,
        "fix the bug",
        [_context("focused", focused)],
        [focused],
    )

    assert route.mode == "reuse"
    assert route.session_id == "focused"
    assert route.bound_focus is True


def test_unbound_focus_does_not_displace_exact_indexed_project_history() -> None:
    """An unrelated home pane must not steal work from the durable project chat."""

    focused = _session(
        "focused",
        work_project_root="",
        canonical_project_root="",
        cwd="/home/raghav",
    )
    historical = _session("historical", last_timestamp="2026-08-01T12:00:00Z")

    route = choose_work_route(
        PROJECT,
        "fix yourself",
        [_context("focused", focused)],
        [focused, historical],
    )

    assert route.mode == "reuse"
    assert route.session_id == "historical"
    assert route.bridge_port is None


def test_focused_chat_bound_to_another_repository_is_not_hijacked() -> None:
    """Focus alone must never retarget a chat already owned by another repo."""

    focused = _session("focused", work_project_root="/projects/locket")
    route = choose_work_route(
        PROJECT,
        "fix the bug",
        [_context("focused", focused)],
        [focused],
    )

    assert route.mode == "private"
    assert route.session_id == ""


def test_focused_claude_routes_only_to_its_exact_split_codex_partner() -> None:
    """A linked group's unrelated Codex members must not become candidates."""

    claude = {
        "session_id": "claude-focus",
        "agent": "claude",
        "group_id": "polluted-group",
    }
    split_codex = _session("split-codex", group_id="polluted-group")
    unrelated = _session(
        "unrelated-codex",
        group_id="polluted-group",
        last_timestamp="2026-08-03T13:00:00Z",
    )
    context = _context(
        "claude-focus",
        claude,
        split_codex,
        unrelated,
        split_session_ids=["claude-focus", "split-codex"],
    )

    route = choose_work_route(
        PROJECT,
        "fix the bug",
        [context],
        [claude, split_codex, unrelated],
    )

    assert route.mode == "reuse"
    assert route.session_id == "split-codex"


def test_group_membership_alone_never_supplies_project_identity() -> None:
    """A polluted linked group must not make an unbound history row reusable."""

    exact = _session("exact", group_id="polluted-group")
    unbound = _session(
        "unbound",
        group_id="polluted-group",
        work_project_root="",
        canonical_project_root="",
        last_timestamp="2026-08-03T13:00:00Z",
    )
    bridge = _context("", exact, unbound, window_active=False)

    route = choose_work_route(PROJECT, "fix it", [bridge], [exact, unbound])

    assert route.mode == "reuse"
    assert route.session_id == "exact"


@pytest.mark.parametrize("effort", ["high", "xhigh", "max", "ultra"])
def test_all_allowed_sol_reasoning_efforts_can_be_reused(effort: str) -> None:
    """Valid high-effort Sol chats must not be rejected by a stale allowlist."""

    focused = _session("focused", effort=effort)

    route = choose_work_route(
        PROJECT,
        "fix it",
        [_context("focused", focused)],
        [focused],
    )

    assert route.mode == "reuse"
    assert route.effort == effort


@pytest.mark.parametrize(
    "overrides",
    [
        {"agent": "claude"},
        {"agent": "serena-voice"},
        {"fleet_worker": {"run_id": "fleet-1"}},
        {"external_runtime_active": True},
        {"done": True},
        {"busy": True},
        {"draft": "unsent words"},
        {"reserved": True},
        {"model": "gpt-5.6-terra"},
        {"effort": "medium"},
    ],
)
def test_unsafe_focused_sessions_are_never_reused(overrides: dict) -> None:
    """Owned, read-only, incompatible, and in-flight chats must stay untouched."""

    focused = _session("focused", **overrides)

    route = choose_work_route(
        PROJECT,
        "fix it",
        [_context("focused", focused)],
        [focused],
    )

    assert route.mode == "private"
    assert route.session_id == ""


def test_newest_exact_project_indexed_chat_is_the_history_fallback() -> None:
    """Auto routing should reuse the latest exact project chat when focus is absent."""

    old = _session("old", last_timestamp="2026-08-01T12:00:00Z")
    new = _session("new", last_timestamp="2026-08-03T12:00:00Z")
    bridge = _context("", old, new, window_active=False)

    route = choose_work_route(PROJECT, "fix it", [bridge], [old, new])

    assert route.mode == "reuse"
    assert route.session_id == "new"


def test_closed_history_chat_is_resumed_without_an_open_terminal() -> None:
    """Closing Chats must not make the best exact-project transcript disappear."""

    closed = _session("closed", last_timestamp="2026-08-03T12:00:00Z")
    bridge = _context("", window_active=False, sessions=[])

    route = choose_work_route(PROJECT, "fix it", [bridge], [closed])

    assert route.mode == "reuse"
    assert route.session_id == "closed"
    assert route.bridge_port is None
    assert "indexed" in route.reason


def test_active_headless_owner_prevents_a_second_historical_resume() -> None:
    """A hidden Codex process must remain one owner even without a Chats pane."""

    closed = _session("closed")

    route = choose_work_route(
        PROJECT,
        "fix it",
        [],
        [closed],
        active_session_ids={"closed"},
    )

    assert route.mode == "private"
    assert route.session_id == ""


def test_indexed_first_message_can_prove_a_headless_chats_project(tmp_path) -> None:
    """A neutral cache cwd must not hide an explicit repository path in the log."""

    repo = _git_repo(tmp_path / "serena")
    cache = tmp_path / "headless-cache"
    cache.mkdir()
    session = _session(
        "headless",
        work_project_root="",
        cwd=str(cache),
        first_message=f"work end to end in {repo}/core/work_session_router.py",
    )
    bound: list[tuple[str, str]] = []

    route = discover_work_route(
        repo,
        "fix yourself",
        runtime_contexts=[],
        sessions=[session],
        active_session_ids=[],
        get_metadata=lambda _sid: {},
        get_project_binding=lambda _sid: None,
        bind_project=lambda sid, root: bound.append((sid, str(root))) or str(root),
        external_runtime_active=lambda _sid: False,
    )

    assert route.mode == "reuse"
    assert route.session_id == "headless"
    assert route.bridge_port is None
    assert bound == [("headless", str(repo))]


def test_similarly_named_project_in_a_log_is_not_an_exact_match() -> None:
    """Transcript affinity must not confuse serena-old with the Serena repo."""

    session = _session(
        "wrong-project",
        work_project_root="",
        first_message="work in /projects/serena-old",
    )

    route = choose_work_route(PROJECT, "fix it", [], [session])

    assert route.mode == "private"


def test_equally_recent_exact_project_history_is_refused_as_ambiguous() -> None:
    """A tie must not be broken by an arbitrary session id."""

    first = _session("first")
    second = _session("second")
    bridge = _context("", first, second, window_active=False)

    route = choose_work_route(PROJECT, "fix it", [bridge], [first, second])

    assert route.mode == "refused"
    assert "equally recent" in route.reason


def test_multiple_foreground_windows_are_refused_as_ambiguous() -> None:
    """Two visible focused chats must not race for one accepted voice job."""

    one = _session("one")
    two = _session("two")
    contexts = [
        _context("one", one, bridge_port=46747),
        _context("two", two, bridge_port=46748),
    ]

    route = choose_work_route(PROJECT, "fix it", contexts, [one, two])

    assert route.mode == "refused"
    assert "foreground" in route.reason


def test_discovery_binds_only_the_selected_exact_session(tmp_path) -> None:
    """Discovery must never stamp a whole linked group with one project root."""

    repo = _git_repo(tmp_path / "serena")
    selected = _session(
        "selected",
        work_project_root="",
        canonical_project_root="",
        cwd=str(tmp_path),
    )
    sibling = _session(
        "sibling",
        work_project_root="",
        canonical_project_root="",
        cwd=str(tmp_path),
    )
    context = _context(
        "selected",
        selected,
        sibling,
        split_session_ids=["selected", "sibling"],
    )
    bound: list[tuple[str, str]] = []

    route = discover_work_route(
        repo,
        "fix it",
        runtime_contexts=[context],
        sessions=[selected, sibling],
        get_metadata=lambda _sid: {},
        get_project_binding=lambda _sid: None,
        bind_project=lambda sid, root: bound.append((sid, str(root))) or str(root),
        external_runtime_active=lambda _sid: False,
    )

    assert route.mode == "reuse"
    assert route.session_id == "selected"
    assert route.bound_focus is True
    assert bound == [("selected", str(repo))]


def test_discovery_turns_a_binding_race_into_a_refusal(tmp_path) -> None:
    """A late conflicting bind must fail closed before any prompt is dispatched."""

    repo = _git_repo(tmp_path / "serena")
    selected = _session(
        "selected",
        work_project_root="",
        canonical_project_root="",
        cwd=str(tmp_path),
    )

    def refuse_bind(_sid, _root):
        raise ValueError("already bound elsewhere")

    route = discover_work_route(
        repo,
        "fix it",
        runtime_contexts=[_context("selected", selected)],
        sessions=[selected],
        get_metadata=lambda _sid: {},
        get_project_binding=lambda _sid: None,
        bind_project=refuse_bind,
        external_runtime_active=lambda _sid: False,
    )

    assert route.mode == "refused"
    assert route.session_id == ""
    assert "could not be safely bound" in route.reason


def test_discovery_reads_each_live_context_and_the_index_once(tmp_path) -> None:
    """The live wrapper must use bounded context discovery instead of guessing."""

    repo = _git_repo(tmp_path / "serena")
    selected = _session(
        "selected",
        work_project_root="",
        canonical_project_root="",
        cwd=str(tmp_path),
    )
    fetched: list[int] = []
    loaded: list[bool] = []
    bound: list[str] = []

    def fetch(port: int) -> dict:
        fetched.append(port)
        return _context("selected", selected, bridge_port=port)

    def load() -> list[dict]:
        loaded.append(True)
        return [selected]

    route = discover_work_route(
        repo,
        "fix it",
        runtime_ports=[46747],
        context_fetcher=fetch,
        session_loader=load,
        get_metadata=lambda _sid: {},
        get_project_binding=lambda _sid: None,
        bind_project=lambda sid, _root: bound.append(sid) or str(repo),
        external_runtime_active=lambda _sid: False,
    )

    assert route.mode == "reuse"
    assert fetched == [46747]
    assert loaded == [True]
    assert bound == ["selected"]


def test_fresh_focused_chat_can_prove_sol_identity_before_indexing(
    tmp_path, monkeypatch
) -> None:
    """A just-opened pane must not miss reuse only because SQLite is stale."""

    from core import codex_bridge

    repo = _git_repo(tmp_path / "serena")
    sid = "019fcaaa-1111-7222-8333-123456789abc"
    rollout_root = tmp_path / "sessions" / "2026" / "08" / "03"
    rollout_root.mkdir(parents=True)
    rollout = rollout_root / f"rollout-2026-08-03T00-00-00-{sid}.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-sol", "effort": "max"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_bridge, "CODEX_SESSIONS_ROOT", tmp_path / "sessions")
    member = {
        "sid": sid,
        "agent": "codex",
        "cwd": str(tmp_path),
        "alive": True,
        "state": "live",
        "busy": False,
        "draft": False,
        "reserved": False,
    }

    route = discover_work_route(
        repo,
        "fix it",
        runtime_contexts=[
            {
                "port": 46747,
                "focused_sid": sid,
                "runtimes": [member],
            }
        ],
        sessions=[],
        get_metadata=lambda _sid: {},
        get_project_binding=lambda _sid: None,
        bind_project=lambda _sid, root: str(root),
        external_runtime_active=lambda _sid: False,
    )

    assert route.mode == "reuse"
    assert route.session_id == sid
    assert route.bound_focus is True


def test_discovery_rejects_a_focused_cwd_inside_another_git_root(tmp_path) -> None:
    """An unlabelled pane in another repository must not be rebound by focus."""

    serena = _git_repo(tmp_path / "serena")
    locket = _git_repo(tmp_path / "locket")
    selected = _session(
        "selected",
        work_project_root="",
        canonical_project_root="",
        cwd=str(locket),
    )
    bound: list[str] = []

    route = discover_work_route(
        serena,
        "fix it",
        runtime_contexts=[_context("selected", selected)],
        sessions=[selected],
        get_metadata=lambda _sid: {},
        get_project_binding=lambda _sid: None,
        bind_project=lambda sid, _root: bound.append(sid) or str(serena),
        external_runtime_active=lambda _sid: False,
    )

    assert route.mode == "private"
    assert bound == []


def test_runtime_port_discovery_recognizes_the_gtk_desktop_process(monkeypatch) -> None:
    """A dynamic Flask port must be found when Chats starts as app_gtk.py."""

    listeners = [
        SimpleNamespace(
            status="LISTEN",
            pid=101,
            laddr=SimpleNamespace(ip="127.0.0.1", port=46747),
        ),
        SimpleNamespace(
            status="LISTEN",
            pid=202,
            laddr=SimpleNamespace(ip="127.0.0.1", port=12345),
        ),
    ]
    processes = {
        101: SimpleNamespace(
            cmdline=lambda: ["python", "/projects/serena/desktop/app_gtk.py"],
            create_time=lambda: 20,
        ),
        202: SimpleNamespace(
            cmdline=lambda: ["python", "unrelated_server.py"],
            create_time=lambda: 30,
        ),
    }
    monkeypatch.delenv("SERENA_CHATS_PORTS", raising=False)
    monkeypatch.delenv("SERENA_CHATS_PORT", raising=False)
    monkeypatch.setattr(
        "core.work_session_router.psutil.net_connections",
        lambda **_kwargs: listeners,
    )
    monkeypatch.setattr(
        "core.work_session_router.psutil.Process",
        lambda pid: processes[pid],
    )

    assert discover_runtime_ports() == [46747]


def test_runtime_port_discovery_follows_webkit_to_the_chats_parent(monkeypatch) -> None:
    """WebKit inheriting Flask's fd must not make every open pane invisible."""

    listener = SimpleNamespace(
        status="LISTEN",
        pid=101,
        laddr=SimpleNamespace(ip="127.0.0.1", port=44577),
    )
    parent = SimpleNamespace(
        cmdline=lambda: ["/projects/serena/.venv/bin/chats", "desktop"],
        create_time=lambda: 20,
        parent=lambda: None,
    )
    child = SimpleNamespace(
        cmdline=lambda: ["/usr/lib/webkit2gtk/WebKitWebProcess"],
        create_time=lambda: 21,
        parent=lambda: parent,
    )
    monkeypatch.delenv("SERENA_CHATS_PORTS", raising=False)
    monkeypatch.delenv("SERENA_CHATS_PORT", raising=False)
    monkeypatch.setattr(
        "core.work_session_router.psutil.net_connections",
        lambda **_kwargs: [listener],
    )
    monkeypatch.setattr("core.work_session_router.psutil.Process", lambda _pid: child)

    assert discover_runtime_ports() == [44577]


def test_active_codex_discovery_reads_exact_resume_ids(monkeypatch) -> None:
    """Only provider resume argv may block a closed transcript as process-owned."""

    sid = "019fc3d6-1e4f-7b32-9f29-5e0115a20bd3"
    processes = [
        SimpleNamespace(
            info={"cmdline": ["node", "/opt/bin/codex", "resume", sid]},
            cmdline=lambda: [],
        ),
        SimpleNamespace(
            info={"cmdline": ["claude", "--resume", sid]},
            cmdline=lambda: [],
        ),
    ]
    monkeypatch.setattr(
        "core.work_session_router.psutil.process_iter",
        lambda _attrs: processes,
    )

    assert discover_active_codex_sessions() == {sid}
