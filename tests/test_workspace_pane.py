"""Browser contract for the real rich-event pane, without starting agents."""

from pathlib import Path
from urllib.parse import urlparse

import pytest

playwright = pytest.importorskip("playwright.sync_api")
STATIC = Path(__file__).resolve().parents[1] / "ui" / "static"


@pytest.fixture
def pane():
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(r):
            path = urlparse(r.request.url).path
            if path == "/":
                return r.fulfill(
                    content_type="text/html",
                    body=r"""
<!doctype html><html><head><link rel="stylesheet" href="/workspace-pane.css">
<style>body{margin:0;background:#000}.panes{display:grid;grid-template-columns:1fr 1fr;height:100vh}#left{border-right:1px solid #302730}section{min-width:0}@media(max-width:600px){.panes{grid-template-columns:1fr}#right{display:none}}</style>
</head><body><main class="panes"><section id="left"></section><section id="right"></section></main>
<script src="/vendor/lucide.min.js"></script><script type="module">
import {WorkspacePane} from '/workspace-pane.mjs';
window.calls=[];window.seq=0;
window.controls={submit:async v=>{window.calls.push(['submit',{text:v.text,files:v.files.map(f=>f.name)}]);if(window.failSubmit)throw Error('Submission unconfirmed');},interrupt:async()=>window.calls.push(['interrupt']),answer:async(id,a)=>window.calls.push(['answer',id,a])};
window.pane=new WorkspacePane(document.querySelector('#left'),{sessionId:'exact',provider:'Claude',model:'Configured model',controls});
window.right=new WorkspacePane(document.querySelector('#right'),{sessionId:'other',provider:'Codex',controls});
window.emit=event=>pane.receive({sequence:++window.seq,event});
emit({method:'workspace/history',params:{thread:{id:'exact',turns:[{id:'t',status:'completed',items:[
{id:'u',type:'userMessage',content:[{type:'text',text:'Review the change and show the result.'}]},
{id:'a',type:'agentMessage',text:'The change is ready for review. <img src=x onerror=alert(1)>'},
{id:'cmd',type:'commandExecution',command:'pytest tests/test_contract.py -q',status:'completed',exitCode:0,aggregatedOutput:'3 passed'},
{id:'diff',type:'fileChange',status:'completed',changes:[{path:'core/example.py',diff:'-old_value\n+new_value'}]}
]}]}}});
</script></body></html>""",
                )
            asset = STATIC / path.lstrip("/")
            if asset.is_file():
                return r.fulfill(
                    path=str(asset),
                    content_type="text/javascript"
                    if asset.suffix in {".js", ".mjs"}
                    else "text/css",
                )
            return r.fulfill(status=404)

        page.route("**/*", route)
        page.goto("http://workspace-pane.test/")
        assert not errors
        page.wait_for_function("window.pane && pane.conversation.sequence === 1", timeout=5000)
        page.locator(".aw-message").wait_for()
        yield page, errors
        browser.close()


def test_real_items_tool_expansion_and_injection_safety(pane, tmp_path):
    page, errors = pane
    assert page.locator("#left .aw-item").count() == 4
    assert page.locator("#left .aw-message").inner_text().endswith("<img src=x onerror=alert(1)>")
    assert page.locator("#left .aw-message img").count() == 0
    page.locator("#left .aw-tool summary").click()
    assert page.get_by_text("3 passed", exact=True).is_visible()
    assert page.get_by_text("Exit 0", exact=True).is_visible()
    assert page.locator(".aw-add").inner_text().strip() == "+new_value"
    page.screenshot(path=str(tmp_path / "rich-workspace-desktop.png"))
    print("Screenshot:", tmp_path / "rich-workspace-desktop.png")
    assert not errors


