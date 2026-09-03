"""Serena decides when to start coding work; the broker keeps her honest.

The old design let a regex decide before she ever saw the words, so ordinary
asks silently never became jobs. These tests pin the new contract: her
judgement is the authority, and the broker only proves the request is
grounded in a real spoken turn.
"""

from __future__ import annotations

import subprocess

import pytest

from core.voice_inbox import VoiceInboxStore
from core.work_authority import WorkAuthorityResult, authority_denial, start_coding_work
from core.work_session_router import WorkRoute


class _Inbox:
    def __init__(self, *, lease: bool = True) -> None:
        self.lease = lease
        self.enqueued: list[tuple[str, str, str]] = []

    def resident_lease_active(self) -> bool:
        return self.lease

    def enqueue(self, request: str, *, call_id: str, turn_id: str):
        self.enqueued.append((request, call_id, turn_id))
        return type("Item", (), {"item_id": f"item-{len(self.enqueued)}"})()


def _turn(text: str, protocol: str = "voice") -> dict:
    return {
        "text": text,
        "protocol": protocol,
        "call_id": "desk-1",
        "turn_id": "desk-1:3",
    }


def _git_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Serena Test",
            "-c",
            "user.email=serena@example.test",
            "commit",
            "-qm",
            "baseline",
        ],
        check=True,
    )
    return path.resolve()


def _brief(repo) -> dict:
    return {
        "project": str(repo),
        "relevant_conversation": [],
        "project_context": [],
        "memory_guidance": [],
        "ledger_guidance": [],
        "handoff_guidance": [],
        "requested_outcome": "complete the requested change",
        "acceptance_criteria": ["the requested behavior works"],
        "authority_boundaries": ["do not commit"],
        "commit_authorized": False,
    }


def _new_chat(root, _spoken) -> WorkRoute:
    return WorkRoute(
        mode="private",
        preference="auto",
        project_root=str(root),
        reason="no safe existing exact-project Sol chat is available",
    )


@pytest.mark.parametrize(
    "spoken",
    [
        "can you fix the phev tracker",
        "hey serena the phev tracker deep link is broken, sort it out",
        "i need the trash purge bug looked at",
        "keep going on the voice work",
        "the macros screen is a mess, clean it up when you get a chance",
        "actually go ahead and start on konpeki",
    ],
)
def test_ordinary_asks_are_hers_to_start(spoken: str, tmp_path) -> None:
    """No verb-order rules. If he asked for work, she may start it."""
    repo = _git_repo(tmp_path / "phev-tracker")
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=123)
    result = start_coding_work(
        f"fix the phev tracker deep link in {repo}",
        origin=_turn(spoken),
        brief_context=_brief(repo),
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
        route_resolver=_new_chat,
    )
    assert result.allowed, result.reason
    assert result.item_id
    brief = inbox.accepted_brief(result.item_id)
    assert brief is not None
    assert brief["triggering_request"] == spoken
    assert brief["project_root"] == str(repo)
    assert brief["codex_model"] == "gpt-5.6-sol"
    assert brief["codex_effort"] == "high"
    assert brief["complexity"] == "ordinary"


@pytest.mark.parametrize(
    "spoken,fragment",
    [
        ("what's the status of the phev work?", "question"),
        ("why is the wake word so twitchy?", "question"),
        # "don't touch the phev tracker" used to be pinned here. It is gone
        # deliberately: a bare "don't" reads as a withdrawal in that sentence
        # and as a scope constraint in "add a comment, don't commit", and the
        # regex cannot tell them apart. She can, and she has the conversation.
        ("hold off on the phev tracker for now", "withdrew"),
        ("actually never mind, leave it", "withdrew"),
        ("how are you doing today?", "question"),
    ],
)
def test_broker_refuses_when_he_did_not_ask_for_work(
    spoken: str, fragment: str, tmp_path
) -> None:
    inbox = _Inbox()
    result = start_coding_work(
        "fix the phev tracker", origin=_turn(spoken), inbox=inbox, audit_path=tmp_path / "audit.jsonl"
    )
    assert not result.allowed
    assert fragment in result.reason
    assert inbox.enqueued == []


def test_she_cannot_invent_a_job_with_no_spoken_turn(tmp_path) -> None:
    inbox = _Inbox()
    result = start_coding_work(
        "refactor everything", origin={}, inbox=inbox, audit_path=tmp_path / "audit.jsonl"
    )
    assert not result.allowed
    assert "no originating spoken turn" in result.reason
    assert inbox.enqueued == []


