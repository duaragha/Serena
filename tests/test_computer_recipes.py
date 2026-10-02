"""Learned flows retain the same authority and failure boundaries as browser input."""

import asyncio
import json
import stat
from types import SimpleNamespace

import pytest
from test_computer_use import Desktop, begin

from core.action_authority import ActionAuthority
from core.computer_platform import ComputerError
from core.computer_recipes import MAX_STEPS, RecipeStore, bounded, normalize
from core.computer_tools import visual_tools
from core.computer_use import ComputerController
from core.computer_web import STOPPED, HerBrowser

STEPS = [{"click": {"role": "button", "name": "Continue"}}]


@pytest.fixture
def controller(tmp_path):
    c = ComputerController(Desktop(), desk="isolated",
                           authority=ActionAuthority(tmp_path / "authority.sqlite", publish_events=False))
    c.recipes = RecipeStore(tmp_path / "recipes")
    c.web = SimpleNamespace(run=lambda steps, cancelled: {
        "ok": not cancelled(), "steps": [{"step": 1, "ok": True}],
        "snapshot": "fresh", "url": "https://example.com/done",
        "_recipe_steps": steps, "_starting_url": "https://example.com/start?token=private",
    }, snapshot=lambda: {"snapshot": "fresh", "url": "https://example.com/start"})
    yield c
    c.close()


def test_store_permissions_matching_bounds_and_corruption(tmp_path):
    store = RecipeStore(tmp_path / "recipes", clock=lambda: 123)
    recipe = store.save("Create, MY project!", "https://user:secret@example.com/start?tab=setup#step1", [STEPS])
    path = store.directory / (recipe["id"] + ".json")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert recipe["origin"] == "https://example.com"
    assert "secret" not in path.read_text()
    assert recipe["starting_url"] == "https://example.com/start?tab=setup#step1"
    assert normalize(" CREATE, my   project!") == "create my project"
    assert store.matches("create my project")[0]["recipe_id"] == recipe["id"]
    assert not store.matches("remove my project")
    assert store.matches("remove my project", threshold=0.5)
    assert not store.matches("!!!")
    with pytest.raises(ComputerError):
        bounded([STEPS] * (MAX_STEPS + 1))
    large = [{"fill": {"label": "Description"}, "value": "x" * 2000}] * 25
    with pytest.raises(ComputerError, match="size"):
        bounded([large, large])
    with pytest.raises(ComputerError):
        store.load("../outside")
    path.write_text("broken")
    assert not store.matches("create my project")


def test_finish_without_successful_browser_input_saves_nothing(controller):
    c = controller
    sid, _ = begin(c)
    c.web.run = lambda *args, **kwargs: {"ok": False, "_recipe_steps": []}
    c.browser(sid, STEPS, request_id="capture-001", intent="continue")
    c.stop("visual task finished")
    assert not list(c.recipes.directory.glob("*.json"))


@pytest.mark.parametrize("reason,desk,mode,saved", [
    ("visual task finished", "isolated", "control", True),
    ("visual task failed", "isolated", "control", False),
    ("stopped by user", "isolated", "control", False),
    ("visual task finished", "host", "control", False),
    ("visual task finished", "isolated", "watch", False),
])
def test_only_finished_isolated_control_is_saved(controller, reason, desk, mode, saved):
    c = controller
    c.desk = desk
    sid, _ = begin(c, mode=mode)
    if mode == "control":
        c.browser(sid, STEPS, request_id="capture-001", intent="continue")
    c.stop(reason)
    assert bool(list(c.recipes.directory.glob("*.json"))) is saved


