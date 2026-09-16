"""The phone-to-pull-request loop: queue policy, phone line, checkouts, delivery.

Nothing here reaches GitHub, the hub, or Fleet. Git runs for real against a
local bare repository that stands in for github.com through `insteadOf`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from memory import store

BRIEF = "Fix memory/store.py so expired task claims can be reclaimed safely."


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(store, "_active_v2_store", lambda: None)
    monkeypatch.setattr(store, "_source_context", lambda: ("", "", "", ""))
    import memory.locket_mirror as mirror
    for name in ("mirror_add", "mirror_update", "mirror_delete", "mirror_archive"):
        monkeypatch.setattr(mirror, name, lambda *a, **kw: None)
    return tmp_path / "memory"


# ---- only the phone boundary makes work dispatchable ----------------------


def test_a_task_filed_as_a_note_is_backlog_and_never_claimed(queue):
    store.add_memory("Fix the flaky login test in locket before friday", "task")
    (row,) = store.tasks_in_state("backlog")
    assert row["state"] == "backlog"
    assert store.claim_next_task("dispatcher") is None


def test_an_enqueued_brief_is_the_only_ready_work(queue):
    store.add_memory("Fix the flaky login test in locket before friday", "task")
    task = store.enqueue_task(BRIEF, source_id="imessage:m1")
    claimed = store.claim_next_task("dispatcher")
    assert claimed["id"] == task["id"]


def test_finish_requires_the_matching_run(queue):
    task = store.enqueue_task(BRIEF)
    claimed = store.claim_next_task("d")
    assert store.mark_task_running(task["id"], "d", claimed["lease_token"], "run-1")
    assert not store.finish_task_run(task["id"], "run-other", "done")
    assert store.finish_task_run(task["id"], "run-1", "done", "pr: https://x\nstate: ready")
    row = store.get_memory(task["id"])
    assert row["state"] == "done"
    assert row["result"] == "pr: https://x state: ready"
    assert not store.finish_task_run(task["id"], "run-1", "done")


def test_triage_question_is_asked_once_and_an_answer_requeues(queue):
    task = store.enqueue_task("locket is broken", source_id="imessage:m2")
    assert task["state"] == "needs_triage"
    assert store.mark_task_asked(task["id"])
    assert not store.mark_task_asked(task["id"])
    thin = store.answer_triage(task["id"], "idk")
    assert thin["state"] == "needs_triage"
    answered = store.answer_triage(
        task["id"], "fix the journal editor so saving an entry no longer crashes the app")
    assert answered["state"] == "ready"
    assert "Clarification:" in answered["content"]
    assert len(list((queue / "task").glob(f"{task['id']:03d}-*.md"))) == 1
    assert store.answer_triage(task["id"], "more") is None


# ---- the phone line --------------------------------------------------------


class FakeHub:
    def __init__(self):
        self.state = {"tokens": {"x": 1}, "conversation_id": "conv"}
        self.messages: list[dict] = []
        self.sent: list[str] = []

    def install(self, monkeypatch):
        from core import unified_hub

        monkeypatch.setattr(unified_hub, "_load", lambda path=None: dict(self.state))
        monkeypatch.setattr(unified_hub, "configure",
                            lambda path=None, **f: self.state.update(f) or self.state)
        monkeypatch.setattr(unified_hub, "recent_messages",
                            lambda **kw: sorted(self.messages, key=lambda m: m["createdAt"]))

        def send_text(text, **kw):
            self.sent.append(text)
            mid = f"sent-{len(self.sent)}"
            self.state.setdefault("sent_message_ids", []).append(mid)
            self.messages.append(self.msg(mid, text, f"2026-09-16T12:{len(self.sent):02d}:30Z"))
            return unified_hub.SentMessage(True, message_id=mid)

        monkeypatch.setattr(unified_hub, "send_text", send_text)
        return self

    @staticmethod
    def msg(mid, text, created):
        return {"id": mid, "textPreview": text, "createdAt": created, "kind": "text",
                "direction": "outgoing"}


@pytest.fixture
def hub(monkeypatch, queue):
    return FakeHub().install(monkeypatch)


def test_first_poll_only_sets_the_watermark(hub):
    from core import phone_line

    hub.messages.append(hub.msg("old", "task: " + BRIEF, "2026-09-16T11:00:00Z"))
    assert phone_line.poll(now=1000).commands == []
    assert hub.state["inbound_watermark"] == "2026-09-16T11:00:00Z"
    assert store.tasks_in_state("ready") == []


def test_poll_queues_answers_and_ignores_its_own_words(hub):
    from core import phone_line

    hub.state["inbound_watermark"] = "2026-09-16T11:00:00Z"
    hub.messages += [
        hub.msg("a", "task: " + BRIEF, "2026-09-16T11:01:00Z"),
        hub.msg("a-copy", "Task:  " + BRIEF, "2026-09-16T11:01:01Z"),
        hub.msg("b", "hey are you there", "2026-09-16T11:02:00Z"),
        hub.msg("c", "task: locket is broken", "2026-09-16T11:03:00Z"),
        hub.msg("d", "serena: task: never a command", "2026-09-16T11:04:00Z"),
    ]
    report = phone_line.poll(now=1000)
    assert [c["kind"] for c in report.commands] == ["task", "task"]
    ready, = store.tasks_in_state("ready")
    thin, = store.tasks_in_state("needs_triage")
    assert thin["asked_at"]
    assert all(text.startswith("serena:") for text in hub.sent)
    assert f"#{thin['id']}" in hub.sent[1]

    # Its own replies are now in the thread; a second poll must ignore them.
    assert phone_line.poll(now=1010).commands == []

    hub.messages.append(hub.msg(
        "e", f"#{thin['id']} fix the journal editor so saving an entry no longer crashes",
        "2026-09-16T13:00:00Z"))
    hub.messages.append(hub.msg("f", "status", "2026-09-16T13:00:01Z"))
    report = phone_line.poll(now=1020)
    assert [c["kind"] for c in report.commands] == ["answer", "status"]
    assert store.get_memory(thin["id"])["state"] == "ready"
    assert "queued:" in hub.sent[-1]
    assert store.get_memory(ready["id"])["source_id"] == "imessage:a"


def test_poll_limit_leaves_the_rest_for_the_next_pass(hub, monkeypatch):
    from core import phone_line

    monkeypatch.setattr(phone_line, "MAX_COMMANDS_PER_POLL", 1)
    hub.state["inbound_watermark"] = "2026-09-16T11:00:00Z"
    hub.messages += [hub.msg("s1", "status", "2026-09-16T11:01:00Z"),
                     hub.msg("s2", "status please", "2026-09-16T11:02:00Z"),
                     hub.msg("s3", "task: " + BRIEF, "2026-09-16T11:03:00Z")]
    assert len(phone_line.poll(now=1000).commands) == 1
    assert [c["kind"] for c in phone_line.poll(now=2000).commands] == ["task"]


# ---- private checkouts and delivery ---------------------------------------


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def github(tmp_path, monkeypatch):
    """A bare repo reachable as https://github.com/duaragha/demo.git."""
    remotes = tmp_path / "remotes"
    bare = remotes / "duaragha" / "demo.git"
    bare.parent.mkdir(parents=True)
    _git("init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git("clone", str(bare), str(seed))
    (seed / "README.md").write_text("demo\n")
    _git("-c", "user.name=t", "-c", "user.email=t@t", "add", ".", cwd=seed)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init", cwd=seed)
    _git("push", "origin", "main", cwd=seed)
    config = tmp_path / "gitconfig"
    config.write_text(f'[url "file://{remotes}/"]\n\tinsteadOf = https://github.com/\n'
                      "[protocol \"file\"]\n\tallow = always\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    synced = tmp_path / "Projects" / "demo"
    _git("clone", str(bare), str(synced))
    _git("remote", "set-url", "origin", "git@github.com:duaragha/demo.git", cwd=synced)
    monkeypatch.setenv("SERENA_AGENT_REPOS_DIR", str(tmp_path / "state" / "agent-repos"))
    monkeypatch.setenv("SERENA_DISPATCH_CONFIG", str(tmp_path / "dispatch.json"))
    return SimpleNamespace(bare=bare, synced=synced, projects=tmp_path / "Projects",
                           root=tmp_path)


def test_prepare_never_uses_the_synced_tree(github):
    from core import agent_checkouts

    (github.synced / "README.md").write_text("laptop edit in progress\n")
    checkout = agent_checkouts.prepare(github.synced, 5, projects_root=github.projects)
    assert checkout.remote == "https://github.com/duaragha/demo.git"
    assert checkout.branch == "serena/task-5"
    assert (checkout.path / "README.md").read_text() == "demo\n"
    assert not str(checkout.path).startswith(str(github.projects))
    assert agent_checkouts.locate(checkout.path).branch == "serena/task-5"


def test_checkouts_inside_the_synced_tree_are_refused(github, monkeypatch):
    from core import agent_checkouts

    monkeypatch.setenv("SERENA_AGENT_REPOS_DIR", str(github.projects / ".agents"))
    with pytest.raises(agent_checkouts.CheckoutError):
        agent_checkouts.prepare(github.synced, 5, projects_root=github.projects)


def _fake_gh(monkeypatch, responses):
    from core import agent_checkouts

    real = agent_checkouts._run
    calls = []

    def run(args, **kw):
        if args[0] != "gh":
            return real(args, **kw)
        calls.append(args)
        out = responses(args)
        return subprocess.CompletedProcess(args, out[0], out[1], "")

    monkeypatch.setattr(agent_checkouts, "_run", run)
    return calls


def test_deliver_pushes_a_task_branch_and_opens_one_pr(github, monkeypatch):
    from core import agent_checkouts

    created = []

    def gh(args):
        if args[1:3] == ["pr", "view"]:
            return (0, created[-1] + " OPEN") if created else (1, "")
        if args[1:3] == ["pr", "create"]:
            created.append("https://github.com/duaragha/demo/pull/1")
            return 0, "Creating pull request\nhttps://github.com/duaragha/demo/pull/1\n"
        raise AssertionError(args)

    calls = _fake_gh(monkeypatch, gh)
    checkout = agent_checkouts.prepare(github.synced, 9, projects_root=github.projects)
    (checkout.path / "fix.txt").write_text("fixed\n")
    first = agent_checkouts.deliver(checkout, task_id=9, brief=BRIEF, run_id="run-9")
    again = agent_checkouts.deliver(checkout, task_id=9, brief=BRIEF, run_id="run-9")
    assert first.status == again.status == "pr"
    assert first.url == again.url
    assert sum(1 for c in calls if c[1:3] == ["pr", "create"]) == 1
    assert _git("ls-tree", "--name-only", "serena/task-9", cwd=github.bare).splitlines() == [
        "README.md", "fix.txt"]
    assert _git("rev-parse", "main", cwd=github.bare) == _git(
        "rev-parse", "origin/main", cwd=checkout.path)
    agent_checkouts.cleanup(checkout)
    assert not checkout.path.exists()


def test_deliver_reports_no_changes_without_pushing(github, monkeypatch):
    from core import agent_checkouts

    calls = _fake_gh(monkeypatch, lambda args: (1, ""))
    checkout = agent_checkouts.prepare(github.synced, 3, projects_root=github.projects)
    result = agent_checkouts.deliver(checkout, task_id=3, brief=BRIEF, run_id="r")
    assert result.status == "no_changes" and calls == []


def test_automerge_is_opt_in_per_repository(github, monkeypatch):
    import json

    from core import agent_checkouts

    (github.root / "dispatch.json").write_text(json.dumps({"automerge_repos": ["duaragha/demo"]}))

    def gh(args):
        if args[1:3] == ["pr", "view"]:
            return 1, ""
        if args[1:3] == ["pr", "create"]:
            return 0, "https://github.com/duaragha/demo/pull/2\n"
        if args[1:3] == ["pr", "merge"]:
            return 0, ""
        raise AssertionError(args)

    _fake_gh(monkeypatch, gh)
    checkout = agent_checkouts.prepare(github.synced, 4, projects_root=github.projects)
    (checkout.path / "x.txt").write_text("x\n")
    assert agent_checkouts.deliver(checkout, task_id=4, brief=BRIEF, run_id="r").status == "merged"


# ---- reconciliation closes the loop --------------------------------------


def test_reconcile_delivers_notifies_and_asks_once(queue, monkeypatch):
    from core import agent_checkouts, scheduler_actions
    from fleet import supervisor

    task = store.enqueue_task(BRIEF, source_id="imessage:x")
    claimed = store.claim_next_task("d")
    store.mark_task_running(task["id"], "d", claimed["lease_token"], "run-1")
    thin = store.enqueue_task("locket is broken", source_id="imessage:y")
    note = store.enqueue_task("please fix it now", source_id="")

    runs = {"run-1": {"state": "running", "cwd": "/agents/demo.task-1"}}
    monkeypatch.setattr(supervisor, "get_run", lambda run_id: runs[run_id])
    checkout = SimpleNamespace(path=Path("/agents/demo.task-1"))
    monkeypatch.setattr(agent_checkouts, "locate", lambda path: checkout)
    deliver = Mock(return_value=agent_checkouts.Delivery("pr", url="https://pr/1"))
    monkeypatch.setattr(agent_checkouts, "deliver", deliver)
    cleanup = Mock()
    monkeypatch.setattr(agent_checkouts, "cleanup", cleanup)
    texts = []
    monkeypatch.setattr(scheduler_actions, "_notify_phone",
                        lambda text, key: texts.append((key, text)) or True)

    action = scheduler_actions.REVIEWED_ACTIONS["serena.fleet.reconcile"]
    first = action({})
    assert first.output["asked"] == [thin["id"]]
    assert store.get_memory(task["id"])["state"] == "running"
    deliver.assert_not_called()

    runs["run-1"]["state"] = "completed"
    second = action({})
    assert second.output["asked"] == []
    assert store.get_memory(task["id"])["state"] == "done"
    assert store.get_memory(task["id"])["result"] == "pr: https://pr/1"
    cleanup.assert_called_once_with(checkout)
    keys = [key for key, _ in texts]
    assert keys == [f"task:{thin['id']}:question", f"task:{task['id']}:done"]
    assert "https://pr/1" in texts[-1][1]
    assert store.get_memory(note["id"])["asked_at"] == ""
    assert action({}).output["closed"] == []


def test_reconcile_keeps_a_failed_run_for_inspection(queue, monkeypatch):
    from core import agent_checkouts, scheduler_actions
    from fleet import supervisor

    task = store.enqueue_task(BRIEF)
    claimed = store.claim_next_task("d")
    store.mark_task_running(task["id"], "d", claimed["lease_token"], "run-2")
    monkeypatch.setattr(supervisor, "get_run",
                        lambda run_id: {"state": "failed", "error": "tests failed", "cwd": "/a"})
    monkeypatch.setattr(agent_checkouts, "locate", lambda path: SimpleNamespace(path=Path(path)))
    cleanup = Mock()
    monkeypatch.setattr(agent_checkouts, "cleanup", cleanup)
    texts = []
    monkeypatch.setattr(scheduler_actions, "_notify_phone",
                        lambda text, key: texts.append(text) or True)
    scheduler_actions.REVIEWED_ACTIONS["serena.fleet.reconcile"]({})
    assert store.get_memory(task["id"])["state"] == "blocked"
    assert "tests failed" in texts[0]
    cleanup.assert_not_called()


def test_a_failed_delivery_is_retried_next_tick(queue, monkeypatch):
    from core import agent_checkouts, scheduler_actions
    from fleet import supervisor

    task = store.enqueue_task(BRIEF)
    claimed = store.claim_next_task("d")
    store.mark_task_running(task["id"], "d", claimed["lease_token"], "run-3")
    monkeypatch.setattr(supervisor, "get_run", lambda run_id: {"state": "completed", "cwd": "/a"})
    monkeypatch.setattr(agent_checkouts, "locate", lambda path: SimpleNamespace(path=Path(path)))
    monkeypatch.setattr(agent_checkouts, "deliver",
                        Mock(side_effect=agent_checkouts.CheckoutError("push rejected")))
    monkeypatch.setattr(scheduler_actions, "_notify_phone", Mock(return_value=True))
    outcome = scheduler_actions.REVIEWED_ACTIONS["serena.fleet.reconcile"]({})
    assert "push rejected" in outcome.output["closed"][0]["error"]
    assert store.get_memory(task["id"])["state"] == "running"


def test_dispatch_is_held_at_the_active_run_ceiling(queue, monkeypatch):
    from core import agent_checkouts, coding_job_contract, scheduler_actions
    from fleet import supervisor

    monkeypatch.setenv("SERENA_TASK_MAX_ACTIVE_RUNS", "1")
    monkeypatch.setattr(coding_job_contract, "resolve_repository_root",
                        lambda text, project_hint="": Path("/repo"))
    monkeypatch.setattr(agent_checkouts, "prepare",
                        lambda source, task_id: SimpleNamespace(path=Path("/agents/x")))
    start = Mock(return_value={"run_id": "run-new"})
    monkeypatch.setattr(supervisor, "start_run", start)
    monkeypatch.setattr(supervisor, "list_runs", lambda limit=500: [
        {"origin_session_id": "serena-task:99", "run_id": "busy", "state": "running"},
        {"origin_session_id": "serena-task:98", "run_id": "old", "state": "completed"},
        {"origin_session_id": "", "run_id": "manual", "state": "running"},
    ])
    task = store.enqueue_task(BRIEF)
    outcome = scheduler_actions.REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert "at capacity" in outcome.detail
    start.assert_not_called()
    assert store.get_memory(task["id"])["state"] == "ready"


# ---- hub session handling --------------------------------------------------


def test_hub_refresh_rotates_and_persists_before_use(tmp_path, monkeypatch):
    from core import unified_hub

    path = tmp_path / "hub.json"
    unified_hub._save({
        "hub_url": "https://hub", "session_id": "s", "device_id": "d",
        "conversation_id": "conv",
        "tokens": {"accessToken": "old", "accessTokenExpiresAt": "2000-01-01T00:00:00Z",
                   "refreshToken": "r1", "refreshTokenExpiresAt": "2999-01-01T00:00:00Z"},
    }, path)
    posts = []

    def post(url, body, token=""):
        posts.append((url, body, token))
        if url.endswith("/sessions/refresh"):
            return {"tokens": {"accessToken": "new", "accessTokenExpiresAt": "2999-01-01T00:00:00Z",
                               "refreshToken": "r2", "refreshTokenExpiresAt": "2999-01-01T00:00:00Z"}}
        return {"disposition": "completed", "result": {"messageId": "m1"}}

    monkeypatch.setattr(unified_hub, "_post", post)
    sent = unified_hub.send_text("serena: hi", path=path)
    assert sent.ok and sent.message_id == "m1"
    assert posts[0][1]["refreshToken"] == "r1"
    assert posts[1][2] == "new"
    assert posts[1][1]["command"]["conversationId"] == "conv"
    saved = unified_hub._load(path)
    assert saved["tokens"]["refreshToken"] == "r2"
    assert "pending_refresh_key" not in saved
    assert saved["sent_message_ids"] == ["m1"]


def test_hub_send_fails_closed_without_a_thread(tmp_path, monkeypatch):
    from core import unified_hub

    path = tmp_path / "hub.json"
    assert not unified_hub.send_text("hi", path=path).ok
    unified_hub._save({"tokens": {}}, path)
    assert not unified_hub.send_text("hi", path=path).ok


def test_ship_triggers_only_configured_codemagic_builds(tmp_path, monkeypatch):
    import io
    import json
    import urllib.request

    from core import agent_checkouts

    config = tmp_path / "dispatch.json"
    monkeypatch.setenv("SERENA_DISPATCH_CONFIG", str(config))
    monkeypatch.setenv("CODEMAGIC_API_TOKEN", "tok")
    checkout = agent_checkouts.TaskCheckout(
        path=tmp_path, base=tmp_path, remote="https://github.com/duaragha/Locket.git",
        branch="serena/task-1", default_branch="main")
    assert agent_checkouts.ship(checkout) == ""

    config.write_text(json.dumps({"ship": {"duaragha/locket": {
        "codemagic_app_id": "app", "codemagic_workflow": "ios"}}}))
    seen = []

    def urlopen(request, timeout=0):
        seen.append((request.full_url, json.loads(request.data), request.headers))
        return io.BytesIO(b'{"buildId": "b1"}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert agent_checkouts.ship(checkout) == "codemagic build b1"
    url, body, headers = seen[0]
    assert body == {"appId": "app", "workflowId": "ios", "branch": "main"}
    assert headers["X-auth-token"] == "tok"
