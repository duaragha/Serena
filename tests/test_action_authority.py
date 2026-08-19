import json

import pytest

from core.action_authority import (
    BASIS_CONFIRMATION,
    BASIS_GRANT,
    BASIS_ORIGIN_TURN,
    TIER_CONSEQUENTIAL,
    TIER_IRREVERSIBLE,
    TIER_OBSERVE,
    TIER_REVERSIBLE,
    ActionAuthority,
    ActionAuthorityError,
    build_request,
    classify_tier,
)


@pytest.fixture()
def authority(tmp_path):
    return ActionAuthority(
        tmp_path / "action.sqlite3",
        audit_path=tmp_path / "action.jsonl",
        publish_events=False,
    )


def _request(**overrides):
    base = {
        "capability": "laptop.volume_up",
        "intent": "turn the volume up",
        "source": "voice",
        "effect": "reversible",
        "authorization_basis": BASIS_ORIGIN_TURN,
    }
    return build_request(**{**base, **overrides})


def _proved(authority, **overrides):
    """A request whose live turn is backed by a proof, the way a broker sends it.

    Claiming `origin_turn_verified` is no longer enough on its own, so almost
    every allowed-path test needs the receipt that goes with the claim.
    """

    fields = {"capability": "laptop.volume_up", "source": "voice", "target": ""}
    fields.update({key: overrides[key] for key in fields.keys() & overrides.keys()})
    proof = authority.issue_turn_proof(
        source=fields["source"],
        covers=[(fields["capability"], fields["target"])],
        session_id=overrides.get("session_id", ""),
        turn_id=overrides.get("turn_id", ""),
    )
    return _request(origin_proof=proof.proof_id, **overrides)


def _grant(authority, **kwargs):
    """Grants now need a person behind them; tests say so explicitly."""

    kwargs.setdefault("operator_confirmed", True)
    return authority.issue_grant(**kwargs)


# -- schema validation ------------------------------------------------------


def test_request_rejects_unknown_effect_source_and_basis():
    with pytest.raises(ActionAuthorityError, match="unknown action effect"):
        _request(effect="whatever")
    with pytest.raises(ActionAuthorityError, match="unknown action source"):
        _request(source="telepathy")
    with pytest.raises(ActionAuthorityError, match="unknown authorization basis"):
        _request(authorization_basis="vibes")


def test_request_requires_intent_and_clean_capability():
    with pytest.raises(ActionAuthorityError, match="intent is required"):
        _request(intent="   ")
    with pytest.raises(ActionAuthorityError, match="invalid action capability"):
        _request(capability="laptop volume;rm -rf")


def test_request_carries_the_whole_contract():
    request = _request(
        session_id="s-1",
        turn_id="t-9",
        target="spotify",
        requested_scope=["device:laptop", "capability:laptop.open_app"],
    )
    payload = request.to_dict()
    for field in (
        "identity",
        "source",
        "session_id",
        "turn_id",
        "intent",
        "requested_scope",
        "authorization_basis",
        "capability",
    ):
        assert field in payload
    assert payload["requested_scope"] == ["capability:laptop.open_app", "device:laptop"]


# -- tier classification ----------------------------------------------------


def test_effect_sets_the_tier_floor():
    assert classify_tier(effect="read", intent="read the volume")[0] == TIER_OBSERVE
    assert classify_tier(effect="reversible", intent="nudge volume")[0] == TIER_REVERSIBLE
    assert classify_tier(effect="external", intent="turn on a light")[0] == TIER_CONSEQUENTIAL
    assert classify_tier(effect="irreversible", intent="wipe it")[0] == TIER_IRREVERSIBLE


def test_risk_words_escalate_but_never_downgrade():
    tier, level, _reason = classify_tier(
        effect="reversible", intent="rotate the production credential"
    )
    assert tier == TIER_IRREVERSIBLE
    assert level == "critical"
    # A caller cannot talk an irreversible action back down to a small one.
    tier, _level, _reason = classify_tier(
        effect="irreversible", intent="tiny harmless thing", declared_risk="low"
    )
    assert tier == TIER_IRREVERSIBLE


def test_normal_risk_does_not_inflate_a_read():
    tier, level, _reason = classify_tier(effect="read", intent="what is playing")
    assert level == "normal"
    assert tier == TIER_OBSERVE


# -- the decision -----------------------------------------------------------


def test_observation_is_allowed_without_any_authorization(authority):
    decision = authority.authorize(
        _request(capability="laptop.context", effect="read", authorization_basis="none")
    )
    assert decision.allowed is True
    assert decision.tier == TIER_OBSERVE


