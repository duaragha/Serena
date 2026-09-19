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


def test_a_legacy_writer_ready_note_without_a_source_is_not_claimed(queue):
    path = queue / "task" / "001-note.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nid: 1\ntype: task\ncreated: 2026-01-01 00:00:00\n"
                    "updated: 2026-01-01 00:00:00\nstate: ready\nsource_id: \n---\n\n"
                    "Fix the flaky login test in locket before friday\n", encoding="utf-8")
    assert store.claim_next_task("dispatcher") is None
    assert store.get_memory(1)["state"] == "backlog"
    assert store.tasks_in_state("ready") == []
    assert store.enqueue_task(BRIEF)["source_id"].startswith("queue:")


def test_projects_root_follows_the_serena_checkout(tmp_path, monkeypatch):
    from core import coding_job_contract

    monkeypatch.delenv("SERENA_PROJECTS_ROOT", raising=False)
    monkeypatch.setattr(coding_job_contract.Path, "home", lambda: tmp_path)
    (tmp_path / "Documents" / "Projects" / "mcp-gateway").mkdir(parents=True)
    (tmp_path / "Projects" / "serena").mkdir(parents=True)
    assert coding_job_contract._default_projects_root() == tmp_path / "Projects"
    (tmp_path / "Documents" / "Projects" / "serena").mkdir()
    assert coding_job_contract._default_projects_root() == tmp_path / "Documents" / "Projects"


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

        monkeypatch.setenv("SERENA_PHONE_LINE_CONFIG", "/nonexistent/phone-line.json")

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
    # One line a task, so a failure reason has somewhere to go.
    assert f"#{thin['id']} queued" in hub.sent[-1]
    assert f"#{ready['id']} queued" in hub.sent[-1]
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
    (seed / "README.md").write_text("demo\n", encoding="utf-8")
    _git("-c", "user.name=t", "-c", "user.email=t@t", "add", ".", cwd=seed)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init", cwd=seed)
    _git("push", "origin", "main", cwd=seed)
    config = tmp_path / "gitconfig"
    config.write_text(f'[url "file://{remotes}/"]\n\tinsteadOf = https://github.com/\n'
                      "[protocol \"file\"]\n\tallow = always\n", encoding="utf-8")
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

    (github.synced / "README.md").write_text("laptop edit in progress\n", encoding="utf-8")
    checkout = agent_checkouts.prepare(github.synced, 5, projects_root=github.projects)
    assert checkout.remote == "https://github.com/duaragha/demo.git"
    assert checkout.branch == "serena/task-5"
    assert (checkout.path / "README.md").read_text(encoding="utf-8") == "demo\n"
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
    (checkout.path / "fix.txt").write_text("fixed\n", encoding="utf-8")
    (checkout.path / "__pycache__").mkdir()
    (checkout.path / "__pycache__" / "fix.cpython-313.pyc").write_bytes(b"junk")
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

    (github.root / "dispatch.json").write_text(json.dumps({"automerge_repos": ["duaragha/demo"]}), encoding="utf-8")

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
    (checkout.path / "x.txt").write_text("x\n", encoding="utf-8")
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
                        lambda text, key, **kw: texts.append((key, text, kw.get('answers_request'))) or True)

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
    keys = [key for key, _text, _reply in texts]
    assert keys == [f"task:{thin['id']}:question", f"task:{task['id']}:done"]
    assert "https://pr/1" in texts[-1][1]
    # Both answer briefs he texted, so quiet hours must not hold them.
    assert [reply for _key, _text, reply in texts] == [True, True]
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
                        lambda text, key, **kw: texts.append(text) or True)
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
        "codemagic_app_id": "app", "codemagic_workflow": "ios"}}}), encoding="utf-8")
    seen = []

    def urlopen(request, timeout=0):
        seen.append((request.full_url, json.loads(request.data), request.headers))
        return io.BytesIO(b'{"buildId": "b1"}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert agent_checkouts.ship(checkout) == "codemagic build b1"
    url, body, headers = seen[0]
    assert body == {"appId": "app", "workflowId": "ios", "branch": "main"}
    assert headers["X-auth-token"] == "tok"


def test_a_run_waiting_for_input_texts_once_and_frees_its_slot(queue, monkeypatch):
    from core import agent_checkouts, coding_job_contract, scheduler_actions
    from fleet import supervisor

    task = store.enqueue_task(BRIEF)
    claimed = store.claim_next_task("d")
    store.mark_task_running(task["id"], "d", claimed["lease_token"], "run-w")
    monkeypatch.setattr(supervisor, "get_run", lambda run_id: {
        "state": "waiting_for_input", "error": "delivery requirements were not answered"})
    keys = []
    monkeypatch.setattr(scheduler_actions, "_notify_phone",
                        lambda text, key, **kw: keys.append(key) or True)
    reconcile = scheduler_actions.REVIEWED_ACTIONS["serena.fleet.reconcile"]
    reconcile({})
    reconcile({})
    assert store.get_memory(task["id"])["state"] == "running"
    assert len(keys) == 1 and keys[0].startswith(f"task:{task['id']}:waiting:")

    # The parked run does not count against the ceiling.
    monkeypatch.setenv("SERENA_TASK_MAX_ACTIVE_RUNS", "1")
    monkeypatch.setattr(coding_job_contract, "resolve_repository_root",
                        lambda text, project_hint="": Path("/repo"))
    monkeypatch.setattr(agent_checkouts, "prepare",
                        lambda source, task_id: SimpleNamespace(path=Path("/agents/y")))
    monkeypatch.setattr(supervisor, "list_runs", lambda limit=500: [
        {"origin_session_id": f"serena-task:{task['id']}", "run_id": "run-w",
         "state": "waiting_for_input"}])
    start = Mock(return_value={"run_id": "run-next"})
    monkeypatch.setattr(supervisor, "start_run", start)
    store.enqueue_task(BRIEF + " Also cover the empty name case.")
    outcome = scheduler_actions.REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert outcome.output.get("run_id") == "run-next"


def test_retry_command_reopens_a_blocked_run(hub, monkeypatch):
    from core import phone_line
    from fleet import supervisor

    task = store.enqueue_task(BRIEF)
    claimed = store.claim_next_task("d")
    store.mark_task_running(task["id"], "d", claimed["lease_token"], "run-r")
    store.finish_task_run(task["id"], "run-r", "blocked", "failed: old codex")
    retried = []
    monkeypatch.setattr(supervisor, "retry_run", lambda run_id: retried.append(run_id) or {})
    hub.state["inbound_watermark"] = "2026-09-16T11:00:00Z"
    hub.messages.append(hub.msg("r1", f"retry #{task['id']}", "2026-09-16T11:01:00Z"))
    hub.messages.append(hub.msg("r2", "retry #99999", "2026-09-16T11:02:00Z"))
    report = phone_line.poll(now=1000)
    assert [c["kind"] for c in report.commands] == ["retry", "retry"]
    assert retried == ["run-r"]
    row = store.get_memory(task["id"])
    assert (row["state"], row["run_id"], row["result"]) == ("running", "run-r", "")
    assert "retrying" in hub.sent[0] and "isn't blocked" in hub.sent[1]


def test_dispatcher_owned_runs_get_no_generic_blocked_alert():
    from fleet import attention

    run = {"run_id": "r", "origin_session_id": "serena-task:5", "state": "running",
           "phases": [{"legs": [{"leg_id": "l", "state": "waiting_for_input"}]}]}
    factory = Mock()
    attention.notify_blocked_run(Mock(), run, factory)
    factory.assert_not_called()


def test_delivery_rules_quote_fleet_requirements_verbatim():
    import json

    from core.scheduler_actions import _delivery_rules
    from fleet.completion import _delivery_failures
    from fleet.contracts import _completion_contract

    required = _completion_contract("coding", "")["delivery_requirements"]
    note = _delivery_rules(12)
    answers = json.loads(note[note.index("\n[") + 1:])
    answers[0]["evidence"] = "greet.py"
    failures, deferred, verified = _delivery_failures(required, answers)
    assert failures == [] and deferred == [] and verified == [required[0]]
    assert "serena/task-12" in note


def test_windows_test_reruns_keep_system_variables(monkeypatch):
    from fleet import completion_gate

    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setattr(completion_gate.os, "name", "nt")
    environment = completion_gate._test_environment({}, [])
    assert environment["SYSTEMROOT"] == "C:\\Windows"
    assert "ANTHROPIC_API_KEY" not in environment
    monkeypatch.setattr(completion_gate.os, "name", "posix")
    assert "SYSTEMROOT" not in completion_gate._test_environment({}, [])


def test_a_delivery_it_cannot_finish_is_not_retried_in_silence(queue, monkeypatch):
    """The run passed; pushing it did not. That is still an answer he is owed.

    Delivery fails on things only he can clear -- expired GitHub auth, a
    protected branch -- and the old path recorded the reason and retried every
    60s without ever mentioning it, so a finished job looked like a hung one.
    """

    from core import agent_checkouts, scheduler_actions
    from fleet import supervisor

    # Sourced the way his texts are, which is what makes the answer his.
    task = store.enqueue_task(BRIEF, source_id="imessage:12")
    claimed = store.claim_next_task("d")
    store.mark_task_running(task["id"], "d", claimed["lease_token"], "run-9")
    monkeypatch.setattr(supervisor, "get_run",
                        lambda run_id: {"state": "completed", "cwd": "/checkout"})
    checkout = SimpleNamespace(path="/checkout", branch=f"serena/task-{task['id']}")
    monkeypatch.setattr(agent_checkouts, "locate", lambda cwd: checkout)
    monkeypatch.setattr(agent_checkouts, "deliver", Mock(
        side_effect=agent_checkouts.CheckoutError("gh pr create failed: token is invalid")))
    texts = []
    monkeypatch.setattr(scheduler_actions, "_notify_phone",
                        lambda text, key, **kw: texts.append((key, text, kw.get("answers_request"))) or True)

    reconcile = scheduler_actions.REVIEWED_ACTIONS["serena.fleet.reconcile"]
    first = reconcile({})

    assert texts and "can't deliver it" in texts[0][1]
    assert "token is invalid" in texts[0][1]
    assert texts[0][2] is True, "he asked for this task, so it must not wait for morning"
    # It stays running so the next tick delivers once he clears the cause.
    assert store.get_memory(task["id"])["state"] == "running"
    assert first.output["closed"][0]["error"].startswith("delivery failed")

    # And the same reason does not text him again on every tick.
    reconcile({})
    assert len(texts) == 1


def test_an_internal_task_stuck_on_delivery_can_wait_for_morning(queue, monkeypatch):
    """Nobody is sitting on the other end of a queue write, so it is a notice."""

    from core import agent_checkouts, scheduler_actions
    from fleet import supervisor

    task = store.enqueue_task(BRIEF)  # no imessage source: internal
    claimed = store.claim_next_task("d")
    store.mark_task_running(task["id"], "d", claimed["lease_token"], "run-10")
    monkeypatch.setattr(supervisor, "get_run",
                        lambda run_id: {"state": "completed", "cwd": "/checkout"})
    checkout = SimpleNamespace(path="/checkout", branch=f"serena/task-{task['id']}")
    monkeypatch.setattr(agent_checkouts, "locate", lambda cwd: checkout)
    monkeypatch.setattr(agent_checkouts, "deliver", Mock(
        side_effect=agent_checkouts.CheckoutError("gh pr create failed: token is invalid")))
    seen = []
    monkeypatch.setattr(scheduler_actions, "_notify_phone",
                        lambda text, key, **kw: seen.append(kw.get("answers_request")) or True)

    scheduler_actions.REVIEWED_ACTIONS["serena.fleet.reconcile"]({})

    assert seen == [False]


def test_the_handoff_names_the_owner_the_validator_accepts():
    """The handoff used to forbid the only owner a dispatched run can defer to.

    It said "Never defer delivery to root" while fleet.completion rejects
    anything but "root" or a worker key -- so the worker on #1054 obeyed the
    handoff, wrote "Fleet supervisor", and burned an attempt on
    "deferred delivery named an owner Fleet cannot route to". The handoff also
    says the dispatcher opens the pull request, and that dispatcher is root.
    """

    import json

    from core.scheduler_actions import _delivery_rules
    from fleet.completion import _delivery_failures
    from fleet.contracts import _completion_contract

    note = _delivery_rules(1054)
    assert "Never defer delivery to root" not in note

    required = _completion_contract("coding", "")["delivery_requirements"]
    answers = json.loads(note[note.index("\n[") + 1:])

    # What the handoff now tells it to write is what the validator accepts.
    answers[0].update(state="deferred", reason="the dispatcher opens the PR",
                      owner="root")
    failures, deferred, _verified = _delivery_failures(required, answers)
    assert failures == [], failures
    assert deferred and deferred[0]["owner"] == "root"

    # And the prose it wrote last time is still refused, so the rule is real.
    answers[0]["owner"] = "Fleet supervisor"
    failures, _deferred, _verified = _delivery_failures(required, answers)
    assert any("cannot route to" in f for f in failures)


def test_a_task_that_cannot_even_start_is_not_blocked_in_silence(queue, monkeypatch):
    """No GitHub credential on the PC means no private checkout, so no run.

    The dispatcher held the task as blocked and said why only in its own run
    record, so the queue looked idle while a brief he texted never started.
    """

    from core import agent_checkouts, coding_job_contract, scheduler_actions

    task = store.enqueue_task(BRIEF, source_id="imessage:77")
    monkeypatch.setattr(coding_job_contract, "resolve_repository_root",
                        lambda *a, **k: Path("/repo"))
    monkeypatch.setattr(scheduler_actions, "resolve_repository_root",
                        lambda *a, **k: Path("/repo"), raising=False)
    monkeypatch.setattr(agent_checkouts, "prepare", Mock(
        side_effect=agent_checkouts.CheckoutError(
            "git -C failed: fatal: could not read Username for 'https://github.com'")))
    texts = []
    monkeypatch.setattr(scheduler_actions, "_notify_phone",
                        lambda text, key, **kw: texts.append((key, text, kw.get("answers_request"))) or True)

    outcome = scheduler_actions.REVIEWED_ACTIONS["serena.fleet.start"]({})

    assert outcome.output["state"] == "blocked"
    assert texts, "a task that cannot start has to say so"
    assert "can't even start" in texts[0][1]
    assert "could not read Username" in texts[0][1]
    assert texts[0][2] is True
    assert store.get_memory(task["id"])["state"] == "blocked"

    # The same wall does not text him again every minute.
    store.enqueue_task(BRIEF + " again", source_id="imessage:78")
    scheduler_actions.REVIEWED_ACTIONS["serena.fleet.start"]({})
    assert len(texts) == 2, "a different task is a different notice"


def test_a_base_that_cannot_be_refreshed_still_produces_a_checkout(github):
    """Expired GitHub auth must not stop work from starting.

    `prepare` fetches origin every time the base already exists, and that is
    the only credential-dependent step before a run. On the PC it failed with
    "could not read Username for 'https://github.com'", so a brief he texted
    was held blocked with no run at all. origin/<default> is already a real
    commit, so an unreachable remote is a stale base, not a dead queue.
    """

    from core import agent_checkouts

    first = agent_checkouts.prepare(github.synced, 5, projects_root=github.projects)
    assert first.stale_base is False

    # Point the base at a remote that cannot be reached, exactly as an expired
    # credential does, and prepare a second task against the same base.
    agent_checkouts._git(first.base, "remote", "set-url", "origin",
                         "https://github.com/duaragha/does-not-exist-offline.git")
    second = agent_checkouts.prepare(github.synced, 6, projects_root=github.projects)

    assert second.stale_base is True, "an unreachable remote is a stale base"
    assert second.branch == "serena/task-6"
    assert (second.path / "README.md").read_text(encoding="utf-8") == "demo\n"
    assert agent_checkouts.locate(second.path).branch == "serena/task-6"


def test_a_stale_base_is_recorded_on_the_dispatch(queue, monkeypatch):
    from core import agent_checkouts, coding_job_contract, scheduler_actions
    from fleet import supervisor

    task = store.enqueue_task(BRIEF, source_id="imessage:90")
    monkeypatch.setattr(coding_job_contract, "resolve_repository_root",
                        lambda *a, **k: Path("/repo"))
    checkout = SimpleNamespace(path="/checkout", branch=f"serena/task-{task['id']}",
                               stale_base=True)
    monkeypatch.setattr(agent_checkouts, "prepare", lambda *a, **k: checkout)
    monkeypatch.setattr(supervisor, "list_runs", lambda **k: [])
    monkeypatch.setattr(supervisor, "start_run", lambda **k: {"run_id": "run-90"})

    outcome = scheduler_actions.REVIEWED_ACTIONS["serena.fleet.start"]({})

    assert outcome.output.get("stale_base") is True
    assert outcome.output.get("run_id") == "run-90"


def test_a_refused_fleet_start_says_what_refused_it(queue, monkeypatch):
    """"requires reconciliation: ValueError" hid the actual cause for an hour.

    Fleet's shipped config had drifted from PHASE_MODEL_POLICY, so load_config
    raised inside start_run and every dispatch reported a bare type name. The
    message is the whole diagnosis, and it is also what gets texted to him.
    """

    from core import agent_checkouts, coding_job_contract, scheduler_actions
    from fleet import supervisor

    store.enqueue_task(BRIEF, source_id="imessage:91")
    monkeypatch.setattr(coding_job_contract, "resolve_repository_root",
                        lambda *a, **k: Path("/repo"))
    monkeypatch.setattr(agent_checkouts, "prepare", lambda *a, **k: SimpleNamespace(
        path="/checkout", branch="serena/task-91", stale_base=False))
    monkeypatch.setattr(supervisor, "list_runs", lambda **k: [])
    monkeypatch.setattr(supervisor, "start_run", Mock(side_effect=ValueError(
        "research verify workers must match Fleet's fixed model policy")))
    texts = []
    monkeypatch.setattr(scheduler_actions, "_notify_phone",
                        lambda text, key, **kw: texts.append(text) or True)

    outcome = scheduler_actions.REVIEWED_ACTIONS["serena.fleet.start"]({})

    assert "ValueError" in outcome.detail
    assert "fixed model policy" in outcome.detail, outcome.detail
    # And the same detail is what reaches him, not a bare type name.
    assert texts and "fixed model policy" in texts[0]


def test_a_failure_with_no_message_still_reads_as_something():
    from core.scheduler_actions import _why

    assert _why(ValueError("boom")) == "ValueError: boom"
    assert _why(ValueError()) == "ValueError: no detail"
    assert len(_why(ValueError("x" * 500))) < 240


# ---- the PR that did nothing ----------------------------------------------


def test_the_handoff_says_what_the_checkout_cannot_show(queue):
    """A clone holds what git tracks. memory/ is not tracked, so the queue is absent.

    Task #1077 asked a worker to audit the active task list and complete the
    highest-impact item. Its checkout had no memory/task directory at all, so
    it found nothing, invented an unrelated refactor of core/indexer.py and
    reported success. Reporting success for different work is worse than
    failing, so the handoff now names what is missing and forbids substituting.
    """

    from core.scheduler_actions import _unseen_state_rules

    note = _unseen_state_rules("fix the routines list in locket")

    assert "only files git tracks are present" in note
    assert "memory/" in note
    assert "do not substitute different work" in note
    assert "do not report success" in note
    # Nothing to attach for a brief that is not about his queue.
    assert "His open tasks" not in note


def test_a_brief_about_his_tasks_carries_the_tasks(queue):
    from core.scheduler_actions import _unseen_state_rules

    store.enqueue_task("Fix the routines list in locket so it shows this week",
                       source_id="imessage:1")
    note = _unseen_state_rules("audit the active task list and do the highest impact one")

    assert "His open tasks" in note
    assert "routines list in locket" in note


def test_the_attached_task_list_is_bounded(queue, monkeypatch):
    from core import scheduler_actions

    monkeypatch.setattr(scheduler_actions, "MAX_ATTACHED_TASKS", 3)
    for index in range(8):
        store.enqueue_task(f"Fix thing number {index} in locket right now",
                           source_id=f"imessage:{index}")

    note = scheduler_actions._unseen_state_rules("work through my backlog")

    assert note.count("\n  #") <= 3, "an unbounded queue would flood the worker's prompt"


def test_every_queue_word_pulls_the_list_in(queue):
    from core.scheduler_actions import _unseen_state_rules

    store.enqueue_task("Fix the routines list in locket so it shows this week",
                       source_id="imessage:1")
    for phrasing in ("look through all open tasks", "clear my backlog",
                     "what is in the queue", "finish my todo list",
                     "complete the highest impact task"):
        assert "His open tasks" in _unseen_state_rules(phrasing), phrasing