def test_failed_batch_is_excluded_and_transport_retry_not_recaptured(controller):
    c = controller
    sid, _ = begin(c)
    first = c.browser(sid, STEPS, request_id="capture-001", intent="continue")
    assert "_recipe_steps" not in first
    assert c.browser(sid, STEPS, request_id="capture-001", intent="continue")["replayed"]
    c.web.run = lambda *args, **kwargs: {"ok": False, "_recipe_steps": [],
        "steps": [{"step": 1, "ok": True}, {"step": 2, "ok": False}], "snapshot": "failed"}
    c.browser(sid, [{"fill": {"label": "Password"}, "value": "secret"}],
              request_id="capture-002", intent="fill")
    c.stop("visual task finished")
    paths = list(c.recipes.directory.glob("*.json"))
    assert len(paths) == 1
    recipe = json.loads(paths[0].read_text())
    assert recipe["batches"] == [STEPS]
    assert "secret" not in paths[0].read_text()


@pytest.mark.parametrize("fail", [False, True])
def test_replay_uses_browser_receipts_stops_and_is_idempotent(controller, fail):
    c = controller
    recipe = c.recipes.save("continue", "https://example.com", [STEPS, STEPS, STEPS])
    sid, _ = begin(c)
    calls = []
    browser = c.browser

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return browser(*args, **kwargs)

    c.browser = spy
    c.web.run = lambda steps, cancelled: {
        "ok": not (fail and len(calls) == 2), "snapshot": f"fresh-{len(calls)}",
        "steps": [{"step": 1, "ok": not (fail and len(calls) == 2)}],
    }
    result = c.replay(sid, recipe["id"], request_id="replay-001", intent="continue")
    assert result["ok"] is not fail
    assert len(calls) == (2 if fail else 3)
    assert result["snapshot"] == f"fresh-{len(calls)}"
    assert all(result["receipts"])
    assert len([e for e in c.events if e["type"] == "action_finished"]) == len(calls)
    again = c.replay(sid, recipe["id"], request_id="replay-001", intent="continue")
    assert again["replayed"] and len(calls) == (2 if fail else 3)
    updated = c.recipes.load(recipe["id"])
    assert updated["success_count"] == (1 if fail else 2)
    assert updated["failure_count"] == int(fail)
    assert not c.session.browser_batches
    with pytest.raises(ComputerError, match="reused"):
        c.replay(sid, recipe["id"], request_id="replay-001", intent="other")


def test_replay_denied_grant_and_pause_never_dispatch(controller):
    c = controller
    recipe = c.recipes.save("continue", "https://example.com", [STEPS])
    sid, _ = begin(c)
    c.pause(sid, "held")
    with pytest.raises(ComputerError):
        c.replay(sid, recipe["id"], request_id="replay-001", intent="continue")
    c.session.state = "active"
    c.session.input_hold.clear()
    c.authority.revoke_grant(c.session.grant_id, reason="test")
    c.web.run = lambda *args, **kwargs: pytest.fail("denied input dispatched")
    assert not c.replay(sid, recipe["id"], request_id="replay-002", intent="continue")["ok"]


def test_password_failure_discards_entire_recorded_batch():
    web = object.__new__(HerBrowser)
    page = SimpleNamespace(url="https://example.com")
    web.opened = asyncio.Event()
    web._pages = lambda: [page]

    async def current():
        return page

    async def state(*args):
        return {"snapshot": "fresh"}

    async def attribute(*args, **kwargs):
        return "password"

    web._current, web._state = current, state
    web._locator = lambda *args: SimpleNamespace(get_attribute=attribute)
    result = asyncio.run(web._run([
        {"fill": {"label": "Password"}, "value": "do-not-save"},
        *STEPS,
    ], lambda: False))
    assert not result["ok"] and result["_recipe_steps"] == []
    assert len(result["steps"]) == 1 and "do-not-save" not in json.dumps(result)


