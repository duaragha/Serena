"""One gate every Serena action passes through, whoever is asking.

Before this, authority was re-implemented per surface. The MCP capability
broker proved fresh direct authority its own way, the laptop broker proved it
another way with a different regex table, and the work broker a third way.
Three copies of the same idea drift, and a copy that drifts is a hole.

This module owns the vocabulary instead: what tier an action is, what counts
as authorization for that tier, whether Serena is currently allowed to do
anything at all, and what actually happened afterwards. The existing brokers
keep their own checks and keep their own audits. They call this to get the
classification, the lock, and the durable evidence, so there is one place that
can say no to everything at once.

Tiers, by what the action does rather than how it is phrased:

- 0 observe: reads state, changes nothing. Allowed while everything else is
  frozen, because being unable to ask "what is happening" during an emergency
  is its own failure.
- 1 reversible: local and undoable in one step. Volume, media, opening an app.
- 2 consequential: leaves this machine or someone else can see it. Sending a
  message, changing a light, writing to a service.
- 3 irreversible: cannot be undone by asking again. Deleting data, credentials,
  payments, anything production.

The tier is the worse of what the caller declares and what the deterministic
risk rules in core.serena_policy find in the text. A caller can escalate its
own action. A caller can never talk its way down.

Fail closed is the default everywhere: unknown source, unknown effect, expired
grant, missing confirmation, unreadable store. All of them deny.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_ACTION_DB_PATH = Path.home() / ".local" / "state" / "serena" / "action-authority.sqlite3"
DEFAULT_ACTION_AUDIT_PATH = Path.home() / ".local" / "state" / "serena" / "action-authority.jsonl"

SCHEMA_VERSION = 2

TIER_OBSERVE = 0
TIER_REVERSIBLE = 1
TIER_CONSEQUENTIAL = 2
TIER_IRREVERSIBLE = 3
# Credentials, one-time codes, payment details and security settings. Every
# other tier can be cleared by saying yes, which is the gap: a voice surface
# authenticates a VOICE, and a voice is the one factor an attacker in the room,
# a recording, or a TTS clone can supply. Anything at this tier needs Raghav at
# a keyboard, so a spoken "yes" can never be the thing that authorises it.
TIER_SECRET = 4
TIERS = (
    TIER_OBSERVE,
    TIER_REVERSIBLE,
    TIER_CONSEQUENTIAL,
    TIER_IRREVERSIBLE,
    TIER_SECRET,
)

TIER_NAMES = {
    TIER_OBSERVE: "observe",
    TIER_REVERSIBLE: "reversible",
    TIER_CONSEQUENTIAL: "consequential",
    TIER_IRREVERSIBLE: "irreversible",
    TIER_SECRET: "secret",
}

# Surfaces where Raghav is typing rather than speaking. Only these can clear
# TIER_SECRET.
TYPED_SOURCES = frozenset({"chat", "ui", "cli"})
# Narrower than the "critical" risk level on purpose: a database migration is
# critical and still perfectly fine to approve out loud. This is only the
# handful of things where the secret itself is the subject of the action.
_SECRET_ACTION = re.compile(
    r"\b(?:password|passphrase|otp|one[\s-]time[\s-]code|2fa|mfa|totp|"
    r"authenticator|api[\s-]key|secret[\s-]key|private[\s-]key|ssh[\s-]key|"
    r"credential|token|payment[\s-]method|card[\s-]number|cvv|"
    r"security[\s-]settings?|recovery[\s-]code)\b",
    re.IGNORECASE,
)

# What the caller declares its action does. This is the floor of the tier.
EFFECT_TIERS = {
    "read": TIER_OBSERVE,
    "reversible": TIER_REVERSIBLE,
    "external": TIER_CONSEQUENTIAL,
    "irreversible": TIER_IRREVERSIBLE,
    "credential": TIER_SECRET,
}

# core.serena_policy.classify_risk speaks low/normal/high/critical. Only the
# levels that mean something escalate: "normal" is the absence of a signal, not
# evidence that an action is consequential, so it must not push a read up.
RISK_TIER_FLOORS = {
    "low": TIER_OBSERVE,
    "normal": TIER_OBSERVE,
    "high": TIER_CONSEQUENTIAL,
    "critical": TIER_IRREVERSIBLE,
}

# Surfaces where Raghav is physically present at this machine. Anything else
# cannot supply a live instruction, so it never reaches tier 1 on its own.
LOCAL_SOURCES = frozenset({"voice", "desk", "chat", "ui", "cli"})
# Surfaces that exist but are not a person talking. They may observe, and they
# may act only on a grant or a confirmation someone made earlier on purpose.
UNATTENDED_SOURCES = frozenset({"system", "scheduler", "fleet", "automation", "device"})
SOURCES = LOCAL_SOURCES | UNATTENDED_SOURCES

BASIS_NONE = "none"
BASIS_ORIGIN_TURN = "origin_turn_verified"
BASIS_GRANT = "grant"
BASIS_CONFIRMATION = "confirmation"
BASES = frozenset({BASIS_NONE, BASIS_ORIGIN_TURN, BASIS_GRANT, BASIS_CONFIRMATION})

OUTCOME_STATES = frozenset(
    {"pending", "completed", "failed", "denied", "simulated", "abandoned"}
)

DEFAULT_GRANT_SECONDS = 300.0
MAX_GRANT_SECONDS = 3_600.0
DEFAULT_CONFIRMATION_SECONDS = 120.0
MAX_CONFIRMATION_SECONDS = 600.0
DEFAULT_TURN_PROOF_SECONDS = 120.0
MAX_TURN_PROOF_SECONDS = 600.0
# One turn can ask for a scene, not for the whole capability surface.
MAX_PROOF_ACTIONS = 25

# Issuing a grant is itself an action a person has to agree to, so it goes
# through the same confirmation surface as anything else sensitive.
CAPABILITY_ISSUE_GRANT = "authority.issue_grant"

MAX_TEXT = 2_000
_CAPABILITY_MAX = 128


class ActionAuthorityError(ValueError):
    """The action request, grant, or confirmation is not valid."""


def _clean(value: object, limit: int = MAX_TEXT) -> str:
    return " ".join(str(value or "").split())[:limit]


_CAPABILITY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)


def _capability(value: object) -> str:
    clean = _clean(value, _CAPABILITY_MAX)
    if not clean:
        raise ActionAuthorityError("action capability is required")
    if set(clean) - _CAPABILITY_CHARS:
        raise ActionAuthorityError(f"invalid action capability: {clean!r}")
    return clean


def _capability_pattern(value: object) -> str:
    """A grant names capabilities, or one prefix ending in `.*`.

    Kept separate from `_capability` on purpose. A concrete action must never
    be allowed to carry a wildcard in its own name, or a caller could ask to
    do `home.*` and have the store agree it did something.
    """

    clean = _clean(value, _CAPABILITY_MAX)
    if not clean:
        raise ActionAuthorityError("a grant capability is required")
    body = clean[:-2] if clean.endswith(".*") else clean
    if not body or set(body) - _CAPABILITY_CHARS:
        raise ActionAuthorityError(f"invalid grant capability: {clean!r}")
    return clean


def _scope_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw: Sequence[object] = (value,)
    elif isinstance(value, Sequence):
        raw = value
    else:
        raise ActionAuthorityError("requested scope must be a list of text")
    scopes = {_clean(item, 128) for item in raw}
    return tuple(sorted(scope for scope in scopes if scope))


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """Everything needed to judge one action, and to explain it afterwards."""

    capability: str
    intent: str
    source: str
    effect: str = "read"
    identity: str = "raghav"
    session_id: str = ""
    turn_id: str = ""
    target: str = ""
    requested_scope: tuple[str, ...] = ()
    authorization_basis: str = BASIS_NONE
    grant_id: str = ""
    confirmation_id: str = ""
    origin_proof: str = ""
    declared_risk: str = ""
    dry_run: bool = False
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["requested_scope"] = list(self.requested_scope)
        return payload


def build_request(
    *,
    capability: object,
    intent: object,
    source: object,
    effect: object = "read",
    identity: object = "raghav",
    session_id: object = "",
    turn_id: object = "",
    target: object = "",
    requested_scope: object = (),
    authorization_basis: object = BASIS_NONE,
    grant_id: object = "",
    confirmation_id: object = "",
    origin_proof: object = "",
    declared_risk: object = "",
    dry_run: object = False,
    request_id: object = "",
    context: Mapping[str, Any] | None = None,
) -> ActionRequest:
    """Validate an action request. Anything unrecognized is refused here."""

    effect_clean = _clean(effect, 32).lower()
    if effect_clean not in EFFECT_TIERS:
        raise ActionAuthorityError(
            f"unknown action effect {effect_clean or '<empty>'}; "
            "declare read, reversible, external, or irreversible"
        )
    source_clean = _clean(source, 32).lower()
    if source_clean not in SOURCES:
        raise ActionAuthorityError(f"unknown action source {source_clean or '<empty>'}")
    basis_clean = _clean(authorization_basis, 32) or BASIS_NONE
    if basis_clean not in BASES:
        raise ActionAuthorityError(f"unknown authorization basis {basis_clean}")
    intent_clean = _clean(intent)
    if not intent_clean:
        raise ActionAuthorityError("action intent is required")
    identity_clean = _clean(identity, 64)
    if not identity_clean:
        raise ActionAuthorityError("action identity is required")
    return ActionRequest(
        capability=_capability(capability),
        intent=intent_clean,
        source=source_clean,
        effect=effect_clean,
        identity=identity_clean,
        session_id=_clean(session_id, 128),
        turn_id=_clean(turn_id, 128),
        target=_clean(target, 512),
        requested_scope=_scope_tuple(requested_scope),
        authorization_basis=basis_clean,
        grant_id=_clean(grant_id, 64),
        confirmation_id=_clean(confirmation_id, 64),
        origin_proof=_clean(origin_proof, 64),
        declared_risk=_clean(declared_risk, 32).lower(),
        dry_run=bool(dry_run),
        request_id=_clean(request_id, 64) or uuid.uuid4().hex,
        context=dict(context or {}),
    )


@dataclass(frozen=True, slots=True)
class AuthorityDecision:
    allowed: bool
    tier: int
    tier_name: str
    reason: str
    request_id: str
    capability: str
    risk_level: str
    risk_reason: str
    basis: str
    requires_confirmation: bool = False
    confirmation_id: str = ""
    evidence_id: str = ""
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Grant:
    grant_id: str
    capabilities: tuple[str, ...]
    max_tier: int
    issued_at: float
    expires_at: float
    uses_remaining: int
    reason: str
    issued_by: str
    revoked_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["capabilities"] = list(self.capabilities)
        return payload

    def covers(self, capability: str, tier: int, *, now: float) -> bool:
        if self.revoked_at is not None or now >= self.expires_at:
            return False
        if self.uses_remaining <= 0 or tier > self.max_tier:
            return False
        return any(_capability_matches(pattern, capability) for pattern in self.capabilities)


@dataclass(frozen=True, slots=True)
class Confirmation:
    confirmation_id: str
    capability: str
    target: str
    tier: int
    prompt: str
    state: str
    requested_at: float
    expires_at: float
    resolved_at: float | None = None
    resolved_by: str = ""
    consumed_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TurnProof:
    """A broker's receipt that it verified one live local turn.

    `origin_turn_verified` used to be a string the caller asserted about
    itself, which meant anything that could import this module could claim a
    live voice turn and drive a phone. A proof is issued by whoever actually
    did the verifying, and it is bound to the identity, surface, session, turn,
    and the exact (capability, target) pairs that turn asked for. Each pair is
    good for one action.

    What this closes is scope-widening and replay: a proof minted to nudge the
    volume cannot open a door, and it cannot be used twice. It is not a defence
    against code already running inside this process, which can call
    `issue_turn_proof` itself. That boundary needs a separate privileged
    process and is named in the spec as still outstanding.
    """

    proof_id: str
    identity: str
    source: str
    session_id: str
    turn_id: str
    issued_at: float
    expires_at: float
    issued_by: str
    covers: tuple[tuple[str, str], ...] = ()
    revoked_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["covers"] = [list(pair) for pair in self.covers]
        return payload


def _binding(identity: str, source: str, session_id: str, turn_id: str) -> str:
    """The turn half of a proof's identity, hashed so the audit carries no text."""

    canonical = json.dumps(
        [identity, source, session_id, turn_id], separators=(",", ":"), sort_keys=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _capability_matches(pattern: str, capability: str) -> bool:
    """Prefix wildcards only. `laptop.*` is a scope; `*` alone is not.

    A single star would be a grant to do anything, which is exactly the thing
    a short-lived grant is supposed to make impossible to hand out by accident.
    """

    if pattern == capability:
        return True
    if not pattern.endswith(".*"):
        return False
    prefix = pattern[:-1]
    return capability.startswith(prefix) and len(capability) > len(prefix)


def classify_tier(
    *,
    effect: str,
    intent: str = "",
    capability: str = "",
    target: str = "",
    declared_risk: str = "",
) -> tuple[int, str, str]:
    """Return (tier, risk_level, risk_reason), never below the declared effect."""

    effect_clean = str(effect or "").lower()
    if effect_clean not in EFFECT_TIERS:
        raise ActionAuthorityError(f"unknown action effect {effect_clean or '<empty>'}")
    base = EFFECT_TIERS[effect_clean]
    try:
        from core.serena_policy import classify_risk

        risk_level, risk_reason = classify_risk(
            " ".join(part for part in (intent, capability, target) if part),
            explicit=declared_risk,
        )
    except Exception as error:  # pragma: no cover - policy file problems
        # An unreadable policy must not quietly downgrade anything. Treat the
        # action as consequential and say why.
        return (
            max(base, TIER_CONSEQUENTIAL),
            "high",
            f"risk policy unavailable ({type(error).__name__}), escalated by default",
        )
    floor = RISK_TIER_FLOORS.get(str(risk_level), TIER_CONSEQUENTIAL)
    tier = max(base, floor)
    if tier > TIER_OBSERVE and _SECRET_ACTION.search(
        " ".join(part for part in (intent, capability, target) if part)
    ):
        # Reading about a password is still a read; doing something TO one is
        # never something a voice may authorise.
        return TIER_SECRET, "critical", "action operates on a credential or security setting"
    return tier, str(risk_level), str(risk_reason)


class ActionAuthority:
    """The durable half: lock, grants, confirmations, decisions, outcomes."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        audit_path: Path | None = None,
        publish_events: bool = True,
    ) -> None:
        configured = os.environ.get("SERENA_ACTION_DB_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_ACTION_DB_PATH).expanduser()
        configured_audit = os.environ.get("SERENA_ACTION_AUDIT_PATH", "").strip()
        if audit_path is not None:
            self.audit_path = Path(audit_path).expanduser()
        elif configured_audit:
            self.audit_path = Path(configured_audit).expanduser()
        else:
            self.audit_path = self.path.with_suffix(".jsonl")
        self._publish_events = bool(publish_events)
        self._initialize()

    # -- global stop --------------------------------------------------------

    def engage_lock(self, *, reason: str, engaged_by: str = "raghav") -> dict[str, Any]:
        """Stop everything above observation, and kill every outstanding grant.

        Revoking grants is the point. A stop that leaves a live grant sitting
        there is a stop that ends the moment someone lifts it.
        """

        clean_reason = _clean(reason) or "operator requested a global stop"
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO action_lock(id, engaged, reason, engaged_by, updated_at) "
                "VALUES (1, 1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "engaged=1, reason=excluded.reason, engaged_by=excluded.engaged_by, "
                "updated_at=excluded.updated_at",
                (clean_reason, _clean(engaged_by, 64), now),
            )
            revoked = connection.execute(
                "UPDATE action_grants SET revoked_at=? WHERE revoked_at IS NULL",
                (now,),
            ).rowcount
            # A live turn proof is standing permission too, for a shorter time.
            # Leaving one alive through a stop is the same hole as a grant.
            proofs_revoked = connection.execute(
                "UPDATE action_turn_proofs SET revoked_at=? WHERE revoked_at IS NULL",
                (now,),
            ).rowcount
            connection.execute(
                "UPDATE action_confirmations SET state='cancelled', resolved_at=? "
                "WHERE state='pending'",
                (now,),
            )
        record = {
            "engaged": True,
            "reason": clean_reason,
            "engaged_by": _clean(engaged_by, 64),
            "grants_revoked": int(revoked or 0),
            "turn_proofs_revoked": int(proofs_revoked or 0),
            "at": now,
        }
        self._audit("lock.engaged", record)
        self._publish("lock.engaged", record, lifecycle_state="locked")
        return record

    def release_lock(
        self, *, released_by: str = "raghav", operator_confirmed: bool = False
    ) -> dict[str, Any]:
        """Only a person lifts a stop. There is no automatic release path."""

        if not operator_confirmed:
            raise ActionAuthorityError(
                "releasing the emergency lock requires an explicit operator confirmation"
            )
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO action_lock(id, engaged, reason, engaged_by, updated_at) "
                "VALUES (1, 0, '', ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "engaged=0, reason='', engaged_by=excluded.engaged_by, "
                "updated_at=excluded.updated_at",
                (_clean(released_by, 64), now),
            )
        record = {"engaged": False, "released_by": _clean(released_by, 64), "at": now}
        self._audit("lock.released", record)
        self._publish("lock.released", record, lifecycle_state="unlocked")
        return record

    def lock_state(self) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT engaged, reason, engaged_by, updated_at FROM action_lock WHERE id=1"
                ).fetchone()
        except sqlite3.Error as error:
            # An unreadable lock table is not permission to act.
            return {
                "engaged": True,
                "reason": f"the authority store is unreadable ({type(error).__name__})",
                "engaged_by": "fail-closed",
                "updated_at": time.time(),
            }
        if row is None or not int(row["engaged"] or 0):
            return {"engaged": False, "reason": "", "engaged_by": "", "updated_at": 0.0}
        return {
            "engaged": True,
            "reason": str(row["reason"] or ""),
            "engaged_by": str(row["engaged_by"] or ""),
            "updated_at": float(row["updated_at"] or 0.0),
        }

    # -- proof that a live local turn really happened ------------------------

    def issue_turn_proof(
        self,
        *,
        source: str,
        covers: Sequence[tuple[str, str]] | Sequence[str],
        session_id: str = "",
        turn_id: str = "",
        identity: str = "raghav",
        ttl_seconds: float = DEFAULT_TURN_PROOF_SECONDS,
        issued_by: str = "",
        now: float | None = None,
    ) -> TurnProof:
        """Record that a broker verified a live local turn for these exact actions.

        Only a surface Raghav can physically speak from can carry a live turn,
        so an unattended source is refused here rather than at authorization
        time: a proof that could never be honoured should not exist.
        """

        moment = time.time() if now is None else float(now)
        if self.lock_state()["engaged"]:
            raise ActionAuthorityError(
                "no turn proof can be issued while the emergency lock is engaged"
            )
        clean_source = _clean(source, 32).lower()
        if clean_source not in LOCAL_SOURCES:
            raise ActionAuthorityError(
                f"{clean_source or '<empty>'} is not a surface Raghav can speak from, "
                "so it cannot carry a verified live turn"
            )
        clean_identity = _clean(identity, 64)
        if not clean_identity:
            raise ActionAuthorityError("a turn proof must name whose turn it was")
        ttl = float(ttl_seconds)
        if not 0 < ttl <= MAX_TURN_PROOF_SECONDS:
            raise ActionAuthorityError(
                f"turn proof lifetime must be between 0 and {MAX_TURN_PROOF_SECONDS:.0f} seconds"
            )
        pairs = _cover_pairs(covers)
        if not pairs:
            raise ActionAuthorityError("a turn proof must name at least one action it covers")
        if len(pairs) > MAX_PROOF_ACTIONS:
            raise ActionAuthorityError(
                f"a turn proof may cover at most {MAX_PROOF_ACTIONS} actions"
            )
        proof = TurnProof(
            proof_id=uuid.uuid4().hex,
            identity=clean_identity,
            source=clean_source,
            session_id=_clean(session_id, 128),
            turn_id=_clean(turn_id, 128),
            issued_at=moment,
            expires_at=moment + ttl,
            issued_by=_clean(issued_by, 64) or clean_source,
            covers=pairs,
        )
        binding = _binding(proof.identity, proof.source, proof.session_id, proof.turn_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO action_turn_proofs(proof_id, binding_sha256, identity, source, "
                "session_id, turn_id, issued_at, expires_at, issued_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proof.proof_id,
                    binding,
                    proof.identity,
                    proof.source,
                    proof.session_id,
                    proof.turn_id,
                    proof.issued_at,
                    proof.expires_at,
                    proof.issued_by,
                ),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO action_turn_proof_uses(proof_id, capability, target) "
                "VALUES (?, ?, ?)",
                [(proof.proof_id, capability, target) for capability, target in pairs],
            )
        self._audit(
            "turn_proof.issued",
            {**proof.to_dict(), "binding_sha256": binding},
        )
        return proof

    def revoke_turn_proof(self, proof_id: str) -> bool:
        now = time.time()
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE action_turn_proofs SET revoked_at=? WHERE proof_id=? AND revoked_at IS NULL",
                (now, _clean(proof_id, 64)),
            ).rowcount
        if changed:
            self._audit("turn_proof.revoked", {"proof_id": _clean(proof_id, 64)})
        return bool(changed)

    def turn_proof(self, proof_id: str) -> TurnProof | None:
        identifier = _clean(proof_id, 64)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM action_turn_proofs WHERE proof_id=?", (identifier,)
            ).fetchone()
            if row is None:
                return None
            uses = connection.execute(
                "SELECT capability, target FROM action_turn_proof_uses "
                "WHERE proof_id=? AND consumed_at IS NULL ORDER BY capability, target",
                (identifier,),
            ).fetchall()
        return _proof_from_row(row, uses)

    # -- short-lived grants -------------------------------------------------

    def request_grant_confirmation(
        self,
        *,
        capabilities: Sequence[str],
        max_tier: int = TIER_CONSEQUENTIAL,
        reason: str = "",
        ttl_seconds: float = DEFAULT_CONFIRMATION_SECONDS,
        now: float | None = None,
    ) -> Confirmation:
        """Ask a local surface to approve handing out this exact grant.

        The descriptor the confirmation carries is built the same way
        `issue_grant` rebuilds it, so an approval for `home.*` at tier 2 cannot
        be redeemed for anything wider.
        """

        patterns, tier = _grant_shape(capabilities, max_tier)
        return self.request_confirmation(
            capability=CAPABILITY_ISSUE_GRANT,
            target=_grant_descriptor(patterns, tier),
            tier=TIER_CONSEQUENTIAL,
            prompt=_clean(reason) or f"hand out a tier {tier} grant for {', '.join(patterns)}",
            ttl_seconds=ttl_seconds,
            now=now,
        )

    def issue_grant(
        self,
        *,
        capabilities: Sequence[str],
        reason: str,
        max_tier: int = TIER_CONSEQUENTIAL,
        ttl_seconds: float = DEFAULT_GRANT_SECONDS,
        uses: int = 1,
        issued_by: str = "raghav",
        operator_confirmed: bool = False,
        confirmation_id: str = "",
        now: float | None = None,
    ) -> Grant:
        """Hand out narrow, expiring, counted authority. Never for tier 3.

        A grant is standing permission to act without asking again, so minting
        one is not something a library call should be able to do on its own.
        It needs either an approved confirmation from a local surface naming
        this exact grant, or `operator_confirmed=True`, which is the same
        deliberate switch `release_lock` uses and means a person is right here
        asking for it.
        """

        moment = time.time() if now is None else float(now)
        if self.lock_state()["engaged"]:
            raise ActionAuthorityError("no grant can be issued while the emergency lock is engaged")
        patterns, tier = _grant_shape(capabilities, max_tier)
        ttl = float(ttl_seconds)
        if not 0 < ttl <= MAX_GRANT_SECONDS:
            raise ActionAuthorityError(
                f"grant lifetime must be between 0 and {MAX_GRANT_SECONDS:.0f} seconds"
            )
        use_count = int(uses)
        if not 1 <= use_count <= 50:
            raise ActionAuthorityError("grant uses must be between 1 and 50")
        clean_reason = _clean(reason)
        if not clean_reason:
            raise ActionAuthorityError("a grant must record why it was issued")
        approver = self._approve_grant_issue(
            patterns, tier, operator_confirmed, confirmation_id, moment
        )
        grant = Grant(
            grant_id=uuid.uuid4().hex,
            capabilities=patterns,
            max_tier=tier,
            issued_at=moment,
            expires_at=moment + ttl,
            uses_remaining=use_count,
            reason=clean_reason,
            issued_by=_clean(issued_by, 64),
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO action_grants(grant_id, capabilities_json, max_tier, issued_at, "
                "expires_at, uses_remaining, reason, issued_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    grant.grant_id,
                    json.dumps(list(grant.capabilities), separators=(",", ":")),
                    grant.max_tier,
                    grant.issued_at,
                    grant.expires_at,
                    grant.uses_remaining,
                    grant.reason,
                    grant.issued_by,
                ),
            )
        self._audit("grant.issued", {**grant.to_dict(), "approved_by": approver})
        return grant

    def _approve_grant_issue(
        self,
        patterns: tuple[str, ...],
        tier: int,
        operator_confirmed: bool,
        confirmation_id: str,
        moment: float,
    ) -> str:
        """Nobody mints standing authority without a person agreeing to it."""

        identifier = _clean(confirmation_id, 64)
        if identifier:
            request = ActionRequest(
                capability=CAPABILITY_ISSUE_GRANT,
                intent=f"issue a tier {tier} grant",
                source="ui",
                target=_grant_descriptor(patterns, tier),
                confirmation_id=identifier,
            )
            ok, detail = self._consume_confirmation(request, TIER_CONSEQUENTIAL, moment)
            if not ok:
                raise ActionAuthorityError(f"this grant was not approved: {detail}")
            return f"confirmation {identifier[:8]}"
        if operator_confirmed:
            return "operator"
        raise ActionAuthorityError(
            "issuing a grant requires an approved confirmation naming it, or an explicit "
            "operator_confirmed=True from a local surface"
        )

    def revoke_grant(self, grant_id: str, *, reason: str = "revoked") -> bool:
        now = time.time()
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE action_grants SET revoked_at=? WHERE grant_id=? AND revoked_at IS NULL",
                (now, _clean(grant_id, 64)),
            ).rowcount
        if changed:
            self._audit("grant.revoked", {"grant_id": _clean(grant_id, 64), "reason": _clean(reason)})
        return bool(changed)

    def grant(self, grant_id: str) -> Grant | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM action_grants WHERE grant_id=?", (_clean(grant_id, 64),)
            ).fetchone()
        return _grant_from_row(row) if row is not None else None

    def active_grants(self, *, now: float | None = None) -> list[Grant]:
        moment = time.time() if now is None else float(now)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM action_grants WHERE revoked_at IS NULL AND expires_at > ? "
                "AND uses_remaining > 0 ORDER BY issued_at",
                (moment,),
            ).fetchall()
        return [_grant_from_row(row) for row in rows]

    # -- local confirmation -------------------------------------------------

    def request_confirmation(
        self,
        *,
        capability: str,
        target: str = "",
        tier: int = TIER_IRREVERSIBLE,
        prompt: str = "",
        ttl_seconds: float = DEFAULT_CONFIRMATION_SECONDS,
        now: float | None = None,
    ) -> Confirmation:
        moment = time.time() if now is None else float(now)
        ttl = float(ttl_seconds)
        if not 0 < ttl <= MAX_CONFIRMATION_SECONDS:
            raise ActionAuthorityError("confirmation lifetime is out of range")
        clean_capability = _capability(capability)
        clean_target = _clean(target, 512)
        clean_tier = int(tier)
        if clean_tier not in TIERS:
            raise ActionAuthorityError(f"invalid confirmation tier {tier}")
        # An empty target used to match anything, which turned "approve
        # deleting this backup" into "approve deleting". A confirmation for
        # something consequential has to name what it is about.
        if clean_tier >= TIER_CONSEQUENTIAL and not clean_target:
            raise ActionAuthorityError(
                f"a tier {clean_tier} confirmation must name the exact target it approves"
            )
        record = Confirmation(
            confirmation_id=uuid.uuid4().hex,
            capability=clean_capability,
            target=clean_target,
            tier=clean_tier,
            prompt=_clean(prompt) or f"approve {clean_capability} {clean_target}".strip(),
            state="pending",
            requested_at=moment,
            expires_at=moment + ttl,
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO action_confirmations(confirmation_id, capability, target, tier, "
                "prompt, state, requested_at, expires_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    record.confirmation_id,
                    record.capability,
                    record.target,
                    record.tier,
                    record.prompt,
                    record.requested_at,
                    record.expires_at,
                ),
            )
        self._audit("confirmation.requested", record.to_dict())
        self._publish(
            "confirmation.requested", record.to_dict(), lifecycle_state="awaiting_confirmation"
        )
        return record

    def resolve_confirmation(
        self,
        confirmation_id: str,
        *,
        approved: bool,
        resolved_by: str = "raghav",
        now: float | None = None,
    ) -> Confirmation:
        moment = time.time() if now is None else float(now)
        identifier = _clean(confirmation_id, 64)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM action_confirmations WHERE confirmation_id=?", (identifier,)
            ).fetchone()
            if row is None:
                raise ActionAuthorityError(f"unknown confirmation {identifier}")
            if str(row["state"]) != "pending":
                raise ActionAuthorityError("this confirmation was already resolved")
            if moment >= float(row["expires_at"]):
                connection.execute(
                    "UPDATE action_confirmations SET state='expired', resolved_at=? "
                    "WHERE confirmation_id=?",
                    (moment, identifier),
                )
                raise ActionAuthorityError("this confirmation expired before it was answered")
            connection.execute(
                "UPDATE action_confirmations SET state=?, resolved_at=?, resolved_by=? "
                "WHERE confirmation_id=?",
                ("approved" if approved else "denied", moment, _clean(resolved_by, 64), identifier),
            )
            updated = connection.execute(
                "SELECT * FROM action_confirmations WHERE confirmation_id=?", (identifier,)
            ).fetchone()
        record = _confirmation_from_row(updated)
        self._audit("confirmation.resolved", record.to_dict())
        return record

    def confirmation(self, confirmation_id: str) -> Confirmation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM action_confirmations WHERE confirmation_id=?",
                (_clean(confirmation_id, 64),),
            ).fetchone()
        return _confirmation_from_row(row) if row is not None else None

    def pending_confirmations(self, *, now: float | None = None) -> list[Confirmation]:
        moment = time.time() if now is None else float(now)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM action_confirmations WHERE state='pending' AND expires_at > ? "
                "ORDER BY requested_at",
                (moment,),
            ).fetchall()
        return [_confirmation_from_row(row) for row in rows]

    # -- the decision -------------------------------------------------------

    def authorize(
        self, request: ActionRequest, *, now: float | None = None
    ) -> AuthorityDecision:
        """Judge one action and record the judgement, allowed or not."""

        moment = time.time() if now is None else float(now)
        tier, risk_level, risk_reason = classify_tier(
            effect=request.effect,
            intent=request.intent,
            capability=request.capability,
            target=request.target,
            declared_risk=request.declared_risk,
        )
        allowed, reason, basis, requires_confirmation, confirmation_id = self._judge(
            request, tier, moment
        )
        decision = AuthorityDecision(
            allowed=allowed,
            tier=tier,
            tier_name=TIER_NAMES[tier],
            reason=reason,
            request_id=request.request_id,
            capability=request.capability,
            risk_level=risk_level,
            risk_reason=risk_reason,
            basis=basis,
            requires_confirmation=requires_confirmation,
            confirmation_id=confirmation_id,
            dry_run=request.dry_run,
        )
        evidence_id = self._record_decision(request, decision, moment)
        return AuthorityDecision(**{**decision.to_dict(), "evidence_id": evidence_id})

    def _judge(
        self, request: ActionRequest, tier: int, moment: float
    ) -> tuple[bool, str, str, bool, str]:
        lock = self.lock_state()
        if lock["engaged"] and tier > TIER_OBSERVE:
            return (
                False,
                f"the emergency lock is engaged: {lock['reason']}",
                BASIS_NONE,
                False,
                "",
            )
        if tier == TIER_OBSERVE:
            return True, "observation changes nothing", request.authorization_basis, False, ""

        if request.source not in SOURCES:
            return False, f"unknown source {request.source}", BASIS_NONE, False, ""

        if tier >= TIER_SECRET and request.source not in TYPED_SOURCES:
            # Deliberately checked before the confirmation is even consumed, so
            # a spoken yes cannot burn a confirmation it was never allowed to
            # use, and cannot be replayed at a typed surface afterwards.
            return (
                False,
                "credential and security actions cannot be authorised by voice, "
                f"and {request.source} is not a surface Raghav types at",
                BASIS_NONE,
                False,
                "",
            )

        # A confirmation is the strongest basis and the only one tier 3 accepts.
        if request.confirmation_id:
            ok, detail = self._consume_confirmation(request, tier, moment)
            if ok:
                return True, detail, BASIS_CONFIRMATION, False, request.confirmation_id
            return False, detail, BASIS_NONE, tier >= TIER_IRREVERSIBLE, ""

        if tier >= TIER_IRREVERSIBLE:
            return (
                False,
                "irreversible actions require a fresh local confirmation naming this exact action",
                BASIS_NONE,
                True,
                "",
            )

        if request.grant_id:
            ok, detail = self._consume_grant(request, tier, moment)
            if ok:
                return True, detail, BASIS_GRANT, False, ""
            return False, detail, BASIS_NONE, False, ""

        if request.authorization_basis == BASIS_ORIGIN_TURN:
            if request.source not in LOCAL_SOURCES:
                return (
                    False,
                    f"{request.source} is not a surface Raghav can speak from, "
                    "so it needs a grant or a confirmation",
                    BASIS_NONE,
                    tier >= TIER_CONSEQUENTIAL,
                    "",
                )
            ok, detail = self._consume_turn_proof(request, tier, moment)
            if ok:
                return True, detail, BASIS_ORIGIN_TURN, False, ""
            return False, detail, BASIS_NONE, tier >= TIER_CONSEQUENTIAL, ""

        return (
            False,
            f"tier {tier} {TIER_NAMES[tier]} actions need a verified live turn, "
            "an unexpired grant, or a local confirmation",
            BASIS_NONE,
            tier >= TIER_CONSEQUENTIAL,
            "",
        )

    def _consume_turn_proof(
        self, request: ActionRequest, tier: int, moment: float
    ) -> tuple[bool, str]:
        """A claimed live turn has to be backed by a receipt from whoever saw it."""

        identifier = _clean(request.origin_proof, 64)
        if not identifier:
            return (
                False,
                "claiming a verified live turn needs a broker-issued turn proof "
                "bound to this exact action",
            )
        if tier >= TIER_IRREVERSIBLE:
            return False, "a turn proof never covers an irreversible action"
        binding = _binding(
            request.identity, request.source, request.session_id, request.turn_id
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM action_turn_proofs WHERE proof_id=?", (identifier,)
            ).fetchone()
            if row is None:
                return False, "the named turn proof does not exist"
            if row["revoked_at"] is not None:
                return False, "the named turn proof was revoked"
            if moment >= float(row["expires_at"]):
                return False, "the named turn proof expired"
            if str(row["binding_sha256"]) != binding:
                return (
                    False,
                    "the turn proof was issued for a different person, surface, session, or turn",
                )
            use = connection.execute(
                "SELECT consumed_at FROM action_turn_proof_uses "
                "WHERE proof_id=? AND capability=? AND target=?",
                (identifier, request.capability, request.target),
            ).fetchone()
            if use is None:
                return (
                    False,
                    f"that turn proof does not cover {request.capability} "
                    f"on {request.target or '<no target>'}",
                )
            if use["consumed_at"] is not None:
                return False, "that turn proof was already used for this action"
            if not request.dry_run:
                connection.execute(
                    "UPDATE action_turn_proof_uses SET consumed_at=? "
                    "WHERE proof_id=? AND capability=? AND target=?",
                    (moment, identifier, request.capability, request.target),
                )
        return True, f"turn proof {identifier[:8]} verified this live local turn"

    def authorize_compensation(
        self,
        *,
        original_request_id: str,
        intent: str = "",
        now: float | None = None,
    ) -> AuthorityDecision:
        """Judge undoing an action that this authority already allowed and saw run.

        Compensation is not a free pass. It is warranted by a specific recorded
        action rather than by a fresh grant, because the thing that authorized
        the original step is usually spent by the time a later step fails. What
        it does not escape is the global stop: if Raghav said stop between the
        step and the rollback, the undo does not run either, and the scene
        reports the step as uncompensated instead of quietly touching hardware.
        """

        moment = time.time() if now is None else float(now)
        identifier = _clean(original_request_id, 64)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM action_requests WHERE request_id=?", (identifier,)
            ).fetchone()
        if row is None:
            original: Mapping[str, Any] = {}
        else:
            original = dict(row)
        capability = str(original.get("capability") or "")
        target = str(original.get("target") or "")
        tier = int(original.get("tier") or TIER_CONSEQUENTIAL)
        request = ActionRequest(
            capability=capability or "authority.compensate",
            intent=_clean(intent) or f"undo {capability or 'an action'}",
            source=str(original.get("source") or "system"),
            effect=str(original.get("effect") or "external"),
            identity=str(original.get("identity") or "raghav"),
            session_id=str(original.get("session_id") or ""),
            turn_id=str(original.get("turn_id") or ""),
            target=target,
            requested_scope=("compensation",),
            authorization_basis=BASIS_NONE,
            context={"compensates": identifier},
        )
        allowed, reason = self._judge_compensation(original, identifier, tier, moment)
        decision = AuthorityDecision(
            allowed=allowed,
            tier=tier,
            tier_name=TIER_NAMES.get(tier, "consequential"),
            reason=reason,
            request_id=request.request_id,
            capability=request.capability,
            risk_level=str(original.get("risk_level") or "normal"),
            risk_reason="compensating a recorded action",
            basis="compensation" if allowed else BASIS_NONE,
        )
        evidence_id = self._record_decision(request, decision, moment)
        return AuthorityDecision(**{**decision.to_dict(), "evidence_id": evidence_id})

    def _judge_compensation(
        self, original: Mapping[str, Any], identifier: str, tier: int, moment: float
    ) -> tuple[bool, str]:
        lock = self.lock_state()
        if lock["engaged"] and tier > TIER_OBSERVE:
            return False, f"the emergency lock is engaged: {lock['reason']}"
        if not original:
            return False, f"there is no recorded action {identifier} to undo"
        if not int(original.get("allowed") or 0):
            return False, "the original action was never allowed, so there is nothing to undo"
        if int(original.get("dry_run") or 0):
            return False, "a simulated action did not happen, so it cannot be undone"
        if str(original.get("outcome_status") or "") not in {"completed", "failed"}:
            return False, "the original action never reported running, so undoing it is guesswork"
        return True, f"undoing recorded action {identifier[:8]}"

    def _consume_grant(
        self, request: ActionRequest, tier: int, moment: float
    ) -> tuple[bool, str]:
        identifier = _clean(request.grant_id, 64)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM action_grants WHERE grant_id=?", (identifier,)
            ).fetchone()
            if row is None:
                return False, "the named grant does not exist"
            grant = _grant_from_row(row)
            if grant.revoked_at is not None:
                return False, "the named grant was revoked"
            if moment >= grant.expires_at:
                return False, "the named grant expired"
            if grant.uses_remaining <= 0:
                return False, "the named grant has no uses left"
            if tier > grant.max_tier:
                return (
                    False,
                    f"the grant covers tier {grant.max_tier} at most, this action is tier {tier}",
                )
            if not any(
                _capability_matches(pattern, request.capability)
                for pattern in grant.capabilities
            ):
                return False, f"the grant does not cover {request.capability}"
            # A dry run must not burn a use. Simulating is not acting.
            if not request.dry_run:
                connection.execute(
                    "UPDATE action_grants SET uses_remaining=uses_remaining-1 WHERE grant_id=?",
                    (identifier,),
                )
        return True, f"grant {identifier[:8]} covers this action"

    def _consume_confirmation(
        self, request: ActionRequest, tier: int, moment: float
    ) -> tuple[bool, str]:
        identifier = _clean(request.confirmation_id, 64)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM action_confirmations WHERE confirmation_id=?", (identifier,)
            ).fetchone()
            if row is None:
                return False, "the named confirmation does not exist"
            record = _confirmation_from_row(row)
            # Consumption is checked before state so a replay reads as a replay.
            # Reporting "it is used, not approved" told the truth but buried
            # the thing the caller actually needs to know.
            if record.consumed_at is not None:
                return False, "that confirmation was already used once"
            if record.state != "approved":
                return False, f"the named confirmation is {record.state}, not approved"
            if moment >= record.expires_at:
                return False, "the named confirmation expired"
            if record.capability != request.capability:
                return (
                    False,
                    f"the confirmation approved {record.capability}, not {request.capability}",
                )
            # Exact, both ways. A stored empty target matches only an empty
            # request target; it is not a wildcard.
            if record.target != request.target:
                return (
                    False,
                    f"the confirmation approved {record.target or '<no target>'}, "
                    f"not {request.target or '<no target>'}",
                )
            if tier > record.tier:
                return (
                    False,
                    f"the confirmation approved tier {record.tier}, this action is tier {tier}",
                )
            if not request.dry_run:
                connection.execute(
                    "UPDATE action_confirmations SET consumed_at=?, state='used' "
                    "WHERE confirmation_id=?",
                    (moment, identifier),
                )
        return True, f"confirmation {identifier[:8]} approved this exact action"

    # -- outcome ------------------------------------------------------------

    def record_outcome(
        self,
        request_id: str,
        *,
        status: str,
        detail: str = "",
        receipt: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """What actually happened, which is not the same as what was allowed."""

        moment = time.time() if now is None else float(now)
        clean_status = _clean(status, 32).lower()
        if clean_status not in OUTCOME_STATES:
            raise ActionAuthorityError(f"invalid action outcome {clean_status or '<empty>'}")
        identifier = _clean(request_id, 64)
        payload = {
            "request_id": identifier,
            "status": clean_status,
            "detail": _clean(detail),
            "receipt": dict(receipt or {}),
            "at": moment,
        }
        with self._connect() as connection:
            connection.execute(
                "UPDATE action_requests SET outcome_status=?, outcome_detail=?, "
                "outcome_receipt_json=?, outcome_at=? WHERE request_id=?",
                (
                    clean_status,
                    payload["detail"],
                    json.dumps(payload["receipt"], separators=(",", ":"), default=str),
                    moment,
                    identifier,
                ),
            )
        self._audit("action.outcome", payload)
        self._publish(
            f"action.{clean_status}",
            payload,
            lifecycle_state=clean_status,
            job_id=identifier,
        )
        return payload

    @contextmanager
    def guard(self, request: ActionRequest) -> Iterator[AuthorityDecision]:
        """Authorize, run, and always record an outcome, even on a crash.

        Callers that use this cannot forget the second half. A body that raises
        records `failed` with the exception class and re-raises; a denied
        decision records `denied` and yields it so the caller can refuse with
        the real reason instead of guessing.
        """

        decision = self.authorize(request)
        if not decision.allowed:
            self.record_outcome(request.request_id, status="denied", detail=decision.reason)
            yield decision
            return
        try:
            yield decision
        except BaseException as error:
            self.record_outcome(
                request.request_id,
                status="failed",
                detail=f"{type(error).__name__}: {error}",
            )
            raise
        else:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT outcome_status FROM action_requests WHERE request_id=?",
                    (request.request_id,),
                ).fetchone()
            if row is None or str(row["outcome_status"] or "pending") == "pending":
                self.record_outcome(
                    request.request_id,
                    status="simulated" if request.dry_run else "completed",
                    detail="the caller recorded no explicit outcome",
                )

    # -- evidence -----------------------------------------------------------

    def history(self, *, limit: int = 50, capability: str = "") -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[object] = []
        if capability:
            clauses.append("capability=?")
            params.append(_capability(capability))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(min(1_000, max(1, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM action_requests" + where + " ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def unfinished(self, *, older_than: float = 0.0, now: float | None = None) -> list[dict[str, Any]]:
        """Allowed actions that never recorded an outcome, for restart recovery."""

        moment = time.time() if now is None else float(now)
        cutoff = moment - max(0.0, float(older_than))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM action_requests WHERE allowed=1 AND outcome_status='pending' "
                "AND created_at <= ? ORDER BY created_at",
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _record_decision(
        self, request: ActionRequest, decision: AuthorityDecision, moment: float
    ) -> str:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO action_requests(request_id, capability, intent, identity, "
                "source, session_id, turn_id, target, effect, tier, risk_level, risk_reason, "
                "requested_scope_json, authorization_basis, decided_basis, allowed, reason, "
                "dry_run, outcome_status, created_at, context_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request.request_id,
                    request.capability,
                    request.intent,
                    request.identity,
                    request.source,
                    request.session_id,
                    request.turn_id,
                    request.target,
                    request.effect,
                    decision.tier,
                    decision.risk_level,
                    decision.risk_reason,
                    json.dumps(list(request.requested_scope), separators=(",", ":")),
                    request.authorization_basis,
                    decision.basis,
                    int(decision.allowed),
                    decision.reason,
                    int(request.dry_run),
                    "pending" if decision.allowed else "denied",
                    moment,
                    json.dumps(request.context, separators=(",", ":"), default=str)[:8_000],
                ),
            )
        evidence = self._audit(
            "action.decided",
            {**request.to_dict(), "decision": decision.to_dict()},
        )
        self._publish(
            "action.authorized" if decision.allowed else "action.denied",
            {
                "capability": request.capability,
                "tier": decision.tier,
                "allowed": decision.allowed,
                "reason": decision.reason,
                "basis": decision.basis,
                "dry_run": request.dry_run,
                "summary": f"finish the {request.capability} action",
            },
            lifecycle_state="authorized" if decision.allowed else "denied",
            job_id=request.request_id if decision.allowed else "",
            session_id=request.session_id,
            turn_id=request.turn_id,
        )
        return evidence

    def _audit(self, event: str, payload: Mapping[str, Any]) -> str:
        """Append one hash-chained line. Best effort, never fatal to an action.

        The chain matters because an audit anyone can quietly edit is not
        evidence. Each line carries the digest of the one before it, so a
        deletion in the middle is visible without needing a second copy.
        """

        entry_id = uuid.uuid4().hex
        try:
            path = self.audit_path
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            previous = self._last_digest(path)
            encoded = json.dumps(dict(payload), separators=(",", ":"), default=str)
            # Truncating a JSON string mid-object produces something that will
            # never parse again, so an oversized payload becomes a preview
            # object instead of a corrupt line.
            body: Any = (
                json.loads(encoded)
                if len(encoded) <= 32_000
                else {"truncated": True, "characters": len(encoded), "preview": encoded[:8_000]}
            )
            record = {
                "entry_id": entry_id,
                "event": str(event),
                "at": time.time(),
                "previous_sha256": previous,
                "payload": body,
            }
            unsigned = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
            record["sha256"] = hashlib.sha256((previous + unsigned).encode("utf-8")).hexdigest()
            line = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, (line + "\n").encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except (OSError, ValueError, TypeError):
            return entry_id
        return entry_id

    @staticmethod
    def _last_digest(path: Path) -> str:
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                if not size:
                    return "genesis"
                window = min(size, 8_192)
                handle.seek(size - window)
                tail = handle.read(window).decode("utf-8", errors="replace").strip().splitlines()
            for line in reversed(tail):
                with suppress(json.JSONDecodeError):
                    return str(json.loads(line).get("sha256") or "genesis")
        except OSError:
            return "genesis"
        return "genesis"

    def verify_audit_chain(self) -> dict[str, Any]:
        """Recompute the chain and say exactly where it first breaks, if it does."""

        path = self.audit_path
        if not path.exists():
            return {"ok": True, "entries": 0, "broken_at": None, "reason": "no audit file yet"}
        previous = "genesis"
        index = 0
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for index, raw in enumerate(handle, start=1):
                    line = raw.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    claimed = str(record.pop("sha256", ""))
                    if str(record.get("previous_sha256") or "") != previous:
                        return {
                            "ok": False,
                            "entries": index,
                            "broken_at": index,
                            "reason": "an entry does not follow the one before it",
                        }
                    unsigned = json.dumps(
                        record, sort_keys=True, separators=(",", ":"), default=str
                    )
                    digest = hashlib.sha256((previous + unsigned).encode("utf-8")).hexdigest()
                    if digest != claimed:
                        return {
                            "ok": False,
                            "entries": index,
                            "broken_at": index,
                            "reason": "an entry was altered after it was written",
                        }
                    previous = digest
        except (OSError, json.JSONDecodeError) as error:
            return {
                "ok": False,
                "entries": index,
                "broken_at": index or None,
                "reason": f"{type(error).__name__}: the audit file is unreadable",
            }
        return {"ok": True, "entries": index, "broken_at": None, "reason": ""}

    def _publish(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        lifecycle_state: str,
        job_id: str = "",
        session_id: str = "",
        turn_id: str = "",
    ) -> None:
        """Mirror into the shared control plane when it will take an action event.

        Deliberately tolerant. If the control plane has not registered the
        `action` surface yet, or its database is unavailable, the local audit
        above is still authoritative and the action still runs. A missing
        mirror is a smaller failure than refusing to act.
        """

        if not self._publish_events:
            return
        with suppress(Exception):
            from core.control_plane import ControlPlaneStore

            ControlPlaneStore().append_event(
                surface="action",
                event_type=event_type,
                lifecycle_state=lifecycle_state,
                authority="action_authority",
                payload=dict(payload),
                job_id=job_id or None,
                session_id=session_id or None,
                turn_id=turn_id or None,
            )

    # -- storage ------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS action_requests (
                    request_id TEXT PRIMARY KEY,
                    capability TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    source TEXT NOT NULL,
                    session_id TEXT,
                    turn_id TEXT,
                    target TEXT,
                    effect TEXT NOT NULL,
                    tier INTEGER NOT NULL,
                    risk_level TEXT NOT NULL,
                    risk_reason TEXT,
                    requested_scope_json TEXT NOT NULL DEFAULT '[]',
                    authorization_basis TEXT NOT NULL,
                    decided_basis TEXT NOT NULL,
                    allowed INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    outcome_status TEXT NOT NULL DEFAULT 'pending',
                    outcome_detail TEXT,
                    outcome_receipt_json TEXT,
                    outcome_at REAL,
                    created_at REAL NOT NULL,
                    context_json TEXT
                );
                CREATE INDEX IF NOT EXISTS action_requests_capability_idx
                    ON action_requests(capability, created_at);
                CREATE INDEX IF NOT EXISTS action_requests_outcome_idx
                    ON action_requests(outcome_status, created_at);

                CREATE TABLE IF NOT EXISTS action_grants (
                    grant_id TEXT PRIMARY KEY,
                    capabilities_json TEXT NOT NULL,
                    max_tier INTEGER NOT NULL,
                    issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    uses_remaining INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    issued_by TEXT NOT NULL,
                    revoked_at REAL
                );

                CREATE TABLE IF NOT EXISTS action_confirmations (
                    confirmation_id TEXT PRIMARY KEY,
                    capability TEXT NOT NULL,
                    target TEXT,
                    tier INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    state TEXT NOT NULL,
                    requested_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    resolved_at REAL,
                    resolved_by TEXT,
                    consumed_at REAL
                );

                CREATE TABLE IF NOT EXISTS action_turn_proofs (
                    proof_id TEXT PRIMARY KEY,
                    binding_sha256 TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    source TEXT NOT NULL,
                    session_id TEXT,
                    turn_id TEXT,
                    issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    issued_by TEXT NOT NULL,
                    revoked_at REAL
                );

                CREATE TABLE IF NOT EXISTS action_turn_proof_uses (
                    proof_id TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    consumed_at REAL,
                    PRIMARY KEY (proof_id, capability, target)
                );

                CREATE TABLE IF NOT EXISTS action_lock (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    engaged INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    engaged_by TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS action_schema (
                    version INTEGER PRIMARY KEY
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO action_schema(version) VALUES (?)", (SCHEMA_VERSION,)
            )
        if os.name != "nt":
            with suppress(OSError):
                self.path.chmod(0o600)


def _grant_shape(capabilities: Sequence[str], max_tier: object) -> tuple[tuple[str, ...], int]:
    """Validate the shape of a grant once, so the confirmation and the grant agree."""

    tier = int(max_tier)  # type: ignore[arg-type]
    if tier not in TIERS:
        raise ActionAuthorityError(f"invalid grant tier {max_tier}")
    if tier >= TIER_IRREVERSIBLE:
        raise ActionAuthorityError(
            "irreversible actions are never covered by a grant; they need a fresh confirmation"
        )
    raw_patterns = [_clean(item, _CAPABILITY_MAX) for item in capabilities]
    if any(pattern in {"*", ".*", ""} for pattern in raw_patterns):
        raise ActionAuthorityError("a grant may not cover every capability")
    patterns = tuple(sorted({_capability_pattern(item) for item in raw_patterns}))
    if not patterns:
        raise ActionAuthorityError("a grant must name at least one capability")
    return patterns, tier


def _grant_descriptor(patterns: Sequence[str], tier: int) -> str:
    return f"{','.join(patterns)}@tier{int(tier)}"


def _cover_pairs(covers: Sequence[Any]) -> tuple[tuple[str, str], ...]:
    """Accept `["laptop.volume_up"]` or `[("laptop.open_app", "spotify")]`."""

    pairs: set[tuple[str, str]] = set()
    for item in covers or ():
        if isinstance(item, str):
            pairs.add((_capability(item), ""))
            continue
        if isinstance(item, Sequence) and len(item) == 2:
            capability, target = item
            pairs.add((_capability(capability), _clean(target, 512)))
            continue
        raise ActionAuthorityError(
            "a turn proof covers capability strings or (capability, target) pairs"
        )
    return tuple(sorted(pairs))


def _proof_from_row(row: sqlite3.Row, uses: Sequence[sqlite3.Row]) -> TurnProof:
    return TurnProof(
        proof_id=str(row["proof_id"]),
        identity=str(row["identity"]),
        source=str(row["source"]),
        session_id=str(row["session_id"] or ""),
        turn_id=str(row["turn_id"] or ""),
        issued_at=float(row["issued_at"]),
        expires_at=float(row["expires_at"]),
        issued_by=str(row["issued_by"] or ""),
        covers=tuple((str(use["capability"]), str(use["target"] or "")) for use in uses),
        revoked_at=float(row["revoked_at"]) if row["revoked_at"] is not None else None,
    )


def _grant_from_row(row: sqlite3.Row) -> Grant:
    try:
        capabilities = tuple(json.loads(str(row["capabilities_json"] or "[]")))
    except json.JSONDecodeError:
        capabilities = ()
    return Grant(
        grant_id=str(row["grant_id"]),
        capabilities=tuple(str(item) for item in capabilities),
        max_tier=int(row["max_tier"]),
        issued_at=float(row["issued_at"]),
        expires_at=float(row["expires_at"]),
        uses_remaining=int(row["uses_remaining"]),
        reason=str(row["reason"] or ""),
        issued_by=str(row["issued_by"] or ""),
        revoked_at=float(row["revoked_at"]) if row["revoked_at"] is not None else None,
    )


def _confirmation_from_row(row: sqlite3.Row) -> Confirmation:
    return Confirmation(
        confirmation_id=str(row["confirmation_id"]),
        capability=str(row["capability"]),
        target=str(row["target"] or ""),
        tier=int(row["tier"]),
        prompt=str(row["prompt"] or ""),
        state=str(row["state"]),
        requested_at=float(row["requested_at"]),
        expires_at=float(row["expires_at"]),
        resolved_at=float(row["resolved_at"]) if row["resolved_at"] is not None else None,
        resolved_by=str(row["resolved_by"] or ""),
        consumed_at=float(row["consumed_at"]) if row["consumed_at"] is not None else None,
    )


_default_authority: ActionAuthority | None = None


def default_authority() -> ActionAuthority:
    """The process-wide authority. Tests pass their own store instead."""

    global _default_authority
    if _default_authority is None:
        _default_authority = ActionAuthority()
    return _default_authority


def reset_default_authority() -> None:
    global _default_authority
    _default_authority = None


def emergency_lock_engaged() -> bool:
    """Cheap read for callers that only need the global stop, fail closed."""

    try:
        return bool(default_authority().lock_state()["engaged"])
    except Exception:
        return True
