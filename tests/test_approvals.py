"""Spec: approval routes, Phase 1 — broker + nonces.

Pending confirmations reach Raghav on exactly one channel with a single-use
4-digit code. Voice and text can never clear tier 4 (secret): only typed
surfaces can. Silence denies on timeout.
"""

import json

import pytest

from core.action_authority import (
    TIER_IRREVERSIBLE,
    TIER_SECRET,
    ActionAuthority,
)
from core.approvals import prompt_text


@pytest.fixture()
def authority(tmp_path):
    return ActionAuthority(
        tmp_path / "action.sqlite3",
        audit_path=tmp_path / "action.jsonl",
        publish_events=False,
    )


@pytest.fixture()
def notifications(tmp_path, monkeypatch):
    from core.notification_authority import NotificationAuthority

    monkeypatch.setenv("SERENA_CONTROL_PLANE_DB_PATH",
                       str(tmp_path / "control.sqlite3"))
    sent = []

    def _send(request):
        sent.append(request)
        return True

    authority = NotificationAuthority(
        path=tmp_path / "notifications.sqlite3",
        senders={"voice": _send, "imessage": _send, "telegram": _send,
                 "desktop": _send},
    )
    authority.sent_requests = sent
    return authority


@pytest.fixture()
def broker(tmp_path, authority, notifications):
    from core import approvals

    return approvals.ApprovalBroker(
        path=tmp_path / "approvals.sqlite3",
        authority=authority,
        notifications=notifications,
        in_call=lambda: False,
    )


def _pending(authority, **overrides):
    fields = {"capability": "data.delete_one", "target": "old-cache",
              "tier": TIER_IRREVERSIBLE, "ttl_seconds": 120, "now": 1000.0}
    fields.update(overrides)
    return authority.request_confirmation(**fields)


def _audit_events(authority):
    return [json.loads(line) for line in
            authority.audit_path.read_text(encoding="utf-8").splitlines()]


def test_two_pending_confirmations_never_share_a_nonce(broker, authority):
    first = _pending(authority)
    second = _pending(authority, target="other-cache")
    report = broker.scan(now=1001.0)
    nonces = {entry["nonce"] for entry in report}
    assert len(nonces) == 2
    assert all(len(n) == 4 and n.isdigit() for n in nonces)
    assert {entry["confirmation_id"] for entry in report} == {
        first.confirmation_id, second.confirmation_id}


def test_yes_nonce_approves_once_and_resolves_the_action(broker, authority):
    pending = _pending(authority)
    (entry,) = broker.scan(now=1001.0)
    resolved = broker.answer_nonce(entry["nonce"], approved=True,
                                   surface="text", now=1002.0)
    assert resolved.state == "approved"
    assert "text" in resolved.resolved_by
    assert authority.confirmation(pending.confirmation_id).state == "approved"
    with pytest.raises(Exception, match="already used"):
        broker.answer_nonce(entry["nonce"], approved=True, surface="text",
                            now=1003.0)


def test_wrong_nonce_is_refused_and_stays_pending(broker, authority):
    pending = _pending(authority)
    broker.scan(now=1001.0)
    with pytest.raises(Exception, match="[Uu]nknown|[Ii]nvalid|wrong"):
        broker.answer_nonce("0000", approved=True, surface="text", now=1002.0)
    assert authority.confirmation(pending.confirmation_id).state == "pending"
    refusals = [e for e in _audit_events(authority)
                if e["event"] == "approval.refused"]
    assert len(refusals) == 1
    assert refusals[0]["payload"]["surface"] == "text"


def test_expired_nonce_is_refused(broker, authority):
    pending = _pending(authority, ttl_seconds=10)
    (entry,) = broker.scan(now=1001.0)
    with pytest.raises(Exception, match="[Ee]xpired"):
        broker.answer_nonce(entry["nonce"], approved=True, surface="text",
                            now=2000.0)
    assert authority.confirmation(
        pending.confirmation_id).state == "pending"


