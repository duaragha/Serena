"""Exercise the pane's actual Chromium clipboard without provider inference."""

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def main():
    static = Path(__file__).resolve().parents[1] / "ui" / "static"
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=os.environ.get("SERENA_PROOF_BROWSER_CHANNEL", "msedge"))
        try:
            context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))

            def route(request):
                path = urlparse(request.request.url).path
                if path == "/":
                    return request.fulfill(content_type="text/html", body="""+<main id="pane"></main><script type="module">
import {WorkspacePane} from '/workspace-pane.mjs';
window.pane=new WorkspacePane(document.getElementById('pane'),{sessionId:'proof',provider:'Codex',controls:{}});
pane.receive({sequence:1,event:{method:'workspace/history',params:{thread:{id:'proof',turns:[
{id:'done',status:'completed',items:[{id:'answer',type:'agentMessage',text:'Exact completed output\\nSecond line'}]},
{id:'running',status:'inProgress',items:[{id:'partial',type:'agentMessage',text:'Partial output'}]}
]}}}});pane.input.value='Unsent draft';
</script>""")
                asset = static / path.lstrip("/")
                if asset.is_file() and asset.resolve().is_relative_to(static):
                    return request.fulfill(path=str(asset), content_type="text/javascript")
                return request.fulfill(status=404)

            page.route("**/*", route)
            page.goto("http://127.0.0.1:19837/")
            page.get_by_role("button", name="Copy latest completed output", exact=True).click()
            page.wait_for_function("async()=>await navigator.clipboard.readText()==='Exact completed output\\nSecond line'")
            assert page.evaluate("pane.input.value") == "Unsent draft"
            assert not errors, errors
            context.close()
        finally:
            browser.close()
    print(json.dumps({"ok": True, "actualBrowserClipboard": True, "partialOutputExcluded": True,
                      "draftPreserved": True, "providerLaunched": False, "browserClosed": True}))


if __name__ == "__main__":
    main()