def test_reversible_local_action_accepts_a_verified_live_turn(authority):
    decision = authority.authorize(_proved(authority))
    assert decision.allowed is True
    assert decision.basis == BASIS_ORIGIN_TURN


def test_claiming_a_live_turn_without_a_proof_is_refused(authority):
    """The hole: `origin_turn_verified` used to be a string you asserted."""

    decision = authority.authorize(_request())
    assert decision.allowed is False
    assert "broker-issued turn proof" in decision.reason


def test_a_turn_proof_does_not_stretch_to_another_action(authority):
    proof = authority.issue_turn_proof(source="voice", covers=["laptop.volume_up"])
    decision = authority.authorize(
        _request(capability="android.screen_on", origin_proof=proof.proof_id)
    )
    assert decision.allowed is False
    assert "does not cover android.screen_on" in decision.reason


def test_a_turn_proof_does_not_stretch_to_another_target(authority):
    proof = authority.issue_turn_proof(
        source="voice", covers=[("laptop.open_app", "spotify")]
    )
    decision = authority.authorize(
        _request(capability="laptop.open_app", target="banking", origin_proof=proof.proof_id)
    )
    assert decision.allowed is False
    assert "does not cover" in decision.reason


def test_a_turn_proof_is_good_for_exactly_one_action(authority):
    proof = authority.issue_turn_proof(source="voice", covers=["laptop.volume_up"])
    first = authority.authorize(_request(origin_proof=proof.proof_id))
    assert first.allowed is True
    replay = authority.authorize(_request(origin_proof=proof.proof_id))
    assert replay.allowed is False
    assert "already used" in replay.reason


def test_a_turn_proof_from_another_session_does_not_transfer(authority):
    proof = authority.issue_turn_proof(
        source="voice", covers=["laptop.volume_up"], session_id="s-1", turn_id="t-1"
    )
    decision = authority.authorize(
        _request(origin_proof=proof.proof_id, session_id="s-2", turn_id="t-1")
    )
    assert decision.allowed is False
    assert "different person, surface, session, or turn" in decision.reason


def test_a_turn_proof_expires(authority):
    proof = authority.issue_turn_proof(
        source="voice", covers=["laptop.volume_up"], ttl_seconds=30, now=1_000.0
    )
    decision = authority.authorize(
        _request(origin_proof=proof.proof_id), now=1_000.0 + 31
    )
    assert decision.allowed is False
    assert "expired" in decision.reason


def test_a_dry_run_does_not_burn_a_turn_proof(authority):
    proof = authority.issue_turn_proof(source="voice", covers=["laptop.volume_up"])
    assert authority.authorize(_request(origin_proof=proof.proof_id, dry_run=True)).allowed
    assert authority.authorize(_request(origin_proof=proof.proof_id)).allowed is True


def test_an_unattended_surface_cannot_be_handed_a_turn_proof(authority):
    with pytest.raises(ActionAuthorityError, match="not a surface Raghav can speak from"):
        authority.issue_turn_proof(source="scheduler", covers=["home.light_on"])


def test_unattended_surface_cannot_act_on_a_turn_it_never_had(authority):
    decision = authority.authorize(_request(source="scheduler"))
    assert decision.allowed is False
    assert "not a surface Raghav can speak from" in decision.reason


def test_no_basis_at_all_is_refused(authority):
    decision = authority.authorize(_request(authorization_basis="none"))
    assert decision.allowed is False
    assert "verified live turn" in decision.reason


def test_irreversible_needs_a_confirmation_not_a_live_turn(authority):
    decision = authority.authorize(
        _request(capability="data.delete_everything", effect="irreversible", intent="delete it all")
    )
    assert decision.allowed is False
    assert decision.requires_confirmation is True
    assert decision.tier == TIER_IRREVERSIBLE


# -- grants -----------------------------------------------------------------


def test_grant_covers_a_prefix_and_is_consumed_once(authority):
    grant = _grant(
        authority,
        capabilities=["home.*"], reason="he asked me to set the house up", uses=1
    )
    allowed = authority.authorize(
        _request(
            capability="home.light_on",
            effect="external",
            source="scheduler",
            authorization_basis=BASIS_GRANT,
            grant_id=grant.grant_id,
        )
    )
    assert allowed.allowed is True
    assert allowed.basis == BASIS_GRANT

    again = authority.authorize(
        _request(
            capability="home.light_off",
            effect="external",
            source="scheduler",
            authorization_basis=BASIS_GRANT,
            grant_id=grant.grant_id,
        )
    )
    assert again.allowed is False
    assert "no uses left" in again.reason