def test_voice_and_text_can_never_clear_tier_4(broker, authority):
    pending = _pending(authority, tier=TIER_SECRET, target="api-key",
                       capability="secret.read")
    (entry,) = broker.scan(now=1001.0)
    assert entry["nonce"] not in prompt_text(
        authority.confirmation(pending.confirmation_id), entry["nonce"],
        channel=entry["channel"])
    for surface in ("voice", "text"):
        with pytest.raises(Exception, match="[Tt]yped|tier 4|secret"):
            broker.answer_nonce(entry["nonce"], approved=True,
                                surface=surface, now=1002.0)
    assert authority.confirmation(pending.confirmation_id).state == "pending"
    resolved = broker.answer_confirmation(
        pending.confirmation_id, approved=True, surface="ui", now=1003.0)
    assert resolved.state == "approved"
    assert "ui" in resolved.resolved_by


def test_tier_4_refusal_from_each_typed_surface_is_allowed(broker, authority):
    for surface in ("chat", "ui", "cli"):
        pending = _pending(authority, tier=TIER_SECRET, target=f"key-{surface}")
        resolved = broker.answer_confirmation(
            pending.confirmation_id, approved=True, surface=surface,
            now=1002.0)
        assert resolved.state == "approved"


def test_timeout_denies_and_the_action_stays_denied(broker, authority):
    from core.action_authority import build_request

    pending = _pending(authority, ttl_seconds=10)
    broker.scan(now=1001.0)
    denied = authority.deny_expired(now=2000.0)
    assert [d.confirmation_id for d in denied] == [pending.confirmation_id]
    assert denied[0].state == "denied"
    decision = authority.authorize(build_request(
        capability="data.delete_one", target="old-cache",
        effect="irreversible", intent="delete it",
        source="automation", authorization_basis="confirmation",
        confirmation_id=pending.confirmation_id))
    assert decision.allowed is False


def test_double_approval_is_a_noop_not_an_error(authority):
    pending = _pending(authority)
    first = authority.resolve_confirmation(
        pending.confirmation_id, approved=True, resolved_by="raghav",
        surface="ui", now=1001.0)
    second = authority.resolve_confirmation(
        pending.confirmation_id, approved=False, resolved_by="raghav",
        surface="ui", now=1002.0)
    assert (second.state, second.resolved_by) == (first.state,
                                                  first.resolved_by)
    assert first.state == "approved"


def test_resolution_records_surface_and_actor(authority):
    pending = _pending(authority)
    resolved = authority.resolve_confirmation(
        pending.confirmation_id, approved=True, resolved_by="raghav",
        surface="voice", now=1001.0)
    assert resolved.resolved_by == "raghav via voice"
    events = _audit_events(authority)
    done = [e for e in events if e["event"] == "confirmation.resolved"][0]
    assert done["payload"]["resolved_by"] == "raghav via voice"


def test_scan_sends_exactly_one_channel_and_only_once(broker, authority,
                                                      notifications):
    pending = _pending(authority)
    (entry,) = broker.scan(now=1001.0)
    assert entry["channel"] == "imessage"
    assert len(notifications.sent_requests) == 1
    body = notifications.sent_requests[0].summary
    assert "data.delete_one" in body and "old-cache" in body
    assert entry["nonce"] in body
    again = broker.scan(now=1002.0)
    assert len(notifications.sent_requests) == 1
    assert again[0]["nonce"] == entry["nonce"]
    assert authority.confirmation(pending.confirmation_id).state == "pending"


def test_scan_routes_to_voice_while_in_a_call(tmp_path, authority,
                                              notifications):
    from core import approvals

    broker = approvals.ApprovalBroker(
        path=tmp_path / "approvals.sqlite3", authority=authority,
        notifications=notifications, in_call=lambda: True)
    _pending(authority)
    (entry,) = broker.scan(now=1001.0)
    assert entry["channel"] == "voice"
    assert notifications.sent_requests[0].channel == "voice"


def test_web_answer_invalidates_the_text_nonce(broker, authority):
    pending = _pending(authority)
    (entry,) = broker.scan(now=1001.0)
    broker.answer_confirmation(pending.confirmation_id, approved=True,
                               surface="ui", now=1002.0)
    with pytest.raises(Exception, match="already used"):
        broker.answer_nonce(entry["nonce"], approved=True, surface="text",
                            now=1003.0)


@pytest.mark.parametrize("text,expected", [
    ("approve 4821", (True, "4821")),
    ("yes 4821", (True, "4821")),
    ("deny 4821", (False, "4821")),
    ("no 4821", (False, "4821")),
    ("Approve 4821.", (True, "4821")),
    ("approve 4821 please", None),
    ("i approve 4821", None),
    ("yes", None),
    ("approve", None),
    ("approve 48210", None),
    ("approve 482", None),
    ("", None),
])
def test_spoken_approval_matching_is_strict(text, expected):
    """Ambiguous speech is conversation, never consent."""

    from core.approvals import match_spoken_approval

    assert match_spoken_approval(text) == expected


