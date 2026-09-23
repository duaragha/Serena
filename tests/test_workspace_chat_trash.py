"""Unlink/trash interactions in the production renderer, without user sessions."""

import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.test_workspace_browser import workspace  # noqa: F401


def _begin_delete(page, sid):
    page.evaluate("sid => { void deleteSession(sid); }", sid)
    expect(page.locator("#modalTitle")).to_have_text("Move conversation to trash?")
    page.locator("#modalConfirmBtn").click()


def _wait_done(page, sid):
    page.wait_for_function("sid => !_trashInFlight.has(sid)", arg=sid)


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("structured", [False, True])
def test_owned_chat_stop_and_trash_only_targets_selected_member(workspace, width, structured):
    page, calls, errors, rows = workspace
    page.set_viewport_size({"width": width, "height": 850})
    sid, sibling = rows[2]["session_id"], rows[0]["session_id"]
    requests = []

    def remove(route):
        requests.append("delete")
        if requests.count("delete") == 1:
            return route.fulfill(status=409, json={"code": "session_owned", "error": "Owned"})
        rows[:] = [row for row in rows if row["session_id"] != sid]
        route.fulfill(json={"ok": True})

    def stop(route):
        requests.append("stop:" + route.request.url.rsplit("/", 1)[-1])
        route.fulfill(json={"ok": True})

    page.route("**/api/session/" + sid, remove)
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
    expect(page.locator("#modalConfirmBtn")).to_have_text("Stop and move to trash")
    assert requests == ["delete"]
    assert page.evaluate("closedChats") == []
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if output := os.environ.get("SERENA_EVIDENCE_DIR"):
        path = Path(output)
        path.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path / f"stop-trash-{width}-{structured}.png"))
    page.locator("#modalConfirmBtn").click()
    _wait_done(page, sid)
    assert requests == (["delete", "delete"] if structured else ["delete", "stop:selected-terminal", "delete"])
    assert page.evaluate("closedChats") == ([sid] if structured else [])
    assert page.evaluate("sid => termSessions.has(sid)", sibling)
    assert not page.evaluate("sid => termSessions.has(sid)", sid)
    assert page.evaluate("currentSessionId") == sibling
    assert not errors


@pytest.mark.parametrize("action", ["cancel", "changed", "stop-failed", "still-owned", "elsewhere"])
def test_unconfirmed_stop_never_discards_history(workspace, action):
    page, calls, errors, rows = workspace
    sid = rows[2]["session_id"]
    deletes = []

    def remove(route):
        deletes.append(sid)
        route.fulfill(status=409, json={"code": "session_owned", "error": "Still owned"})

    page.route("**/api/session/" + sid, remove)
    page.evaluate("""({sid, action}) => {
      window.closeCalls = 0;
      currentSessionId = sid;
      if (action !== 'elsewhere') termSessions.set(sid, {
        structured: true, close: async () => { closeCalls++; return {ok: action !== 'stop-failed', error: 'No receipt'}; }
      });
    }""", {"sid": sid, "action": action})
    _begin_delete(page, sid)
    if action != "elsewhere":
        expect(page.locator("#modalConfirmBtn")).to_have_text("Stop and move to trash")
        if action == "changed":
            page.evaluate("sid => termSessions.set(sid, {tid:'replacement'})", sid)
        page.locator("#modalCancelBtn" if action == "cancel" else "#modalConfirmBtn").click()
    _wait_done(page, sid)
    assert len(deletes) == (2 if action == "still-owned" else 1)
    assert page.evaluate("currentSessionId") == sid
    assert len(rows) == 4
    assert page.evaluate("closeCalls") == (1 if action in {"stop-failed", "still-owned"} else 0)
    if action != "cancel":
        expect(page.locator(".toast").last).to_contain_text("Could not move chat to trash")
    assert not errors


@pytest.mark.parametrize("status", [403, 409, 500])
def test_http_errors_are_visible_and_never_stop_a_chat(workspace, status):
    page, calls, errors, rows = workspace
    sid = rows[2]["session_id"]
    page.route("**/api/session/" + sid, lambda route: route.fulfill(status=status, body="Not JSON"))
    page.evaluate("sid => { currentSessionId = sid; termSessions.set(sid, {tid:'keep-running'}); }", sid)
    _begin_delete(page, sid)
    _wait_done(page, sid)
    expect(page.locator(".toast").last).to_contain_text(f"HTTP {status}")
    assert page.evaluate("currentSessionId") == sid
    assert not any("kill-terminal" in path for path, _ in calls)
    assert not errors


def test_bulk_delete_preserves_failed_selection_and_open_chat(workspace):
    page, calls, errors, rows = workspace
    owned, free = rows[2]["session_id"], rows[1]["session_id"]

    def remove(route):
        rows[:] = [row for row in rows if row["session_id"] != free]
        route.fulfill(json={"deleted": [free], "errors": [{"id": owned, "error": "Still running"}]})

    page.route("**/api/sessions/bulk-delete", remove)
    page.evaluate("""({owned, free}) => {
      selectedIds.add(owned); selectedIds.add(free); currentSessionId = owned;
      void bulkDelete();
    }""", {"owned": owned, "free": free})
    expect(page.locator("#modalConfirmBtn")).to_have_text("Move to Trash")
    expect(page.locator("#modalBody")).to_contain_text("recoverable trash")
    page.locator("#modalConfirmBtn").click()
    expect(page.locator(".toast").last).to_contain_text("1 chat(s) kept: Still running")
    assert page.evaluate("[...selectedIds]") == [owned]
    assert page.evaluate("currentSessionId") == owned
    assert not any("kill-terminal" in path for path, _ in calls)
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