def test_grant_does_not_cover_a_capability_outside_its_prefix(authority):
    grant = _grant(authority, capabilities=["home.*"], reason="house only")
    decision = authority.authorize(
        _request(
            capability="android.screen_on",
            effect="reversible",
            source="scheduler",
            authorization_basis=BASIS_GRANT,
            grant_id=grant.grant_id,
        )
    )
    assert decision.allowed is False
    assert "does not cover" in decision.reason


def test_grant_expires(authority):
    grant = _grant(
        authority,
        capabilities=["home.light_on"], reason="briefly", ttl_seconds=30, now=1_000.0
    )
    decision = authority.authorize(
        _request(
            capability="home.light_on",
            effect="external",
            source="scheduler",
            authorization_basis=BASIS_GRANT,
            grant_id=grant.grant_id,
        ),
        now=1_000.0 + 31,
    )
    assert decision.allowed is False
    assert "expired" in decision.reason


def test_a_grant_can_never_cover_an_irreversible_action(authority):
    with pytest.raises(ActionAuthorityError, match="never covered by a grant"):
        _grant(
        authority,
            capabilities=["data.delete"], reason="no", max_tier=TIER_IRREVERSIBLE
        )


def test_a_grant_may_not_cover_everything(authority):
    with pytest.raises(ActionAuthorityError, match="may not cover every capability"):
        _grant(authority, capabilities=["*"], reason="blanket authority")


def test_grant_tier_ceiling_is_enforced(authority):
    grant = _grant(
        authority,
        capabilities=["home.*"], reason="reversible only", max_tier=TIER_REVERSIBLE
    )
    decision = authority.authorize(
        _request(
            capability="home.light_on",
            effect="external",
            source="scheduler",
            authorization_basis=BASIS_GRANT,
            grant_id=grant.grant_id,
        )
    )
    assert decision.allowed is False
    assert "covers tier 1 at most" in decision.reason


def test_a_dry_run_does_not_burn_a_grant_use(authority):
    grant = _grant(authority, capabilities=["home.*"], reason="planning", uses=1)
    simulated = authority.authorize(
        _request(
            capability="home.light_on",
            effect="external",
            source="scheduler",
            authorization_basis=BASIS_GRANT,
            grant_id=grant.grant_id,
            dry_run=True,
        )
    )
    assert simulated.allowed is True
    assert authority.grant(grant.grant_id).uses_remaining == 1


def test_a_grant_cannot_be_minted_by_merely_importing_the_module(authority):
    """The hole: any caller could hand itself standing tier-2 authority."""

    with pytest.raises(ActionAuthorityError, match="requires an approved confirmation"):
        authority.issue_grant(capabilities=["home.*"], reason="quietly helping")


def test_an_approved_confirmation_is_the_other_way_to_issue_a_grant(authority):
    pending = authority.request_grant_confirmation(
        capabilities=["home.*"], reason="he asked me to set the house up"
    )
    authority.resolve_confirmation(pending.confirmation_id, approved=True)
    grant = authority.issue_grant(
        capabilities=["home.*"],
        reason="he asked me to set the house up",
        confirmation_id=pending.confirmation_id,
    )
    assert grant.capabilities == ("home.*",)


def test_a_grant_approval_cannot_be_redeemed_for_a_wider_grant(authority):
    pending = authority.request_grant_confirmation(capabilities=["home.light_on"])
    authority.resolve_confirmation(pending.confirmation_id, approved=True)
    with pytest.raises(ActionAuthorityError, match="not approved"):
        authority.issue_grant(
            capabilities=["home.*"],
            reason="just a little wider",
            confirmation_id=pending.confirmation_id,
        )


def test_a_grant_approval_is_good_once(authority):
    pending = authority.request_grant_confirmation(capabilities=["home.*"])
    authority.resolve_confirmation(pending.confirmation_id, approved=True)
    authority.issue_grant(
        capabilities=["home.*"], reason="first", confirmation_id=pending.confirmation_id
    )
    with pytest.raises(ActionAuthorityError, match="already used once"):
        authority.issue_grant(
            capabilities=["home.*"], reason="second", confirmation_id=pending.confirmation_id
        )


# -- confirmations ----------------------------------------------------------


