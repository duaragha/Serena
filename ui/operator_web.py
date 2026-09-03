"""Loopback-only operator APIs for Chats inspection and prompt control."""

from __future__ import annotations

import ipaddress
from dataclasses import asdict
from typing import Any

from flask import Blueprint, jsonify, request

from core.operator_workspace import (
    OperatorWorkspaceStore,
    inspect_session,
    steering_capability,
)

operator_bp = Blueprint("operator", __name__)


def _store() -> OperatorWorkspaceStore:
    return OperatorWorkspaceStore()


@operator_bp.before_request
def _operator_is_local_only():
    try:
        peer = ipaddress.ip_address((request.remote_addr or "").split("%", 1)[0])
    except ValueError:
        return _error("operator workspace is available only from this computer", 403)
    if not peer.is_loopback:
        return _error("operator workspace is available only from this computer", 403)
    return None


@operator_bp.get("/api/operator/sessions/<session_id>/inspect")
def operator_inspect_session(session_id: str):
    try:
        return jsonify({"ok": True, "inspection": inspect_session(session_id)})
    except KeyError:
        return _error("session not found", 404)
    except ValueError as exc:
        return _error(str(exc), 400)


@operator_bp.get("/api/operator/prompts")
def operator_prompts():
    session_id = str(request.args.get("session_id") or "").strip()
    query = str(request.args.get("q") or "")
    raw_states = str(request.args.get("states") or "queued,paused,stashed,ambiguous")
    states = tuple(state.strip() for state in raw_states.split(",") if state.strip())
    try:
        prompts = _store().list_prompts(
            session_id=session_id,
            states=states,
            query=query,
            limit=_limit(request.args.get("limit"), 100),
        )
        return jsonify({"ok": True, "prompts": [item.to_dict() for item in prompts]})
    except ValueError as exc:
        return _error(str(exc), 400)


@operator_bp.post("/api/operator/prompts")
def operator_queue_prompt():
    payload = request.get_json(silent=True) or {}
    try:
        prompt = _store().queue_prompt(
            session_id=payload.get("session_id"),
            provider=payload.get("provider"),
            text=payload.get("text"),
            mode=payload.get("mode") or "next_turn",
        )
        return jsonify({"ok": True, "prompt": prompt.to_dict()})
    except ValueError as exc:
        return _error(str(exc), 400)


@operator_bp.put("/api/operator/prompts/<prompt_id>")
def operator_edit_prompt(prompt_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        prompt = _store().edit_prompt(prompt_id, payload.get("text"))
        return jsonify({"ok": True, "prompt": prompt.to_dict()})
    except KeyError:
        return _error("queued prompt not found", 404)
    except ValueError as exc:
        return _error(str(exc), 400)
    except RuntimeError as exc:
        return _error(str(exc), 409)


@operator_bp.post("/api/operator/prompts/<prompt_id>/<action>")
def operator_prompt_action(prompt_id: str, action: str):
    try:
        prompt = _store().transition(prompt_id, action)
        return jsonify({"ok": True, "prompt": prompt.to_dict()})
    except KeyError:
        return _error("queued prompt not found", 404)
    except ValueError as exc:
        return _error(str(exc), 400)
    except RuntimeError as exc:
        return _error(str(exc), 409)


@operator_bp.post("/api/operator/prompts/<prompt_id>/dispatch")
def operator_dispatch_prompt(prompt_id: str):
    """Dispatch exactly once to the renderer-owned native runtime."""

    from ui import pty_terminal

    workspace = _store()
    try:
        prompt = workspace.get(prompt_id)
    except ValueError as exc:
        return _error(str(exc), 400)
    if prompt is None:
        return _error("queued prompt not found", 404)
    terminal_id = pty_terminal.tid_for_session(prompt.session_id)
    if not terminal_id:
        return _error("native runtime is not open", 409)
    runtime = next(
        (
            entry
            for entry in pty_terminal.runtime_context_snapshot().get("runtimes", [])
            if entry.get("terminal_id") == terminal_id
        ),
        {},
    )
    capability = steering_capability(runtime, prompt.mode)
    if not capability.get("supported"):
        return jsonify({"ok": False, "error": capability["reason"], "capability": capability}), 409
    try:
        workspace.begin_dispatch(prompt.prompt_id, terminal_id)
    except (KeyError, ValueError) as exc:
        return _error(str(exc), 400)
    except RuntimeError as exc:
        return _error(str(exc), 409)
    delivered = False
    try:
        capability = pty_terminal.write_operator_prompt(
            terminal_id,
            (prompt.text + "\r").encode("utf-8"),
            prompt.mode,
        )
        delivered = bool(capability.get("delivered"))
        finished = workspace.finish_dispatch(prompt.prompt_id, delivered=delivered)
    except Exception as exc:
        # Once a PTY write may have happened, replay is unsafe. Leave an honest
        # ambiguous journal entry and require an operator decision.
        try:
            finished = workspace.finish_dispatch(
                prompt.prompt_id,
                delivered=False,
                ambiguous=delivered,
            )
        except Exception:
            return _error(f"prompt dispatch state is uncertain: {exc}", 500)
        return jsonify(
            {
                "ok": False,
                "error": "prompt dispatch state is uncertain" if delivered else str(exc),
                "prompt": finished.to_dict(),
            }
        ), 500
    if not delivered:
        return jsonify(
            {
                "ok": False,
                "error": capability.get("reason") or "native runtime refused the prompt",
                "prompt": finished.to_dict(),
                "capability": capability,
            }
        ), 409
    return jsonify(
        {"ok": True, "prompt": finished.to_dict(), "capability": capability}
    )


@operator_bp.get("/api/operator/artifacts")
def operator_artifacts():
    from core.artifacts import get_default_artifact_registry

    try:
        links = get_default_artifact_registry().search(
            str(request.args.get("q") or ""),
            origin_session_id=str(request.args.get("session_id") or ""),
            fleet_run_id=str(request.args.get("fleet_run_id") or ""),
            limit=_limit(request.args.get("limit"), 100),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _error(str(exc), 400)
    artifacts: list[dict[str, Any]] = []
    for link in links:
        entry = asdict(link)
        entry.pop("path", None)
        entry["url"] = link.url
        artifacts.append(entry)
    return jsonify({"ok": True, "artifacts": artifacts})


def _limit(value: object, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        return min(200, max(1, int(str(value))))
    except ValueError as error:
        raise ValueError("limit must be an integer") from error


def _error(message: str, status: int):
    return jsonify({"ok": False, "error": str(message or "operator request failed")}), status