def test_non_spoken_surfaces_cannot_start_work(tmp_path) -> None:
    """Front-door and plain turns keep the existing pane protocol."""
    inbox = _Inbox()
    result = start_coding_work(
        "fix the deep link",
        origin=_turn("fix the deep link", protocol="frontdoor"),
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
    )
    assert not result.allowed
    assert "live spoken turn" in result.reason


def test_refusal_when_the_private_worker_is_down(tmp_path) -> None:
    inbox = _Inbox(lease=False)
    repo = _git_repo(tmp_path / "phev-tracker")
    result = start_coding_work(
        "fix the deep link",
        origin=_turn("fix the deep link"),
        brief_context=_brief(repo),
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
    )
    assert not result.allowed
    assert "coding runtime" in result.reason


def test_turn_identity_is_the_idempotency_key(tmp_path) -> None:
    """Repeated broker calls for one brain turn do not double-queue."""
    repo = _git_repo(tmp_path / "phev-tracker")
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=123)
    origin = _turn("fix the phev tracker")
    first = start_coding_work(
        f"fix it in {repo}", origin=origin, brief_context=_brief(repo), inbox=inbox,
        audit_path=tmp_path / "audit.jsonl", route_resolver=_new_chat
    )
    second = start_coding_work(
        f"fix it in {repo}", origin=origin, brief_context=_brief(repo), inbox=inbox,
        audit_path=tmp_path / "audit.jsonl", route_resolver=_new_chat
    )
    assert first.allowed and second.allowed
    assert first.item_id == second.item_id
    assert inbox.pending_count() == 1


def test_safe_exact_project_chat_is_frozen_into_the_accepted_job(
    monkeypatch, tmp_path
) -> None:
    """A later focus change must not retarget an already accepted voice job."""

    from core import metadata

    repo = _git_repo(tmp_path / "serena")
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=123)
    bound: list[tuple[str, str]] = []
    monkeypatch.setattr(
        metadata,
        "set_work_project_root",
        lambda sid, root: bound.append((sid, str(root))) or str(root),
    )
    sid = "019fcaaa-1111-7222-8333-123456789abc"

    result = start_coding_work(
        f"fix it in {repo}",
        origin=_turn("keep working on serena"),
        brief_context=_brief(repo),
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
        route_resolver=lambda root, _spoken: WorkRoute(
            mode="reuse",
            preference="auto",
            project_root=str(root),
            session_id=sid,
            group_id="g_exact",
            bridge_port=45678,
            title="Tightening Serena",
            reason="focused exact-project chat",
            bound_focus=True,
            effort="high",
        ),
    )

    assert result.allowed
    assert result.route_mode == "reuse"
    assert result.session_id == sid
    assert result.title == "Tightening Serena"
    assert result.reason == "continuing in Tightening Serena"
    assert inbox.accepted_brief(result.item_id)["work_route"]["session_id"] == sid
    assert bound == [(sid, str(repo))]


def test_a_reuse_route_at_the_wrong_effort_falls_back_before_acceptance(tmp_path) -> None:
    repo = _git_repo(tmp_path / "serena")
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=123)

    result = start_coding_work(
        f"fix it in {repo}",
        origin=_turn("fix serena"),
        brief_context=_brief(repo),
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
        route_resolver=lambda root, _spoken: WorkRoute(
            mode="reuse",
            preference="auto",
            project_root=str(root),
            session_id="019fcaaa-1111-7222-8333-123456789abc",
            bridge_port=45678,
            effort="xhigh",
        ),
    )

    assert result.allowed
    assert result.route_mode == "private"
    brief = inbox.accepted_brief(result.item_id)
    assert brief["codex_effort"] == "high"
    assert brief["work_route"]["mode"] == "private"


def test_an_explicit_existing_route_at_the_wrong_effort_is_refused(tmp_path) -> None:
    repo = _git_repo(tmp_path / "serena")
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=123)

    result = start_coding_work(
        f"fix it in {repo}",
        origin=_turn("continue using the existing chat and fix serena"),
        brief_context=_brief(repo),
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
        route_resolver=lambda root, _spoken: WorkRoute(
            mode="reuse",
            preference="existing",
            project_root=str(root),
            session_id="019fcaaa-1111-7222-8333-123456789abc",
            bridge_port=45678,
            effort="xhigh",
        ),
    )

    assert not result.allowed
    assert result.route_mode == "refused"
    assert "froze high" in result.reason
    assert inbox.pending_count() == 0


