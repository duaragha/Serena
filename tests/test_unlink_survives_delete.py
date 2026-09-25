"""A chat removed from a linked thread stays out of it.

Regression for 2026-09-25: he removed one agent from a thread, tried to delete
it, and it merged straight back. The delete was refused (the chat was still
running), the sidebar refreshed the index, and the custom-title repair saw the
removed chat still carrying the thread's name and re-linked it. The sequence
here is the real one: the unlink and delete routes, a refused delete, then full
index refreshes through the real update_index.
"""

from pathlib import Path

import pytest

from core import indexer
from core import metadata as meta
from core.parser import SessionMeta
from core.workspace_lease import SessionLease
from tests.test_workspace_browser import workspace  # noqa: F401

PROJECT = "-home-raghav-Documents-Projects-serena-chats"
CWD = "/home/raghav/Documents/Projects/serena/chats"
SIDS = {
    "claude": "979a60b0-e0e6-4d71-874f-72627f9c4c4e",
    "codex": "019de3ff-997e-76e3-9e51-2eeff93b0318",
    "gemini": "0ecc2072-ef74-43fb-9130-9d7db3463317",
    "muse": "01a0b756-6fb9-7980-8418-0f484e1cc198",
}
TITLE = "Unified Changes"


@pytest.fixture
def thread(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(indexer, "DB_PATH", tmp_path / "data" / "index.db")
    monkeypatch.setattr(indexer, "_INDEX_LOCK_PATH", tmp_path / "index.lock")
    monkeypatch.setattr(meta, "METADATA_DIR", tmp_path / "meta")
    monkeypatch.setattr(meta, "METADATA_PATH", tmp_path / "metadata.json")
    monkeypatch.setenv("SERENA_RUNTIME_LEASE_DIR", str(tmp_path / "leases"))
    (tmp_path / "data").mkdir()
    files = {
        "claude": tmp_path / "claude" / PROJECT / f"{SIDS['claude']}.jsonl",
        "codex": tmp_path / "codex" / f"rollout-2026-09-25T10-00-00-{SIDS['codex']}.jsonl",
        "gemini": tmp_path / "gemini" / f"{SIDS['gemini']}.db",
        "muse": tmp_path / "muse" / "2026" / "09" / "18" / SIDS["muse"] / "session.jsonl",
    }
    for path in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")

    def parsed(agent):
        def parse(path, *_args):
            stat = path.stat()
            return SessionMeta(
                session_id=SIDS[agent], project_dir=PROJECT, cwd=CWD, last_cwd=CWD,
                first_message=f"{agent} picks up the handoff", message_count=2,
                raw_message_count=2, file_path=str(path), file_size=stat.st_size,
                file_mtime=stat.st_mtime,
            )
        return parse

    def present(agent, label):
        return lambda: [(label, files[agent])] if files[agent].exists() else []

    monkeypatch.setattr(indexer, "scan_sessions", present("claude", PROJECT))
    monkeypatch.setattr(indexer, "scan_codex_sessions", present("codex", PROJECT))
    monkeypatch.setattr(indexer, "scan_gemini_sessions", present("gemini", "gemini"))
    monkeypatch.setattr(indexer, "scan_muse_sessions", present("muse", "muse"))
    monkeypatch.setattr(indexer, "scan_locket_sessions", lambda: [])
    monkeypatch.setattr(indexer, "scan_voice_sessions", lambda: [])
    monkeypatch.setattr(indexer, "parse_metadata", parsed("claude"))
    monkeypatch.setattr(indexer, "parse_codex_metadata", parsed("codex"))
    monkeypatch.setattr(indexer, "parse_gemini_metadata", parsed("gemini"))
    monkeypatch.setattr(indexer, "parse_muse_metadata", parsed("muse"))
    monkeypatch.setattr(indexer, "_attribute_codex_parents", lambda _conn: None)

    for sid in SIDS.values():
        meta.set_custom_title(sid, TITLE)
    group = meta.link_sessions(list(SIDS.values()))
    indexer.update_index()
    assert {sid: meta.get_group(sid) for sid in SIDS.values()} == dict.fromkeys(SIDS.values(), group)
    return group


def _post(client, path, **kwargs):
    return client.open(path, base_url="http://127.0.0.1:46747",
                       environ_base={"REMOTE_ADDR": "127.0.0.1"}, **kwargs)


@pytest.mark.parametrize("agent", sorted(SIDS))
def test_removed_chat_stays_out_through_a_refused_delete_and_refreshes(thread, agent):
    from ui import web

    client = web.app.test_client()
    victim = SIDS[agent]
    others = [sid for sid in SIDS.values() if sid != victim]

    assert _post(client, "/api/group/unlink", method="POST", json={"session_id": victim}).status_code == 200
    # Still running: the delete is refused, and the sidebar refreshes the index.
    owner = SessionLease(victim)
    try:
        assert _post(client, "/api/session/" + victim, method="DELETE").status_code == 409
        indexer.update_index()
        indexer.update_index(force=True)
    finally:
        owner.release()

    assert meta.get_group(victim) is None
    assert sorted(meta.list_group_members(thread)) == sorted(others)
    assert indexer.get_session(victim)["custom_title"] == TITLE

    # Once it can be deleted, the thread it left is untouched.
    assert _post(client, "/api/session/" + victim, method="DELETE").status_code == 200
    indexer.update_index()
    assert indexer.get_session(victim) is None
    assert sorted(meta.list_group_members(thread)) == sorted(others)


def test_removing_down_to_one_member_leaves_both_unlinked_through_refreshes(thread):
    from ui import web

    client = web.app.test_client()
    first, second, third, fourth = SIDS.values()
    for sid in (first, second, third):
        assert _post(client, "/api/group/unlink", method="POST", json={"session_id": sid}).status_code == 200
    indexer.update_index(force=True)
    assert [meta.get_group(sid) for sid in SIDS.values()] == [None] * 4


def test_an_explicit_link_still_brings_a_removed_chat_back(thread):
    from ui import web

    client = web.app.test_client()
    victim, keeper = SIDS["muse"], SIDS["claude"]
    _post(client, "/api/group/unlink", method="POST", json={"session_id": victim})
    indexer.update_index()
    response = _post(client, "/api/group/link", method="POST", json={"session_ids": [keeper, victim]})
    assert response.status_code == 200
    indexer.update_index()
    assert meta.get_group(victim) == thread


@pytest.mark.parametrize("removed", ["pending", "member", "disband", "front-door"])
def test_a_pending_handoff_never_relinks_a_chat_he_removed(workspace, removed):
    """The live half: a chat still starting links its thread when it resolves."""
    page, calls, errors, rows = workspace
    src, other = rows[0]["session_id"], rows[1]["session_id"]
    links = []

    def link(route):
        links.append(sorted(route.request.post_data_json["session_ids"]))
        route.fulfill(json={"ok": True, "group_id": "linked"})

    page.route("**/api/group/link", link)
    page.route("**/api/group/unlink", lambda route: route.fulfill(json={"ok": True}))
    page.route("**/api/group/disband", lambda route: route.fulfill(json={"ok": True}))
    page.evaluate("""({src, other, removed}) => {
      _pseudoSessions.unshift({
        session_id: 'pending-muse', agent: 'muse', isPseudo: true, cwd: '/project/serena',
        first_timestamp: new Date().toISOString(), group: 'linked',
        pending_group_link_with: src, pending_group_member_sids: [src, other],
        fd_pair_id: removed === 'front-door' ? 'pair' : undefined,
      });
      if (removed === 'front-door') _fdPairResolved.pair = [other];
    }""", {"src": src, "other": other, "removed": removed})
    if removed == "pending":
        page.evaluate("unlinkSession('pending-muse')")
    elif removed in ("member", "front-door"):
        page.evaluate("sid => unlinkSession(sid)", other)
    else:
        page.evaluate("() => { window.gone = disbandGroup('linked'); }")
        page.locator("#modalConfirmBtn").click()
        page.evaluate("window.gone")
    page.evaluate("_adoptStructuredIdentity('pending-muse', 'real-muse')")
    page.wait_for_timeout(300)
    expected = {
        "pending": [],
        "member": [sorted([src, "real-muse"])],
        "disband": [],
        "front-door": [sorted([src, "real-muse"])],
    }[removed]
    assert links == expected
    assert not errors
