"""One local API over Serena's Gideon foundations.

The underlying modules deliberately own storage, policy, and adapters.  This
facade only gives resident surfaces one stable contract instead of teaching
Claude, Codex, voice, and the web UI four different ways to reach them.

No method silently grants authority.  Read methods are observations.  Mutating
methods are called only after the surface broker has produced an allowed
``ActionAuthority`` decision for the live turn.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from core.briefings import build_evening_briefing, build_morning_briefing
from core.commitments import CommitmentStore
from core.device_actions import DeviceActionRunner, build_default_runner
from core.provider_health import ContinuityStore, assess_continuity
from core.runtime_readiness import RuntimeCoordinator, RuntimeLedger
from core.state_graph import StateGraphStore, register_current_system
from core.supportive_mode import SupportiveModeStore, support_boundary
from core.world_cockpit import WorldCockpit


class GideonAPIError(RuntimeError):
    """A requested Gideon capability is unavailable or malformed."""


class GideonAPI:
    """The provider-neutral API used by every resident Serena surface."""

    def __init__(
        self,
        *,
        commitments: CommitmentStore | None = None,
        state_graph: StateGraphStore | None = None,
        continuity: ContinuityStore | None = None,
        runtime_ledger: RuntimeLedger | None = None,
        devices: DeviceActionRunner | None = None,
        supportive: SupportiveModeStore | None = None,
        runtime: RuntimeCoordinator | None = None,
        visual: Any | None = None,
    ) -> None:
        self.commitment_store = commitments or CommitmentStore()
        self.state_store = state_graph or StateGraphStore()
        self.continuity_store = continuity or ContinuityStore()
        self.runtime_ledger = runtime_ledger or RuntimeLedger()
        self.device_runner = devices or build_default_runner()
        self.support_store = supportive or SupportiveModeStore()
        self.runtime = runtime
        self.visual = visual

    # -- overview ---------------------------------------------------------

    def status(
        self,
        *,
        capacity: Mapping[str, Any] | None = None,
        probe_local: bool = True,
    ) -> dict[str, Any]:
        continuity = assess_continuity(capacity, probe_local=probe_local)
        self.continuity_store.record_mode(continuity)
        commitments = self.commitment_store.list(open_only=True, limit=500)
        support = self.support_store.settings()
        runtime = self.runtime.snapshot().to_dict() if self.runtime is not None else None
        return {
            "schema_version": 1,
            "continuity": continuity.to_dict(),
            "authority": self.device_runner.authority.lock_state(),
            "commitments": {
                "open": len(commitments),
                "overdue": len(self.commitment_store.overdue()),
            },
            "state_graph": {
                "entities": len(self.state_store.entities()),
                "fresh_entities": len(self.state_store.entities(include_stale=False)),
            },
            "devices": {
                "scenes": sorted(self.device_runner.scenes()),
                "adapters": self.device_runner.availability(),
                "default_dry_run": self.device_runner.default_dry_run,
            },
            "supportive": asdict(support),
            "runtime": runtime,
            "visual": {
                "available": self.visual is not None,
                "reason": "registered" if self.visual is not None else "no desktop capture adapter registered",
            },
        }

    # -- commitments and briefings --------------------------------------

    def list_commitments(
        self,
        *,
        state: str = "",
        open_only: bool = True,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return [
            asdict(item)
            for item in self.commitment_store.list(
                state=state or None,
                open_only=bool(open_only and not state),
                limit=max(1, min(int(limit), 200)),
            )
        ]

    def briefing(self, kind: str = "morning", *, mode: str = "full") -> dict[str, Any]:
        clean = str(kind or "morning").strip().lower()
        if clean == "morning":
            value = build_morning_briefing(self.commitment_store, mode=mode)
        elif clean == "evening":
            value = build_evening_briefing(self.commitment_store, mode=mode)
        else:
            raise GideonAPIError("briefing kind must be morning or evening")
        return asdict(value)

    def create_commitment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        item = self.commitment_store.propose(
            title=str(payload.get("title") or ""),
            detail=str(payload.get("detail") or ""),
            owner=str(payload.get("owner") or "raghav"),
            priority=str(payload.get("priority") or "normal"),
            due_at=payload.get("due_at"),
            recurrence=payload.get("recurrence"),
            lead_seconds=int(payload.get("lead_seconds") or 900),
            actor="raghav-via-serena",
            source="live_turn",
            source_ref=str(payload.get("source_ref") or ""),
            state="accepted",
        )
        return asdict(item)

    def change_commitment(
        self,
        commitment_id: str,
        action: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        values = dict(payload or {})
        clean = str(action or "").strip().lower()
        actor = "raghav-via-serena"
        if clean == "complete":
            current, following = self.commitment_store.complete(
                commitment_id,
                actor=actor,
                reason=str(values.get("reason") or "completed from live turn"),
            )
            return {
                "commitment": asdict(current),
                "next_occurrence": asdict(following) if following else None,
            }
        if clean == "snooze":
            current = self.commitment_store.snooze(
                commitment_id,
                actor=actor,
                until=values.get("until"),
                seconds=values.get("seconds"),
                reason=str(values.get("reason") or "snoozed from live turn"),
            )
            return {"commitment": asdict(current)}
        if clean == "dismiss":
            current, following = self.commitment_store.dismiss(
                commitment_id,
                actor=actor,
                reason=str(values.get("reason") or "dismissed from live turn"),
            )
            return {
                "commitment": asdict(current),
                "next_occurrence": asdict(following) if following else None,
            }
        if clean == "correct":
            allowed = {
                key: values[key]
                for key in (
                    "title",
                    "detail",
                    "owner",
                    "priority",
                    "due_at",
                    "recurrence",
                    "lead_seconds",
                    "subject_entity_id",
                )
                if key in values
            }
            current = self.commitment_store.correct(
                commitment_id,
                actor=actor,
                reason=str(values.get("reason") or "corrected from live turn"),
                **allowed,
            )
            return {"commitment": asdict(current)}
        raise GideonAPIError("commitment action must be complete, snooze, dismiss, or correct")

    # -- local state and world -------------------------------------------

    def state_snapshot(
        self,
        *,
        kind: str = "",
        include_stale: bool = False,
    ) -> dict[str, Any]:
        entities = self.state_store.entities(
            kind=kind or None,
            include_stale=include_stale,
        )
        identifiers = {item.entity_id for item in entities}
        edges = [
            edge
            for edge in self.state_store.edges(include_stale=include_stale)
            if not identifiers or edge.subject_id in identifiers or edge.object_id in identifiers
        ]
        return {
            "entities": [asdict(item) for item in entities[:200]],
            "relationships": [asdict(item) for item in edges[:400]],
        }

    def refresh_system_state(self) -> dict[str, Any]:
        return register_current_system(self.state_store)

    def world_snapshot(self, *, limit: int = 40) -> dict[str, Any]:
        return WorldCockpit(self.state_store).snapshot(limit=max(1, min(int(limit), 100)))

    # -- devices ----------------------------------------------------------

    def device_status(self) -> dict[str, Any]:
        return {
            "scenes": {
                name: {
                    "description": scene.description,
                    "steps": [asdict(step) for step in scene.steps],
                }
                for name, scene in self.device_runner.scenes().items()
            },
            "adapters": self.device_runner.availability(),
            "default_dry_run": self.device_runner.default_dry_run,
        }

    def run_scene(
        self,
        name: str,
        *,
        intent: str,
        source: str,
        session_id: str,
        turn_id: str,
        origin_proof: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        report = self.device_runner.run_scene(
            name,
            intent=intent,
            source=source,
            session_id=session_id,
            turn_id=turn_id,
            authorization_basis="origin_turn_verified",
            origin_proof=origin_proof,
            dry_run=dry_run,
        )
        return asdict(report)

    # -- opt-in support ---------------------------------------------------

    def support_status(self, *, include_reflections: bool = False) -> dict[str, Any]:
        settings = self.support_store.settings()
        value: dict[str, Any] = {
            "settings": asdict(settings),
            "patterns": [asdict(item) for item in self.support_store.patterns()],
        }
        if include_reflections:
            value["reflections"] = [
                asdict(item) for item in self.support_store.reflections(limit=100)
            ]
        return value

    def configure_support(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        current = self.support_store.settings()
        settings = self.support_store.configure(
            enabled=bool(payload.get("enabled", current.enabled)),
            allow_checkins=bool(payload.get("allow_checkins", current.allow_checkins)),
            allow_pattern_insights=bool(
                payload.get("allow_pattern_insights", current.allow_pattern_insights)
            ),
            retention_days=int(payload.get("retention_days", current.retention_days)),
            checkin_interval_hours=int(
                payload.get("checkin_interval_hours", current.checkin_interval_hours)
            ),
            relationship_context=str(
                payload.get("relationship_context", current.relationship_context)
            ),
        )
        return asdict(settings)

    def add_reflection(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        value = self.support_store.add_reflection(
            str(payload.get("body") or ""),
            source=str(payload.get("source") or "live_turn"),
            mood=str(payload.get("mood") or ""),
            tags=tuple(str(item) for item in payload.get("tags", []) or []),
            retention_days=(
                int(payload["retention_days"])
                if payload.get("retention_days") is not None
                else None
            ),
        )
        return asdict(value)

    def forget_reflection(self, entry_id: str) -> dict[str, Any]:
        return {"deleted": self.support_store.forget_reflection(entry_id), "entry_id": entry_id}

    def support_boundary(self, text: str) -> dict[str, Any]:
        return asdict(support_boundary(text))

    # -- consent-gated visual context ------------------------------------

    def visual_status(self) -> dict[str, Any]:
        return {
            "available": self.visual is not None,
            "reason": "registered" if self.visual is not None else "no desktop capture adapter registered",
        }

    def capture_visual(self, consent: Any) -> Any:
        if self.visual is None:
            raise GideonAPIError("visual context is unavailable: no desktop capture adapter registered")
        return self.visual.capture(consent)

    # -- runtime work ledger ---------------------------------------------

    def unfinished_runtime_work(self) -> list[dict[str, Any]]:
        return [asdict(item) for item in self.runtime_ledger.resumable_work()]


_DEFAULT_API: GideonAPI | None = None


def default_gideon_api() -> GideonAPI:
    global _DEFAULT_API
    if _DEFAULT_API is None:
        _DEFAULT_API = GideonAPI()
    return _DEFAULT_API


def reset_default_gideon_api() -> None:
    global _DEFAULT_API
    _DEFAULT_API = None


__all__ = [
    "GideonAPI",
    "GideonAPIError",
    "default_gideon_api",
    "reset_default_gideon_api",
]