def test_explicit_existing_chat_refusal_does_not_create_a_fallback_job(tmp_path) -> None:
    """A required existing chat must fail closed instead of quietly making one."""

    repo = _git_repo(tmp_path / "serena")
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=123)

    result = start_coding_work(
        f"fix it in {repo}",
        origin=_turn("continue using the existing chat and fix serena"),
        brief_context=_brief(repo),
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
        route_resolver=lambda root, _spoken: WorkRoute(
            mode="refused",
            preference="existing",
            project_root=str(root),
            reason="no safe exact-project Sol chat is available",
        ),
    )

    assert not result.allowed
    assert result.route_mode == "refused"
    assert inbox.pending_count() == 0


def test_explicit_new_chat_bypasses_reuse_and_is_reported(tmp_path) -> None:
    """Saying new chat must remain an unambiguous escape from default reuse."""

    repo = _git_repo(tmp_path / "serena")
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=123)

    result = start_coding_work(
        f"fix it in {repo}",
        origin=_turn("start a new chat and fix serena"),
        brief_context=_brief(repo),
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
        route_resolver=lambda root, _spoken: WorkRoute(
            mode="private",
            preference="new",
            project_root=str(root),
            reason="Raghav explicitly asked for a new chat",
        ),
    )

    assert result.allowed
    assert result.route_mode == "private"
    assert "because you asked for one" in result.reason
    assert inbox.accepted_brief(result.item_id)["work_route"]["preference"] == "new"


def test_common_conversation_rows_are_normalised_before_queueing(tmp_path) -> None:
    """A role and text row must not make a valid spoken coding ask fail intake."""

    repo = _git_repo(tmp_path / "serena")
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=123)
    brief = _brief(repo)
    brief["relevant_conversation"] = [
        {"role": "user", "text": "dismissing the coding pane loses the job"},
        {"role": "assistant", "text": "i'll keep it reachable"},
    ]

    result = start_coding_work(
        "fix the Serena coding pane",
        origin=_turn("fix the coding pane so i can reopen it"),
        brief_context=brief,
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
        route_resolver=_new_chat,
    )

    assert result.allowed
    accepted = inbox.accepted_brief(result.item_id)
    assert accepted["relevant_conversation"] == [
        "user: dismissing the coding pane loses the job",
        "assistant: i'll keep it reachable",
    ]


def test_start_with_381_never_queues_before_brain(tmp_path) -> None:
    inbox = _Inbox()
    result = start_coding_work(
        "start with memory 381",
        origin=_turn("start with 381"),
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
    )
    assert not result.allowed
    assert "memory follow-up" in result.reason
    assert inbox.enqueued == []


@pytest.mark.parametrize("spoken", ["start with 381", "let's start with 381", "and start with 381"])
def test_memory_list_prefixes_never_become_coding(spoken: str) -> None:
    assert "memory follow-up" in (authority_denial("start with memory 381", _turn(spoken)) or "")


@pytest.mark.parametrize(
    "spoken",
    ["can you fix the phev tracker?", "could you please update serena?"],
)
def test_polite_modal_requests_are_instructions_not_status_questions(spoken: str) -> None:
    assert authority_denial("update the named software project", _turn(spoken)) is None


@pytest.mark.parametrize(
    "spoken",
    [
        "don't create a new chat, use the current chat and fix serena",
        "do not reuse the current chat, start a new chat and fix serena",
    ],
)
def test_chat_routing_negation_is_not_mistaken_for_cancelling_work(spoken: str) -> None:
    """Choosing where to code must not sound like withdrawing the coding ask."""

    assert authority_denial("fix serena", _turn(spoken)) is None


def test_incomplete_structured_brief_is_rejected_before_git_is_frozen(tmp_path) -> None:
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=123)

    result = start_coding_work(
        "fix serena",
        origin=_turn("please fix serena"),
        brief_context={"project": "serena"},
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
    )

    assert not result.allowed
    assert "structured coding brief is missing" in result.reason
    assert inbox.pending_count() == 0


@pytest.mark.parametrize(
    "spoken",
    [
        "start with anything regarding trans memories",
        "open a notepad and write them all down",
        "make a Word document listing those memories",
    ],
)
def test_trans_memory_and_personal_document_followups_never_become_coding(
    spoken: str, tmp_path
) -> None:
    inbox = _Inbox()
    result = start_coding_work(
        "write the list",
        origin=_turn(spoken),
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
    )
    assert not result.allowed
    assert inbox.enqueued == []


def test_empty_request_is_refused() -> None:
    assert authority_denial("", _turn("fix the phev tracker")) is not None


