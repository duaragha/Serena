"""Task ownership is durable across processes without migrating legacy files."""

import multiprocessing
from pathlib import Path

import pytest

from memory import store

BRIEF = "Fix memory/store.py so expired task claims can be reclaimed safely."


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(store, "_active_v2_store", lambda: None)
    monkeypatch.setattr(store, "_source_context", lambda: ("", "", "", ""))
    import memory.locket_mirror as mirror
    for name in ("mirror_add", "mirror_update", "mirror_delete", "mirror_archive"):
        monkeypatch.setattr(mirror, name, lambda *a, **kw: None)
    return tmp_path


def legacy(root, mid=1, extra=""):
    directory = root / "task"
    directory.mkdir(exist_ok=True)
    path = directory / f"{mid:03d}-old.md"
    path.write_text(f"---\nid: {mid}\ntype: task\ncreated: 2026-01-01 00:00:00\n"
                    f"updated: 2026-01-01 00:00:00\n{extra}---\n\nlegacy task\n", encoding='utf-8')
    return path


def test_legacy_defaults_do_not_rewrite_or_change_task_surface(queue):
    path = legacy(queue)
    before = path.read_bytes()
    row = store.get_memory(1)
    # Legacy work is human-owned: it is listed as before but never dispatched.
    assert (row["state"], row["assignee"], row["priority"]) == ("backlog", "", "normal")
    assert "- [1] legacy task" in store.format_tasks()
    assert "legacy task" in store.format_active()
    assert path.read_bytes() == before


@pytest.mark.parametrize("text,project,state", [
    ("locket is broken", None, "needs_triage"),
    ("fix it", "/repo", "needs_triage"),
    ("the whole application is broken and I really need help now", "/repo", "needs_triage"),
    (BRIEF, None, "ready"),
    ("Add a settings screen with a working dark mode toggle", "serena", "ready"),
])
def test_triage(queue, text, project, state):
    task = store.enqueue_task(text, project_hint=project)
    assert task["state"] == state
    assert store.get_memory(task["id"])["state"] == state


@pytest.mark.parametrize("kwargs", [
    {"text": ""}, {"text": "x" * 4001}, {"text": []},
    {"text": BRIEF, "project_hint": "x" * 513},
    {"text": BRIEF, "project_hint": "ok\nstate: ready"},
    {"text": BRIEF, "priority": "urgent"},
    {"text": BRIEF, "priority": []},
    {"text": BRIEF, "source_id": "id\nstate: done"},
])
def test_enqueue_validation_has_no_task_side_effect(queue, kwargs):
    with pytest.raises(ValueError):
        store.enqueue_task(**kwargs)
    assert store.list_memories("task") == []


@pytest.mark.parametrize("text", [
    "Please fix it because it is still broken today",
    "Please can you just fix the thing now because it is really broken again today",
    "investigate the weird strange annoying behaviour that sometimes happens randomly lately",
    "fix fix fix fix fix fix fix fix fix fix fix fix",
])
def test_padded_vague_briefs_stay_in_triage_with_a_project_hint(queue, text):
    """Length and a repository are not a specification. Only the brief can name a target."""
    assert store.classify_task(text, "serena") == "needs_triage"
    assert store.enqueue_task(text, project_hint="serena")["state"] == "needs_triage"


@pytest.mark.parametrize("text", [
    BRIEF,
    "Add a settings screen with a working dark mode toggle",
    "locket is broken",
    "Please fix it because it is still broken today",
])
def test_project_hint_never_changes_triage(queue, text):
    assert store.classify_task(text, "serena") == store.classify_task(text, None)


def test_line_break_constant_covers_every_splitlines_separator():
    """The parser splits on all of these, so validation must reject all of them."""
    assert {chr(code) for code in range(0x110000)
            if len(f"x{chr(code)}x".splitlines()) > 1} == store._LINE_BREAKS


SEPARATORS = sorted(store._LINE_BREAKS)


@pytest.mark.parametrize("separator", SEPARATORS)
@pytest.mark.parametrize("field", ["project_hint", "source_id"])
def test_enqueue_metadata_separators_cannot_forge_state(queue, separator, field):
    thin = "locket is broken"
    with pytest.raises(ValueError):
        store.enqueue_task(thin, **{field: f"serena{separator}state: ready"})
    assert store.list_memories("task") == []
    assert store.enqueue_task(thin)["state"] == "needs_triage"


