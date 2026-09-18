"""Release manager rendering and interaction, with no GitHub writes or app restarts."""
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
import pytest

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / "config/promotion-features.json").read_text())


@pytest.mark.parametrize("width", [800, 390])
def test_release_selection_dependencies_confirmation_and_status(width):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 850})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""window.submissions=[];window.serenaReleases={
          load:async()=>structuredClone(window.fixture), status:async()=>null, github:async()=>{}, dismiss:async()=>true,
          submit:async v=>{window.submissions.push(v);return {request:'a'.repeat(32),mode:v.mode,run:null}}
        };""" + "window.fixture=" + json.dumps({"source": "a" * 40, "stable": "v0.3.4", "version": "0.3.9-dev.1",
            "catalog": CATALOG, "installed": [], "status": None}) + ";")
        page.goto((ROOT / "apps/desktop/promotion.html").as_uri())
        page.get_by_text(CATALOG["features"][0]["title"], exact=True).wait_for()
        publish = page.get_by_role("button", name="Build and publish to main", exact=True)
        assert publish.is_disabled()
        feature = CATALOG["features"][2]
        page.get_by_role("checkbox", name=f'Include {feature["title"]}', exact=True).check()
        assert "requires" in page.locator("#validation").inner_text()
        assert publish.is_disabled()
        for f in CATALOG["features"][:3]:
            page.get_by_role("checkbox", name=f'Include {f["title"]}', exact=True).check()
            page.get_by_role("checkbox", name=f'Tested {f["title"]}', exact=True).check()
        assert publish.is_enabled()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        for button in page.locator("button:visible").all():
            assert button.evaluate("e => e.scrollWidth <= e.clientWidth")
        if output := os.environ.get("SERENA_EVIDENCE_DIR"):
            path = Path(output)
            path.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path / f"release-manager-{width}.png"), full_page=True)
        page.get_by_role("button", name="Check selection", exact=True).click()
        assert page.evaluate("submissions[0].mode") == "verify"
        assert len(page.evaluate("submissions[0].selected")) == 3
        assert publish.is_disabled()
        assert "Awaiting GitHub" in page.locator("#requestStatus").inner_text()
        page.get_by_role("button", name="Dismiss tracking", exact=True).click()
        assert publish.is_enabled()
        page.evaluate("fixture.source='b'.repeat(40)")
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(publish).to_be_disabled()
        assert page.get_by_role("checkbox").evaluate_all("es => es.every(e => !e.checked)")
        assert not errors
        browser.close()
