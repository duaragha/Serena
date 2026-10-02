"""The renderer half of merged-view sleep: who is engaged, what looks asleep,
and that a click wakes a pane at once instead of on the next sweep."""

from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect

from tests.test_workspace_browser import workspace  # noqa: F401


def test_peers_sleep_visibly_and_a_click_wakes_one_immediately(workspace):
    page, calls, errors, rows = workspace
    sids = [row["session_id"] for row in rows[:3]]
    bodies, wakes = [], []

    def sync(route):
        body = route.request.post_data_json
        bodies.append(body)
        # The server's answer: every pane but the focused one is asleep.
        states = {tid: {"state": "live" if tid == body["focus_tid"] else "paused", "busy": False}
                  for tid in body["visible_tids"]}
        route.fulfill(json={"ok": True, "states": states})

    def wake(route):
        wakes.append(urlparse(route.request.url).path.rsplit("/", 1)[-1])
        route.fulfill(json={"ok": True, "state": "live"})

    page.route("**/api/terminal-runtime/sync", sync)
    page.route("**/api/terminal-runtime/wake/*", wake)
    page.evaluate("sid => openConv(sid)", sids[0])
    page.wait_for_function("termSessions.size === 3 && _gtkSplitSids?.length === 3")
    page.evaluate("() => _syncWebRuntimePolicy()")
    page.wait_for_function("document.querySelectorAll('.term-pane.runtime-asleep').length === 2")

    # Only the pane he opened into counts as engaged; the others may sleep on load.
    assert bodies[-1]["engaged_tids"] == [sids[0]]
    assert set(bodies[-1]["visible_tids"]) == set(sids)
    focused = page.locator(f'.term-pane[data-sid="{sids[0]}"]')
    peer = page.locator(f'.term-pane[data-sid="{sids[1]}"]')
    expect(focused).not_to_have_class("runtime-asleep")
    assert "runtime-asleep" in (peer.get_attribute("class") or "")
    # The frame is kept: the sleeping pane still renders its terminal.
    assert peer.locator(".xterm").count() == 1

    page.evaluate("sid => document.querySelector(`.term-pane[data-sid='${sid}']`)"
                  ".dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}))", sids[1])

    assert wakes == [sids[1]], "the click waited for the next sweep"
    assert "runtime-asleep" not in (peer.get_attribute("class") or "")
    page.evaluate("() => _syncWebRuntimePolicy()")
    page.wait_for_function("n => document.querySelectorAll('.term-pane.runtime-asleep').length === n", arg=2)
    assert sorted(bodies[-1]["engaged_tids"]) == sorted(sids[:2]), "the clicked pane never became engaged"
    assert not errors


def test_a_click_that_lands_during_a_sweep_is_not_undone_by_its_stale_answer(workspace):
    page, calls, errors, rows = workspace
    sids = [row["session_id"] for row in rows[:3]]
    pending = []

    page.route("**/api/terminal-runtime/sync", lambda route: pending.append(route))
    page.route("**/api/terminal-runtime/wake/*", lambda route: route.fulfill(json={"ok": True, "state": "live"}))
    page.evaluate("sid => openConv(sid)", sids[0])
    page.wait_for_function("termSessions.size === 3 && _gtkSplitSids?.length === 3")
    page.evaluate("sids => { for (const sid of sids.slice(1)) _gtkRuntimeStates.set(sid, 'paused'); }", sids)
    page.evaluate("() => { window._sweep = _syncWebRuntimePolicy(); }")
    page.wait_for_timeout(100)
    page.evaluate("sid => document.querySelector(`.term-pane[data-sid='${sid}']`)"
                  ".dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}))", sids[1])
    stale = {sid: {"state": "paused", "busy": False} for sid in sids}
    for route in pending:
        route.fulfill(json={"ok": True, "states": stale})
    page.evaluate("() => window._sweep")

    assert page.evaluate("sid => _gtkRuntimeStates.get(sid)", sids[1]) == "live"
    assert "runtime-asleep" not in (page.locator(f'.term-pane[data-sid="{sids[1]}"]').get_attribute("class") or "")
    assert not errors