def test_first_prompt_and_tool_expose_recipe(controller, monkeypatch):
    from core import computer_agent

    c = controller
    sid, _ = begin(c)
    recipe = c.recipes.save(c.session.request, "https://example.com", [STEPS])
    prompts = []
    monkeypatch.setattr(computer_agent, "build_task_pack", lambda request: "")

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline)

    class Client:
        active_turn_id = None

        def __init__(self, **kwargs):
            assert "replay" in {tool.name for tool in kwargs["tools"]}

        async def turn(self, prompt, **kwargs):
            prompts.append(prompt)
            return {"text": "done", "tool_calls": []}

        async def close(self):
            pass

    asyncio.run(computer_agent.ComputerAgent(c, client_factory=Client).run())
    assert recipe["id"] in prompts[0] and "one replay call" in prompts[0]
    assert "Verify" in prompts[0]
    c.stop()
    sid, _ = begin(c, mode="watch")
    assert "replay" not in {tool.name for tool in visual_tools(c, sid)}


@pytest.mark.parametrize("missing", [False, True])
def test_snapshot_refs_are_frozen_without_blocking_normal_input(missing):
    web = object.__new__(HerBrowser)
    page = SimpleNamespace(url="https://example.com")
    web.opened = asyncio.Event()
    web._pages = lambda: [page]
    calls = []

    async def current():
        return page

    async def state(*args):
        return {"snapshot": "fresh"}

    async def snapshot_text(*args):
        return ""

    async def element_handle(*args, **kwargs):
        if missing:
            raise RuntimeError("old ref vanished")
        return "handle"

    async def evaluate(expression, arg=None, **kwargs):
        return arg == "handle" if arg is not None else {"label": "", "text": "Continue"}

    async def count():
        return 1

    async def step(*args):
        calls.append(args)

    web._current, web._state, web._step, web._snapshot_text = current, state, step, snapshot_text
    web.last_snapshot = "" if missing else '- button "Continue" [ref=e12] [cursor=pointer]'
    web._locator = lambda *args: SimpleNamespace(
        element_handle=element_handle, evaluate=evaluate, count=count
    )
    steps = [{"click": {"ref": "e12"}}]
    result = asyncio.run(web._run(steps, lambda: False))
    assert result["ok"] and len(calls) == 1
    assert steps == [{"click": {"ref": "e12"}}]
    assert result["_recipe_steps"] == ([] if missing else [
        {"click": {"role": "button", "name": "Continue", "exact": True}}
    ])
    assert result["_recipe_partial"] is missing


def test_hold_during_semantic_resolution_stops_before_input():
    web = object.__new__(HerBrowser)
    page = SimpleNamespace(url="https://example.com")
    web.opened = asyncio.Event()
    web._pages = lambda: [page]
    held = []
    calls = []

    async def current():
        return page

    async def state(*args):
        return {"snapshot": "fresh"}

    async def element_handle(*args, **kwargs):
        held.append(True)
        return "handle"

    async def evaluate(expression, arg=None, **kwargs):
        return arg == "handle" if arg is not None else {"label": "", "text": ""}

    async def count():
        return 1

    async def step(*args):
        calls.append(args)

    web._current, web._state, web._step = current, state, step
    web.last_snapshot = '- button "Continue" [ref=e12]'
    web._locator = lambda *args: SimpleNamespace(
        element_handle=element_handle, evaluate=evaluate, count=count
    )
    result = asyncio.run(web._run([{"click": {"ref": "e12"}}], lambda: bool(held)))
    assert held and not calls
    assert not result["ok"] and result["steps"] == [
        {"step": 1, "ok": False, "detail": STOPPED}
    ]
    assert result["_recipe_steps"] == []