@pytest.mark.parametrize("separator", SEPARATORS)
def test_transition_metadata_separators_are_rejected(queue, separator):
    task = store.enqueue_task(BRIEF)
    with pytest.raises(ValueError):
        store.claim_next_task(f"worker{separator}state: done")
    claim = store.claim_next_task("worker")
    with pytest.raises(ValueError):
        store.mark_task_running(task["id"], "worker", claim["lease_token"],
                                f"run{separator}state: done")
    row = store._parse_file(store._find_path(task["id"]))
    with pytest.raises(ValueError):
        store._task_metadata(row, assignee=f"worker{separator}state: done")
    assert store.get_memory(task["id"])["state"] == "claimed"


@pytest.mark.parametrize("separator", SEPARATORS)
def test_free_text_frontmatter_round_trips_as_one_field(queue, separator):
    """A captured chat title shares a task's frontmatter; it cannot open a second line."""
    forged = f"chat{separator}state: done"
    store._write_file(1, "task", BRIEF, source_title=forged,
                      task_fields={"state": "needs_triage"})
    row = store.get_memory(1)
    assert row["state"] == "needs_triage"
    assert row["source_title"] == forged.replace(separator, " ")


def test_dedupe_and_v2_independence(queue, monkeypatch):
    monkeypatch.setattr(store, "_active_v2_store", lambda: pytest.fail("queue must not propose"))
    first = store.enqueue_task(BRIEF, source_id="webhook:receipt", priority="high")
    retry = store.enqueue_task("locket is broken", source_id="webhook:receipt")
    assert retry == first
    assert len(store.list_memories("task")) == 1


def test_priority_ties_snooze_and_nonready(queue):
    low = store.enqueue_task(BRIEF, priority="low")
    high = store.enqueue_task(BRIEF, priority="high")
    high2 = store.enqueue_task(BRIEF, priority="high")
    snoozed = store.enqueue_task(BRIEF, priority="critical")
    store.snooze_memory(snoozed["id"])
    store.enqueue_task("locket is broken", priority="critical")
    assert [store.claim_next_task("worker")["id"] for _ in range(3)] == [high["id"], high2["id"], low["id"]]
    assert store.claim_next_task("worker") is None


def test_expiry_fences_old_owner_even_with_same_owner_name(queue):
    task = store.enqueue_task(BRIEF)
    first = store.claim_next_task("same", now=100)
    assert store.claim_next_task("other", now=129) is None
    second = store.claim_next_task("same", now=130)
    assert first["lease_token"] != second["lease_token"]
    assert not store.mark_task_running(task["id"], "same", first["lease_token"], "old", now=131)
    assert not store.release_task_claim(task["id"], "same", first["lease_token"], now=131)
    assert store.mark_task_running(task["id"], "same", second["lease_token"], "run", now=131)
    assert store.get_memory(task["id"])["run_id"] == "run"


def test_running_expiry_blocks_and_retains_run_receipt(queue):
    task = store.enqueue_task(BRIEF)
    claim = store.claim_next_task("worker", now=100)
    assert store.mark_task_running(task["id"], "worker", claim["lease_token"], "run", now=101)
    assert store.claim_next_task("other", now=101 + store.TASK_WORK_SECONDS) is None
    row = store.get_memory(task["id"])
    assert (row["state"], row["run_id"], row["assignee"]) == ("blocked", "run", "")


@pytest.mark.parametrize("state", ["ready", "blocked", "needs_triage", "done"])
def test_conditional_release(queue, state):
    task = store.enqueue_task(BRIEF)
    claim = store.claim_next_task("worker", now=100)
    assert not store.release_task_claim(task["id"], "intruder", claim["lease_token"], state=state, now=101)
    assert store.release_task_claim(task["id"], "worker", claim["lease_token"], state=state, now=101)
    assert store.get_memory(task["id"])["state"] == state