def test_audit_records_refusals_too(tmp_path) -> None:
    audit = tmp_path / "work-authority.jsonl"
    start_coding_work(
        "fix it",
        origin=_turn("what's the status?"),
        inbox=_Inbox(),
        audit_path=audit,
    )
    body = audit.read_text(encoding="utf-8").strip()
    assert body
    assert '"allowed":false' in body.replace(" ", "")
    # Raw speech is never persisted, only its digest.
    assert "what's the status" not in body


def test_result_shape_is_stable() -> None:
    result = WorkAuthorityResult(True, "queued", "fix it", "abc")
    assert (result.allowed, result.request, result.item_id) == (True, "fix it", "abc")


def test_turn_context_survives_the_sdk_dispatch_task() -> None:
    """The SDK runs tool handlers off its own task, which does not inherit the
    daemon's context. Every brokered action was silently refused because of it,
    and Serena then claimed work had started. The mirror must hold."""
    import threading

    from core.brain_laptop_tools import current_turn, reset_current_turn, set_current_turn

    payload = {"text": "fix the phev tracker", "protocol": "voice",
               "call_id": "desk-9", "turn_id": "desk-9:1"}
    token = set_current_turn(payload)
    try:
        seen_values = []
        foreign = threading.Thread(target=lambda: seen_values.append(current_turn()))
        foreign.start()
        foreign.join(timeout=5)
        assert not foreign.is_alive()
        seen = seen_values[0]
        assert seen.get("text") == "fix the phev tracker"
        assert seen.get("call_id") == "desk-9"
    finally:
        reset_current_turn(token)
    assert current_turn() == {}


@pytest.mark.parametrize(
    "spoken",
    [
        # 2026-08-04, verbatim. Refused because the vocabulary held "keep" and
        # he wrote "keeps", held "code" and he wrote "coding". One letter.
        "okay drop that, right now when i try typing the coding panel keeps "
        "trying to open whic is annoying. it should onlmy open when you are codin",
        "the dot field flickers every time she starts talking",
        "typing to her opens that panel and it shouldn't",
        "phev tab is blank on my phone again",
        "her voice cuts out halfway through sentences",
    ],
)
def test_he_never_has_to_guess_the_magic_word(spoken: str, tmp_path) -> None:
    """He describes what is wrong in his own words and that is enough.

    A required vocabulary is a guess about his phrasing made in advance
    without him, so it can never be complete. Four refusals of this exact
    shape landed in one week against zero false starts ever recorded.
    """
    repo = _git_repo(tmp_path / "serena")
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=123)
    result = start_coding_work(
        f"fix the reported behaviour in {repo}",
        origin=_turn(spoken),
        brief_context=_brief(repo),
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
        route_resolver=_new_chat,
    )
    assert result.allowed, result.reason


def test_stop_still_means_stop(tmp_path) -> None:
    """Cancellation is a blacklist and stays: told to stop, she stops."""
    repo = _git_repo(tmp_path / "serena")
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=123)
    # "don't touch X" is deliberately NOT here: a bare "don't" is a scope
    # instruction as often as a withdrawal, and "add a comment, don't commit"
    # was refused as a cancellation on 2026-08-04. Only unambiguous
    # withdrawals count now.
    for spoken in ("forget it", "actually never mind, leave it", "cancel that job"):
        result = start_coding_work(
            f"fix it in {repo}",
            origin=_turn(spoken),
            brief_context=_brief(repo),
            inbox=inbox,
            audit_path=tmp_path / "audit.jsonl",
            route_resolver=_new_chat,
        )
        assert not result.allowed


@pytest.mark.parametrize(
    "spoken",
    [
        # Both refused as "withdrew or postponed the work" on 2026-08-04.
        "add a comment at the top of coding_provider, don't commit",
        "stop the coding panel from opening on typed messages",
        "make it never open unless a job is running",
    ],
)
def test_a_constraint_inside_the_work_is_not_a_withdrawal(spoken: str, tmp_path) -> None:
    """Telling her how to do the job is not telling her to drop it."""
    repo = _git_repo(tmp_path / "serena")
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=123)
    result = start_coding_work(
        f"do the requested change in {repo}",
        origin=_turn(spoken),
        brief_context=_brief(repo),
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
        route_resolver=_new_chat,
    )
    assert result.allowed, result.reason