@pytest.mark.parametrize("unrecordable", [False, True])
def test_unrecordable_or_oversized_flow_is_not_saved(controller, unrecordable):
    c = controller
    sid, _ = begin(c)
    if unrecordable:
        c.web.run = lambda *args, **kwargs: {"ok": True, "_recipe_steps": []}
    for index in range(MAX_STEPS // 25 + 1):
        c.browser(sid, STEPS * 25, request_id=f"capture-{index:03d}", intent="continue")
    assert c.session.recipe_overflow and not c.session.browser_batches
    c.stop("visual task finished")
    assert not list(c.recipes.directory.glob("*.json"))


def test_hold_between_replay_batches_stops_before_more_input(controller):
    c = controller
    recipe = c.recipes.save("continue", "https://example.com", [STEPS, STEPS])
    sid, _ = begin(c)
    calls = []

    def run(*args, **kwargs):
        calls.append(1)
        c.pause(sid, "operator hold")
        return {"ok": True, "snapshot": "before-hold"}

    c.web.run = run
    result = c.replay(sid, recipe["id"], request_id="replay-001", intent="continue")
    assert not result["ok"] and len(calls) == 1
    assert result["snapshot"] == "fresh"
    assert c.recipes.load(recipe["id"])["failure_count"] == 1


@pytest.mark.parametrize("fail_navigation", [False, True])
def test_cross_origin_navigation_uses_browser_receipts(controller, fail_navigation):
    c = controller
    recipe = c.recipes.save("continue", "https://example.com/setup", [STEPS])
    sid, _ = begin(c)
    c.web.snapshot = lambda: {"url": "https://other.example/", "snapshot": "other tab"}
    calls = []

    def run(steps, **kwargs):
        calls.append(steps)
        return {"ok": not fail_navigation, "snapshot": "fresh"}

    c.web.run = run
    result = c.replay(sid, recipe["id"], request_id="replay-001", intent="continue")
    assert calls[0] == [{"goto": "https://example.com/setup"}]
    assert len(calls) == (1 if fail_navigation else 2)
    assert result["ok"] is not fail_navigation
    assert all(result["receipts"])
    assert len([e for e in c.events if e["type"] == "action_finished"]) == len(calls)


@pytest.mark.parametrize("prefix", [[], STEPS])
def test_partial_recipe_never_records_or_replays_past_unresolved_target(controller, prefix):
    c = controller
    sid, _ = begin(c)
    c.web.run = lambda *args, **kwargs: {
        "ok": True, "_recipe_steps": prefix, "_recipe_partial": True,
        "_starting_url": "https://example.com/setup",
    }
    c.browser(sid, STEPS * 2, request_id="capture-001", intent="continue")
    c.web.run = lambda *args, **kwargs: {"ok": True, "_recipe_steps": STEPS}
    c.browser(sid, STEPS, request_id="capture-002", intent="continue")
    c.stop("visual task finished")
    path, = c.recipes.directory.glob("*.json")
    recipe = c.recipes.load(path.stem)
    assert recipe["partial"] and recipe["batches"] == ([prefix] if prefix else [])
    assert bool(c.recipes.matches(c.session.request)) is bool(prefix)
    sid, _ = begin(c)
    if prefix:
        result = c.replay(sid, recipe["id"], request_id="replay-001", intent="continue")
        assert result["ok"] and result["partial"] and "remaining task" in result["next"]
        assert len(result["receipts"]) == 1
    else:
        c.web.snapshot = lambda: {"url": "https://other.example"}
        with pytest.raises(ComputerError, match="no replayable prefix"):
            c.replay(sid, recipe["id"], request_id="replay-001", intent="continue")


def test_semantic_target_label_fallback_and_ambiguity():
    web = object.__new__(HerBrowser)
    web.last_snapshot = "- generic [ref=e1]"  # no accessible name to record

    async def snapshot_text(*args):
        return ""

    async def element_handle(*args, **kwargs):
        return "handle"

    async def evaluate(expression, arg=None, **kwargs):
        return arg == "handle" if arg is not None else {"label": "Project name", "text": ""}

    async def count():
        return 1

    locator = SimpleNamespace(element_handle=element_handle, evaluate=evaluate, count=count)
    web._locator = lambda *args: locator
    web._snapshot_text = snapshot_text
    assert asyncio.run(web._recipe_target(None, {"ref": "e1"}, 100)) == {
        "label": "Project name", "exact": True,
    }

    async def ambiguous():
        return 2

    locator.count = ambiguous
    with pytest.raises(ComputerError, match="unique semantic"):
        asyncio.run(web._recipe_target(None, {"ref": "e1"}, 100))