def test_renew_and_finish_running(queue):
    task = store.enqueue_task(BRIEF)
    claim = store.claim_next_task("worker", now=100)
    token = claim["lease_token"]
    assert store.renew_task_claim(task["id"], "worker", token, now=120, lease_seconds=60)
    assert store.mark_task_running(task["id"], "worker", token, "run", now=150)
    assert not store.release_task_claim(task["id"], "worker", token, now=151)
    assert store.release_task_claim(task["id"], "worker", token, state="done", now=151)
    assert not store.renew_task_claim(task["id"], "worker", token, now=152)


def test_legacy_rewriters_preserve_operational_fields(queue):
    task = store.enqueue_task(BRIEF, project_hint="serena", priority="critical", source_id="receipt")
    claim = store.claim_next_task("worker")
    store.set_locket_id(task["id"], 7)
    store.update_memory(task["id"], content=BRIEF + " Preserve source fields.")
    store.snooze_memory(task["id"])
    row = store.get_memory(task["id"])
    for key in store._TASK_FIELDS:
        assert row[key] == claim[key]
    assert row["locket_id"] == "7"


def test_claim_preserves_unknown_frontmatter_and_body(queue):
    path = legacy(queue, extra="custom: keep me\nsource_session_id: abc\n")
    body = path.read_text(encoding="utf-8").split("---", 2)[2]
    store.claim_next_task("worker")
    assert "custom: keep me" in path.read_text(encoding="utf-8")
    assert path.read_text(encoding="utf-8").split("---", 2)[2] == body
    assert store.get_memory(1)["source_session_id"] == "abc"


def test_locket_stamp_does_not_leave_duplicate_legacy_filename(queue):
    legacy(queue)
    store.set_locket_id(1, 42)
    assert len(list((queue / "task").glob("*.md"))) == 1
    assert store.get_memory(1)["locket_id"] == "42"
    assert store.claim_next_task("worker") is None


def test_bad_state_is_triage_and_corrupt_lease_is_recoverable(queue):
    legacy(queue, extra="state: whatever\n")
    assert store.get_memory(1)["state"] == "needs_triage"
    legacy(queue, mid=2, extra="state: claimed\nlease_until: nan\nassignee: dead\n"
                               "source_id: webhook:d2\n")
    assert store.claim_next_task("worker")["id"] == 2


def test_failed_publication_retains_old_file(queue, monkeypatch):
    task = store.enqueue_task(BRIEF)
    path = store._find_path(task["id"])
    before = path.read_bytes()
    def fail(*args):
        raise OSError("injected replace failure")
    monkeypatch.setattr(store.os, "replace", fail)
    with pytest.raises(OSError):
        store.claim_next_task("worker")
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".memory-*"))


def test_queue_capacity(queue, monkeypatch):
    monkeypatch.setattr(store, "MAX_TASKS", 1)
    task = store.enqueue_task(BRIEF, source_id="receipt")
    assert store.enqueue_task(BRIEF, source_id="receipt")["id"] == task["id"]
    with pytest.raises(ValueError, match="capacity"):
        store.enqueue_task(BRIEF)


def test_deleted_ids_are_not_reused_by_any_writer(queue):
    # Each space is monotonic on its own: finishing a task frees nothing,
    # because a dispatch receipt outlives the task it names.
    task = store.enqueue_task(BRIEF)
    assert store.delete_memory(task["id"], "task")
    assert store.enqueue_task(BRIEF)["id"] > task["id"]
    memory_id = store.add_memory("a note", _no_mirror=True)
    assert store.delete_memory(memory_id, "general")
    assert store.add_memory("another note", _no_mirror=True) > memory_id


def test_tasks_and_memories_count_in_separate_sequences(queue):
    """His todo list numbers itself. A passing note never pushes it along."""
    first = store.enqueue_task(BRIEF)["id"]
    for i in range(5):
        store.add_memory(f"note {i}", _no_mirror=True)
    assert store.enqueue_task(BRIEF, source_id="second")["id"] == first + 1


