"""Unlink/trash interactions in the production renderer, without user sessions."""

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect

from tests.test_workspace_browser import workspace  # noqa: F401


def _begin_delete(page, sid):
    # Alt+Delete never asks: no dialog stands between the key and the trash.
    page.evaluate("sid => { void deleteSession(sid); }", sid)


def _wait_done(page, sid):
    page.wait_for_function("sid => !_trashInFlight.has(sid)", arg=sid)


def _no_dialog(page):
    assert not page.evaluate("document.getElementById('modalBackdrop').classList.contains('visible')")


def _session_route(sid):
    return lambda url: urlparse(url).path == "/api/session/" + sid


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("structured", [False, True])
def test_owned_chat_is_stopped_and_trashed_without_asking(workspace, width, structured):
    page, calls, errors, rows = workspace
    page.set_viewport_size({"width": width, "height": 850})
    sid, sibling = rows[2]["session_id"], rows[0]["session_id"]
    requests = []

    def remove(route):
        forced = "force=1" in route.request.url
        requests.append("delete" + ("?force" if forced else ""))
        if not forced:
            return route.fulfill(status=409, json={"code": "session_owned", "error": "Owned"})
        rows[:] = [row for row in rows if row["session_id"] != sid]
        route.fulfill(json={"ok": True})

    def stop(route):
        requests.append("stop:" + route.request.url.rsplit("/", 1)[-1])
        route.fulfill(json={"ok": True})

    page.route(_session_route(sid), remove)
    page.route("**/api/kill-terminal/*", stop)
    page.evaluate("""({sid, sibling, structured}) => {
      window.closedChats = [];
      termSessions.set(sibling, {tid: 'keep-running'});
      termSessions.set(sid, structured
        ? {structured: true, close: async () => { closedChats.push(sid); return {ok: true}; }}
        : {tid: 'selected-terminal'});
      currentSessionId = sibling;
    }""", {"sid": sid, "sibling": sibling, "structured": structured})
    _begin_delete(page, sid)
    _no_dialog(page)
    _wait_done(page, sid)
    _no_dialog(page)
    assert requests == (["delete", "delete?force"] if structured
                        else ["delete", "stop:selected-terminal", "delete?force"])
    assert page.evaluate("closedChats") == ([sid] if structured else [])
    assert page.evaluate("sid => termSessions.has(sid)", sibling)
    assert not page.evaluate("sid => termSessions.has(sid)", sid)
    assert page.evaluate("currentSessionId") == sibling
    undo = page.locator(".toast", has_text="Moved \"Electron app migration\" to trash")
    expect(undo.locator(".toast-action")).to_have_text("Undo")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if output := os.environ.get("SERENA_EVIDENCE_DIR"):
        path = Path(output)
        path.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path / f"nuke-{width}-{structured}.png"))
    assert not errors


@pytest.mark.parametrize("action", ["stop-failed", "still-owned", "elsewhere", "changed"])
def test_a_chat_the_backend_still_owns_is_never_trashed(workspace, action):
    page, calls, errors, rows = workspace
    sid = rows[2]["session_id"]
    deletes = []

    def remove(route):
        deletes.append("force=1" in route.request.url)
        route.fulfill(status=409, json={"code": "session_owned", "error": "Still owned"})

    page.route(_session_route(sid), remove)
    page.evaluate("""({sid, action}) => {
      window.closeCalls = 0;
      currentSessionId = sid;
      if (action !== 'elsewhere') termSessions.set(sid, {
        structured: true, close: async () => {
          closeCalls++;
          if (action === 'changed') termSessions.set(sid, {tid: 'replacement'});
          return {ok: action !== 'stop-failed', error: 'No receipt'};
        }
      });
    }""", {"sid": sid, "action": action})
    _begin_delete(page, sid)
    _wait_done(page, sid)
    _no_dialog(page)
    # The plain delete is refused, the forced one still is: nothing is trashed.
    assert deletes == [False, True]
    assert page.evaluate("currentSessionId") == sid
    assert len(rows) == 4
    assert page.evaluate("closeCalls") == (0 if action == "elsewhere" else 1)
    expect(page.locator(".toast").last).to_contain_text("Could not move chat to trash")
    expect(page.locator(".session-row.trashing")).to_have_count(0)
    assert not errors


@pytest.mark.parametrize("status", [403, 409, 500])
def test_http_errors_are_visible_and_never_stop_a_chat(workspace, status):
    page, calls, errors, rows = workspace
    sid = rows[2]["session_id"]
    page.route(_session_route(sid), lambda route: route.fulfill(status=status, body="Not JSON"))
    page.evaluate("sid => { currentSessionId = sid; termSessions.set(sid, {tid:'keep-running'}); }", sid)
    _begin_delete(page, sid)
    _wait_done(page, sid)
    expect(page.locator(".toast").last).to_contain_text(f"HTTP {status}")
    assert page.evaluate("currentSessionId") == sid
    assert page.evaluate("sid => termSessions.has(sid)", sid)
    assert not any("kill-terminal" in path for path, _ in calls)
    assert not errors


