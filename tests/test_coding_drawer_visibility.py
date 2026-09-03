"""The coding drawer only opens for a coding job that is actually running.

Raghav has reported the same thing three times: the drawer reappears while
nothing is coding, and closing it does not stick. The architecture already says
the coding surfaces are optional viewing history that a voice request must
never launch, focus, hide, or close, so anything less than a live job has no
business taking the screen.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from voice import brain_bridge
from voice.call.tasking import parse_code_panel_intent

REPO = Path(__file__).resolve().parents[1]
DESKTOP = REPO / "voice" / "desktop"


class _StubInbox:
    """Just the two reads the bridge makes when an overlay connects."""

    def __init__(self, jobs: list[dict]) -> None:
        self.jobs = jobs
        self.asked: list[str] = []

    def recent_jobs(self, *, limit: int = 20) -> list[dict]:
        return self.jobs[:limit]

    def overlay_snapshot(self, item_id: str) -> dict:
        self.asked.append(item_id)
        return {"item_id": item_id, "state": "working"}


def _use_inbox(monkeypatch: pytest.MonkeyPatch, jobs: list[dict]) -> _StubInbox:
    store = _StubInbox(jobs)
    monkeypatch.setattr(
        "core.voice_inbox.get_default_voice_inbox", lambda: store, raising=True
    )
    return store


@pytest.mark.parametrize("state", ["queued", "completed", "failed", "cancelled"])
def test_a_job_that_is_not_running_is_not_replayed_to_the_overlay(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    store = _use_inbox(monkeypatch, [{"item_id": "job-1", "state": state}])

    assert brain_bridge.current_durable_job_snapshot() is None
    assert store.asked == []


@pytest.mark.parametrize("state", ["working", "resume_queued"])
def test_a_running_job_survives_an_overlay_restart(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    _use_inbox(monkeypatch, [{"item_id": "job-1", "state": state}])

    snapshot = brain_bridge.current_durable_job_snapshot()
    assert snapshot is not None and snapshot["item_id"] == "job-1"


def test_a_finished_job_does_not_shadow_the_running_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_inbox(
        monkeypatch,
        [
            {"item_id": "newest", "state": "completed"},
            {"item_id": "running", "state": "working"},
        ],
    )

    snapshot = brain_bridge.current_durable_job_snapshot()
    assert snapshot is not None and snapshot["item_id"] == "running"


@pytest.mark.parametrize(
    "text",
    [
        "the coding panel keeps trying to open which is annoying",
        "the coding panel keeps popping open even when nothing is running",
        "why does the code window show up on every turn",
        "if i close the drawer the coding panel should stay closed",
        "i was reading the code window while you were talking",
    ],
)
def test_talking_about_the_drawer_is_not_a_command(text: str) -> None:
    assert parse_code_panel_intent(text) is None


@pytest.mark.parametrize(
    "text,action",
    [
        ("Can you open a coding panel now?", "open"),
        ("hey Serena, show the code terminal", "open"),
        ("pull up the coding window", "open"),
        ("hide the coding panel", "hide"),
        ("please close the code window", "hide"),
        # Split phrasings: the verb and its particle straddle the subject.
        ("bring the coding panel up", "open"),
        ("put the code window back up", "open"),
        ("put the coding panel away", "hide"),
    ],
)
def test_asking_for_the_drawer_still_works(text: str, action: str) -> None:
    intent = parse_code_panel_intent(text)
    assert intent is not None and intent.action == action


def test_the_renderer_leaves_drawer_visibility_to_the_main_process() -> None:
    html = (DESKTOP / "renderer" / "index.html").read_text(encoding="utf-8")
    body = html.split("onSnapshot:", 1)[1].split("onControlResult:", 1)[0]
    assert "renderSnapshot" in body
    assert "codePanel.show()" not in body


def _element_ancestors(markup: str) -> dict[str, list[str]]:
    """Map every id in the document to the ids of the elements containing it."""

    from html.parser import HTMLParser

    void = {"br", "img", "input", "link", "meta", "hr", "source"}

    class _Tree(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.stack: list[str | None] = []
            self.ancestors: dict[str, list[str]] = {}

        def _record(self, attrs: list[tuple[str, str | None]]) -> str | None:
            found = dict(attrs).get("id")
            if found:
                self.ancestors[found] = [name for name in self.stack if name]
            return found

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            found = self._record(attrs)
            # Void elements never close, so they never nest anything.
            if tag not in void:
                self.stack.append(found)

        def handle_startendtag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self._record(attrs)

        def handle_endtag(self, _tag: str) -> None:
            if self.stack:
                self.stack.pop()

    tree = _Tree()
    tree.feed(markup)
    return tree.ancestors


def _declarations(source: str, selector: str) -> dict[str, str]:
    """Return one rule's declarations as property -> value.

    Whitespace, ordering, and shorthand spacing are all free to change; the
    tests below care about what the rule says, not how it is typed.
    """

    # Comments can contain braces and colons, so they go before anything else.
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    opener = r"\s*\{(?P<body>[^{}]*)\}"
    pattern = re.compile(r"(?:^|[}/*;\s])" + re.escape(selector) + opener, re.MULTILINE)
    match = pattern.search(source)
    assert match is not None, f"no {selector} rule found"
    declarations: dict[str, str] = {}
    for line in match.group("body").split(";"):
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        name = name.strip()
        if name and not name.startswith("/*"):
            declarations[name] = " ".join(value.split())
    return declarations


def _stylesheet() -> str:
    return (DESKTOP / "renderer" / "styles.css").read_text(encoding="utf-8")


def test_the_drawer_is_a_column_of_the_window_not_a_sheet_over_it() -> None:
    rule = _declarations(_stylesheet(), ".code-panel")

    # A sheet is taken out of the flow and slides over the app. A column is not.
    assert rule.get("position", "static") in {"static", "relative"}
    assert "translateX" not in rule.get("transform", "")
    # It occupies its own track in a flex row rather than the whole viewport.
    assert "flex" in rule or "width" in rule
    assert rule.get("height") != "100vh"


def test_the_rest_of_the_app_is_a_sibling_the_drawer_pushes_over() -> None:
    html = (DESKTOP / "renderer" / "index.html").read_text(encoding="utf-8")

    assert _declarations(html, "#shell").get("display") == "flex"
    stage = _declarations(html, "#stage")
    # It has to be able to give up room, so it grows and may shrink below its
    # content width.
    assert stage.get("flex", "").startswith("1")
    assert stage.get("min-width") == "0"
    # The widget no longer pins itself to the viewport over everything else.
    assert _declarations(html, "#widget").get("position") == "absolute"
    # Both live inside the stage, so shrinking the stage shrinks them with it.
    ancestors = _element_ancestors(html)
    assert ancestors["widget"][-2:] == ["shell", "stage"]
    assert ancestors["overlay"][-2:] == ["shell", "stage"]
    # The drawer joins that row and nothing else does: panels other code mounts
    # on the body must not become columns of it.
    assert "document.getElementById('shell')" in html.split("new CodePanel(", 1)[1]
    in_shell = [name for name, chain in ancestors.items() if chain[-1:] == ["shell"]]
    assert in_shell == ["stage"], "only the stage may share the row with the drawer"


def test_a_second_job_start_preserves_the_coding_apps_selected_job() -> None:
    html = (DESKTOP / "renderer" / "index.html").read_text(encoding="utf-8")
    on_start = html.split("onStart: (data) => {", 1)[1].split("onEvent:", 1)[0]

    assert "if (!codePanel.__codingJobsInstalled)" in on_start
    assert on_start.index("if (!codePanel.__codingJobsInstalled)") < on_start.index(
        "codePanel.clear()"
    )


def test_the_drawer_still_paints_level_with_the_other_panels() -> None:
    """Leaving the flow behind must not send it under the dashboard."""
    css = _stylesheet()
    drawer = _declarations(css, ".code-panel")
    dashboard = _declarations(css, ".dashboard")

    assert int(drawer.get("z-index", 0)) >= int(dashboard.get("z-index", 0))


def test_the_window_and_drawer_share_the_same_default_resizable_width() -> None:
    """The persisted value may move, but both surfaces need the same default."""
    main = (DESKTOP / "main.js").read_text(encoding="utf-8")
    drawer = _declarations(_stylesheet(), ".code-panel")
    declared = re.search(r"const DEFAULT_CODE_PANEL_WIDTH\s*=\s*(\d+)", main)

    assert declared is not None, "main.js no longer declares the drawer default"
    assert drawer.get("--code-panel-width") == f"{declared.group(1)}px"
    assert drawer.get("width") == "var(--code-panel-width)"
    assert drawer.get("min-width") == "300px"
    assert drawer.get("max-width") == "720px"


@pytest.mark.parametrize(
    "suite",
    [
        "code-drawer-visibility.test.cjs",
        "code-panel-resize.test.mjs",
        "coding-jobs-drawer.test.mjs",
    ],
)
def test_overlay_drawer_visibility_rules_hold(suite: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the Electron suites cannot run here")
    result = subprocess.run(
        [node, "--test", str(DESKTOP / "tests" / suite)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    assert result.returncode == 0, result.stdout + result.stderr
