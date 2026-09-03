"""Serena's hands: one path from "do the thing" to a receipt that tells the truth.

Every action here goes through core.action_authority first, whatever the
adapter underneath is. That is the whole point of the module. The laptop
broker, a phone over ADB, and a light in the house are different hardware with
the same question in front of them: is this allowed, right now, for this
person, at this tier.

What this adds on top of an adapter call:

- dry run, which is the default for anything a caller has not thought about.
  A simulated step reports `simulated`, never `completed`, so a plan can be
  read out loud without anyone believing it happened.
- named scenes, so "wind down" is one authorized thing rather than six.
- idempotency, so a retried step inside a window returns the recorded result
  instead of pressing the button twice.
- timeouts and per-step failure policy.
- compensation, best effort, in reverse order, for the steps that already ran
  when a later one failed. Compensation is not a transaction and does not
  pretend to be: an adapter that cannot honestly undo something says so and
  the report carries that gap instead of hiding it.
- postcondition checks, so "I turned the light on" can be backed by asking
  whether the light is on.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.action_authority import (
    ActionAuthority,
    ActionAuthorityError,
    build_request,
    default_authority,
)
from core.adapters.base import (
    AdapterRegistry,
    AdapterStatus,
    DeviceAdapter,
    DeviceCommand,
    DeviceResult,
)
from core.adapters.base import (
    registry as default_registry,
)

DEFAULT_SCENE_PATH = Path.home() / ".config" / "serena" / "scenes.json"
DEFAULT_STEP_TIMEOUT = 15.0
MAX_STEP_TIMEOUT = 120.0
MAX_SCENE_STEPS = 25
IDEMPOTENCY_WINDOW_SECONDS = 90.0

ON_FAILURE_MODES = frozenset({"stop", "continue", "rollback"})


class DeviceActionError(ValueError):
    """The requested device action or scene is not valid."""


@dataclass(frozen=True, slots=True)
class StepReport:
    capability: str
    target: str
    ok: bool
    status: str
    detail: str
    tier: int
    simulated: bool
    authorized: bool
    authority_reason: str
    postcondition_checked: bool = False
    postcondition_ok: bool | None = None
    compensated: bool = False
    compensation_detail: str = ""
    request_id: str = ""
    duration_seconds: float = 0.0
    reused_idempotent_result: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ActionReport:
    """What ran, what did not, and what is now in an unknown state."""

    ok: bool
    name: str
    dry_run: bool
    steps: list[StepReport] = field(default_factory=list)
    stopped_early: bool = False
    rolled_back: bool = False
    uncompensated: list[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def partial(self) -> bool:
        """True when some steps worked and some did not. The dangerous shape."""

        done = [step for step in self.steps if step.ok]
        return bool(done) and len(done) != len(self.steps)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["steps"] = [step.to_dict() for step in self.steps]
        payload["partial"] = self.partial
        return payload

    def summary(self) -> str:
        if not self.steps:
            return f"{self.name}: nothing to do"
        done = sum(1 for step in self.steps if step.ok)
        if self.dry_run:
            return f"{self.name}: {done} of {len(self.steps)} steps would run"
        if self.ok:
            return f"{self.name}: all {len(self.steps)} steps done"
        failed = [step.capability for step in self.steps if not step.ok]
        detail = f"{done} of {len(self.steps)} done, failed at {', '.join(failed[:3])}"
        if self.uncompensated:
            detail += f"; could not undo {', '.join(self.uncompensated[:3])}"
        return f"{self.name}: {detail}"


@dataclass(frozen=True, slots=True)
class SceneStep:
    capability: str
    target: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_STEP_TIMEOUT
    on_failure: str = "stop"
    optional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Scene:
    name: str
    description: str
    steps: tuple[SceneStep, ...]
    rollback: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["steps"] = [step.to_dict() for step in self.steps]
        return payload


def parse_scene(name: object, raw: Mapping[str, Any]) -> Scene:
    clean_name = " ".join(str(name or "").split())[:64]
    if not clean_name:
        raise DeviceActionError("a scene needs a name")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, str) or not raw_steps:
        raise DeviceActionError(f"scene {clean_name} has no steps")
    if len(raw_steps) > MAX_SCENE_STEPS:
        raise DeviceActionError(f"scene {clean_name} has more than {MAX_SCENE_STEPS} steps")
    steps: list[SceneStep] = []
    for index, item in enumerate(raw_steps, start=1):
        if not isinstance(item, Mapping):
            raise DeviceActionError(f"scene {clean_name} step {index} must be an object")
        capability = " ".join(str(item.get("capability") or "").split())
        if not capability:
            raise DeviceActionError(f"scene {clean_name} step {index} has no capability")
        on_failure = str(item.get("on_failure") or "stop").strip().lower()
        if on_failure not in ON_FAILURE_MODES:
            raise DeviceActionError(
                f"scene {clean_name} step {index} has an unknown failure mode {on_failure}"
            )
        params = item.get("params")
        if params is not None and not isinstance(params, Mapping):
            raise DeviceActionError(f"scene {clean_name} step {index} params must be an object")
        timeout = float(item.get("timeout_seconds") or DEFAULT_STEP_TIMEOUT)
        if not 0 < timeout <= MAX_STEP_TIMEOUT:
            raise DeviceActionError(
                f"scene {clean_name} step {index} timeout must be 0 to {MAX_STEP_TIMEOUT:.0f}s"
            )
        steps.append(
            SceneStep(
                capability=capability,
                target=" ".join(str(item.get("target") or "").split())[:512],
                params=dict(params or {}),
                timeout_seconds=timeout,
                on_failure=on_failure,
                optional=bool(item.get("optional", False)),
            )
        )
    return Scene(
        name=clean_name,
        description=" ".join(str(raw.get("description") or "").split())[:500],
        steps=tuple(steps),
        rollback=bool(raw.get("rollback", True)),
    )


def load_scenes(path: Path | None = None) -> dict[str, Scene]:
    """Read the scene file. A missing file is no scenes, not an error."""

    target = Path(path or DEFAULT_SCENE_PATH).expanduser()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        raise DeviceActionError(f"the scene file could not be read: {error}") from error
    scenes = raw.get("scenes") if isinstance(raw, Mapping) else None
    if not isinstance(scenes, Mapping):
        raise DeviceActionError("the scene file needs a scenes object")
    return {str(name): parse_scene(name, body) for name, body in scenes.items() if isinstance(body, Mapping)}


class DeviceActionRunner:
    """Authority in front, adapters behind, one honest report out."""

    def __init__(
        self,
        *,
        authority: ActionAuthority | None = None,
        adapters: AdapterRegistry | None = None,
        scenes: Mapping[str, Scene] | None = None,
        default_dry_run: bool = True,
    ) -> None:
        self._authority = authority
        self._registry = adapters if adapters is not None else default_registry
        self._scenes = dict(scenes or {})
        self.default_dry_run = bool(default_dry_run)
        self._idempotency: dict[str, tuple[float, DeviceResult]] = {}

    @property
    def authority(self) -> ActionAuthority:
        if self._authority is None:
            self._authority = default_authority()
        return self._authority

    @property
    def adapters(self) -> AdapterRegistry:
        return self._registry

    def register(self, adapter: DeviceAdapter) -> DeviceAdapter:
        return self._registry.register(adapter)

    def scenes(self) -> dict[str, Scene]:
        return dict(self._scenes)

    def add_scene(self, scene: Scene) -> Scene:
        self._scenes[scene.name] = scene
        return scene

    def availability(self) -> dict[str, Any]:
        return self._registry.snapshot()

    def scene_actions(self, name: str) -> list[tuple[str, str]]:
        """The exact (capability, target) pairs a scene will ask for.

        A broker that has verified a live turn passes this to
        `ActionAuthority.issue_turn_proof` so the resulting proof covers this
        scene and nothing else. The runner deliberately does not mint its own
        proof: whoever actually saw the turn is the only one who can honestly
        say it happened.
        """

        scene = self._scenes.get(str(name))
        if scene is None:
            raise DeviceActionError(f"there is no scene called {name}")
        return [(step.capability, step.target) for step in scene.steps]

    # -- one action ---------------------------------------------------------

    def run(
        self,
        capability: str,
        *,
        target: str = "",
        params: Mapping[str, Any] | None = None,
        intent: str = "",
        source: str = "voice",
        session_id: str = "",
        turn_id: str = "",
        authorization_basis: str = "none",
        grant_id: str = "",
        confirmation_id: str = "",
        origin_proof: str = "",
        dry_run: bool | None = None,
        timeout_seconds: float = DEFAULT_STEP_TIMEOUT,
        idempotency_key: str = "",
        check_postcondition: bool = True,
    ) -> ActionReport:
        simulate = self.default_dry_run if dry_run is None else bool(dry_run)
        started = time.time()
        step = self._run_step(
            SceneStep(
                capability=str(capability),
                target=str(target or ""),
                params=dict(params or {}),
                timeout_seconds=float(timeout_seconds),
            ),
            intent=intent or f"run {capability}",
            source=source,
            session_id=session_id,
            turn_id=turn_id,
            authorization_basis=authorization_basis,
            grant_id=grant_id,
            confirmation_id=confirmation_id,
            origin_proof=origin_proof,
            dry_run=simulate,
            idempotency_key=idempotency_key,
            check_postcondition=check_postcondition,
        )
        return ActionReport(
            ok=step.ok,
            name=str(capability),
            dry_run=simulate,
            steps=[step],
            started_at=started,
            finished_at=time.time(),
        )

    # -- a scene ------------------------------------------------------------

    def run_scene(
        self,
        name: str,
        *,
        intent: str = "",
        source: str = "voice",
        session_id: str = "",
        turn_id: str = "",
        authorization_basis: str = "none",
        grant_id: str = "",
        confirmation_id: str = "",
        origin_proof: str = "",
        dry_run: bool | None = None,
        check_postcondition: bool = True,
    ) -> ActionReport:
        scene = self._scenes.get(str(name))
        if scene is None:
            raise DeviceActionError(f"there is no scene called {name}")
        simulate = self.default_dry_run if dry_run is None else bool(dry_run)
        started = time.time()
        reports: list[StepReport] = []
        completed: list[tuple[SceneStep, StepReport]] = []
        stopped = False
        rolled_back = False
        uncompensated: list[str] = []

        # An optional step that fails is tolerated; anything else that fails
        # makes the scene not ok even when the remaining steps still ran.
        unforgiven = 0

        for step in scene.steps:
            report = self._run_step(
                step,
                intent=intent or f"{scene.name}: {scene.description or step.capability}",
                source=source,
                session_id=session_id,
                turn_id=turn_id,
                authorization_basis=authorization_basis,
                grant_id=grant_id,
                confirmation_id=confirmation_id,
                origin_proof=origin_proof,
                dry_run=simulate,
                idempotency_key="",
                check_postcondition=check_postcondition,
            )
            reports.append(report)
            if report.ok:
                completed.append((step, report))
                continue
            if step.optional:
                continue
            unforgiven += 1
            if step.on_failure == "continue":
                continue
            stopped = True
            if step.on_failure == "rollback" and scene.rollback and not simulate:
                rolled_back = True
                uncompensated = self._compensate(completed, reports)
            break

        return ActionReport(
            ok=unforgiven == 0 and not stopped,
            name=scene.name,
            dry_run=simulate,
            steps=reports,
            stopped_early=stopped,
            rolled_back=rolled_back,
            uncompensated=uncompensated,
            started_at=started,
            finished_at=time.time(),
        )

    def _compensate(
        self,
        completed: Sequence[tuple[SceneStep, StepReport]],
        reports: list[StepReport],
    ) -> list[str]:
        """Undo what ran, newest first. Record every step that cannot be undone.

        Undoing is doing. Each compensation is a fresh authority request tied
        to the recorded step it reverses, so a global stop engaged between the
        step and the rollback stops the rollback too, and the step is reported
        as uncompensated rather than quietly touching hardware anyway.
        """

        uncompensated: list[str] = []
        by_index = {id(report): index for index, report in enumerate(reports)}
        for step, report in reversed(list(completed)):
            adapter = self._adapter_for(step.capability)
            index = by_index.get(id(report))
            if adapter is None:
                uncompensated.append(step.capability)
                continue
            verdict = self.authority.authorize_compensation(
                original_request_id=report.request_id,
                intent=f"undo {step.capability}",
            )
            if not verdict.allowed:
                uncompensated.append(step.capability)
                if index is not None:
                    reports[index] = _with(
                        reports[index],
                        compensation_detail=f"undo was not allowed: {verdict.reason}",
                    )
                continue
            command = self._command(step, "")
            attempt = _bounded_call(adapter.compensate, command, command.timeout_seconds)
            undone = attempt.result
            if undone is None and not attempt.clean:
                undone = DeviceResult(
                    False, attempt.status, step.capability, step.target, attempt.detail
                )
            self.authority.record_outcome(
                verdict.request_id,
                status="completed" if (undone is not None and undone.ok) else "failed",
                detail=(undone.detail if undone is not None else "this adapter has no honest undo"),
            )
            if undone is None:
                uncompensated.append(step.capability)
                if index is not None:
                    reports[index] = _with(
                        reports[index],
                        compensation_detail="this adapter has no honest undo for that",
                    )
                continue
            if not undone.ok:
                uncompensated.append(step.capability)
            if index is not None:
                reports[index] = _with(
                    reports[index],
                    compensated=bool(undone.ok),
                    compensation_detail=undone.detail or undone.status,
                )
        return uncompensated

    # -- the shared path ----------------------------------------------------

    def _run_step(
        self,
        step: SceneStep,
        *,
        intent: str,
        source: str,
        session_id: str,
        turn_id: str,
        authorization_basis: str,
        grant_id: str,
        confirmation_id: str,
        origin_proof: str,
        dry_run: bool,
        idempotency_key: str,
        check_postcondition: bool,
    ) -> StepReport:
        began = time.time()
        adapter = self._adapter_for(step.capability)
        if adapter is None:
            return StepReport(
                step.capability, step.target, False, "unsupported",
                f"nothing on this machine can do {step.capability}",
                tier=0, simulated=dry_run, authorized=False,
                authority_reason="no adapter claims this capability",
                duration_seconds=time.time() - began,
            )
        effect = adapter.capabilities().get(step.capability, "external")

        try:
            request = build_request(
                capability=step.capability,
                intent=intent,
                source=source,
                effect=effect,
                session_id=session_id,
                turn_id=turn_id,
                target=step.target,
                requested_scope=(f"device:{adapter.name}", f"capability:{step.capability}"),
                authorization_basis=authorization_basis,
                grant_id=grant_id,
                confirmation_id=confirmation_id,
                origin_proof=origin_proof,
                dry_run=dry_run,
                context={"adapter": adapter.name, "params": _safe(step.params)},
            )
        except ActionAuthorityError as error:
            return StepReport(
                step.capability, step.target, False, "rejected", str(error),
                tier=0, simulated=dry_run, authorized=False, authority_reason=str(error),
                duration_seconds=time.time() - began,
            )

        authority = self.authority
        decision = authority.authorize(request)
        if not decision.allowed:
            authority.record_outcome(request.request_id, status="denied", detail=decision.reason)
            return StepReport(
                step.capability, step.target, False, "denied", decision.reason,
                tier=decision.tier, simulated=dry_run, authorized=False,
                authority_reason=decision.reason, request_id=request.request_id,
                duration_seconds=time.time() - began,
            )

        command = self._command(step, idempotency_key)

        if dry_run:
            try:
                status: AdapterStatus = adapter.status()
                description = adapter.describe(command)
            except Exception as error:
                status = AdapterStatus(False, f"{type(error).__name__}: adapter check failed")
                description = f"would run {step.capability}"
            detail = description if status.available else f"{description} (blocked: {status.reason})"
            authority.record_outcome(
                request.request_id, status="simulated", detail=detail,
                receipt={"adapter_available": status.available},
            )
            return StepReport(
                step.capability, step.target, bool(status.available), "simulated", detail,
                tier=decision.tier, simulated=True, authorized=True,
                authority_reason=decision.reason, request_id=request.request_id,
                duration_seconds=time.time() - began,
            )

        cached = self._cached(command)
        if cached is not None:
            authority.record_outcome(
                request.request_id, status="completed",
                detail="returned the recorded result for an identical recent action",
                receipt={"idempotent": True},
            )
            return StepReport(
                step.capability, step.target, cached.ok, cached.status, cached.detail,
                tier=decision.tier, simulated=False, authorized=True,
                authority_reason=decision.reason, request_id=request.request_id,
                duration_seconds=time.time() - began, reused_idempotent_result=True,
            )

        attempt = _bounded_call(adapter.execute, command, command.timeout_seconds)
        if attempt.result is None:
            authority.record_outcome(
                request.request_id, status="failed", detail=attempt.detail
            )
            return StepReport(
                step.capability, step.target, False, attempt.status, attempt.detail,
                tier=decision.tier, simulated=False, authorized=True,
                authority_reason=decision.reason, request_id=request.request_id,
                duration_seconds=time.time() - began,
            )
        result = attempt.result

        checked = False
        postcondition: bool | None = None
        if check_postcondition and result.ok:
            try:
                postcondition = adapter.postcondition(command)
            except Exception:
                postcondition = None
            checked = postcondition is not None

        ok = bool(result.ok) and postcondition is not False
        detail = result.detail
        if postcondition is False:
            # The adapter said it worked and the world disagrees. The world wins.
            detail = f"{detail or result.status}; the device did not actually change"

        if ok and command.idempotency_key:
            self._idempotency[command.idempotency_key] = (time.time(), result)

        authority.record_outcome(
            request.request_id,
            status="completed" if ok else "failed",
            detail=detail,
            receipt={
                **_safe(result.receipt),
                "adapter": adapter.name,
                "postcondition_ok": postcondition,
            },
        )
        return StepReport(
            step.capability, step.target, ok,
            "completed" if ok else (result.status or "failed"), detail,
            tier=decision.tier, simulated=False, authorized=True,
            authority_reason=decision.reason,
            postcondition_checked=checked, postcondition_ok=postcondition,
            request_id=request.request_id, duration_seconds=time.time() - began,
        )

    def _command(self, step: SceneStep, idempotency_key: str) -> DeviceCommand:
        return DeviceCommand(
            capability=step.capability,
            target=step.target,
            params=dict(step.params),
            timeout_seconds=min(MAX_STEP_TIMEOUT, max(0.1, float(step.timeout_seconds))),
            idempotency_key=str(idempotency_key or ""),
        )

    def _adapter_for(self, capability: str) -> DeviceAdapter | None:
        return self._registry.for_capability(capability)

    def _cached(self, command: DeviceCommand) -> DeviceResult | None:
        if not command.idempotency_key:
            return None
        entry = self._idempotency.get(command.idempotency_key)
        if entry is None:
            return None
        stamped, result = entry
        if time.time() - stamped > IDEMPOTENCY_WINDOW_SECONDS:
            self._idempotency.pop(command.idempotency_key, None)
            return None
        return result


@dataclass(frozen=True, slots=True)
class _Attempt:
    """One adapter call that either answered, raised, or ran out of time."""

    result: DeviceResult | None
    status: str = "completed"
    detail: str = ""
    clean: bool = True


def _bounded_call(
    call: Any, command: DeviceCommand, timeout_seconds: float
) -> _Attempt:
    """Never wait on an adapter forever, even one that ignores its own timeout.

    The timeout is handed to the adapter in the command, but an adapter is
    ordinary code and a laptop path that blocks on a subprocess would otherwise
    hang the whole runner. This stops waiting and reports honestly.

    It cannot kill the thread; Python has no safe way to do that. So the wording
    is `abandoned`, not `cancelled`: the call may still be in flight, which is
    exactly why an adapter is still required to bound its own work. Saying it
    was stopped when it was only abandoned would be the kind of lie this module
    exists to avoid.
    """

    limit = min(MAX_STEP_TIMEOUT, max(0.1, float(timeout_seconds)))
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="serena-device")
    try:
        future = executor.submit(call, command)
        try:
            return _Attempt(result=future.result(timeout=limit))
        except FuturesTimeout:
            future.cancel()
            return _Attempt(
                result=None,
                status="timeout",
                detail=(
                    f"{command.capability} did not answer within {limit:.0f}s and was "
                    "abandoned; it may still be running"
                ),
                clean=False,
            )
        except Exception as error:
            return _Attempt(
                result=None,
                status="failed",
                detail=f"{type(error).__name__}: {error}",
                clean=False,
            )
    finally:
        executor.shutdown(wait=False)


def _with(report: StepReport, **changes: Any) -> StepReport:
    return StepReport(**{**report.to_dict(), **changes})


def _safe(value: Mapping[str, Any]) -> dict[str, Any]:
    """Params can carry a whole origin turn. Keep evidence small and printable."""

    out: dict[str, Any] = {}
    for key, item in dict(value or {}).items():
        name = str(key)[:64]
        if name == "origin":
            out[name] = "<origin turn withheld>"
        elif isinstance(item, (str, int, float, bool)) or item is None:
            out[name] = item if not isinstance(item, str) else item[:500]
        else:
            out[name] = str(type(item).__name__)
    return out


def build_default_runner(
    *,
    authority: ActionAuthority | None = None,
    scene_path: Path | None = None,
) -> DeviceActionRunner:
    """Every adapter this machine could possibly use, present or not.

    Registering an adapter is not a claim that its hardware exists. Each one
    reports its own availability, so the phone and the house show up as known
    but unreachable rather than silently missing.
    """

    from core.adapters.android_adb import AndroidAdbAdapter
    from core.adapters.home_assistant import HomeAssistantAdapter, MqttAdapter
    from core.adapters.laptop import LaptopAdapter

    bench = AdapterRegistry()
    bench.register(LaptopAdapter())
    bench.register(AndroidAdbAdapter())
    bench.register(HomeAssistantAdapter())
    bench.register(MqttAdapter())
    try:
        scenes = load_scenes(scene_path)
    except DeviceActionError:
        scenes = {}
    return DeviceActionRunner(authority=authority, adapters=bench, scenes=scenes)
