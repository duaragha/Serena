"""Resident-brain tools for the provider-neutral Gideon API.

Reads are local observations.  Writes require a deterministic match against
the actual live turn, then receive a single-use proof from ActionAuthority.
The model can choose arguments, but it cannot manufacture the originating
instruction that makes those arguments executable.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations

from core.action_authority import (
    BASIS_ORIGIN_TURN,
    ActionAuthority,
    build_request,
    default_authority,
)
from core.brain_laptop_tools import current_turn
from core.gideon_api import GideonAPI, default_gideon_api
from core.visual_context import CAPTURE_SCOPE, CaptureConsent

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_LOCAL_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)

_WRITE_SIGNALS: dict[str, re.Pattern[str]] = {
    "commitment.create": re.compile(
        r"\b(?:remind|remember|add|create|schedule|i need to|i have to|put .* on)\b",
        re.IGNORECASE,
    ),
    "commitment.complete": re.compile(
        r"\b(?:done|complete|completed|finished|i did|mark .* done)\b", re.IGNORECASE
    ),
    "commitment.snooze": re.compile(
        r"\b(?:snooze|later|remind me (?:again|in|at)|stop reminding .* until)\b",
        re.IGNORECASE,
    ),
    "commitment.dismiss": re.compile(
        r"\b(?:dismiss|skip|not this one|hide this reminder)\b", re.IGNORECASE
    ),
    "commitment.correct": re.compile(
        r"\b(?:change|correct|update|move|rename|set .* (?:to|for))\b", re.IGNORECASE
    ),
    "support.configure": re.compile(
        r"\b(?:(?:enable|disable|turn on|turn off|configure|change).*(?:support|check[- ]?in|reflection|retention|pattern))\b",
        re.IGNORECASE,
    ),
    "support.reflect": re.compile(
        r"\b(?:save|record|remember|add|write).*(?:reflection|journal|how i feel|mood)\b",
        re.IGNORECASE,
    ),
    "device.scene": re.compile(
        r"\b(?:run|start|activate|trigger|turn on|apply|preview|test)\b", re.IGNORECASE
    ),
    "screen.capture": re.compile(
        r"\b(?:look at|see|view|check|read|inspect|capture|what(?:'s| is) on).*(?:screen|window|desktop|display|this)\b",
        re.IGNORECASE,
    ),
}


def _api() -> GideonAPI:
    return default_gideon_api()


def _text(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    return {"content": [{"type": "text", "text": value}]}


def _payload(raw: object) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise ValueError("payload_json is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("payload_json must contain a JSON object")
    return value


def _surface(origin: Mapping[str, object]) -> str:
    protocol = str(origin.get("protocol") or "").strip().lower()
    surface = str(origin.get("surface") or "").strip().lower()
    if protocol == "voice" or surface in {"voice", "desk", "overlay"}:
        return "voice"
    return "chat"


def _normal(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _target_is_grounded(text: str, target_label: str) -> bool:
    target = _normal(target_label)
    if not target:
        return True
    words = [word for word in target.split() if len(word) > 2]
    spoken = set(_normal(text).split())
    return bool(words) and all(word in spoken for word in words[:8])


def _live_turn_allows(capability: str, *, target_label: str = "") -> tuple[bool, str]:
    origin = current_turn()
    text = str(origin.get("text") or "").strip()
    signal = _WRITE_SIGNALS.get(capability)
    if not text:
        return False, "there is no originating local turn"
    if signal is None or signal.search(text) is None:
        return False, "Raghav's live turn did not directly request this action"
    if target_label and not _target_is_grounded(text, target_label):
        return False, "the requested target is not grounded in Raghav's live turn"
    return True, "the live turn directly requested this exact action"


def _turn_binding() -> tuple[Mapping[str, object], str, str, str]:
    origin = current_turn()
    return (
        origin,
        _surface(origin),
        str(origin.get("call_id") or origin.get("session_id") or ""),
        str(origin.get("turn_id") or ""),
    )


@contextmanager
def _authorized_write(
    capability: str,
    *,
    target: str,
    target_label: str,
    intent: str,
    authority: ActionAuthority | None = None,
):
    allowed, reason = _live_turn_allows(capability, target_label=target_label)
    if not allowed:
        yield None, reason
        return
    gate = authority or default_authority()
    origin, source, session_id, turn_id = _turn_binding()
    proof = gate.issue_turn_proof(
        source=source,
        covers=[(capability, target)],
        session_id=session_id,
        turn_id=turn_id,
        identity="raghav",
        issued_by="resident-brain-gideon-api",
    )
    request = build_request(
        capability=capability,
        intent=intent,
        source=source,
        effect="reversible",
        identity="raghav",
        session_id=session_id,
        turn_id=turn_id,
        target=target,
        authorization_basis=BASIS_ORIGIN_TURN,
        origin_proof=proof.proof_id,
        context={"origin_text_sha256_bound_by_proof": True, "protocol": origin.get("protocol")},
    )
    with gate.guard(request) as decision:
        yield decision if decision.allowed else None, decision.reason


@tool(
    "gideon_status",
    "Read the live local status of Serena's continuity mode, action lock, commitments, "
    "state graph, device adapters/scenes, supportive mode, runtime, and visual adapter. "
    "Use this instead of guessing whether a Gideon capability is wired.",
    {"detail": str},
    annotations=_READ_ONLY,
)
async def gideon_status(_args):
    value = await asyncio.to_thread(_api().status)
    return _text(value)


@tool(
    "gideon_briefing",
    "Build a deterministic morning or evening briefing from Serena's local commitment "
    "store. This works without a cloud model and never marks a briefing delivered.",
    {"kind": str},
    annotations=_READ_ONLY,
)
async def gideon_briefing(args):
    try:
        value = await asyncio.to_thread(_api().briefing, str(args.get("kind") or "morning"))
    except (RuntimeError, ValueError) as exc:
        return _text({"ok": False, "error": str(exc)})
    return _text({"ok": True, **value})


@tool(
    "gideon_commitments",
    "List or change Serena's durable commitments. action=list is read-only. For create, "
    "complete, snooze, dismiss, or correct, payload_json contains the exact fields and "
    "Raghav's current live turn must directly request the same action and target. Never "
    "claim a refused mutation happened.",
    {"action": str, "commitment_id": str, "payload_json": str},
    annotations=_LOCAL_WRITE,
)
async def gideon_commitments(args):
    action = str(args.get("action") or "list").strip().lower()
    try:
        values = _payload(args.get("payload_json"))
        if action == "list":
            items = await asyncio.to_thread(
                _api().list_commitments,
                state=str(values.get("state") or ""),
                open_only=bool(values.get("open_only", True)),
                limit=int(values.get("limit") or 50),
            )
            return _text({"ok": True, "commitments": items})
        if action == "create":
            title = str(values.get("title") or "").strip()
            with _authorized_write(
                "commitment.create",
                target=title,
                target_label=title,
                intent=f"create commitment {title}",
                authority=_api().device_runner.authority,
            ) as (decision, reason):
                if decision is None:
                    return _text({"ok": False, "changed": False, "error": reason})
                item = await asyncio.to_thread(_api().create_commitment, values)
            return _text({"ok": True, "changed": True, "commitment": item})
        commitment_id = str(args.get("commitment_id") or "").strip()
        current = _api().commitment_store.require(commitment_id)
        capability = f"commitment.{action}"
        with _authorized_write(
            capability,
            target=commitment_id,
            target_label=current.title,
            intent=f"{action} commitment {current.title}",
            authority=_api().device_runner.authority,
        ) as (decision, reason):
            if decision is None:
                return _text({"ok": False, "changed": False, "error": reason})
            changed = await asyncio.to_thread(
                _api().change_commitment,
                commitment_id,
                action,
                values,
            )
        return _text({"ok": True, "changed": True, **changed})
    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
        return _text({"ok": False, "changed": False, "error": str(exc)})


@tool(
    "gideon_state",
    "Read Serena's local personal state graph. Optionally filter by entity kind. "
    "Stale entities are excluded unless include_stale is true.",
    {"kind": str, "include_stale": bool, "refresh": bool},
    annotations=_READ_ONLY,
)
async def gideon_state(args):
    refreshed = None
    if bool(args.get("refresh", False)):
        refreshed = await asyncio.to_thread(_api().refresh_system_state)
    value = await asyncio.to_thread(
        _api().state_snapshot,
        kind=str(args.get("kind") or ""),
        include_stale=bool(args.get("include_stale", False)),
    )
    if refreshed is not None:
        value["refreshed"] = refreshed
    return _text(value)


@tool(
    "gideon_world",
    "Read the cached world and household cockpit, including evidence cards, provider "
    "freshness, map data, and the bounded voice handoff. This does not perform network I/O.",
    {"limit": int},
    annotations=_READ_ONLY,
)
async def gideon_world(args):
    value = await asyncio.to_thread(_api().world_snapshot, limit=int(args.get("limit") or 40))
    return _text(value)


@tool(
    "gideon_device_scene",
    "List configured device scenes and adapter availability, preview one scene, or run it. "
    "run requires Raghav's live local turn to name that scene. dry_run defaults true and "
    "real execution still passes through per-step ActionAuthority checks and postconditions.",
    {"action": str, "name": str, "dry_run": bool},
    annotations=_LOCAL_WRITE,
)
async def gideon_device_scene(args):
    action = str(args.get("action") or "list").strip().lower()
    name = str(args.get("name") or "").strip()
    try:
        if action == "list":
            return _text({"ok": True, **_api().device_status()})
        if action == "preview":
            scene = _api().device_runner.scenes().get(name)
            if scene is None:
                raise ValueError(f"there is no scene called {name}")
            return _text({"ok": True, "scene": asdict(scene), "executed": False})
        if action != "run":
            raise ValueError("scene action must be list, preview, or run")
        allowed, reason = _live_turn_allows("device.scene", target_label=name)
        if not allowed:
            return _text({"ok": False, "executed": False, "error": reason})
        origin, source, session_id, turn_id = _turn_binding()
        authority = _api().device_runner.authority
        proof = authority.issue_turn_proof(
            source=source,
            covers=_api().device_runner.scene_actions(name),
            session_id=session_id,
            turn_id=turn_id,
            identity="raghav",
            issued_by="resident-brain-gideon-api",
        )
        report = await asyncio.to_thread(
            _api().run_scene,
            name,
            intent=str(origin.get("text") or f"run scene {name}"),
            source=source,
            session_id=session_id,
            turn_id=turn_id,
            origin_proof=proof.proof_id,
            dry_run=bool(args.get("dry_run", True)),
        )
        return _text({"ok": bool(report.get("ok")), "executed": True, "report": report})
    except (RuntimeError, TypeError, ValueError) as exc:
        return _text({"ok": False, "executed": False, "error": str(exc)})


@tool(
    "gideon_support",
    "Read or configure the opt-in supportive mode, or add a private reflection. status "
    "returns settings and aggregate patterns only, never raw reflection text. configure "
    "and reflect require an explicit matching live turn. This is supportive coaching, "
    "not diagnosis, treatment, or an external emergency service.",
    {"action": str, "payload_json": str},
    annotations=_LOCAL_WRITE,
)
async def gideon_support(args):
    action = str(args.get("action") or "status").strip().lower()
    try:
        values = _payload(args.get("payload_json"))
        if action == "status":
            return _text({"ok": True, **_api().support_status(include_reflections=False)})
        if action == "boundary":
            return _text({"ok": True, **_api().support_boundary(str(values.get("text") or ""))})
        capability = "support.configure" if action == "configure" else "support.reflect"
        if action not in {"configure", "reflect"}:
            raise ValueError("support action must be status, boundary, configure, or reflect")
        label = "supportive mode" if action == "configure" else str(values.get("body") or "")
        with _authorized_write(
            capability,
            target="supportive_mode" if action == "configure" else "private_reflection",
            target_label="" if action == "configure" else label,
            intent=f"{action} supportive mode",
            authority=_api().device_runner.authority,
        ) as (decision, reason):
            if decision is None:
                return _text({"ok": False, "changed": False, "error": reason})
            result = await asyncio.to_thread(
                _api().configure_support if action == "configure" else _api().add_reflection,
                values,
            )
        return _text({"ok": True, "changed": True, "result": result})
    except (RuntimeError, TypeError, ValueError) as exc:
        return _text({"ok": False, "changed": False, "error": str(exc)})


@tool(
    "gideon_visual_context",
    "Read visual-adapter availability or perform one explicitly requested, single-use "
    "screen capture. Capture is refused unless a production desktop adapter and its "
    "consent consumer are registered. Never retry a refused or expired capture silently.",
    {"action": str},
    annotations=_LOCAL_WRITE,
)
async def gideon_visual_context(args):
    action = str(args.get("action") or "status").strip().lower()
    if action == "status":
        return _text({"ok": True, **_api().visual_status()})
    if action != "capture":
        return _text({"ok": False, "captured": False, "error": "visual action must be status or capture"})
    allowed, reason = _live_turn_allows("screen.capture")
    if not allowed:
        return _text({"ok": False, "captured": False, "error": reason})
    if _api().visual is None:
        return _text({"ok": False, "captured": False, "error": _api().visual_status()["reason"]})
    origin, source, session_id, turn_id = _turn_binding()
    authority = _api().device_runner.authority
    proof = authority.issue_turn_proof(
        source=source,
        covers=[("screen.capture", "active_screen")],
        session_id=session_id,
        turn_id=turn_id,
        identity="raghav",
        issued_by="resident-brain-gideon-api",
    )
    now = time.time()
    consent = CaptureConsent(
        request_id=f"visual-{turn_id or proof.proof_id[:12]}",
        actor_id="raghav",
        source=source,
        session_id=session_id or "local-session",
        scopes=(CAPTURE_SCOPE,),
        granted_at=now,
        expires_at=now + 60,
        authority_receipt_id=proof.proof_id,
    )
    try:
        snapshot = await asyncio.to_thread(_api().capture_visual, consent)
    except (RuntimeError, ValueError) as exc:
        return _text({"ok": False, "captured": False, "error": str(exc)})
    payload = snapshot.provider_payload()
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)},
            {
                "type": "image",
                "data": base64.b64encode(snapshot.image.data).decode("ascii"),
                "mimeType": snapshot.image.media_type,
            },
        ]
    }


GIDEON_TOOLS = (
    gideon_status,
    gideon_briefing,
    gideon_commitments,
    gideon_state,
    gideon_world,
    gideon_device_scene,
    gideon_support,
    gideon_visual_context,
)
GIDEON_TOOL_NAMES = [f"mcp__serena-gideon__{item.name}" for item in GIDEON_TOOLS]


def gideon_tools_server():
    return create_sdk_mcp_server(name="serena-gideon", tools=list(GIDEON_TOOLS))


__all__ = ["GIDEON_TOOLS", "GIDEON_TOOL_NAMES", "gideon_tools_server"]