def test_spoken_yes_answers_and_returns_words_to_say(broker, authority):
    from core.approvals import interpret_spoken_reply

    pending = _pending(authority)
    (entry,) = broker.scan(now=1001.0)
    said = interpret_spoken_reply(f"approve {entry['nonce']}",
                                  broker=broker, now=1002.0)
    assert said is not None and "approved" in said
    assert "data.delete_one" in said
    assert authority.confirmation(pending.confirmation_id).state == "approved"


def test_spoken_chatter_is_left_for_conversation(broker):
    from core.approvals import interpret_spoken_reply

    assert interpret_spoken_reply("how are you today", broker=broker) is None
    assert interpret_spoken_reply("yes, exactly", broker=broker) is None


def test_spoken_unknown_code_is_answered_not_obeyed(broker, authority):
    from core.approvals import interpret_spoken_reply

    pending = _pending(authority)
    broker.scan(now=1001.0)
    said = interpret_spoken_reply("approve 0000", broker=broker, now=1002.0)
    assert said is not None and "unknown" in said
    assert authority.confirmation(pending.confirmation_id).state == "pending"


def test_spoken_tier_4_is_refused_aloud(broker, authority):
    from core.approvals import interpret_spoken_reply

    pending = _pending(authority, tier=TIER_SECRET, target="api-key")
    (entry,) = broker.scan(now=1001.0)
    said = interpret_spoken_reply(f"approve {entry['nonce']}",
                                  broker=broker, now=1002.0)
    assert said is not None and "typed" in said
    assert authority.confirmation(pending.confirmation_id).state == "pending"


def test_call_active_marker_reports_fresh_calls_only(tmp_path, monkeypatch):
    import time

    from core import approvals

    marker = tmp_path / "call-active.json"
    monkeypatch.setenv("SERENA_CALL_ACTIVE_PATH", str(marker))
    assert approvals.call_active() is False
    approvals.mark_call_active("call-1")
    assert approvals.call_active() is True
    approvals.clear_call_active("call-1")
    assert approvals.call_active() is False
    marker.write_text('{"call_id": "old", "updated_at": 1}', encoding="utf-8")
    assert approvals.call_active(now=time.time()) is False
    marker.write_text("not json", encoding="utf-8")
    assert approvals.call_active() is False


def test_sweep_denies_expired_and_tells_him_once(broker, authority,
                                                 monkeypatch):
    import time

    from core import approvals, scheduler_actions

    monkeypatch.setattr(approvals, "ApprovalBroker", lambda *a, **k: broker)
    told = []
    monkeypatch.setattr(
        scheduler_actions, "_notify_phone",
        lambda text, key, **kw: told.append((text, key)) or True)
    now = time.time()
    pending = _pending(authority, ttl_seconds=10, now=now - 60)
    outcome = scheduler_actions.sweep_approvals({})
    assert outcome.ok
    assert authority.confirmation(pending.confirmation_id).state == "denied"
    assert len(told) == 1
    assert "denied" in told[0][0] or "expired" in told[0][0]
    # A second sweep has nothing new to say.
    again = scheduler_actions.sweep_approvals({})
    assert again.ok
    assert len(told) == 1


def test_sweep_announces_unannounced_confirmations(broker, authority,
                                                   notifications,
                                                   monkeypatch):
    import time

    from core import approvals, scheduler_actions

    monkeypatch.setattr(approvals, "ApprovalBroker", lambda *a, **k: broker)
    monkeypatch.setattr(
        scheduler_actions, "_notify_phone", lambda *a, **k: True)
    now = time.time()
    _pending(authority, ttl_seconds=600, now=now)
    outcome = scheduler_actions.sweep_approvals({})
    assert outcome.ok
    assert len(notifications.sent_requests) == 1
    assert "approval" in notifications.sent_requests[0].dedupe_key


def test_sweep_rejects_schedule_payload():
    from core import scheduler_actions

    assert scheduler_actions.sweep_approvals(
        {"tick": "daily"}).ok is False