def test_a_stray_high_id_does_not_drag_the_sequence_up(queue):
    """A task imported from a machine that predated the counter is an
    outlier, not a new floor. It pinned his whole todo list in the 1100s."""
    store.enqueue_task(BRIEF, source_id="first")
    stray = queue / "task" / "1120-imported.md"
    stray.write_text("---\nid: 1120\ntype: task\ncreated: 2026-01-01 00:00:00\n"
                     "updated: 2026-01-01 00:00:00\n---\n\nqueued elsewhere\n",
                     encoding="utf-8")
    assert store.enqueue_task(BRIEF, source_id="second")["id"] == 2
    # and the outlier is still never overwritten
    assert store.get_memory(1120, "task")["content"] == "queued elsewhere"


def test_the_counter_survives_deleting_the_highest_task(queue):
    """Deleting the newest task must not hand its id to the next one."""
    store.enqueue_task(BRIEF, source_id="a")
    second = store.enqueue_task(BRIEF, source_id="b")["id"]
    assert store.delete_memory(second, "task")
    assert store.enqueue_task(BRIEF, source_id="c")["id"] > second


def test_an_id_naming_both_spaces_refuses_to_resolve(queue):
    """A bare number is a question once the two spaces overlap."""
    task = store.enqueue_task(BRIEF)
    memory_id = store.add_memory("a note", _no_mirror=True)
    assert memory_id == task["id"]
    with pytest.raises(store.AmbiguousMemoryId):
        store._find_path(task["id"])
    assert store._find_path(task["id"], "task").parent.name == "task"
    assert store._find_path(memory_id, "general").parent.name == "general"
    # Deleting one leaves the other untouched.
    assert store.delete_memory(task["id"], "task")
    assert store.get_memory(memory_id, "general")["content"] == "a note"




def test_duplicate_task_identity_fails_closed(queue):
    path = legacy(queue)
    (path.parent / "001-second.md").write_bytes(path.read_bytes())
    assert store.claim_next_task("worker") is None
    with pytest.raises(store.AmbiguousMemoryId, match="reconcile duplicate files"):
        store.get_memory(1, "task")


def test_task_edit_and_locket_stamp_replace_the_same_path(queue, monkeypatch):
    """A slug change must never publish two task files, even briefly."""
    path = legacy(queue, extra="custom: preserve me\nstate: running\nrun_id: run-a\n")
    original = store._atomic_text
    publications = []

    def observe(target, text):
        if target.parent == path.parent:
            publications.append(target)
            assert target == path
        original(target, text)

    monkeypatch.setattr(store, "_atomic_text", observe)
    store.update_memory(1, content=BRIEF, find_type="task")
    store.set_locket_id(1, 42, "task")
    assert publications == [path, path]
    assert list(path.parent.glob("*.md")) == [path]
    row = store.get_memory(1, "task")
    assert (row["content"], row["state"], row["run_id"], row["locket_id"]) == (BRIEF, "running", "run-a", "42")


def test_type_moves_cannot_overwrite_the_other_id_space(queue):
    task = store.enqueue_task(BRIEF, source_id="original-event")
    claim = store.claim_next_task("worker")
    assert store.mark_task_running(task["id"], "worker", claim["lease_token"], "original-run")
    reference = store.add_memory("Unrelated reference note", "reference", _no_mirror=True)
    assert task["id"] == reference
    paths = [store._find_path(reference, kind) for kind in ("task", "reference")]
    before = [path.read_bytes() for path in paths]
    for origin, target in (("reference", "task"), ("task", "reference")):
        with pytest.raises(store.AmbiguousMemoryId, match="already exists"):
            store.update_memory(reference, content="Must not overwrite", mem_type=target, find_type=origin)
    assert [path.read_bytes() for path in paths] == before


def test_conflicting_ids_leave_other_dispatch_and_reconciliation_working(queue):
    conflicted = store.enqueue_task(BRIEF, source_id="conflicted-event")
    path = store._find_task_path(conflicted["id"])
    copy = path.parent / "manually-renamed-copy.md"
    copy.write_bytes(path.read_bytes())
    before = (path.read_bytes(), copy.read_bytes())
    healthy = store.enqueue_task(BRIEF, source_id="healthy-event")
    claim = store.claim_next_task("worker")
    assert claim["id"] == healthy["id"]
    assert store.mark_task_running(healthy["id"], "worker", claim["lease_token"], "healthy-run")
    assert [row["id"] for row in store.tasks_in_state("running")] == [healthy["id"]]
    assert store.finish_task_run(healthy["id"], "healthy-run", "done")
    assert (path.read_bytes(), copy.read_bytes()) == before
    # Replaying the conflicted ingress cannot allocate another task or run.
    with pytest.raises(store.AmbiguousMemoryId, match="source receipt"):
        store.enqueue_task(BRIEF, source_id="conflicted-event")
    with pytest.raises(store.AmbiguousMemoryId):
        store.mark_task_asked(conflicted["id"])
    assert len(list(path.parent.glob("*.md"))) == 3