def test_bulk_delete_nukes_without_asking_and_keeps_what_failed(workspace):
    page, calls, errors, rows = workspace
    owned, free = rows[2]["session_id"], rows[1]["session_id"]

    def remove_owned(route):
        route.fulfill(status=409, json={"code": "session_owned", "error": "Still running"})

    def remove_free(route):
        rows[:] = [row for row in rows if row["session_id"] != free]
        route.fulfill(json={"ok": True})

    page.route(_session_route(owned), remove_owned)
    page.route(_session_route(free), remove_free)
    page.evaluate("""({owned, free}) => {
      selectedIds.add(owned); selectedIds.add(free); currentSessionId = owned;
      void bulkDelete();
    }""", {"owned": owned, "free": free})
    _no_dialog(page)
    expect(page.locator(".toast", has_text="1 chat(s) kept: Still running")).to_be_visible()
    expect(page.locator(".toast", has_text="Moved 1 chat to trash")).to_be_visible()
    assert page.evaluate("[...selectedIds]") == [owned]
    assert page.evaluate("currentSessionId") == owned
    assert not any("kill-terminal" in path for path, _ in calls)
    assert not errors


def test_alt_delete_in_the_sidebar_nukes_the_row_he_arrowed_to(workspace):
    page, calls, errors, rows = workspace
    open_sid, target = rows[0]["session_id"], rows[3]["session_id"]
    deleted = []

    def remove(route):
        deleted.append(urlparse(route.request.url).path.rsplit("/", 1)[-1])
        route.fulfill(json={"ok": True})

    page.route(lambda url: urlparse(url).path.startswith("/api/session/"), remove)
    page.evaluate("""({open_sid, target}) => {
      currentSessionId = open_sid;
      document.activeElement?.blur();
      setFocus(sessions.findIndex(s => s.session_id === target));
    }""", {"open_sid": open_sid, "target": target})
    page.keyboard.press("Alt+Delete")
    page.wait_for_function("sid => !_trashInFlight.has(sid)", arg=target)
    _no_dialog(page)
    assert deleted == [target]
    assert not errors


def test_undo_and_the_trash_section_restore_chats(workspace):
    page, calls, errors, rows = workspace
    sid = rows[3]["session_id"]
    restores = []
    trash = {"items": [{"id": sid + "-20260925", "session_id": sid, "title": "Completed release",
                        "agent": "codex", "deleted_at": "2026-09-25T09:00:00-04:00", "restorable": True}],
             "total": 1}

    def restore(route):
        restores.append(route.request.post_data_json)
        route.fulfill(json={"ok": True, "restored": [sid], "errors": []})

    page.route(_session_route(sid), lambda route: route.fulfill(json={"ok": True}))
    page.route("**/api/trash/restore", restore)
    page.route(lambda url: urlparse(url).path == "/api/trash", lambda route: route.fulfill(json=trash))
    _begin_delete(page, sid)
    _wait_done(page, sid)
    page.locator(".toast .toast-action", has_text="Undo").click()
    expect(page.locator(".toast", has_text="Restored 1 chat")).to_be_visible()
    assert restores == [{"session_ids": [sid]}]

    header = page.locator('[data-testid="trash-header"]')
    expect(header).to_contain_text("Trash (1)")
    header.click()
    row = page.locator('[data-testid="trash-row"]')
    expect(row).to_contain_text("Completed release")
    expect(row).to_contain_text("Codex")
    row.locator(".trash-restore").click()
    page.wait_for_function("() => document.querySelectorAll('.toast.success').length >= 2")
    assert restores[-1] == {"ids": [sid + "-20260925"]}
    assert not errors


@pytest.mark.parametrize("operation, argument", [("unlinkSession", "chat"), ("disbandGroup", "linked")])
def test_unlink_failures_are_visible_without_refresh(workspace, operation, argument):
    page, calls, errors, rows = workspace
    page.route("**/api/group/*", lambda route: route.fulfill(status=500, json={"error": "Metadata unavailable"}))
    initial = calls.count(("/api/sessions", "GET"))
    page.evaluate("([fn, arg]) => { void window[fn](arg); }", [operation, argument])
    if operation == "disbandGroup":
        page.locator("#modalConfirmBtn").click()
    expect(page.locator(".toast").last).to_contain_text("Metadata unavailable")
    assert calls.count(("/api/sessions", "GET")) == initial
    assert not errors


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("disband", [False, True])
@pytest.mark.parametrize("active", [0, 2])
def test_unlink_updates_live_split_without_stopping_any_member(workspace, pending, disband, active):
    page, calls, errors, rows = workspace
    sids = [row["session_id"] for row in rows[:3]]
    page.evaluate("""({sids, pending, active}) => {
      if (pending) for (const sid of sids) _setPendingPartners(sid, sids);
      openConv(sids[active]);
    }""", {"sids": sids, "pending": pending, "active": active})
    page.wait_for_function("termSessions.size === 3 && _gtkSplitSids?.length === 3")

    def unlink(route):
        for row in rows[:3]:
            if disband or row["session_id"] == sids[2]:
                row["group"] = None
        route.fulfill(json={"ok": True})

    page.route("**/api/group/*", unlink)
    if disband:
        page.evaluate("() => { window.disbanded = false; disbandGroup('linked').then(() => window.disbanded = true); }")
        page.locator("#modalConfirmBtn").click()
        page.wait_for_function("window.disbanded")
    else:
        page.evaluate("sid => unlinkSession(sid)", sids[2])
    assert page.evaluate("sid => _linkedGroupSids(sid, {liveOnly:false})", sids[2]) == [sids[2]]
    if disband or active == 2:
        assert page.evaluate("_gtkSplitActive") is False
    else:
        assert page.evaluate("_gtkSplitSids") == sids[:2]
    if disband:
        assert page.evaluate("_pendingTermPartners.size") == 0
    assert page.evaluate("termSessions.size") == 3
    assert not any("kill-terminal" in path for path, _ in calls)
    assert not errors
