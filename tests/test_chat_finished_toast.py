"""The finished-chat toast and highlight in the real sidebar renderer."""

import json
from urllib.parse import parse_qs, urlparse

from tests.test_workspace_browser import workspace  # noqa: F401  (shared fixture)

CLAUDE, CODEX, GEMINI = (f"00000000-0000-4000-8000-{i:012d}" for i in (1, 2, 3))


def _attention(page):
    state = {"reply": {"sessions": {}, "events": [], "seq": 7}, "since": []}

    def route(r):
        query = parse_qs(urlparse(r.request.url).query)
        state["since"].append(query.get("since", [None])[0])
        r.fulfill(json=state["reply"])

    page.route("**/api/chat-attention", route)
    page.route("**/api/chat-attention?*", route)
    page.evaluate("_pollAttention()")
    return state


def _finish(state, page, sid, **event):
    seq = state["reply"]["seq"] + 1
    state["reply"] = {
        # The flag spreads to the whole linked thread; the event does not.
        "sessions": {CLAUDE: 1, CODEX: 1, GEMINI: 1},
        "events": [{"seq": seq, "sid": sid, "at": 1, "indexed": True, **event}],
        "seq": seq,
    }
    page.evaluate("_pollAttention()")


def test_a_finished_chat_is_named_with_its_agent_and_opens_on_click(workspace):  # noqa: F811
    page, calls, errors, rows = workspace
    state = _attention(page)
    assert page.evaluate("_attentionSeq") == 7
    _finish(state, page, CODEX, agent="codex", title="Electron app migration", quiet=False)
    assert state["since"][-1] == "7"
    toast = page.locator(".toast.finished")
    assert toast.inner_text() == "Electron app migration has finished (Codex)"
    head = page.locator('.session-row[data-sid^="00000000-0000-4000-8000-"]').first
    assert "needs-attention" in head.get_attribute("class")
    with page.expect_request(lambda req: req.url.endswith("/api/chat-attention/clear")) as cleared:
        toast.click()
    assert json.loads(cleared.value.post_data) == {"session_id": CODEX}
    assert not errors


def test_runs_he_does_not_keep_as_chats_stay_quiet(workspace):  # noqa: F811
    page, calls, errors, rows = workspace
    state = _attention(page)
    _finish(state, page, "fleet-worker", agent="claude", title="Worker", quiet=True)
    _finish(state, page, "scratch-run", indexed=False)
    assert page.locator(".toast.finished").count() == 0
    assert not errors


def test_the_chat_he_is_watching_finishes_without_a_toast_or_a_flag(workspace):  # noqa: F811
    page, calls, errors, rows = workspace
    state = _attention(page)
    page.evaluate(f"""() => {{
      document.hasFocus = () => true;
      currentTab = 'chats'; convMode = 'live';
      currentSessionId = '{CLAUDE}'; activeTermSid = '{CLAUDE}';
    }}""")
    with page.expect_request(lambda req: req.url.endswith("/api/chat-attention/clear")) as cleared:
        _finish(state, page, CLAUDE, agent="claude", title="Electron app migration", quiet=False)
    assert json.loads(cleared.value.post_data) == {"session_id": CLAUDE}
    assert page.locator(".toast.finished").count() == 0
    assert page.locator(".session-row.needs-attention").count() == 0
    assert not errors