def test_tier_4_without_a_surface_is_refused(authority):
    """An omitted surface is not a typed one: tier 4 fails closed."""

    from core.action_authority import ActionAuthorityError

    pending = _pending(authority, tier=TIER_SECRET, target="api-key",
                       capability="secret.read")
    with pytest.raises(ActionAuthorityError, match="[Tt]yped"):
        authority.resolve_confirmation(
            pending.confirmation_id, approved=True, now=1001.0)
    assert authority.confirmation(
        pending.confirmation_id).state == "pending"
    resolved = authority.resolve_confirmation(
        pending.confirmation_id, approved=True, surface="cli", now=1002.0)
    assert resolved.state == "approved"


def test_double_answer_is_audited_with_the_losing_surface(authority):
    """First wins, but the retry's surface is still on the record."""

    pending = _pending(authority)
    authority.resolve_confirmation(
        pending.confirmation_id, approved=True, resolved_by="raghav",
        surface="ui", now=1001.0)
    second = authority.resolve_confirmation(
        pending.confirmation_id, approved=False, resolved_by="raghav",
        surface="text", now=1002.0)
    assert second.state == "approved"
    duplicates = [e for e in _audit_events(authority)
                  if e["event"] == "confirmation.duplicate"]
    assert len(duplicates) == 1
    assert duplicates[0]["payload"]["surface"] == "text"
    assert duplicates[0]["payload"]["state"] == "approved"


def test_mint_never_steals_an_existing_row(broker, authority, monkeypatch):
    """A drawn value that is already owned loses the draw; the row survives."""

    import sqlite3
    from contextlib import closing

    old = _pending(authority, target="old-one")
    (entry,) = broker.scan(now=1001.0)
    broker.answer_nonce(entry["nonce"], approved=True, surface="text",
                        now=1002.0)
    draws = iter([int(entry["nonce"]), 4321])
    monkeypatch.setattr("secrets.randbelow", lambda upper: next(draws))
    new = _pending(authority, target="new-one")
    assert broker.mint_nonce(new, now=1003.0) == "4321"
    with closing(sqlite3.connect(broker.path)) as connection:
        row = connection.execute(
            "SELECT confirmation_id FROM approval_nonces WHERE nonce=?",
            (entry["nonce"],)).fetchone()
    assert row[0] == old.confirmation_id


def test_voice_announcement_names_the_waiting_code(broker, authority):
    from core.approvals import pending_voice_announcement

    assert pending_voice_announcement(broker=broker, now=1000.0) is None
    _pending(authority, ttl_seconds=600)
    (entry,) = broker.scan(now=1001.0)
    said = pending_voice_announcement(broker=broker, now=1002.0)
    assert said is not None
    assert entry["nonce"] in said
    assert "approve" in said


def test_voice_announcement_skips_tier_4(broker, authority):
    from core.approvals import pending_voice_announcement

    _pending(authority, tier=TIER_SECRET, target="api-key",
             capability="secret.read", ttl_seconds=600)
    broker.scan(now=1001.0)
    assert pending_voice_announcement(broker=broker, now=1002.0) is None


def test_late_answer_denies_like_the_sweep(authority):
    """One terminal state for silence: a late answer denies, not 'expired'."""

    from core.action_authority import ActionAuthorityError

    pending = _pending(authority, ttl_seconds=10)
    with pytest.raises(ActionAuthorityError, match="expired"):
        authority.resolve_confirmation(
            pending.confirmation_id, approved=True, surface="ui", now=2000.0)
    record = authority.confirmation(pending.confirmation_id)
    assert record.state == "denied"
    assert record.resolved_by == "timeout"


def test_failed_commit_leaves_no_denial_audit(authority, monkeypatch):
    """The audit must never claim a denial the database does not have."""

    import sqlite3

    pending = _pending(authority, ttl_seconds=10)
    real_connect = authority._connect

    class _FailingCommit:
        def __init__(self, connection):
            self._connection = connection

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._connection.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def commit(self):
            raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(
        authority, "_connect", lambda: _FailingCommit(real_connect()))
    with pytest.raises(sqlite3.OperationalError):
        authority.resolve_confirmation(
            pending.confirmation_id, approved=True, surface="ui", now=2000.0)
    assert authority.confirmation(
        pending.confirmation_id).state == "pending"
    assert [e for e in _audit_events(authority)
            if e["event"] == "confirmation.resolved"
            and e["payload"].get("confirmation_id")
            == pending.confirmation_id] == []