def test_running_duplicate_is_not_finished_or_requeued(queue):
    task = store.enqueue_task(BRIEF)
    claim = store.claim_next_task("worker")
    assert store.mark_task_running(task["id"], "worker", claim["lease_token"], "run-a")
    path = store._find_task_path(task["id"])
    copy = path.parent / "001-sync-conflict.md"
    copy.write_bytes(path.read_bytes())
    before = path.read_bytes()
    assert store.tasks_in_state("running") == []
    with pytest.raises(store.AmbiguousMemoryId):
        store.finish_task_run(task["id"], "run-a", "done")
    assert store.claim_next_task("worker", now=10**10) is None
    assert path.read_bytes() == copy.read_bytes() == before


def test_duplicate_cannot_disable_the_real_fleet_schedule_handlers(queue, monkeypatch):
    from core import job_cards, scheduler_actions
    from core.serena_scheduler import MAX_CONSECUTIVE_FAILURES, SerenaScheduler
    from fleet import supervisor

    monkeypatch.setenv("SERENA_CONTROL_PLANE_DB_PATH", str(queue / "control.sqlite3"))
    monkeypatch.setenv("SERENA_DISPATCH_CONFIG", str(queue / "no-publication-config.json"))
    conflicted = store.enqueue_task(BRIEF, source_id="conflicted-event")
    healthy = store.enqueue_task(BRIEF, source_id="healthy-event")
    # Put the healthy task into a real run receipt before introducing a
    # duplicate into the sourced ready task. No model/provider is dispatched.
    claim = store.claim_next_task("worker")
    assert claim["id"] == conflicted["id"]
    assert store.release_task_claim(claim["id"], "worker", claim["lease_token"], state="blocked")
    claim = store.claim_next_task("worker")
    assert store.mark_task_running(healthy["id"], "worker", claim["lease_token"], "healthy-run")
    path = store._find_task_path(conflicted["id"])
    store._task_metadata(store._parse_file(path), state="ready")
    (path.parent / "duplicate.md").write_bytes(path.read_bytes())
    observed = []
    monkeypatch.setattr(supervisor, "get_run", lambda run_id: observed.append(run_id) or None)
    monkeypatch.setattr(job_cards, "show", lambda *args, **kwargs: True)
    monkeypatch.setattr(scheduler_actions, "_task_label", lambda task: "test task")
    scheduler = SerenaScheduler(queue / "scheduler.sqlite3", handlers={
        "serena.fleet.start": scheduler_actions.start_ready_fleet_task,
        "serena.fleet.reconcile": scheduler_actions.reconcile_fleet_tasks,
    }, notifier=None)
    schedules = [scheduler.add_schedule(action=action, interval_seconds=60,
                 actor="test", requires_approval=False, first_run_at=1_000_000)
                 for action in ("serena.fleet.start", "serena.fleet.reconcile")]
    for tick in range(MAX_CONSECUTIVE_FAILURES + 2):
        runs = scheduler.tick(now=1_000_000 + tick * 61)
        assert len(runs) == 2 and all(run.ok for run in runs)
    assert observed == ["healthy-run"] * (MAX_CONSECUTIVE_FAILURES + 2)
    assert all(scheduler.require(s["schedule_id"])["state"] == "active" for s in schedules)


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf"), 86401])
def test_invalid_lease_rejected(queue, seconds):
    store.enqueue_task(BRIEF)
    with pytest.raises(ValueError):
        store.claim_next_task("worker", lease_seconds=seconds)
    assert store.list_memories("task")[0]["state"] == "ready"


