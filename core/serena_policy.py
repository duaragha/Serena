"""Validated shared model policy for Serena's delegated work.

The policy chooses a worker. It never grants tools, permissions, deployment
authority, or filesystem access. Those remain the responsibility of each
activity broker after a model has been selected.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "serena-policy.json"
RISK_LEVELS = ("low", "normal", "high", "critical")


class SerenaPolicyError(ValueError):
    """The shared policy or a requested decision is invalid."""


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    provider: str
    model: str
    runtime_model: str
    effort: str
    label: str
    strength: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    profile: str
    activity: str
    complexity: str
    risk: str
    lane: str
    role: str
    provider: str
    model: str
    runtime_model: str
    effort: str
    manual_override: str
    fallback_reason: str
    reason: str
    policy_version: int
    candidates: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["candidates"] = [dict(item) for item in self.candidates]
        return payload


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _capacity_entry(capacity: Mapping[str, Any] | None, provider: str) -> Mapping[str, Any]:
    if not capacity:
        return {}
    value = capacity.get(provider)
    if isinstance(value, Mapping):
        return value
    if value is not None and hasattr(value, "to_dict"):
        decoded = value.to_dict()
        return decoded if isinstance(decoded, Mapping) else {}
    return {
        key: getattr(value, key)
        for key in ("status", "usable", "reason", "resets_at")
        if value is not None and hasattr(value, key)
    }


def _model_capacity_entry(
    capacity: Mapping[str, Any] | None, model: str
) -> Mapping[str, Any]:
    if not capacity:
        return {}
    models = capacity.get("models")
    if not isinstance(models, Mapping):
        return {}
    value = models.get(model)
    if isinstance(value, Mapping):
        return value
    if value is not None and hasattr(value, "to_dict"):
        decoded = value.to_dict()
        return decoded if isinstance(decoded, Mapping) else {}
    return {
        key: getattr(value, key)
        for key in ("status", "usable", "reason", "resets_at")
        if value is not None and hasattr(value, key)
    }


def provider_usable(capacity: Mapping[str, Any] | None, provider: str) -> bool:
    """Unknown or stale capacity stays usable until a provider says otherwise."""

    entry = _capacity_entry(capacity, provider)
    if "usable" in entry:
        return bool(entry.get("usable"))
    return _clean(entry.get("status") or "unknown") != "unavailable"


def model_usable(
    capacity: Mapping[str, Any] | None, *, provider: str, model: str
) -> bool:
    """Require provider health, then apply a known model-specific block."""

    if not provider_usable(capacity, provider):
        return False
    entry = _model_capacity_entry(capacity, model)
    if entry:
        if "usable" in entry:
            return bool(entry.get("usable"))
        return _clean(entry.get("status") or "unknown") != "unavailable"
    return True


def _validate_candidate(
    candidate: object,
    *,
    models: Mapping[str, Any],
    location: str,
) -> None:
    if not isinstance(candidate, Mapping):
        raise SerenaPolicyError(f"{location} must contain model objects")
    model = str(candidate.get("model") or "")
    if model not in models:
        raise SerenaPolicyError(f"{location} references unknown model {model or '<empty>'}")
    effort = _clean(candidate.get("effort"))
    if effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
        raise SerenaPolicyError(f"{location} has invalid effort {effort or '<empty>'}")


def validate_policy(data: object) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise SerenaPolicyError("policy root must be an object")
    policy = dict(data)
    if policy.get("schema_version") != 1:
        raise SerenaPolicyError("policy schema_version must be 1")
    models = policy.get("models")
    profiles = policy.get("profiles")
    if not isinstance(models, Mapping) or not models:
        raise SerenaPolicyError("policy models must be a non-empty object")
    if not isinstance(profiles, Mapping) or not profiles:
        raise SerenaPolicyError("policy profiles must be a non-empty object")
    for model_id, raw in models.items():
        if not isinstance(raw, Mapping):
            raise SerenaPolicyError(f"model {model_id} must be an object")
        if raw.get("provider") not in {"codex", "claude"}:
            raise SerenaPolicyError(f"model {model_id} has an invalid provider")
        if not str(raw.get("runtime_model") or "").strip():
            raise SerenaPolicyError(f"model {model_id} has no runtime_model")
        if not str(raw.get("label") or "").strip():
            raise SerenaPolicyError(f"model {model_id} has no label")
        try:
            strength = int(raw.get("strength"))
        except (TypeError, ValueError) as exc:
            raise SerenaPolicyError(f"model {model_id} has invalid strength") from exc
        if strength < 1:
            raise SerenaPolicyError(f"model {model_id} has invalid strength")
    required_profiles = {"brain", "coding", "research", "documents", "fleet"}
    missing = sorted(required_profiles - set(profiles))
    if missing:
        raise SerenaPolicyError("policy profiles missing: " + ", ".join(missing))
    fleet = profiles.get("fleet")
    if not isinstance(fleet, Mapping) or fleet.get("mode") != "separate":
        raise SerenaPolicyError("fleet must retain its separate phase policy")
    for name, raw_profile in profiles.items():
        if name == "fleet":
            continue
        if not isinstance(raw_profile, Mapping):
            raise SerenaPolicyError(f"profile {name} must be an object")
        lanes = raw_profile.get("lanes")
        default_lane = str(raw_profile.get("default_lane") or "")
        if not isinstance(lanes, Mapping) or default_lane not in lanes:
            raise SerenaPolicyError(f"profile {name} has an invalid default lane")
        for lane_name, raw_lane in lanes.items():
            if not isinstance(raw_lane, Mapping):
                raise SerenaPolicyError(f"profile {name} lane {lane_name} must be an object")
            try:
                int(raw_lane.get("rank"))
            except (TypeError, ValueError) as exc:
                raise SerenaPolicyError(f"profile {name} lane {lane_name} has invalid rank") from exc
            roles = [key for key in ("execute", "implement", "review") if key in raw_lane]
            if not roles:
                raise SerenaPolicyError(f"profile {name} lane {lane_name} has no candidates")
            for role in roles:
                candidates = raw_lane.get(role)
                if not isinstance(candidates, Sequence) or isinstance(candidates, str) or not candidates:
                    raise SerenaPolicyError(
                        f"profile {name} lane {lane_name} role {role} must be a non-empty list"
                    )
                for candidate in candidates:
                    _validate_candidate(
                        candidate,
                        models=models,
                        location=f"profile {name} lane {lane_name} role {role}",
                    )
        for model in raw_profile.get("manual_models") or []:
            if model not in models:
                raise SerenaPolicyError(f"profile {name} manual model {model} is unknown")
        for mapping_name in ("activity_lanes", "complexity_lanes", "risk_lanes"):
            mapping = raw_profile.get(mapping_name) or {}
            if not isinstance(mapping, Mapping):
                raise SerenaPolicyError(f"profile {name} {mapping_name} must be an object")
            invalid_lanes = sorted({str(value) for value in mapping.values()} - set(lanes))
            if invalid_lanes:
                raise SerenaPolicyError(
                    f"profile {name} {mapping_name} references unknown lanes: "
                    + ", ".join(invalid_lanes)
                )
    floors = policy.get("risk_strength_floors")
    if not isinstance(floors, Mapping) or any(level not in floors for level in RISK_LEVELS):
        raise SerenaPolicyError("risk_strength_floors must define every risk level")
    return policy


def policy_path() -> Path:
    configured = os.environ.get("SERENA_POLICY_PATH", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_POLICY_PATH


@lru_cache(maxsize=8)
def _load_cached(path: str, modified_ns: int) -> dict[str, Any]:
    del modified_ns
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SerenaPolicyError(f"could not read Serena policy: {exc}") from exc
    return validate_policy(data)


def load_policy(path: Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else policy_path()
    try:
        modified_ns = target.stat().st_mtime_ns
    except OSError as exc:
        raise SerenaPolicyError(f"could not stat Serena policy: {exc}") from exc
    return _load_cached(str(target.resolve()), modified_ns)


def _aliases(policy: Mapping[str, Any]) -> dict[str, str]:
    aliases: dict[str, str] = {"auto": "auto", "default": "auto", "": "auto"}
    for model_id, raw in policy["models"].items():
        values = [model_id, raw.get("runtime_model"), raw.get("label"), *(raw.get("aliases") or [])]
        for value in values:
            clean = _clean(value)
            if clean:
                aliases[clean] = str(model_id)
    return aliases


def normalise_model(
    value: object,
    *,
    policy: Mapping[str, Any] | None = None,
    strict: bool = False,
) -> str:
    data = policy or load_policy()
    model = _aliases(data).get(_clean(value))
    if model is not None:
        return model
    if strict:
        raise SerenaPolicyError(f"unsupported model: {value}")
    return "auto"


def model_options(
    profile: str,
    *,
    role: str = "implement",
    policy: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    data = policy or load_policy()
    raw_profile = data["profiles"].get(profile)
    if not isinstance(raw_profile, Mapping) or raw_profile.get("mode") == "separate":
        raise SerenaPolicyError(f"profile {profile} does not expose manual model options")
    allowed = list(raw_profile.get("manual_models") or [])
    if not allowed:
        seen: set[str] = set()
        for lane in raw_profile["lanes"].values():
            for candidate in lane.get(role) or []:
                model = str(candidate["model"])
                if model not in seen:
                    allowed.append(model)
                    seen.add(model)
    options = [{"value": "auto", "label": "auto"}]
    for model in allowed:
        raw = data["models"][model]
        options.append({"value": model, "label": str(raw["label"])})
    return options


def classify_risk(
    text: object,
    *,
    paths: Sequence[object] = (),
    explicit: object = "",
    policy: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    data = policy or load_policy()
    requested = _clean(explicit)
    if requested in RISK_LEVELS:
        return requested, "trusted caller supplied an explicit risk level"
    haystack = " ".join([_clean(text), *(_clean(path) for path in paths)])
    selected = "normal"
    matched = "no deterministic risk rule matched"
    for rule in data.get("risk_rules") or []:
        level = _clean(rule.get("level"))
        if level not in RISK_LEVELS:
            continue
        for term in rule.get("terms") or []:
            clean = _clean(term)
            if clean and re.search(rf"(?<![a-z0-9]){re.escape(clean)}(?![a-z0-9])", haystack):
                if RISK_LEVELS.index(level) > RISK_LEVELS.index(selected):
                    selected = level
                    matched = f"deterministic risk rule matched {clean}"
                break
    return selected, matched


def _candidate(
    data: Mapping[str, Any], raw: Mapping[str, Any], *, effort: str | None = None
) -> ModelCandidate:
    model = str(raw["model"])
    spec = data["models"][model]
    return ModelCandidate(
        provider=str(spec["provider"]),
        model=model,
        runtime_model=str(spec["runtime_model"]),
        effort=str(effort or raw["effort"]),
        label=str(spec["label"]),
        strength=int(spec["strength"]),
    )


def _select_lane(
    profile: Mapping[str, Any],
    *,
    activity: str,
    complexity: str,
    risk: str,
) -> str:
    lane = str(profile.get("default_lane"))
    complexity_lane = (profile.get("complexity_lanes") or {}).get(complexity)
    activity_lane = (profile.get("activity_lanes") or {}).get(activity)
    if complexity_lane:
        lane = str(complexity_lane)
    elif activity_lane:
        lane = str(activity_lane)
    risk_lane = (profile.get("risk_lanes") or {}).get(risk)
    if risk_lane:
        current_rank = int(profile["lanes"][lane]["rank"])
        risk_rank = int(profile["lanes"][risk_lane]["rank"])
        if risk_rank > current_rank:
            lane = str(risk_lane)
    return lane


def resolve_policy(
    profile: str,
    *,
    activity: object = "",
    complexity: object = "",
    risk: object = "normal",
    role: str = "execute",
    manual_override: object = "auto",
    capacity: Mapping[str, Any] | None = None,
    avoid_provider: str = "",
    policy: Mapping[str, Any] | None = None,
) -> PolicyDecision:
    """Select and freeze one capacity-aware model decision."""

    data = policy or load_policy()
    raw_profile = data["profiles"].get(profile)
    if not isinstance(raw_profile, Mapping):
        raise SerenaPolicyError(f"unknown policy profile: {profile}")
    if raw_profile.get("mode") == "separate":
        raise SerenaPolicyError("fleet model selection belongs to core.fleet_policy")
    activity_clean = _clean(activity) or profile
    complexity_clean = _clean(complexity)
    risk_clean = _clean(risk)
    if risk_clean not in RISK_LEVELS:
        raise SerenaPolicyError(f"invalid risk level: {risk}")
    lane = _select_lane(
        raw_profile,
        activity=activity_clean,
        complexity=complexity_clean,
        risk=risk_clean,
    )
    raw_candidates = raw_profile["lanes"][lane].get(role)
    if not raw_candidates:
        raise SerenaPolicyError(f"profile {profile} lane {lane} has no {role} policy")
    candidates = [_candidate(data, item) for item in raw_candidates]
    floor = int(data["risk_strength_floors"][risk_clean])
    candidates = [candidate for candidate in candidates if candidate.strength >= floor]
    if not candidates:
        raise SerenaPolicyError(f"profile {profile} lane {lane} has no model above its safety floor")

    requested = normalise_model(manual_override, policy=data, strict=True)
    fallback_reason = ""
    if requested != "auto":
        allowed = set(raw_profile.get("manual_models") or [])
        if requested not in allowed:
            raise SerenaPolicyError(f"model {requested} is not allowed for profile {profile}")
        manual = _candidate(
            data,
            {"model": requested, "effort": candidates[0].effort},
            effort=candidates[0].effort,
        )
        if manual.strength < floor:
            fallback_reason = (
                f"manual override {requested} is below the {risk_clean} safety floor"
            )
        else:
            candidates = [manual, *(item for item in candidates if item.model != requested)]

    if avoid_provider and requested == "auto":
        candidates.sort(key=lambda item: item.provider == avoid_provider)

    chosen = next(
        (
            candidate
            for candidate in candidates
            if model_usable(
                capacity,
                provider=candidate.provider,
                model=candidate.model,
            )
        ),
        None,
    )
    if chosen is None:
        raise SerenaPolicyError(f"no provider has capacity for {profile} {lane} {role}")
    first = candidates[0]
    if chosen != first:
        provider_entry = _capacity_entry(capacity, first.provider)
        model_entry = _model_capacity_entry(capacity, first.model)
        entry = (
            model_entry
            if provider_usable(capacity, first.provider)
            else provider_entry
        )
        detail = str(entry.get("reason") or f"{first.provider} is unavailable").strip()
        capacity_reason = f"{first.model} was unavailable: {detail}"
        fallback_reason = (
            f"{fallback_reason}; {capacity_reason}" if fallback_reason else capacity_reason
        )
    reason_parts = [
        f"{profile} activity {activity_clean} selected lane {lane}",
        f"risk {risk_clean}",
    ]
    if requested != "auto" and chosen.model == requested:
        reason_parts.append(f"manual override selected {requested}")
    elif requested == "auto":
        reason_parts.append("automatic policy selection")
    if fallback_reason:
        reason_parts.append(fallback_reason)
    return PolicyDecision(
        profile=profile,
        activity=activity_clean,
        complexity=complexity_clean,
        risk=risk_clean,
        lane=lane,
        role=role,
        provider=chosen.provider,
        model=chosen.model,
        runtime_model=chosen.runtime_model,
        effort=chosen.effort,
        manual_override=requested,
        fallback_reason=fallback_reason,
        reason=", ".join(reason_parts),
        policy_version=int(data["schema_version"]),
        candidates=tuple(candidate.as_dict() for candidate in candidates),
    )


def fleet_policy_reference(policy: Mapping[str, Any] | None = None) -> str:
    data = policy or load_policy()
    return str(data["profiles"]["fleet"]["source"])


def validate_frozen_decision(
    decision: object,
    *,
    profile: str,
    role: str,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a durable decision without silently resolving it again."""

    data = policy or load_policy()
    if not isinstance(decision, Mapping):
        raise SerenaPolicyError("frozen policy decision must be an object")
    frozen = dict(decision)
    if frozen.get("profile") != profile or frozen.get("role") != role:
        raise SerenaPolicyError("frozen policy decision has the wrong profile or role")
    if int(frozen.get("policy_version") or 0) != int(data["schema_version"]):
        raise SerenaPolicyError("frozen policy decision has the wrong policy version")
    model = str(frozen.get("model") or "")
    spec = data["models"].get(model)
    if not isinstance(spec, Mapping):
        raise SerenaPolicyError("frozen policy decision has an unknown model")
    if frozen.get("provider") != spec.get("provider"):
        raise SerenaPolicyError("frozen policy decision provider does not match its model")
    if frozen.get("runtime_model") != spec.get("runtime_model"):
        raise SerenaPolicyError("frozen policy decision runtime model does not match")
    effort = _clean(frozen.get("effort"))
    if effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
        raise SerenaPolicyError("frozen policy decision has an invalid effort")
    raw_profile = data["profiles"].get(profile)
    lane = str(frozen.get("lane") or "")
    if not isinstance(raw_profile, Mapping) or lane not in (raw_profile.get("lanes") or {}):
        raise SerenaPolicyError("frozen policy decision has an invalid lane")
    if not raw_profile["lanes"][lane].get(role):
        raise SerenaPolicyError("frozen policy decision lane does not support its role")
    risk = _clean(frozen.get("risk"))
    if risk not in RISK_LEVELS:
        raise SerenaPolicyError("frozen policy decision has an invalid risk")
    if int(spec["strength"]) < int(data["risk_strength_floors"][risk]):
        raise SerenaPolicyError("frozen policy decision is below its safety floor")
    return frozen