def test_the_file_map_and_tier_survive_into_the_durable_brief(tmp_path) -> None:
    """What she names is what the worker gets, without a second search."""

    repo = _git_repo(tmp_path / "serena")
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=123)
    context = _brief(repo)
    context["likely_files"] = [
        "  voice/desktop/renderer/code-panel.js drawer visibility  ",
        "core/voice_work_supervisor.py::_emit_job_snapshot emits code_start",
        "   ",
    ]
    context["complexity"] = "hard"
    result = start_coding_work(
        f"fix the coding drawer in {repo}",
        origin=_turn("the coding drawer keeps opening, fix it"),
        brief_context=context,
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
        route_resolver=_new_chat,
    )

    assert result.allowed, result.reason
    brief = inbox.accepted_brief(result.item_id)
    assert brief is not None
    # Whitespace normalised, the empty entry dropped, and each path checked
    # against this repository, which is a fixture that holds neither file.
    assert brief["likely_files"] == [
        "voice/desktop/renderer/code-panel.js drawer visibility "
        "[NOT FOUND, this guess is wrong]",
        "core/voice_work_supervisor.py::_emit_job_snapshot emits code_start "
        "[NOT FOUND, this guess is wrong]",
    ]
    assert brief["complexity"] == "hard"
    assert brief["codex_effort"] == "xhigh"


def test_a_missing_file_map_slows_the_worker_but_never_refuses_the_job(tmp_path) -> None:
    repo = _git_repo(tmp_path / "serena")
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=123)
    result = start_coding_work(
        f"fix the thing in {repo}",
        origin=_turn("fix the thing"),
        brief_context=_brief(repo),
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
        route_resolver=_new_chat,
    )

    assert result.allowed, result.reason
    assert inbox.accepted_brief(result.item_id)["likely_files"] == []


def test_a_malformed_file_map_is_refused_rather_than_silently_dropped(tmp_path) -> None:
    repo = _git_repo(tmp_path / "serena")
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=123)
    context = _brief(repo)
    context["likely_files"] = [{"path": "core/app.py"}]
    result = start_coding_work(
        f"fix the thing in {repo}",
        origin=_turn("fix the thing"),
        brief_context=context,
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
        route_resolver=_new_chat,
    )

    assert result.allowed is False
    assert "likely_files" in result.reason


def test_a_guessed_path_is_checked_against_the_real_tree(tmp_path) -> None:
    """A guess she cannot check is how a worker gets sent confidently wrong.

    The first live run named core/coding_provider.py for a thread that lives in
    core/voice_work_supervisor.py. Labelling the map as unverified made that
    cheap; stat-ing it makes it visible before the worker spends a Read.
    """

    from core.work_authority import ground_likely_files

    repo = _git_repo(tmp_path / "serena")
    (repo / "core").mkdir()
    (repo / "core" / "voice_work_supervisor.py").write_text("x\n", encoding="utf-8")

    grounded = ground_likely_files(
        [
            "core/voice_work_supervisor.py::watch_controls polls for stop requests",
            "core/coding_provider.py - probably holds the watcher",
            "the store method is defined somewhere in core",
            "/etc/passwd",
            "../outside/the/repo.py",
        ],
        repo,
    )

    assert grounded[0].endswith("[verified to exist]")
    assert "watch_controls" in grounded[0]
    # Kept, not dropped: that she looked here and it is not here is information.
    assert grounded[1].endswith("[NOT FOUND, this guess is wrong]")
    # Prose with no path in it is left exactly as she wrote it.
    assert grounded[2] == "the store method is defined somewhere in core"
    # Nothing outside the repository is ever stat-ed or marked as verified.
    assert grounded[3] == "/etc/passwd"
    assert grounded[4] == "../outside/the/repo.py"


def test_the_grounded_map_is_what_the_durable_brief_stores(tmp_path) -> None:
    repo = _git_repo(tmp_path / "serena")
    (repo / "real.py").write_text("x\n", encoding="utf-8")
    inbox = VoiceInboxStore(tmp_path / "voice.sqlite3")
    inbox.renew_resident_lease("test-worker", pid=123)
    context = _brief(repo)
    context["likely_files"] = ["real.py holds it", "imaginary.py might too"]
    result = start_coding_work(
        f"fix the thing in {repo}",
        origin=_turn("fix the thing"),
        brief_context=context,
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
        route_resolver=_new_chat,
    )

    assert result.allowed, result.reason
    stored = inbox.accepted_brief(result.item_id)["likely_files"]
    assert stored == [
        "real.py holds it [verified to exist]",
        "imaginary.py might too [NOT FOUND, this guess is wrong]",
    ]