def test_approved_confirmation_unlocks_exactly_one_irreversible_action(authority):
    pending = authority.request_confirmation(
        capability="data.delete_everything", target="old-backups", tier=TIER_IRREVERSIBLE
    )
    authority.resolve_confirmation(pending.confirmation_id, approved=True)
    decision = authority.authorize(
        _request(
            capability="data.delete_everything",
            effect="irreversible",
            target="old-backups",
            intent="delete the old backups he pointed at",
            confirmation_id=pending.confirmation_id,
        )
    )
    assert decision.allowed is True
    assert decision.basis == BASIS_CONFIRMATION

    replay = authority.authorize(
        _request(
            capability="data.delete_everything",
            effect="irreversible",
            target="old-backups",
            intent="delete the old backups again",
            confirmation_id=pending.confirmation_id,
        )
    )
    assert replay.allowed is False
    assert "already used once" in replay.reason


def test_a_confirmation_does_not_transfer_to_another_action(authority):
    pending = authority.request_confirmation(
        capability="data.delete_one", target="one-file", tier=TIER_IRREVERSIBLE
    )
    authority.resolve_confirmation(pending.confirmation_id, approved=True)
    decision = authority.authorize(
        _request(
            capability="data.delete_everything",
            effect="irreversible",
            intent="delete everything",
            confirmation_id=pending.confirmation_id,
        )
    )
    assert decision.allowed is False
    assert "approved data.delete_one" in decision.reason


def test_a_denied_confirmation_denies_the_action(authority):
    pending = authority.request_confirmation(
        capability="data.delete_one", target="one-file", tier=TIER_IRREVERSIBLE
    )
    authority.resolve_confirmation(pending.confirmation_id, approved=False)
    decision = authority.authorize(
        _request(
            capability="data.delete_one",
            effect="irreversible",
            target="one-file",
            intent="delete it",
            confirmation_id=pending.confirmation_id,
        )
    )
    assert decision.allowed is False
    assert "denied" in decision.reason


def test_an_expired_confirmation_cannot_be_answered(authority):
    pending = authority.request_confirmation(
        capability="data.delete_one", target="one-file",
        tier=TIER_IRREVERSIBLE, ttl_seconds=10, now=500.0
    )
    with pytest.raises(ActionAuthorityError, match="expired"):
        authority.resolve_confirmation(pending.confirmation_id, approved=True, now=520.0)


def test_a_sensitive_confirmation_must_name_its_target(authority):
    """The hole: an empty stored target matched every target."""

    with pytest.raises(ActionAuthorityError, match="must name the exact target"):
        authority.request_confirmation(
            capability="data.delete_everything", tier=TIER_IRREVERSIBLE
        )


def test_an_empty_confirmation_target_is_not_a_wildcard(authority):
    pending = authority.request_confirmation(
        capability="laptop.open_app", tier=TIER_REVERSIBLE
    )
    authority.resolve_confirmation(pending.confirmation_id, approved=True)
    decision = authority.authorize(
        _request(
            capability="laptop.open_app",
            target="banking",
            intent="open the banking app",
            confirmation_id=pending.confirmation_id,
        )
    )
    assert decision.allowed is False
    assert "not banking" in decision.reason


# -- global stop ------------------------------------------------------------


def test_the_lock_stops_everything_above_observation_and_kills_grants(authority):
    grant = _grant(authority, capabilities=["home.*"], reason="before the stop")
    # Minted before the stop, so this proves the stop beats a live warrant
    # rather than the action failing for want of one.
    standing = _proved(authority)
    engaged = authority.engage_lock(reason="he said stop")
    assert engaged["grants_revoked"] == 1
    assert engaged["turn_proofs_revoked"] == 1

    blocked = authority.authorize(standing)
    assert blocked.allowed is False
    assert "emergency lock is engaged" in blocked.reason

    with_grant = authority.authorize(
        _request(
            capability="home.light_on",
            effect="external",
            source="scheduler",
            authorization_basis=BASIS_GRANT,
            grant_id=grant.grant_id,
        )
    )
    assert with_grant.allowed is False

    # Asking what is happening still works while everything is frozen.
    observed = authority.authorize(
        _request(capability="laptop.context", effect="read", authorization_basis="none")
    )
    assert observed.allowed is True


def test_the_lock_cannot_be_lifted_without_an_operator(authority):
    authority.engage_lock(reason="stop")
    with pytest.raises(ActionAuthorityError, match="explicit operator confirmation"):
        authority.release_lock()
    authority.release_lock(operator_confirmed=True)
    assert authority.authorize(_proved(authority)).allowed is True


def test_no_grant_may_be_issued_while_locked(authority):
    authority.engage_lock(reason="stop")
    with pytest.raises(ActionAuthorityError, match="emergency lock"):
        _grant(authority, capabilities=["home.*"], reason="sneaking one in")


