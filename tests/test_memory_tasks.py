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
    with pytest.raises(ValueError, match="duplicate task id"):
        store.claim_next_task("worker")


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