def test_closed_state_and_owner_validation(queue):
    store.enqueue_task(BRIEF)
    with pytest.raises(ValueError):
        store.claim_next_task("worker\nstate: done")
    claim = store.claim_next_task("worker")
    with pytest.raises(ValueError):
        store.release_task_claim(claim["id"], "worker", claim["lease_token"], state="unknown")
    with pytest.raises(ValueError):
        store._write_file(claim["id"], "task", BRIEF, task_fields={"state": "unknown"})


def test_done_is_retained_but_not_in_active_tasks(queue):
    task = store.enqueue_task(BRIEF)
    claim = store.claim_next_task("worker")
    assert store.release_task_claim(task["id"], "worker", claim["lease_token"], state="done")
    assert store.format_tasks() == store.format_active() == ""
    assert store.list_memories("task")[0]["state"] == "done"


def test_mirror_runs_outside_lock_and_can_reenter(queue, monkeypatch):
    import memory.locket_mirror as mirror
    called = []
    def callback(content, kind, mid, **kwargs):
        assert not store._WRITE_LOCAL.held
        store.set_locket_id(mid, 99)
        called.append(mid)
    monkeypatch.setattr(mirror, "mirror_add", callback)
    mid = store.add_memory(BRIEF, mem_type="task")
    assert called == [mid]
    assert store.get_memory(mid)["locket_id"] == "99"


def test_lock_contention_is_bounded(queue):
    with ((queue / ".task-queue.lock").open("a+b") as handle,
          store.exclusive_lock(handle, timeout=1), pytest.raises(TimeoutError)):
        store.claim_next_task("worker")
    assert store.claim_next_task("worker") is None


def _race_worker(root, barrier, results, enqueue):
    from memory import store as child_store
    child_store.MEMORY_DIR = Path(root)
    barrier.wait(timeout=10)
    if enqueue:
        result = child_store.enqueue_task(BRIEF, source_id="same-delivery")
    else:
        result = child_store.claim_next_task(str(multiprocessing.current_process().pid))
    results.put(result)