def test_pasted_image_drop_preview_and_mobile_cleanup(pane, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("""async () => {
      const canvas=document.createElement('canvas');canvas.width=8;canvas.height=8;
      canvas.getContext('2d').fillRect(0,0,8,8);
      const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
      const clipboard=new DataTransfer();clipboard.items.add(new File([blob],'screenshot.png',{type:'image/png'}));
      clipboard.setData('text/plain','pasted text');
      pane.input.dispatchEvent(new ClipboardEvent('paste',{clipboardData:clipboard,bubbles:true,cancelable:true}));
      const dropped=new DataTransfer();dropped.items.add(new File(['document'],'notes.txt',{type:'text/plain'}));
      pane.form.dispatchEvent(new DragEvent('drop',{dataTransfer:dropped,bubbles:true,cancelable:true}));
    }""")
    assert page.get_by_role("textbox", name="Message Claude").input_value() == "pasted text"
    page.wait_for_function("document.querySelector('.aw-attachment img').naturalWidth === 8")
    assert page.get_by_role("button", name="Remove notes.txt").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / "workspace-mobile-upload.png"))
    page.get_by_role("button", name="Remove screenshot.png").click()
    assert page.locator(".aw-attachment img").count() == 0
    assert page.evaluate("pane.previews.size") == 0
    page.evaluate("pane.dispose()")
    assert not errors


def test_composer_upload_failure_retains_draft_and_closing_does_not_cancel(pane):
    page, errors = pane
    composer = page.get_by_role("textbox", name="Message Claude")
    composer.fill("first line")
    composer.press("End")
    composer.press("Shift+Enter")
    composer.press("x")
    assert composer.input_value() == "first line\nx"
    page.locator("#left input[type=file]").set_input_files(
        {"name": "photo.png", "mimeType": "image/png", "buffer": b"upload-test"}
    )
    page.evaluate("window.failSubmit=true")
    page.get_by_role("button", name="Send message", exact=True).first.click()
    page.get_by_role("alert").filter(has_text="Submission unconfirmed").wait_for()
    assert composer.input_value() == "first line\nx"
    assert page.get_by_role("button", name="Remove photo.png").is_visible()
    assert page.evaluate("window.calls[0]") == [
        "submit",
        {"text": "first line\nx", "files": ["photo.png"]},
    ]
    page.evaluate("window.failSubmit=false")
    page.get_by_role("button", name="Send message", exact=True).first.click()
    page.wait_for_function("pane.input.value === ''")
    page.evaluate("pane.dispose()")
    assert page.evaluate("window.calls.map(x=>x[0])") == ["submit", "submit"]
    assert not errors


def test_questions_resolve_only_from_provider_and_stream_does_not_collapse_tools(pane):
    page, errors = pane
    page.locator("#left .aw-tool summary").click()
    page.evaluate(
        "emit({id:7,method:'item/tool/requestUserInput',params:{threadId:'exact',questions:[{id:'choice',question:'Choose a mode',options:[{label:'Read only',description:'No writes'}]}]}})"
    )
    page.get_by_role("button", name="Read only", exact=True).click()
    page.get_by_role("button", name="Answer", exact=True).click()
    assert page.evaluate("window.calls[0]") == [
        "answer",
        7,
        {"answers": {"choice": {"answers": ["Read only"]}}},
    ]
    assert page.get_by_text("Choose a mode", exact=True).is_visible()
    page.evaluate("emit({method:'serverRequest/resolved',params:{threadId:'exact',requestId:7}})")
    page.wait_for_function("pane.questionArea.children.length===0")
    page.evaluate(
        "emit({method:'item/agentMessage/delta',params:{threadId:'exact',turnId:'t',itemId:'a',delta:' Updated.'}})"
    )
    page.wait_for_function(
        "pane.log.querySelector('.aw-message').textContent.endsWith(' Updated.')"
    )
    assert page.locator("#left .aw-tool").evaluate("(el)=>el.open")
    assert not errors


def test_mobile_text_fits_and_composer_remains_visible(pane, tmp_path):
    page, errors = pane
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth===innerWidth")
    box = page.get_by_role("textbox", name="Message Claude").bounding_box()
    assert box["y"] + box["height"] <= 844
    page.screenshot(path=str(tmp_path / "rich-workspace-mobile.png"))
    print("Screenshot:", tmp_path / "rich-workspace-mobile.png")
    assert not errors
