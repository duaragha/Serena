"""Real renderer/xterm with isolated API and socket fixtures; no provider launches."""

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


def test_navigation_projects_and_drafts_do_not_spawn(workspace):
    page, calls, errors, _ = workspace
    page.get_by_role("button", name="Tooling", exact=True).first.click()
    page.locator("#toolingText").fill("Unsaved tooling draft")
    page.locator('.tab[data-tab="persona"]').click()
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


def test_split_switching_keeps_exact_runtimes_and_input(workspace, tmp_path):
    page, calls, errors, rows = workspace
    page.evaluate("(sid)=>openConv(sid)", rows[0]["session_id"])
    page.wait_for_function("_termStarting.size===0 && termSessions.size===3")
    page.wait_for_timeout(200)
    page.screenshot(path=str(tmp_path / "workspace-desktop.png"))
    print("Screenshot:", tmp_path / "workspace-desktop.png")
    assert page.locator(".workspace-terminal-head").count() == 3
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