@pytest.mark.parametrize("enqueue", [False, True])
def test_two_process_race(queue, enqueue):
    if not enqueue:
        store.enqueue_task(BRIEF)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [context.Process(target=_race_worker, args=(str(queue), barrier, results, enqueue))
                 for _ in range(2)]
    try:
        for process in processes:
            process.start()
        rows = [results.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        if enqueue:
            assert rows[0]["id"] == rows[1]["id"]
            assert len(store.list_memories("task")) == 1
        else:
            assert sum(row is not None for row in rows) == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()


@pytest.mark.parametrize("brief", [
    "In unified when I click search, especially on mobile, it should bring the "
    "keyboard up right away and I should be able to type instantly",
    "There shouldn't be up and down buttons, I should just be able to hold the "
    "exercise and drag it within the routine",
    "Get rid of the rename, up, down, less and more buttons on the routines page",
])
def test_a_requirement_he_states_is_work_not_chat(brief):
    """He writes what it should do, not the verb a triager expects. Reading
    these as too vague bounced his clearest briefs back to him."""
    assert store.classify_task(brief) == "ready"


@pytest.mark.parametrize("brief", [
    "please fix it, it is still broken",
    "how is it going today",
    "it should work",
])
def test_vague_or_chatty_text_still_gets_one_question(brief):
    assert store.classify_task(brief) == "needs_triage"


def test_a_specific_brief_is_not_bounced_for_using_his_own_verbs(queue):
    """#106 sat in triage for a day because 'allow' and 'swap' were not listed."""

    brief = ("Locket workouts: allow swapping an exercise during an active workout "
             "without ending the session or losing logged sets; search the existing "
             "exercise database first, then offer AI-assisted lookup for missing "
             "exercises, and save confirmed additions for future searches.")
    assert store.classify_task(brief) == "ready"
    # A long brief with no listed verb still counts on its own specificity.
    assert store.classify_task(
        "the workout timer on the Locket session screen resets itself to zero every "
        "time the app returns from the background, so a logged set loses its elapsed "
        "time and the summary at the end reports the wrong duration entirely") == "ready"
    # Vagueness is still vagueness, however long he rambles.
    assert store.classify_task("it is still broken") == "needs_triage"
    assert store.classify_task(
        "hey so anyway i was thinking about the thing we talked about the other day "
        "and you know how it goes sometimes with these things when they happen") == "needs_triage"


def test_his_answer_settles_triage_instead_of_being_graded_again(queue):
    """He answered #106 three times; the classifier bounced every one."""

    task = store.enqueue_task("locket is broken", source_id="imessage:x")
    assert task["state"] == "needs_triage"
    answered = store.answer_triage(task["id"], "the workouts tab, swapping exercises")
    assert answered["state"] == "ready"
    thin = store.enqueue_task("fix it", source_id="imessage:y")
    assert store.answer_triage(thin["id"], "yeah")["state"] == "needs_triage"


@pytest.mark.parametrize("brief,expected", [
    ("Fix routines so exercises keep their logged sets and reps", "locket"),
    ("Fix whatsapp message search so inbox results load older messages", "unified"),
    ("Fix exercise suggestions in Unified when a workout message arrives", "unified"),
    ("Fix exercise messages in the inbox when a workout is saved", ""),
    ("Fix search results so the dashboard shows the next page", ""),
])
def test_queue_project_inference_is_specific_and_rejects_mixed_domains(queue, brief, expected):
    from core.task_projects import infer_task_project

    assert infer_task_project(brief) == expected
    task = store.enqueue_task(brief)
    named = infer_task_project(brief, domains=False)
    assert task["project_hint"] == named
    assert store.get_memory(task["id"])["project_hint"] == named


def test_project_only_answer_unsticks_a_complete_brief_but_not_a_vague_one(queue):
    task = store.enqueue_task("Fix the dashboard charts so values refresh when selecting another day")
    claim = store.claim_next_task("dispatcher")
    assert store.release_task_claim(task["id"], "dispatcher", claim["lease_token"],
                                    state="needs_triage", result="which repository?")
    assert store.mark_task_asked(task["id"])
    answered = store.answer_triage(task["id"], "locket")
    assert (answered["state"], answered["project_hint"], answered["asked_at"], answered["result"]) == (
        "ready", "locket", "", "")
    thin = store.enqueue_task("fix it")
    answered = store.answer_triage(thin["id"], "unified")
    assert (answered["state"], answered["project_hint"]) == ("needs_triage", "unified")


def test_dispatch_triage_bounce_clears_a_previous_question_receipt(queue):
    task = store.enqueue_task(BRIEF)
    claim = store.claim_next_task("dispatcher")
    path = store._find_task_path(task["id"])
    store._task_metadata(store._parse_file(path), asked_at=123)
    assert store.release_task_claim(task["id"], "dispatcher", claim["lease_token"],
                                    state="needs_triage", result="which repo?\nstate: done")
    bounced = store.get_memory(task["id"])
    assert bounced["asked_at"] == ""
    assert bounced["result"] == "which repo? state: done"
    assert store.mark_task_asked(task["id"])


def test_a_mixed_project_answer_does_not_silently_reuse_an_existing_hint(queue):
    task = store.enqueue_task("fix it", project_hint="locket")
    assert store.mark_task_asked(task["id"])
    answered = store.answer_triage(task["id"], "it's in both locket and unified")
    assert (answered["state"], answered["project_hint"], answered["asked_at"]) == (
        "needs_triage", "", "")
    assert "multiple projects" in answered["result"]
    assert store.claim_next_task("dispatcher") is None
    corrected = store.answer_triage(task["id"], "unified")
    assert (corrected["state"], corrected["project_hint"]) == ("needs_triage", "unified")
    assert store.answer_triage(task["id"], "it's in locket")["state"] == "needs_triage"


def test_project_only_correction_keeps_genuine_earlier_specification(queue):
    task = store.enqueue_task("fix it")
    answered = store.answer_triage(task["id"], "fix dashboard charts so values refresh after selecting another day")
    claim = store.claim_next_task("dispatcher")
    assert store.release_task_claim(task["id"], "dispatcher", claim["lease_token"], state="needs_triage")
    corrected = store.answer_triage(task["id"], "it's in locket")
    assert answered["state"] == corrected["state"] == "ready"
    assert corrected["project_hint"] == "locket"
