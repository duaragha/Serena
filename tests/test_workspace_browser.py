"""Real renderer/xterm with isolated API and socket fixtures; no provider launches."""

import os
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

from ui import web

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def workspace():
    rows = [
        dict(
            session_id=f"00000000-0000-4000-8000-{i:012d}",
            agent=agent,
            group="linked",
            cwd="/project/serena",
            project_short="serena",
            title="Electron app migration",
            display_title="Electron app migration",
            last_timestamp="2026-09-09T12:00:00Z",
        )
        for i, agent in enumerate(["claude", "codex", "gemini"], 1)
    ]
    rows.append(
        dict(
            session_id="done-session",
            agent="codex",
            cwd="/project/serena",
            project_short="serena",
            display_title="Completed release",
            is_done=True,
        )
    )
    calls, errors = [], []
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""window.sent=[]; window.WebSocket=class {
          constructor(url){this.url=url;this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.();},50);}
          send(value){window.sent.push(value)} close(){this.readyState=3}
          addEventListener(){} removeEventListener(){}
        };""")

        def route(r):
            path = urlparse(r.request.url).path
            calls.append((path, r.request.method))
            if path == "/":
                return r.fulfill(body=web.HTML, content_type="text/html")
            if path.startswith("/static/"):
                asset = Path(web.__file__).parent / path.lstrip("/")
                if asset.is_file():
                    return r.fulfill(path=str(asset))
            if path == "/api/sessions":
                return r.fulfill(json=rows)
            if path == "/api/projects":
                return r.fulfill(
                    json=[
                        dict(
                            short="serena",
                            cwd="/project/serena",
                            project_dir="serena",
                            project_dirs=["serena"],
                        )
                    ]
                )
            if path == "/api/persona-files":
                return r.fulfill(
                    json={
                        "persona": "Persona content",
                        "tooling": "Tooling content",
                        "voice": "Voice content",
                    }
                )
            if path == "/api/spawn-terminal":
                sid = r.request.post_data_json["session_id"]
                agent = next(row["agent"] for row in rows if row["session_id"] == sid)
                return r.fulfill(
                    json={"ok": True, "terminal_id": sid, "agent": agent, "cwd": "/project/serena"}
                )
            if path == "/api/files":
                return r.fulfill(
                    json={
                        "root_name": "serena",
                        "root_path": "/project/serena",
                        "tree": {
                            "children": [{"type": "file", "name": "main.py", "path": "main.py"}]
                        },
                    }
                )
            if path == "/api/workspace-changes":
                return r.fulfill(
                    json={
                        "is_git": True,
                        "root": "/project/serena",
                        "branch": "master",
                        "changes": [{"status": "M", "path": "main.py"}],
                    }
                )
            if path == "/api/read-file":
                return r.fulfill(
                    json={
                        "name": "main.py",
                        "path": "/project/serena/main.py",
                        "text": 'print("hello")',
                    }
                )
            if path == "/api/live-usage":
                return r.fulfill(
                    json={
                        "ok": True,
                        "claude": {
                            "available": True,
                            "model": "Opus",
                            "five_hour": {"used_percentage": 22},
                        },
                        "codex": {"available": True, "five_hour": {"used_percentage": 11}},
                    }
                )
            if path in ("/api/topics", "/api/knowledge", "/api/memories"):
                return r.fulfill(json=[])
            return r.fulfill(json={})

        page.route("**/*", route)
        page.goto("http://serena-workspace.test/")
        page.wait_for_function("allSessions.length === 4 && !!window.SerenaWorkspace")
        yield page, calls, errors, rows
        browser.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_sidebar_pins_fleet_and_voice_above_active_terminals(workspace, width):
    page, calls, errors, rows = workspace
    page.set_viewport_size({"width": width, "height": 850})
    rows[0]["workspace_runtime"] = {"ok": True}
    rows.extend([
        dict(session_id="serena-voice-main", agent="serena-voice", display_title="Serena"),
        dict(session_id="fleet-worker", agent="codex", display_title="Fleet worker",
             fleet_worker={"run_id": "run-1"}, workspace_runtime={"ok": True}),
    ])
    page.evaluate("rows => { allSessions = rows; sessionSource = rows; renderSessionList(); }", rows)
    if width < 760:
        page.locator("#workspaceChatsToggle").click()
    headers = page.locator("#sessionList > .group-header")
    assert headers.all_text_contents()[:4] == [
        "Serena", "▸ Fleet Chats (1)", "▸ Voice Chats (0)", "● Active Terminals",
    ]
    fleet = page.get_by_test_id("fleet-chats-header")
    voice = page.get_by_test_id("voice-chats-header")
    worker = page.locator('#sessionList [data-sid="fleet-worker"]')
    assert not worker.is_visible()
    fleet.click()
    playwright.expect(fleet).to_have_attribute("aria-expanded", "true")
    playwright.expect(worker).to_be_visible()
    assert page.locator("#sessionList .session-row").count() == 4
    fleet.click()
    voice.click()
    playwright.expect(voice).to_have_attribute("aria-expanded", "true")
    voice.click()
    assert headers.all_text_contents()[:4] == [
        "Serena", "▸ Fleet Chats (1)", "▸ Voice Chats (0)", "● Active Terminals",
    ]
    bounds = [headers.nth(i).bounding_box() for i in range(4)]
    assert all(a["y"] + a["height"] <= b["y"] for a, b in zip(bounds, bounds[1:]))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if output := os.environ.get("SERENA_EVIDENCE_DIR"):
        path = Path(output)
        path.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path / f"sidebar-order-{width}.png"))
    assert not errors
    assert not any(path == "/api/spawn-terminal" for path, _ in calls)


def test_unchanged_sidebar_keeps_dom_and_refreshes_changed_rows(workspace):
    page, _, errors, rows = workspace
    result = page.evaluate('''() => {
      renderSessionList();
      const list = document.getElementById('sessionList');
      const row = list.querySelector('.session-row');
      const observer = new MutationObserver(() => {});
      observer.observe(list, {childList:true, subtree:true});
      for(let i=0;i<10;i++) renderSessionList();
      const mutations = observer.takeRecords().length;
      observer.disconnect();
      window.retainedSidebarRow = row;
      return {same: row === list.querySelector('.session-row'), mutations};
    }''')
    assert result == {"same": True, "mutations": 0}
    page.evaluate('''() => {
      setFocus(0, false);
      renderSessionList();
      renderSessionList();
    }''')
    assert page.locator('#sessionList .session-row.focused').count() == 1
    page.evaluate('''() => {
      sessionSource[0].display_title = 'Updated title';
      renderSessionList();
    }''')
    playwright.expect(page.locator('#sessionList')).to_contain_text('Updated title')
    page.evaluate('''() => { setSessionSource([]); renderSessionList(); }''')
    playwright.expect(page.locator('#sessionList')).to_have_text('No conversations found')
    page.evaluate('''rows => { setSessionSource(rows); renderSessionList(); }''', rows)
    assert page.locator('#sessionList .session-row').count() > 0
    assert not errors


def test_sidebar_date_groups_reuse_formatter_without_changing_labels(workspace):
    page, _, errors, _ = workspace
    assert page.evaluate('''() => {
      const timestamp = '2020-01-15T12:00:00Z';
      const expected = new Date(timestamp).toLocaleString('default', {month:'long',year:'numeric'});
      return timeGroup(timestamp) === expected && timeGroup('') === 'Unknown'
        && timeGroup('invalid') === 'Invalid Date' && timeGroup(new Date().toISOString()) === 'Today';
    }''')
    assert not errors


@pytest.mark.parametrize("width", [1440, 390])
def test_sidebar_date_then_project_groups_preserve_chats_and_collapse(workspace, width):
    page, calls, errors, _ = workspace
    page.set_viewport_size({"width": width, "height": 900})
    page.wait_for_function("_collapsedLoaded")
    page.evaluate('''() => {
      const row = (id, cwd, date, extra={}) => ({session_id:id, agent:'codex',
        cwd, project_short:'shared', display_title:id, last_timestamp:date, ...extra});
      setSessionSource([
        row('new-a', '/projects/a', '2020-02-04T12:00:00Z'),
        row('new-b', '/projects/b', '2020-02-03T12:00:00Z'),
        row('older-a', '/projects/a', '2020-02-02T12:00:00Z'),
        row('previous-month', '/projects/a', '2020-01-02T12:00:00Z'),
        row('active', '/projects/a', '2020-02-01T12:00:00Z', {workspace_runtime:{ok:true}}),
        row('linked-head', '/projects/c', '2020-02-05T12:00:00Z', {group:'pair',agent:'claude'}),
        row('linked-sibling', '/projects/c', '2020-02-06T12:00:00Z', {group:'pair'}),
        row('starred', '/projects/a', '2020-02-07T12:00:00Z', {starred:true}),
        row('quoted', '/projects/"<unsafe>&', '2020-01-01T12:00:00Z'),
      ]);
      renderSessionList();
    }''')
    if width < 760:
        page.locator("#workspaceChatsToggle").click()
    buckets = page.locator("#sessionList > .time-section")
    assert buckets.count() == 2
    assert buckets.nth(0).locator(".sidebar-project-header").count() == 3
    assert buckets.nth(0).locator(".session-row").evaluate_all(
        "els => els.map(el => el.dataset.sid)"
    ) == ["linked-head", "new-a", "older-a", "new-b"]
    assert page.locator('.starred-section .session-row').count() == 1
    assert page.locator('[data-sid="active"]').locator('..').get_attribute("class") == "sidebar-project-section"
    assert page.locator('[data-sid="linked-head"] .agent-icon').count() == 2
    quoted = buckets.nth(1).locator('.sidebar-project-header').nth(1)
    assert quoted.get_attribute("title") == '/projects/"<unsafe>&'
    button = buckets.nth(0).locator('.sidebar-project-header').nth(1)
    key = button.get_attribute("data-project-key")
    with page.expect_request(lambda r: r.url.endswith('/api/ui-state') and r.method == 'POST') as request:
        button.click()
    saved = request.value.post_data_json["collapsed"]
    assert key in saved["projectGroups"]
    assert not page.locator('[data-sid="new-a"]').is_visible()
    assert page.locator('[data-sid="previous-month"]').is_visible()
    assert page.locator('[data-sid="new-b"]').is_visible()
    page.evaluate('saved => { _applyCollapsedState(saved); renderSessionList(); }', saved)
    assert not page.locator('[data-sid="new-a"]').is_visible()
    assert page.evaluate('''() => {
      focusedIndex = sessions.findIndex(s => s.session_id === 'linked-head');
      return sessions[nextVisibleSessionIndex(1)].session_id;
    }''') == "new-b"
    page.evaluate("toggleAgentFilter('codex')")
    assert page.locator('[data-sid="new-a"]').is_visible()
    page.evaluate("toggleAgentFilter(null)")
    assert not page.locator('[data-sid="new-a"]').is_visible()
    page.evaluate("_searchQuery = 'new'; renderSessionList()")
    assert page.locator('[data-sid="new-a"]').is_visible()
    page.evaluate("_searchQuery = ''; renderSessionList()")
    button.focus()
    button.press("Enter")
    assert page.locator('[data-sid="new-a"]').is_visible()
    assert page.evaluate("document.activeElement.classList.contains('sidebar-project-header')")
    assert page.locator('#sessionList .session-row').count() == 8
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if output := os.environ.get("SERENA_EVIDENCE_DIR"):
        path = Path(output)
        path.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path / f"sidebar-projects-{width}.png"))
    assert not errors
    assert not any(path == "/api/spawn-terminal" for path, _ in calls)


def test_navigation_projects_and_drafts_do_not_spawn(workspace):
    page, calls, errors, _ = workspace
    page.get_by_role("button", name="Tooling", exact=True).first.click()
    playwright.expect(page.locator("#toolingText")).to_have_value("Tooling content")
    page.locator("#toolingText").fill("Unsaved tooling draft")
    page.locator('.tab[data-tab="persona"]').click()
    playwright.expect(page.locator("#personaText")).to_have_value("Persona content")
    page.locator("#personaText").fill("Unsaved persona draft")
    page.locator('.tab[data-tab="tooling"]').click()
    assert page.locator("#toolingText").input_value() == "Unsaved tooling draft"
    page.locator('.tab[data-tab="chats"]').click()
    page.locator("#workspaceProject").click()
    assert page.locator("#projectSidebar").is_visible()
    page.locator("#projectSidebar .project-all").click()
    assert not page.locator("#projectSidebar").is_visible()
    page.locator("#filterCodex").click()
    page.locator("#filterAll").click()
    page.locator("#workspaceCompleted").click()
    assert page.get_by_text("Completed release", exact=False).first.is_visible()
    assert not any(path == "/api/spawn-terminal" for path, _ in calls)
    assert not errors


def test_slow_persona_load_never_overwrites_a_draft_typed_before_it_lands(workspace):
    """Every tab click reloads, and the first load has no dataset.saved to compare
    against, so a response landing after the user typed used to replace the draft."""
    page, calls, errors, _ = workspace
    page.get_by_role("button", name="Tooling", exact=True).first.click()
    # Let the tab's own load settle first; otherwise it, not the gated one, wins.
    page.wait_for_function("document.getElementById('toolingText').value==='Tooling content'")
    page.evaluate("""()=>{
      const ta=document.getElementById('toolingText');
      ta.value='';delete ta.dataset.saved;delete ta.dataset.loaded;
      let open;window._release=()=>open();
      const gate=new Promise(done=>{open=done;}),real=window.fetch;
      window.fetch=(url,init)=>String(url).includes('/api/persona-files')
        ? gate.then(()=>({json:async()=>({persona:'Persona content',tooling:'Tooling content',voice:'Voice content'})}))
        : real(url,init);
      loadPersona();
    }""")
    page.locator("#toolingText").fill("Unsaved tooling draft")
    page.evaluate("window._release()")
    page.wait_for_timeout(200)
    assert page.locator("#toolingText").input_value() == "Unsaved tooling draft"
    assert not any(path == "/api/spawn-terminal" for path, _ in calls)
    assert not errors


def test_mockup_proportions_ignore_legacy_sizes_and_keep_new_resize(workspace):
    page, calls, errors, _ = workspace
    page.evaluate("localStorage.setItem('serena.paneSizes.v1', JSON.stringify({'chats-w':'20%', 'files-w':'9%'}))")
    page.reload()
    page.wait_for_function("allSessions.length === 4 && !!window.SerenaWorkspace")
    assert round(page.locator('#chatListCol').bounding_box()['width']) == 267
    assert round(page.locator('#filesPane').bounding_box()['width']) == 219
    assert not page.locator('#fdOrb').is_visible()
    page.evaluate("localStorage.setItem('serena.workspacePaneSizes.v1', JSON.stringify({'chats-w':'310px'}))")
    page.reload()
    page.wait_for_function("!!window.SerenaWorkspace")
    assert round(page.locator('#chatListCol').bounding_box()['width']) == 310
    assert page.evaluate("JSON.parse(localStorage.getItem('serena.paneSizes.v1'))['chats-w']") == '20%'
    assert not any(path == '/api/spawn-terminal' for path, _ in calls)
    assert not errors


def test_split_switching_keeps_exact_runtimes_and_input(workspace, tmp_path):
    page, calls, errors, rows = workspace
    page.evaluate("(sid)=>openConv(sid)", rows[0]["session_id"])
    page.wait_for_function("_termStarting.size===0 && termSessions.size===3")
    page.wait_for_timeout(200)
    page.screenshot(path=str(tmp_path / "workspace-desktop.png"))
    print("Screenshot:", tmp_path / "workspace-desktop.png")
    assert page.locator(".workspace-terminal-head").count() == 3
    assert page.locator('.workspace-session-toolbar #viewReadBtn').is_visible()
    assert page.locator('.workspace-session-toolbar #viewLiveBtn').is_visible()
    assert not page.locator('#convMeta').is_visible()
    page.get_by_role("button", name="Show Codex pane", exact=True).click()
    assert page.locator("#termMounts>.term-pane:not(.hidden)").count() == 1
    page.evaluate("termSessions.get(activeTermSid).term.input('hello')")
    assert any("hello" in value for value in page.evaluate("window.sent"))
    page.get_by_role("button", name="Split", exact=True).click()
    assert page.locator("#termMounts>.term-pane:not(.hidden)").count() == 3
    page.locator("#workspaceChanges").click()
    page.locator("#workspaceChangesList button").wait_for()
    assert "master" in page.locator("#workspaceChangesList").inner_text()
    page.locator("#workspaceChangesList button").click()
    page.wait_for_function("_openFiles.length===1")
    assert sum(path == "/api/spawn-terminal" for path, _ in calls) == 3
    assert not any(path.startswith("/api/kill-terminal") for path, _ in calls)
    page.get_by_role("button", name="Show Codex pane", exact=True).click()
    page.evaluate("sid=>teardownLiveTerminal(sid)", rows[1]["session_id"])
    page.wait_for_function("termSessions.size===2 && _gtkSplitSids.length===2")
    assert page.locator("#termMounts>.term-pane:not(.hidden)").count() == 2
    assert not errors


@pytest.mark.parametrize("width", [1440, 1024, 390])
def test_responsive_layout_and_neon_black(workspace, width):
    page, _, errors, _ = workspace
    page.set_viewport_size({"width": width, "height": 850})
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(0, 0, 0)"
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if width < 760:
        page.locator("#workspaceChatsToggle").click()
        assert page.locator("#chatListCol").is_visible()
        page.keyboard.press("Escape")
        assert not page.locator("#chatListCol").is_visible()
    assert not errors


@pytest.mark.parametrize("width", [1440, 390])
def test_muse_limits_show_native_windows_and_stale_snapshot(workspace, width):
    page, _, errors, _ = workspace
    page.set_viewport_size({"width": width, "height": 850})
    now = int(time.time())
    payload = {"ok": True, "muse": {
        "available": True, "source": "muse-cli-usage", "model": "muse-spark",
        "updated_at": now, "window_minutes": 300,
        "five_hour": {"used_percentage": 0, "resets_at": now + 1800},
        "seven_day": {"used_percentage": 9, "resets_at": now + 180000},
    }}
    page.route("**/api/live-usage", lambda route: route.fulfill(json=payload))
    page.evaluate("loadLiveUsage()")
    ribbon = page.locator("#liveUsageRibbon")
    playwright.expect(ribbon.locator(".live-usage-chip.muse .live-usage-pct")).to_have_text("0%")
    ribbon.focus()
    card = ribbon.locator(".live-usage-card").filter(has=page.locator(".live-usage-name.muse"))
    playwright.expect(card).to_be_visible()
    assert card.locator(".live-usage-pct").all_text_contents() == ["0%", "9%"]
    assert card.locator(".live-usage-window-label").all_text_contents() == ["5h", "7d"]
    assert "as of just now" in card.inner_text()
    bounds = card.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
    chip_bounds = ribbon.locator(".live-usage-chip.muse").bounding_box()
    assert chip_bounds["x"] >= 0 and chip_bounds["x"] + chip_bounds["width"] <= width
    assert ribbon.locator(".live-usage-chip.muse").get_attribute("title") == "muse: 0% used"
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if output := os.environ.get("SERENA_EVIDENCE_DIR"):
        path = Path(output)
        path.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path / f"muse-usage-{width}.png"))
    payload["muse"].update(stale=True, window_minutes=180)
    page.evaluate("loadLiveUsage()")
    playwright.expect(card.locator(".live-usage-age")).to_have_class("live-usage-age stale")
    assert "last known" in card.inner_text()
    assert card.locator(".live-usage-window-label").all_text_contents() == ["3h", "7d"]
    payload["muse"] = {"available": False, "loading": True}
    page.evaluate("loadLiveUsage()")
    playwright.expect(card.locator(".live-usage-empty")).to_have_text("loading")
    assert not errors
