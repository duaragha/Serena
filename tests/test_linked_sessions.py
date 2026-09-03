from core import linked_sessions


class _Instance:
    def __init__(self, split_sids=(), split_active=False):
        self._split_sids = split_sids
        self._split_active = split_active


def _install_group(monkeypatch, sessions, members):
    from core import indexer, metadata

    monkeypatch.setattr(metadata, "get_group", lambda _sid: "g_test")
    monkeypatch.setattr(metadata, "list_group_members", lambda _gid: members)
    monkeypatch.setattr(metadata, "get_meta", lambda _sid: {})
    monkeypatch.setattr(metadata, "external_runtime_active", lambda _sid: False)
    monkeypatch.setattr(indexer, "get_session", lambda sid: sessions.get(sid))


def test_visible_split_target_wins_over_newer_group_member(monkeypatch):
    from desktop.app_gtk import ChatsApp

    source = "claude-source"
    visible = "codex-visible"
    newer = "codex-newer"
    _install_group(
        monkeypatch,
        {
            visible: {"agent": "codex", "last_timestamp": "2026-07-20T10:00:00"},
            newer: {"agent": "codex", "last_timestamp": "2026-07-21T10:00:00"},
        },
        [source, newer, visible],
    )
    monkeypatch.setattr(
        ChatsApp,
        "INSTANCE",
        _Instance((source, visible), split_active=True),
    )

    assert linked_sessions.find_linked_session(source, "codex") == visible


def test_newest_matching_session_is_deterministic_fallback(monkeypatch):
    from desktop.app_gtk import ChatsApp

    source = "claude-source"
    older = "codex-older"
    newer = "codex-newer"
    _install_group(
        monkeypatch,
        {
            older: {"agent": "codex", "last_timestamp": "2026-07-20T10:00:00"},
            newer: {"agent": "codex", "last_timestamp": "2026-07-21T10:00:00"},
        },
        [source, older, newer],
    )
    monkeypatch.setattr(ChatsApp, "INSTANCE", _Instance())

    assert linked_sessions.find_linked_session(source, "codex") == newer


def test_unrelated_open_split_is_not_used(monkeypatch):
    from desktop.app_gtk import ChatsApp

    source = "claude-source"
    older = "codex-older"
    newer = "codex-newer"
    _install_group(
        monkeypatch,
        {
            older: {"agent": "codex", "last_timestamp": "2026-07-20T10:00:00"},
            newer: {"agent": "codex", "last_timestamp": "2026-07-21T10:00:00"},
        },
        [source, older, newer],
    )
    monkeypatch.setattr(
        ChatsApp,
        "INSTANCE",
        _Instance(("another-claude", older), split_active=True),
    )

    assert linked_sessions.find_linked_session(source, "codex") == newer


def test_fallback_match_supports_session_not_yet_indexed(monkeypatch):
    from desktop.app_gtk import ChatsApp

    source = "codex-source"
    pending = "claude-pending"
    _install_group(monkeypatch, {}, [source, pending])
    monkeypatch.setattr(ChatsApp, "INSTANCE", _Instance())

    assert linked_sessions.find_linked_session(
        source,
        "claude",
        fallback_match=lambda sid: sid == pending,
    ) == pending


def test_running_external_agent_is_not_selected_as_live_sibling(monkeypatch):
    from core import metadata
    from desktop.app_gtk import ChatsApp

    source = "claude-source"
    interactive = "codex-interactive"
    workflow = "codex-workflow"
    _install_group(
        monkeypatch,
        {
            interactive: {"agent": "codex", "last_timestamp": "2026-07-20T10:00:00"},
            workflow: {"agent": "codex", "last_timestamp": "2026-07-21T10:00:00"},
        },
        [source, interactive, workflow],
    )
    monkeypatch.setattr(
        metadata,
        "external_runtime_active",
        lambda sid: sid == workflow,
    )
    monkeypatch.setattr(ChatsApp, "INSTANCE", _Instance())

    assert linked_sessions.find_linked_session(source, "codex") == interactive


def test_finished_fleet_worker_never_replaces_original_live_sibling(monkeypatch):
    from core import metadata
    from desktop.app_gtk import ChatsApp

    source = "claude-source"
    interactive = "codex-original"
    fleet_worker = "codex-fleet-newer"
    _install_group(
        monkeypatch,
        {
            interactive: {"agent": "codex", "last_timestamp": "2026-07-20T10:00:00"},
            fleet_worker: {"agent": "codex", "last_timestamp": "2026-07-22T10:00:00"},
        },
        [source, interactive, fleet_worker],
    )
    monkeypatch.setattr(
        metadata,
        "get_meta",
        lambda sid: {"fleet_worker": {"run_id": "fleet-test"}} if sid == fleet_worker else {},
    )
    monkeypatch.setattr(ChatsApp, "INSTANCE", _Instance())

    assert linked_sessions.find_linked_session(source, "codex") == interactive
