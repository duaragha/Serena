"""Exercise the themed update surface without downloading or restarting anything."""

from pathlib import Path
from urllib.parse import urlparse

import pytest

from ui import web

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(params=[1440, 360])
def panel(request):
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip("Playwright Chromium unavailable")
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": request.param, "height": 900})
        page.add_init_script("""
          window.calls = []; window.result = {state:'available', remoteVersion:'0.2.17'};
          window.facts = {version:'0.2.16', platform:'Linux', packaged:true,
            electron:'40.8.3', chrome:'144.0.7559.256', node:'24.14.0'};
          window.serenaDesktop = {openExternal: async url => {calls.push(url);}, updates: {
            onOpen: fn => {window.openAbout=fn;}, onProgress: fn => {window.updateProgress=fn;},
            describe: async () => facts,
            check: async () => {calls.push('check');return {...facts,...result};},
            download: () => {calls.push('download');return new Promise(resolve => {
              window.completeDownload=()=>{facts.downloadedVersion='0.2.17';resolve({state:'downloaded',version:'0.2.17'});};
            });}, install: async () => {calls.push('install');return true;}
          }};
        """)

        def route(r):
            path = urlparse(r.request.url).path
            if path == "/":
                return r.fulfill(body=web.HTML, content_type="text/html")
            if path.startswith("/static/"):
                asset = Path(web.__file__).parent / path.lstrip("/")
                if asset.is_file():
                    return r.fulfill(path=str(asset))
            return r.fulfill(json={})

        page.route("**/*", route)
        page.goto("http://serena-test.local/")
        source = Path(web.__file__).parents[1] / 'apps' / 'desktop' / 'about-panel.js'
        page.add_script_tag(path=str(source))
        page.wait_for_function("typeof openAbout === 'function'")
        try:
            yield page
        finally:
            browser.close()


def test_manual_download_progress_reopen_and_restart_confirmation(panel, tmp_path):
    page = panel
    page.evaluate("openAbout({action:'about'})")
    dialog = page.get_by_role("dialog", name="Serena")
    assert dialog.is_visible()
    assert page.evaluate("calls") == []
    assert "0.2.16 / Linux" in dialog.inner_text()
    page.get_by_role("button", name="Check for updates", exact=True).click()
    page.get_by_role("heading", name="Update available").wait_for()
    assert page.evaluate("calls") == ['check']
    screenshot = tmp_path / "about.png"
    page.screenshot(path=str(screenshot))
    print(f"About screenshot: {screenshot}")
    assert dialog.evaluate("e => e.scrollWidth <= e.clientWidth")
    assert dialog.bounding_box()["x"] >= 0
    page.get_by_role("button", name="Download update", exact=True).click()
    page.evaluate("updateProgress({percent:57})")
    assert dialog.get_by_role("progressbar").get_attribute("value") == '57'
    page.get_by_role("button", name="Close About").click()
    assert not dialog.is_visible()
    page.evaluate("openAbout({action:'about'})")
    assert "57% downloaded" in dialog.inner_text()
    assert page.evaluate("calls") == ['check', 'download']
    page.evaluate("completeDownload()")
    page.get_by_role("button", name="Install update", exact=True).click()
    assert page.evaluate("calls") == ['check', 'download']
    page.get_by_role("button", name="Not now", exact=True).click()
    page.get_by_role("button", name="Install update", exact=True).click()
    page.get_by_role("button", name="Restart and install", exact=True).click()
    assert page.evaluate("calls") == ['check', 'download', 'install']


@pytest.mark.parametrize("state,title", [
    ("current", "You are up to date"), ("error", "Update failed"),
    ("none-published", "No release available"), ("unsupported", "Updates unavailable"),
])
def test_check_menu_states_are_themed_and_escape_closes(panel, state, title):
    panel.evaluate("s => {result={state:s, reason:'test detail <b>not HTML</b>'};}", state)
    panel.evaluate("openAbout({action:'check'})")
    panel.get_by_role("heading", name=title, exact=True).wait_for()
    assert panel.evaluate("calls") == ['check']
    assert panel.locator('#desktopAbout b').count() == 0
    panel.keyboard.press('Escape')
    assert not panel.get_by_role('dialog').is_visible()