def test_a_locked_authority_cancels_pending_confirmations(authority):
    pending = authority.request_confirmation(
        capability="data.delete_one", target="one-file", tier=TIER_IRREVERSIBLE
    )
    authority.engage_lock(reason="stop")
    assert authority.confirmation(pending.confirmation_id).state == "cancelled"


# -- evidence ---------------------------------------------------------------


def test_every_decision_and_outcome_is_recorded(authority):
    request = _proved(authority)
    decision = authority.authorize(request)
    authority.record_outcome(request.request_id, status="completed", detail="volume went up")
    row = authority.history(limit=5)[0]
    assert row["capability"] == "laptop.volume_up"
    assert row["allowed"] == 1
    assert row["tier"] == decision.tier
    assert row["outcome_status"] == "completed"
    assert row["identity"] == "raghav"
    assert row["source"] == "voice"


def test_denials_are_recorded_too(authority):
    authority.authorize(_request(authorization_basis="none"))
    row = authority.history(limit=1)[0]
    assert row["allowed"] == 0
    assert row["outcome_status"] == "denied"


def test_audit_chain_verifies_and_detects_tampering(authority, tmp_path):
    authority.authorize(_proved(authority))
    authority.engage_lock(reason="stop")
    assert authority.verify_audit_chain()["ok"] is True

    path = tmp_path / "action.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 2
    first = json.loads(lines[0])
    first["payload"] = {"tampered": True}
    lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verdict = authority.verify_audit_chain()
    assert verdict["ok"] is False
    assert verdict["broken_at"] == 1


def test_guard_records_an_outcome_even_when_the_body_raises(authority):
    request = _proved(authority)
    with pytest.raises(RuntimeError), authority.guard(request) as decision:
        assert decision.allowed is True
        raise RuntimeError("pactl fell over")
    row = authority.history(limit=1)[0]
    assert row["outcome_status"] == "failed"
    assert "pactl fell over" in row["outcome_detail"]


def test_guard_yields_a_denial_without_running_the_body(authority):
    ran = False
    with authority.guard(_request(authorization_basis="none")) as decision:
        if decision.allowed:
            ran = True
    assert ran is False
    assert authority.history(limit=1)[0]["outcome_status"] == "denied"


def test_unfinished_actions_survive_for_restart_recovery(authority):
    request = _proved(authority)
    authority.authorize(request)
    outstanding = authority.unfinished()
    assert [item["request_id"] for item in outstanding] == [request.request_id]
    authority.record_outcome(request.request_id, status="completed")
    assert authority.unfinished() == []


def test_action_surface_is_registered_in_the_shared_control_plane():
    """If a merge drops the surface, this fails loudly instead of going quiet."""

    from core.control_plane import OBLIGATION_RULES, SURFACES

    assert "action" in SURFACES
    assert "device" in SURFACES
    rule = next(item for item in OBLIGATION_RULES if item.surface == "action")
    assert rule.open_on == "action.authorized"
    assert "action.completed" in rule.fulfill_on


def test_an_authorized_action_opens_and_closes_a_control_plane_obligation(
    tmp_path, monkeypatch
):
    """The real publish path, not the tolerant no-op the other tests use."""

    monkeypatch.setenv(
        "SERENA_CONTROL_PLANE_DB_PATH", str(tmp_path / "control-plane.sqlite3")
    )
    from core.control_plane import ControlPlaneStore

    control = ControlPlaneStore(tmp_path / "control-plane.sqlite3")
    live = ActionAuthority(
        tmp_path / "action.sqlite3",
        audit_path=tmp_path / "action.jsonl",
        publish_events=True,
    )
    request = _proved(live)
    assert live.authorize(request).allowed is True

    outstanding = [
        item for item in control.obligations(state="open", surface="action")
        if item.job_id == request.request_id
    ]
    assert len(outstanding) == 1

    live.record_outcome(request.request_id, status="completed", detail="volume went up")
    closed = control.obligations(surface="action")
    assert [item.state for item in closed if item.job_id == request.request_id] == ["fulfilled"]


def test_a_dry_run_owes_nobody_a_control_plane_obligation(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SERENA_CONTROL_PLANE_DB_PATH", str(tmp_path / "control-plane.sqlite3")
    )
    from core.control_plane import ControlPlaneStore

    control = ControlPlaneStore(tmp_path / "control-plane.sqlite3")
    live = ActionAuthority(
        tmp_path / "action.sqlite3",
        audit_path=tmp_path / "action.jsonl",
        publish_events=True,
    )
    request = _proved(live, dry_run=True)
    assert live.authorize(request).allowed is True
    assert control.obligations(state="open", surface="action") == []
