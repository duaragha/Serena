"""Isolated Fleet dashboard and JSON API for Serena's web shell."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request

fleet_bp = Blueprint("fleet", __name__)

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _supervisor():
    """Import lazily so the main Serena UI can still boot during recovery."""

    from core import fleet_supervisor

    return fleet_supervisor


@fleet_bp.before_request
def _fleet_is_local_only():
    """Fleet can launch privileged local workers, so never expose it on LAN binds."""

    try:
        peer = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return _error("Fleet is available only from this computer", 403)
    if not peer.is_loopback:
        return _error("Fleet is available only from this computer", 403)
    return None


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(to_dict())
    return value


def _valid_run_id(run_id: str) -> bool:
    return bool(_RUN_ID_RE.fullmatch(run_id or ""))


def _error(message: str, status: int):
    return jsonify({"ok": False, "error": message}), status


def _call(action, *args, **kwargs):
    try:
        return jsonify({"ok": True, "run": _jsonable(action(*args, **kwargs))})
    except KeyError:
        return _error("run not found", 404)
    except ValueError as exc:
        return _error(str(exc) or "invalid Fleet request", 400)
    except RuntimeError as exc:
        return _error(str(exc) or "Fleet action rejected", 409)


def _action(name: str):
    try:
        return getattr(_supervisor(), name), None
    except (ImportError, AttributeError) as exc:
        return None, _error(f"Fleet supervisor unavailable: {exc}", 503)


@fleet_bp.get("/fleet/view")
def fleet_view():
    return Response(FLEET_HTML, mimetype="text/html")


@fleet_bp.get("/api/fleet/runs")
def fleet_runs():
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        return _error("limit must be an integer", 400)
    limit = max(1, min(limit, 100))
    action, unavailable = _action("list_runs")
    if unavailable:
        return unavailable
    try:
        runs = action(limit=limit)
    except RuntimeError as exc:
        return _error(str(exc) or "Fleet unavailable", 503)
    return jsonify({"ok": True, "runs": _jsonable(runs)})


@fleet_bp.post("/api/fleet/runs")
def fleet_start():
    data = request.get_json(silent=True) or {}
    task = str(data.get("task") or "").strip()
    if not task:
        return _error("task is required", 400)
    action, unavailable = _action("start_run")
    if unavailable:
        return unavailable
    return _call(
        action,
        task,
        activity=str(data.get("activity") or "auto").strip() or "auto",
        provider_mode=str(data.get("provider_mode") or "auto").strip() or "auto",
        worker_count=data.get("worker_count"),
        cwd=(str(data.get("cwd") or "").strip() or None),
        origin_session_id=(str(data.get("origin_session_id") or "").strip() or None),
        origin_agent=(str(data.get("origin_agent") or "").strip() or None),
        dry_run=bool(data.get("dry_run", False)),
    )


@fleet_bp.get("/api/fleet/runs/<run_id>")
def fleet_run(run_id: str):
    if not _valid_run_id(run_id):
        return _error("invalid run id", 400)
    action, unavailable = _action("get_run")
    if unavailable:
        return unavailable
    try:
        run = action(run_id)
    except RuntimeError as exc:
        return _error(str(exc) or "Fleet unavailable", 503)
    if run is None:
        return _error("run not found", 404)
    return jsonify({"ok": True, "run": _jsonable(run)})


@fleet_bp.get("/api/fleet/runs/<run_id>/inspect")
def fleet_inspect(run_id: str):
    if not _valid_run_id(run_id):
        return _error("invalid run id", 400)
    focus = str(request.args.get("focus") or "").strip()
    if len(focus) > 128:
        return _error("focus is too long", 400)
    try:
        event_limit = max(1, min(100, int(request.args.get("events", "50"))))
    except ValueError:
        return _error("events must be an integer", 400)
    action, unavailable = _action("inspect_run")
    if unavailable:
        return unavailable
    try:
        inspection = action(run_id, focus, event_limit=event_limit)
    except KeyError:
        return _error("run or focus not found", 404)
    except (ValueError, RuntimeError) as exc:
        return _error(str(exc) or "Fleet inspection rejected", 409)
    return jsonify({"ok": True, "inspection": _jsonable(inspection)})


@fleet_bp.post("/api/fleet/runs/<run_id>/stop")
def fleet_stop(run_id: str):
    if not _valid_run_id(run_id):
        return _error("invalid run id", 400)
    action, unavailable = _action("stop_run")
    if unavailable:
        return unavailable
    return _call(action, run_id)


@fleet_bp.delete("/api/fleet/runs/<run_id>")
def fleet_delete(run_id: str):
    if not _valid_run_id(run_id):
        return _error("invalid run id", 400)
    action, unavailable = _action("delete_run")
    if unavailable:
        return unavailable
    return _call(action, run_id)


@fleet_bp.delete("/api/fleet/runs")
def fleet_delete_many():
    data = request.get_json(silent=True)
    raw_run_ids = data.get("run_ids") if isinstance(data, dict) else None
    if not isinstance(raw_run_ids, list) or not raw_run_ids:
        return _error("run_ids must be a non-empty list", 400)
    if len(raw_run_ids) > 100:
        return _error("at most 100 Fleet runs can be deleted at once", 400)

    run_ids: list[str] = []
    for raw_run_id in raw_run_ids:
        run_id = str(raw_run_id or "").strip()
        if not _valid_run_id(run_id):
            return _error("invalid run id", 400)
        if run_id not in run_ids:
            run_ids.append(run_id)

    try:
        supervisor = _supervisor()
        get_run = supervisor.get_run
        delete_run = supervisor.delete_run
        preflight_delete_run = getattr(supervisor, "preflight_delete_run", None)
    except (ImportError, AttributeError) as exc:
        return _error(f"Fleet supervisor unavailable: {exc}", 503)

    terminal_states = set(
        getattr(supervisor, "TERMINAL_RUN_STATES", {"completed", "failed", "cancelled", "planned"})
    )
    for run_id in run_ids:
        try:
            run = get_run(run_id)
        except RuntimeError as exc:
            return _error(str(exc) or "Fleet unavailable", 503)
        if run is None:
            return _error(f"run not found: {run_id}", 404)
        if str(run.get("state") or "") not in terminal_states:
            return _error(
                f"stop Fleet {run_id} and wait for it to finish before deleting it",
                409,
            )
        if callable(preflight_delete_run):
            try:
                preflight_delete_run(run_id)
            except RuntimeError as exc:
                return _error(str(exc) or "Fleet deletion rejected", 409)

    deleted = []
    try:
        for run_id in run_ids:
            deleted.append(_jsonable(delete_run(run_id)))
    except KeyError:
        return _error("run not found", 404)
    except ValueError as exc:
        return _error(str(exc) or "invalid Fleet request", 400)
    except RuntimeError as exc:
        return _error(str(exc) or "Fleet action rejected", 409)
    return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted)})


@fleet_bp.post("/api/fleet/runs/<run_id>/retry")
def fleet_retry(run_id: str):
    if not _valid_run_id(run_id):
        return _error("invalid run id", 400)
    action, unavailable = _action("retry_run")
    if unavailable:
        return unavailable
    return _call(action, run_id)


@fleet_bp.post("/api/fleet/runs/<run_id>/legs/<leg_id>/retry")
def fleet_retry_leg(run_id: str, leg_id: str):
    if not _valid_run_id(run_id) or not _valid_run_id(leg_id):
        return _error("invalid run or worker id", 400)
    action, unavailable = _action("retry_leg")
    if unavailable:
        return unavailable
    return _call(action, run_id, leg_id)


@fleet_bp.post("/api/fleet/runs/<run_id>/legs/<leg_id>/handoff")
def fleet_handoff_leg(run_id: str, leg_id: str):
    if not _valid_run_id(run_id) or not _valid_run_id(leg_id):
        return _error("invalid run or worker id", 400)
    data = request.get_json(silent=True) or {}
    provider = str(data.get("provider") or "").strip().lower()
    if provider not in {"codex", "claude"}:
        return _error("provider must be codex or claude", 400)
    action, unavailable = _action("handoff_leg")
    if unavailable:
        return unavailable
    return _call(action, run_id, leg_id, provider)


FLEET_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fleet</title>
<style>
:root {
  color-scheme: dark;
  --bg: #0d0a0c;
  --surface: #151014;
  --surface2: #1c161b;
  --border: #2a2430;
  --border-bright: #393140;
  --text: #c9d1d9;
  --dim: #777079;
  --bright: #e6edf3;
  --accent: #e07ba8;
  --accent-dim: rgba(224,123,168,.12);
  --green: #3fb950;
  --green-dim: rgba(63,185,80,.12);
  --amber: #d29922;
  --red: #f85149;
  --codex: #b07cff;
  --claude: #c15f3c;
  --mono: 'JetBrains Mono', ui-monospace, 'Cascadia Code', monospace;
}
* { box-sizing: border-box; }
html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; }
body { background: var(--bg); color: var(--text); font: 12px/1.45 var(--mono); }
button { font: inherit; }
.shell { display: grid; grid-template-columns: 300px minmax(0,1fr); height: 100%; }
.rail { display: flex; flex-direction: column; min-width: 0; overflow: hidden;
  background: var(--surface); border-right: 1px solid var(--border); }
.rail-head, .detail-head { display: flex; align-items: center; gap: 8px;
  min-height: 44px; padding: 8px 12px; border-bottom: 1px solid var(--border); }
.rail-title { color: var(--bright); font-size: 12px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .8px; }
.rail-count { margin-left: auto; color: var(--dim); font-size: 10px; }
.refresh, .action, .open { border: 1px solid var(--border-bright); border-radius: 4px;
  background: transparent; color: var(--dim); cursor: pointer; padding: 4px 8px; }
.refresh:hover, .action:hover, .open:hover { color: var(--bright); border-color: var(--accent); }
.refresh:disabled, .action:disabled, .open:disabled { opacity: .45; cursor: default; }
.bulk-bar { display: flex; align-items: center; gap: 8px; min-height: 38px;
  padding: 7px 12px; border-bottom: 1px solid var(--border); background: var(--surface2); }
.bulk-toggle { display: inline-flex; align-items: center; gap: 7px; color: var(--dim);
  cursor: pointer; user-select: none; }
.bulk-toggle:has(input:disabled) { cursor: default; opacity: .45; }
.bulk-count { margin-left: auto; color: var(--dim); font-size: 10px; }
.bulk-delete { color: #ff7a90; border-color: rgba(255,122,144,.45); min-width: 70px; }
.bulk-delete:not(:disabled):hover { color: #ff9aab; border-color: #ff7a90; }
.run-check, .bulk-toggle input { width: 14px; height: 14px; margin: 0;
  accent-color: var(--accent); cursor: pointer; }
.run-check { margin-top: 1px; }
.run-check:disabled { cursor: default; opacity: .22; }
.run-list { flex: 1; overflow: auto; }
.run-row { display: grid; grid-template-columns: 14px 10px minmax(0,1fr) auto; gap: 8px;
  align-items: start; min-height: 58px; padding: 9px 12px; cursor: pointer;
  border-left: 3px solid transparent; border-bottom: 1px solid var(--border); }
.run-row:hover { background: rgba(255,255,255,.035); }
.run-row.selected { border-left-color: var(--accent); background: var(--accent-dim); }
.run-row.bulk-selected:not(.selected) { background: rgba(255,122,144,.065); }
.dot { width: 7px; height: 7px; margin-top: 5px; border-radius: 50%;
  background: var(--dim); }
.dot.running, .dot.queued, .dot.pending { background: var(--green);
  box-shadow: 0 0 7px rgba(63,185,80,.72); animation: pulse 1.6s ease-in-out infinite; }
.dot.waiting_for_capacity { background: var(--amber);
  box-shadow: 0 0 7px rgba(210,153,34,.55); animation: pulse 2.4s ease-in-out infinite; }
.dot.complete, .dot.completed, .dot.succeeded, .dot.done { background: var(--green); }
.dot.failed, .dot.error { background: var(--red); }
.dot.stopped, .dot.cancelled, .dot.canceled { background: var(--dim); }
@keyframes pulse { 50% { opacity: .4; } }
.run-copy { min-width: 0; }
.run-task { color: var(--bright); overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.run-meta { margin-top: 3px; color: var(--dim); font-size: 10px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.run-age { color: var(--dim); font-size: 10px; white-space: nowrap; }
.detail { min-width: 0; display: flex; flex-direction: column; overflow: hidden; }
.detail-head { min-height: 56px; }
.detail-heading { min-width: 0; flex: 1; }
.detail-task { color: var(--bright); font-size: 14px; font-weight: 650;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.detail-meta, .detail-routing { color: var(--dim); font-size: 10px; margin-top: 3px; }
.detail-routing { color: var(--accent); overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.detail-actions { display: flex; align-items: center; gap: 6px; }
.action.stop { color: var(--red); border-color: rgba(248,81,73,.45); }
.action.retry { color: var(--amber); border-color: rgba(210,153,34,.45); }
.action.delete { color: #ff7a90; border-color: rgba(255,122,144,.45); }
.action.handoff { color: var(--accent); border-color: rgba(224,123,168,.5); }
.status { display: inline-flex; align-items: center; border: 1px solid var(--border);
  border-radius: 999px; padding: 2px 7px; color: var(--dim); font-size: 9px;
  text-transform: uppercase; letter-spacing: .45px; }
.status.running, .status.queued, .status.pending { color: var(--green);
  border-color: rgba(63,185,80,.35); background: var(--green-dim); }
.status.waiting_for_capacity { color: var(--amber);
  border-color: rgba(210,153,34,.4); background: rgba(210,153,34,.1); }
.status.waiting_for_dependencies { color: var(--amber);
  border-color: rgba(210,153,34,.4); background: rgba(210,153,34,.1); }
.status.blocked_dependency_failed { color: var(--red);
  border-color: rgba(248,81,73,.35); background: rgba(248,81,73,.08); }
.status.retry-queued { color: var(--amber); border-color: rgba(210,153,34,.4); }
.status.handoff-queued { color: var(--accent); border-color: rgba(224,123,168,.45); }
.status.failed, .status.error { color: var(--red); border-color: rgba(248,81,73,.35); }
.status.complete, .status.completed, .status.succeeded, .status.done { color: var(--green); }
.work-plan { margin: 12px 12px 0; border: 1px solid var(--border); border-radius: 7px;
  background: var(--surface); }
.work-plan > summary { cursor: pointer; padding: 8px 10px; color: var(--bright);
  background: var(--surface2); user-select: none; }
.work-unit { display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 5px 12px;
  padding: 8px 10px; border-top: 1px solid var(--border); }
.work-unit-title { color: var(--bright); overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.work-unit-meta, .work-unit-scope { color: var(--dim); font-size: 10px; }
.work-unit-scope { grid-column: 1 / -1; color: var(--accent); overflow-wrap: anywhere; }
.work-unit-executions { grid-column: 1 / -1; color: var(--dim); font-size: 9px;
  overflow-wrap: anywhere; }
.work-unit.focused { border-left: 2px solid var(--accent); background: var(--accent-dim); }
.isolation-row { padding: 7px 10px; border-top: 1px solid var(--border);
  color: var(--dim); font-size: 10px; overflow-wrap: anywhere; }
.isolation-row strong { color: var(--bright); }
.phases { flex: 1; overflow: auto; padding: 12px; }
.phase { margin-bottom: 9px; border: 1px solid var(--border); border-radius: 7px;
  overflow: hidden; background: var(--surface); }
.phase-head { display: grid; grid-template-columns: 26px minmax(0,1fr) auto auto;
  align-items: center; gap: 8px; padding: 8px 10px; background: var(--surface2);
  border-bottom: 1px solid var(--border); }
.phase-index { width: 21px; height: 21px; display: grid; place-items: center;
  border: 1px solid var(--border-bright); border-radius: 50%; color: var(--dim); font-size: 10px; }
.phase-name { color: var(--bright); font-weight: 650; text-transform: capitalize; }
.phase-count { color: var(--dim); font-size: 10px; }
.legs { display: flex; flex-direction: column; }
.leg { display: grid; grid-template-columns: 20px minmax(0,1fr) auto auto;
  gap: 9px; align-items: center; padding: 9px 10px; border-bottom: 1px solid var(--border); }
.leg:last-child { border-bottom: 0; }
.agent-mark { width: 18px; height: 18px; display: grid; place-items: center;
  border-radius: 5px; font-size: 9px; font-weight: 800; border: 1px solid currentColor; }
.agent-mark.codex { color: var(--codex); background: rgba(176,124,255,.08); }
.agent-mark.claude { color: var(--claude); background: rgba(193,95,60,.08); }
.leg-copy { min-width: 0; }
.leg-worker { color: var(--bright); font-weight: 650; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.leg-assignment, .leg-role { margin-top: 2px; color: var(--dim); font-size: 10px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.leg-assignment { color: var(--accent); }
.leg-identity { margin-top: 2px; color: var(--dim); font-size: 10px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.leg-identity .model.codex { color: var(--codex); }
.leg-identity .model.claude { color: #e58c6f; }
.leg-stats { color: var(--dim); font-size: 10px; white-space: nowrap; text-align: right; }
.leg-actions { display: flex; align-items: center; gap: 6px; }
.leg-error { grid-column: 2 / -1; color: var(--red); font-size: 10px;
  white-space: pre-wrap; overflow-wrap: anywhere; }
.leg-wait { grid-column: 2 / -1; color: var(--amber); font-size: 10px;
  white-space: pre-wrap; overflow-wrap: anywhere; }
.leg-context { grid-column: 2 / -1; color: var(--dim); font-size: 9px;
  white-space: pre-wrap; overflow-wrap: anywhere; }
.empty, .error-banner { padding: 36px 20px; text-align: center; color: var(--dim); }
.error-banner { padding: 8px 12px; text-align: left; color: var(--red);
  border-bottom: 1px solid rgba(248,81,73,.3); background: rgba(248,81,73,.08); }
.capacity-banner { padding: 8px 12px; color: var(--amber);
  border-bottom: 1px solid rgba(210,153,34,.3); background: rgba(210,153,34,.08); }
.hidden { display: none !important; }
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-thumb { background: #333; border-radius: 3px; }
@media (max-width: 760px) {
  .shell { grid-template-columns: 1fr; grid-template-rows: minmax(150px, 34%) minmax(0,1fr); }
  .rail { border-right: 0; border-bottom: 1px solid var(--border); }
}
</style>
</head>
<body>
<div class="shell">
  <aside class="rail">
    <div class="rail-head">
      <div class="rail-title">Fleet runs</div>
      <div class="rail-count" id="runCount">0</div>
      <button class="refresh" id="refreshBtn" type="button">refresh</button>
    </div>
    <div class="bulk-bar">
      <label class="bulk-toggle">
        <input id="selectAllRuns" type="checkbox" aria-label="Select all finished Fleet runs">
        <span>select all</span>
      </label>
      <span class="bulk-count" id="bulkCount">0 selected</span>
      <button class="action bulk-delete" id="bulkDeleteBtn" type="button" disabled>delete</button>
    </div>
    <div class="error-banner hidden" id="errorBanner"></div>
    <div class="run-list" id="runList"></div>
  </aside>
  <main class="detail" id="detail">
    <div class="empty">select a Fleet run</div>
  </main>
</div>
<script>
const ACTIVE_STATES = new Set(['created','pending','queued','running','stopping','waiting_for_capacity']);
const DELETABLE_STATES = new Set(['completed','failed','cancelled','planned']);
const RETRY_STATES = new Set(['failed','error','stopped','cancelled','canceled']);
const LEG_RETRY_RUN_STATES = new Set(['queued','running','failed','waiting_for_capacity']);
const LEG_HANDOFF_RUN_STATES = new Set(['queued','running','failed','waiting_for_capacity']);
const state = { runs: [], selectedId: null, detail: null, visible: false,
  timer: null, loading: false, detailSeq: 0, pendingLegRetries: new Set(),
  pendingHandoffs: new Set(), selectedForDelete: new Set(), deletingMany: false,
  focus: null };

function text(value) { return value == null ? '' : String(value); }
function norm(value) { return text(value || 'unknown').toLowerCase().replace(/[^a-z0-9_-]/g, '-'); }
function el(tag, cls, value) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (value != null) node.textContent = text(value);
  return node;
}
function button(label, cls, handler) {
  const node = el('button', cls, label);
  node.type = 'button';
  node.addEventListener('click', handler);
  return node;
}
function runId(run) { return text(run && (run.run_id || run.id)); }
function runState(run) { return norm(run && (run.state || run.status)); }
function runTask(run) { return text(run && (run.task || run.title || run.name || run.summary)) || 'untitled run'; }
function timestamp(run, key) {
  const times = (run && run.timestamps) || {};
  return run && (run[key] || times[key] || times[key.replace('_at', '')]);
}
function parseTime(value) {
  if (value == null || value === '') return null;
  if (typeof value === 'number') return new Date(value < 1e12 ? value * 1000 : value);
  const parsed = new Date(value);
  return isNaN(parsed.getTime()) ? null : parsed;
}
function ago(value) {
  const when = parseTime(value);
  if (!when) return '';
  const seconds = Math.max(0, Math.floor((Date.now() - when.getTime()) / 1000));
  if (seconds < 60) return seconds + 's';
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
  if (seconds < 86400) return Math.floor(seconds / 3600) + 'h';
  return Math.floor(seconds / 86400) + 'd';
}
function durationMs(item) {
  if (!item) return 0;
  const direct = Number(item.duration_ms || item.durationMs || 0);
  if (direct > 0) return direct;
  const times = item.timestamps || {};
  const start = parseTime(item.started_at || item.start_time || times.started_at || times.started);
  const end = parseTime(item.completed_at || item.ended_at || item.end_time ||
    times.completed_at || times.completed || times.ended_at || times.ended) || new Date();
  return start ? Math.max(0, end.getTime() - start.getTime()) : 0;
}
function duration(value) {
  const ms = typeof value === 'number' ? value : durationMs(value);
  if (!ms) return '';
  const seconds = Math.max(1, Math.floor(ms / 1000));
  if (seconds < 60) return seconds + 's';
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm ' + (seconds % 60) + 's';
  return Math.floor(seconds / 3600) + 'h ' + Math.floor((seconds % 3600) / 60) + 'm';
}
function capacityTime(value) {
  const when = parseTime(value);
  if (!when) return '';
  const seconds = Math.max(0, Math.ceil((when.getTime() - Date.now()) / 1000));
  if (!seconds) return 'capacity check due now';
  if (seconds < 60) return 'capacity check in ' + seconds + 's';
  if (seconds < 3600) return 'capacity check in ' + Math.ceil(seconds / 60) + 'm';
  return 'capacity check in ' + Math.ceil(seconds / 3600) + 'h';
}
function providerFor(leg, attempt) {
  const runtime = text((leg && (leg.runtime || leg.provider || leg.backend)) || '').toLowerCase();
  const model = text((attempt && attempt.actual_model) || (leg && leg.model)).toLowerCase();
  return runtime.includes('codex') || model.startsWith('gpt-') || model.includes('sol') || model.includes('terra')
    ? 'codex' : 'claude';
}
function statusPill(status) { return el('span', 'status ' + norm(status), text(status || 'unknown')); }
function progressText(run) {
  const progress = (run && run.progress) || {};
  if (progress.total != null) return text(progress.completed || 0) + '/' + text(progress.total) + ' agent steps';
  return '';
}
function topologyText(run) {
  const agents = Number((run && run.agent_count) || 0);
  const chats = Number((run && run.chat_count) || 0);
  const parts = [];
  if (agents) parts.push(agents + (agents === 1 ? ' agent' : ' agents'));
  if (chats) parts.push(chats + (chats === 1 ? ' chat' : ' chats'));
  return parts.join(' · ');
}
function routingInfo(run) {
  const policy = (run && run.policy) || {};
  const selection = policy.provider_selection || policy.routing || {};
  const scaling = policy.scaling || {};
  const requested = text(policy.requested_provider_mode || selection.requested_mode ||
    selection.provider_mode || scaling.provider_mode || 'auto').toLowerCase();
  const selected = text(policy.provider_mode || selection.selected_mode ||
    selection.selected_provider_mode || policy.selected_provider_mode || requested).toLowerCase();
  const phase = ((run && run.phases) || [])[0] || {};
  const policyPhase = (policy.phases || [])[0] || {};
  const workers = (phase.legs && phase.legs.length) ? phase.legs : (policyPhase.workers || []);
  const counts = { codex: 0, claude: 0 };
  for (const worker of workers) {
    const provider = providerFor(worker, (worker && worker.current_attempt) || {});
    if (Object.prototype.hasOwnProperty.call(counts, provider)) counts[provider] += 1;
  }
  let roster = '';
  if (counts.codex && counts.claude) {
    roster = counts.codex + ' codex + ' + counts.claude + ' claude';
  } else if (counts.codex) {
    roster = 'codex only · ' + counts.codex + (counts.codex === 1 ? ' agent' : ' agents');
  } else if (counts.claude) {
    roster = 'claude only · ' + counts.claude + (counts.claude === 1 ? ' agent' : ' agents');
  }
  const mode = selected && selected !== 'auto' ? selected : requested;
  const route = requested === 'auto' && mode && mode !== 'auto'
    ? 'auto → ' + mode : mode;
  const reason = text(selection.reason || policy.provider_selection_reason ||
    scaling.provider_reason || scaling.reason || '');
  return {
    counts,
    label: [route, roster].filter(Boolean).join(' · '),
    reason,
  };
}

function postCount() {
  const active = state.runs.filter(run => ACTIVE_STATES.has(runState(run))).length;
  parent.postMessage({ type: 'serena-fleet-count', active_count: active }, window.location.origin);
}

function showError(message) {
  const banner = document.getElementById('errorBanner');
  banner.textContent = text(message);
  banner.classList.toggle('hidden', !message);
}

function scrollPosition(node) {
  return node ? { top: node.scrollTop, left: node.scrollLeft } : { top: 0, left: 0 };
}

function restoreScrollPosition(node, position) {
  if (!node || !position) return;
  node.scrollTop = position.top;
  node.scrollLeft = position.left;
}

async function api(path, options) {
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.ok === false) throw new Error(payload.error || ('HTTP ' + response.status));
  return payload;
}

function isDeletable(run) { return DELETABLE_STATES.has(runState(run)); }

function renderBulkControls() {
  const available = state.runs.filter(isDeletable);
  const availableIds = new Set(available.map(runId));
  for (const id of state.selectedForDelete) {
    if (!availableIds.has(id)) state.selectedForDelete.delete(id);
  }
  const selectedCount = state.selectedForDelete.size;
  const selectAll = document.getElementById('selectAllRuns');
  selectAll.checked = available.length > 0 && selectedCount === available.length;
  selectAll.indeterminate = selectedCount > 0 && selectedCount < available.length;
  selectAll.disabled = state.deletingMany || available.length === 0;
  document.getElementById('bulkCount').textContent = selectedCount + ' selected';
  const deleteButton = document.getElementById('bulkDeleteBtn');
  deleteButton.textContent = state.deletingMany ? 'deleting…' :
    (selectedCount ? 'delete ' + selectedCount : 'delete');
  deleteButton.disabled = state.deletingMany || selectedCount === 0;
}

function setRunDeleteSelection(id, selected) {
  if (selected) state.selectedForDelete.add(id);
  else state.selectedForDelete.delete(id);
  renderRuns();
}

function setAllRunDeleteSelections(selected) {
  state.selectedForDelete.clear();
  if (selected) {
    for (const run of state.runs) {
      if (isDeletable(run)) state.selectedForDelete.add(runId(run));
    }
  }
  renderRuns();
}

function renderRuns() {
  const list = document.getElementById('runList');
  const priorScroll = scrollPosition(list);
  list.replaceChildren();
  document.getElementById('runCount').textContent = text(state.runs.length);
  if (!state.runs.length) {
    renderBulkControls();
    list.append(el('div', 'empty', 'no Fleet runs yet. use /fleet or $fleet from a chat.'));
    return;
  }
  for (const run of state.runs) {
    const id = runId(run);
    const status = runState(run);
    const selectedForDelete = state.selectedForDelete.has(id);
    const row = el(
      'div',
      'run-row' + (id === state.selectedId ? ' selected' : '') +
        (selectedForDelete ? ' bulk-selected' : ''),
    );
    row.dataset.runId = id;
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'run-check';
    checkbox.checked = selectedForDelete;
    checkbox.disabled = state.deletingMany || !isDeletable(run);
    checkbox.setAttribute('aria-label', 'Select ' + runTask(run) + ' for deletion');
    checkbox.addEventListener('click', event => event.stopPropagation());
    checkbox.addEventListener('change', event => {
      event.stopPropagation();
      setRunDeleteSelection(id, checkbox.checked);
    });
    row.append(checkbox);
    row.append(el('span', 'dot ' + status));
    const copy = el('div', 'run-copy');
    copy.append(el('div', 'run-task', runTask(run)));
  const current = text(run.current_phase || '');
  const currentRow = ((run && run.phases) || []).find(item => text(item && item.name) === current);
  const phase = text(run.current_phase_display || (currentRow && currentRow.display_name) || current || 'waiting');
    const progress = progressText(run);
    copy.append(el('div', 'run-meta', [status, phase, progress].filter(Boolean).join(' · ')));
    row.append(copy);
    row.append(el('div', 'run-age', ago(timestamp(run, 'updated_at') || timestamp(run, 'created_at'))));
    row.addEventListener('click', () => selectRun(id));
    list.append(row);
  }
  renderBulkControls();
  restoreScrollPosition(list, priorScroll);
}

function attemptFor(leg) { return (leg && (leg.current_attempt || leg.attempt)) || {}; }
function legState(leg) {
  const attempt = attemptFor(leg);
  if (leg && leg.handoff_requested) return 'handoff-queued';
  if (leg && leg.retry_requested) return 'retry-queued';
  const stored = norm(leg && leg.state);
  const attempted = norm(attempt.state);
  if (stored === 'waiting_for_capacity') return stored;
  if (stored === 'queued' && ['failed','error','cancelled','interrupted'].includes(attempted)) {
    return 'queued';
  }
  return norm(attempt.state || leg.state || 'pending');
}
function legRetryPending(leg) {
  const attempt = attemptFor(leg);
  const stored = norm(leg && leg.state);
  const attempted = norm(attempt.state);
  return Boolean(leg && leg.retry_requested) ||
    (stored === 'queued' && ['failed','error','cancelled','interrupted'].includes(attempted));
}
function phaseState(phase) {
  const legs = (phase && phase.legs) || [];
  if (legs.some(leg => leg.handoff_requested)) return 'handoff-queued';
  if (legs.some(leg => leg.retry_requested)) return 'retry-queued';
  if (phase && phase.state) return norm(phase.state);
  const states = legs.map(legState);
  if (states.some(value => value === 'failed' || value === 'error')) return 'failed';
  if (states.some(value => ACTIVE_STATES.has(value))) return 'running';
  if (states.length && states.every(value => ['done','complete','completed','succeeded'].includes(value))) return 'done';
  return 'pending';
}

function openSession(sessionId) {
  if (!sessionId) return;
  parent.postMessage({ type: 'serena-fleet-open-session', session_id: text(sessionId) }, window.location.origin);
}

function displayModel(attempt, leg) {
  const actual = text(attempt && attempt.actual_model).trim();
  return actual && !(actual.startsWith('<') && actual.endsWith('>'))
    ? actual : text(leg && leg.model) || 'model pending';
}

function actualModelConfirmed(attempt) {
  const actual = text(attempt && attempt.actual_model).trim();
  return Boolean(actual && !(actual.startsWith('<') && actual.endsWith('>')));
}

function renderLeg(run, phase, leg) {
  const attempt = attemptFor(leg);
  const provider = providerFor(leg, attempt);
  const status = legState(leg);
  const model = displayModel(attempt, leg);
  const effort = text(attempt.actual_effort || leg.effort || '');
  const role = text(leg.role || leg.label || leg.leg_id || 'worker');
  const workerLabel = text(leg.worker_label || leg.worker_key || (provider === 'codex' ? 'Codex' : 'Claude'));
  const assignment = text(leg.assignment || '');
  const sessionId = text(attempt.session_id || leg.session_id || '');
  const row = el('div', 'leg');
  row.append(el('span', 'agent-mark ' + provider, provider === 'codex' ? 'X' : 'C'));
  const copy = el('div', 'leg-copy');
  copy.append(el('div', 'leg-worker', workerLabel));
  if (assignment) copy.append(el('div', 'leg-assignment', 'owns: ' + assignment));
  copy.append(el('div', 'leg-role', role));
  const identity = el('div', 'leg-identity');
  identity.append(el(
    'span',
    'model ' + provider,
    model + (actualModelConfirmed(attempt) ? '' : ' requested'),
  ));
  if (effort) identity.append(document.createTextNode(' · ' + effort));
  if (attempt.number || leg.attempt_count) {
    identity.append(document.createTextNode(' · attempt ' + text(attempt.number || leg.attempt_count)));
  }
  copy.append(identity);
  row.append(copy);
  const storedStatus = norm(leg && leg.state);
  const waitingForRetry = legRetryPending(leg);
  const waitingForHandoff = Boolean(leg.handoff_requested);
  const capacityWait = (leg && leg.capacity_wait) || null;
  const waitingForCapacity = Boolean(capacityWait || storedStatus === 'waiting_for_capacity');
  const waitingForControl = waitingForRetry || waitingForHandoff || waitingForCapacity;
  const stats = [status, waitingForControl ? '' : duration(attempt) || duration(leg)]
    .filter(Boolean).join(' · ');
  row.append(el('div', 'leg-stats', stats));
  const actions = el('div', 'leg-actions');
  const open = button('open chat', 'open', event => {
    event.stopPropagation();
    openSession(sessionId);
  });
  open.disabled = !sessionId;
  actions.append(open);
  const legFocus = text(leg.worker_key || leg.leg_id);
  actions.append(button(state.focus === legFocus ? 'unfocus' : 'focus', 'action', event => {
    event.stopPropagation();
    state.focus = state.focus === legFocus ? null : legFocus;
    renderDetail();
  }));
  const retryKey = runId(run) + ':' + text(leg.leg_id);
  const requestPending = state.pendingLegRetries.has(retryKey);
  const canRetry = LEG_RETRY_RUN_STATES.has(runState(run));
  if (leg.retry_requested || requestPending ||
      (canRetry && ['failed','error','waiting_for_capacity'].includes(storedStatus))) {
    const retry = button(
      requestPending ? 'queueing…' : leg.retry_requested ? 'retry queued' : 'retry agent',
      'action retry',
      event => {
        event.stopPropagation();
        retryLeg(runId(run), text(leg.leg_id));
      },
    );
    retry.disabled = Boolean(leg.retry_requested || requestPending);
    actions.append(retry);
  }
  const handoffKey = runId(run) + ':' + text(leg.leg_id);
  const handoffPending = state.pendingHandoffs.has(handoffKey);
  const targetProvider = provider === 'codex' ? 'claude' : 'codex';
  const currentPhase = text(run.current_phase || '');
  const isCurrentPhase = !currentPhase || text(phase && phase.name) === currentPhase;
  const canHandoff = LEG_HANDOFF_RUN_STATES.has(runState(run)) &&
    isCurrentPhase && !['completed','complete','done','cancelled','canceled'].includes(storedStatus);
  if (canHandoff || leg.handoff_requested || handoffPending) {
    const label = handoffPending || leg.handoff_requested
      ? 'switching to ' + text(leg.handoff_target_provider || targetProvider) + '…'
      : 'continue with ' + targetProvider;
    const handoff = button(label, 'action handoff', event => {
      event.stopPropagation();
      handoffLeg(runId(run), text(leg.leg_id), targetProvider);
    });
    handoff.disabled = Boolean(leg.handoff_requested || handoffPending);
    actions.append(handoff);
  }
  row.append(actions);
  if (capacityWait) {
    const providers = (capacityWait.eligible_providers || []).join(' or ');
    const timing = capacityTime(capacityWait.not_before || capacityWait.resets_at);
    const reason = text(capacityWait.reason || 'provider usage is exhausted');
    row.append(el('div', 'leg-wait', [reason, providers && 'eligible: ' + providers, timing]
      .filter(Boolean).join(' · ')));
  }
  const context = attempt.context_receipt || null;
  if (context) {
    const contextBits = [
      'context ' + text(context.strategy || 'unknown'),
      text(context.delivered_chars || 0) + '/' + text(context.source_chars || 0) + ' chars',
      context.omitted_chars ? text(context.omitted_chars) + ' omitted' : '',
      context.redaction_count ? text(context.redaction_count) + ' secrets redacted' : '',
      context.full_history_preserved ? 'full history preserved' : 'history preservation unconfirmed',
    ].filter(Boolean);
    row.append(el('div', 'leg-context', contextBits.join(' · ')));
  }
  const error = text(attempt.error || leg.error || '');
  if (error && !waitingForControl) row.append(el('div', 'leg-error', error));
  return row;
}

function renderPhase(run, phase, fallbackIndex) {
  const card = el('section', 'phase');
  const head = el('div', 'phase-head');
  // Policy/storage indexes are zero-based; the UI is deliberately human-numbered.
  const index = fallbackIndex;
  const name = text(phase.display_name || phase.name || phase.title || ('phase ' + index));
  const allLegs = Array.isArray(phase.legs) ? phase.legs : [];
  const legs = state.focus ? allLegs.filter(leg => {
    const assignments = Array.isArray(leg.assignment_ids) ? leg.assignment_ids.map(text) : [];
    return [text(leg.worker_key), text(leg.leg_id), ...assignments].includes(state.focus);
  }) : allLegs;
  const done = legs.filter(leg => ['done','complete','completed','succeeded'].includes(legState(leg))).length;
  head.append(el('div', 'phase-index', index));
  head.append(el('div', 'phase-name', name));
  head.append(el('div', 'phase-count', done + '/' + legs.length + ' agent steps'));
  head.append(statusPill(phaseState(phase)));
  card.append(head);
  const body = el('div', 'legs');
  if (!legs.length) body.append(el('div', 'empty', 'agent steps not scheduled yet'));
  else legs.forEach(leg => body.append(renderLeg(run, phase, leg)));
  card.append(body);
  return card;
}

function renderWorkUnits(run, open = true) {
  const units = Array.isArray(run && run.work_units) ? run.work_units : [];
  if (!units.length) return null;
  const complete = units.filter(unit => norm(unit.state) === 'completed').length;
  const plan = el('details', 'work-plan');
  plan.dataset.panel = 'work-units';
  plan.open = open;
  plan.append(el('summary', '', 'Work units · ' + complete + '/' + units.length + ' complete'));
  for (const unit of units) {
    const focused = state.focus === text(unit.id);
    const row = el('div', 'work-unit' + (focused ? ' focused' : ''));
    row.tabIndex = 0;
    row.addEventListener('click', () => {
      state.focus = focused ? null : text(unit.id);
      renderDetail();
    });
    row.append(el('div', 'work-unit-title', text(unit.id) + ' · ' + text(unit.title)));
    row.append(statusPill(unit.state));
    const progress = unit.progress || {};
    const dependencies = Array.isArray(unit.dependency_ids) && unit.dependency_ids.length
      ? ' · depends on ' + unit.dependency_ids.join(', ') : '';
    row.append(el(
      'div',
      'work-unit-meta',
      'owner ' + text(unit.owner_worker_key || 'unassigned') + ' · ' +
        text(progress.completed || 0) + '/' + text(progress.total || 0) + ' phase receipts' +
        dependencies,
    ));
    const ownership = unit.file_ownership || {};
    row.append(el(
      'div',
      'work-unit-scope',
      text(unit.logical_scope || unit.description) + ' · file ownership: ' +
        text(ownership.mode || 'unspecified'),
    ));
    const blocks = Array.isArray(unit.blocked_dependency_ids) && unit.blocked_dependency_ids.length
      ? ' · blocked by ' + unit.blocked_dependency_ids.join(', ') : '';
    const failure = unit.failure_reason ? ' · ' + text(unit.failure_reason) : '';
    const executions = Array.isArray(unit.phase_executions) ? unit.phase_executions : [];
    row.append(el(
      'div',
      'work-unit-executions',
      executions.map(item => text(item.phase) + ': ' + text(item.state)).join(' · ') +
        blocks + failure,
    ));
    const receipts = Array.isArray(unit.evidence_receipts) ? unit.evidence_receipts : [];
    const latestReceipt = receipts.length ? receipts[receipts.length - 1] : null;
    if (latestReceipt) {
      const verdictUnits = Array.isArray(latestReceipt.units) ? latestReceipt.units : [];
      const verdict = verdictUnits.find(item => text(item.unit_id) === text(unit.id)) || null;
      const changed = verdict && Array.isArray(verdict.changed_paths)
        ? verdict.changed_paths.map(text).filter(Boolean) : [];
      row.append(el(
        'div',
        'work-unit-executions',
        [
          'evidence ' + (latestReceipt.accepted ? 'accepted' : 'recorded'),
          text(latestReceipt.phase || ''),
          changed.length ? 'changed ' + changed.join(', ') : '',
          text(latestReceipt.reason || ''),
        ].filter(Boolean).join(' · '),
      ));
    }
    plan.append(row);
  }
  return plan;
}

function renderIsolation(run, open = false) {
  const isolation = (run && run.isolation) || null;
  if (!isolation || isolation.mode === 'not_applicable') return null;
  const claims = Array.isArray(isolation.claims) ? isolation.claims : [];
  const workspaces = Array.isArray(isolation.workspaces) ? isolation.workspaces : [];
  const integrations = Array.isArray(isolation.integrations) ? isolation.integrations : [];
  const panel = el('details', 'work-plan');
  panel.dataset.panel = 'isolation';
  panel.open = open;
  panel.append(el(
    'summary',
    '',
    'Isolation · ' + text(isolation.mode || 'unknown') + ' · ' +
      claims.length + ' claims · ' + workspaces.length + ' worktrees',
  ));
  panel.append(el(
    'div',
    'isolation-row',
    [
      isolation.enabled ? 'enabled' : 'disabled',
      isolation.safe === false ? 'preflight unsafe' : 'preflight safe',
      text(isolation.reason || ''),
      text(isolation.base_dirty_path_count || 0) + ' dirty base paths',
    ].filter(Boolean).join(' · '),
  ));
  for (const claim of claims) {
    panel.append(el(
      'div',
      'isolation-row',
      'claim · ' + text(claim.worker_key) + ' · ' + text(claim.mode) + ' · ' + text(claim.path),
    ));
  }
  for (const workspace of workspaces) {
    panel.append(el(
      'div',
      'isolation-row',
      'worktree · ' + text(workspace.worker_key) + ' · ' + text(workspace.state) +
        ' · ' + text(workspace.branch) + ' · ' + text(workspace.path),
    ));
  }
  for (const integration of integrations.slice(-8)) {
    panel.append(el(
      'div',
      'isolation-row',
      'integration · ' + text(integration.worker_key) + ' · ' +
        (integration.ok ? 'accepted' : 'blocked') + ' · ' + text(integration.reason),
    ));
  }
  return panel;
}

function renderSupervision(run, open = false) {
  const supervision = (run && run.supervision) || null;
  if (!supervision) return null;
  const workers = Array.isArray(supervision.workers) ? supervision.workers : [];
  if (!workers.length && !supervision.error) return null;
  const panel = el('details', 'work-plan');
  panel.dataset.panel = 'supervision';
  panel.open = open;
  panel.append(el(
    'summary',
    '',
    'Supervision · ' + text(supervision.active || 0) + ' active · ' +
      text(supervision.stalled || 0) + ' stalled · ' +
      text(supervision.retry_scheduled || 0) + ' retry queued',
  ));
  if (supervision.error) {
    panel.append(el('div', 'isolation-row', 'projection unavailable · ' + text(supervision.error)));
  }
  for (const worker of workers) {
    const heartbeat = Number(worker.heartbeat_age_seconds);
    const progress = Number(worker.progress_age_seconds);
    const retries = text(worker.retries_used || 0) + '/' + text(worker.max_retries || 0);
    panel.append(el(
      'div',
      'isolation-row',
      [
        text(worker.leg_id || worker.attempt_id || 'worker'),
        text(worker.state || 'unknown'),
        Number.isFinite(heartbeat) ? 'heartbeat ' + Math.round(heartbeat) + 's ago' : '',
        Number.isFinite(progress) ? 'progress ' + Math.round(progress) + 's ago' : '',
        'stall retries ' + retries,
        worker.lease_expired ? 'lease expired' : '',
        text(worker.recovery_reason || ''),
      ].filter(Boolean).join(' · '),
    ));
  }
  return panel;
}

function renderDetail() {
  const root = document.getElementById('detail');
  const run = state.detail;
  const previousRunId = text(root.dataset.runId || '');
  const nextRunId = runId(run);
  const sameRun = Boolean(nextRunId && previousRunId === nextRunId);
  const priorPhases = sameRun ? root.querySelector('.phases') : null;
  const phaseScroll = scrollPosition(priorPhases);
  const panelState = new Map();
  if (sameRun) {
    root.querySelectorAll('details[data-panel]').forEach(panel => {
      panelState.set(text(panel.dataset.panel), Boolean(panel.open));
    });
  }
  root.replaceChildren();
  if (!run) {
    delete root.dataset.runId;
    root.append(el('div', 'empty', state.selectedId ? 'loading run…' : 'select a Fleet run'));
    return;
  }
  root.dataset.runId = nextRunId;
  const status = runState(run);
  const head = el('div', 'detail-head');
  const heading = el('div', 'detail-heading');
  heading.append(el('div', 'detail-task', runTask(run)));
  const meta = [runId(run), text(run.activity), topologyText(run), progressText(run), duration(run)].filter(Boolean).join(' · ');
  heading.append(el('div', 'detail-meta', meta));
  const routing = routingInfo(run);
  if (routing.label || routing.reason) {
    heading.append(el(
      'div',
      'detail-routing',
      [routing.label, routing.reason].filter(Boolean).join(' · '),
    ));
  }
  head.append(heading);
  head.append(statusPill(status));
  const actions = el('div', 'detail-actions');
  if (state.focus) actions.append(button('clear focus', 'action', () => {
    state.focus = null;
    renderDetail();
  }));
  const originSid = text(run.origin_session_id || '');
  if (originSid) actions.append(button('open origin', 'action', () => openSession(originSid)));
  if (ACTIVE_STATES.has(status)) {
    actions.append(button('stop', 'action stop', () => stopRun(runId(run))));
  } else if (RETRY_STATES.has(status)) {
    actions.append(button('retry', 'action retry', () => retryRun(runId(run))));
  }
  if (!ACTIVE_STATES.has(status)) {
    actions.append(button('delete', 'action delete', () => deleteRun(runId(run))));
  }
  head.append(actions);
  root.append(head);
  if (run.error) root.append(el(
    'div',
    status === 'waiting_for_capacity' ? 'capacity-banner' : 'error-banner',
    run.error,
  ));
  const workPlan = renderWorkUnits(
    run,
    panelState.has('work-units') ? panelState.get('work-units') : true,
  );
  if (workPlan) root.append(workPlan);
  const isolation = renderIsolation(
    run,
    panelState.has('isolation') ? panelState.get('isolation') : false,
  );
  if (isolation) root.append(isolation);
  const supervision = renderSupervision(
    run,
    panelState.has('supervision') ? panelState.get('supervision') : false,
  );
  if (supervision) root.append(supervision);
  const phases = el('div', 'phases');
  const rows = Array.isArray(run.phases) ? run.phases : [];
  if (!rows.length) phases.append(el('div', 'empty', 'phase plan not available yet'));
  rows.forEach((phase, index) => phases.append(renderPhase(run, phase, index + 1)));
  root.append(phases);
  if (sameRun) restoreScrollPosition(phases, phaseScroll);
}

async function loadDetail(id) {
  const seq = ++state.detailSeq;
  if (!state.detail || runId(state.detail) !== id) {
    state.detail = null;
    renderDetail();
  }
  try {
    const payload = await api('/api/fleet/runs/' + encodeURIComponent(id));
    if (seq !== state.detailSeq || id !== state.selectedId) return;
    state.detail = payload.run || null;
    renderDetail();
  } catch (error) {
    if (seq !== state.detailSeq) return;
    showError(error.message);
    renderDetail();
  }
}

function selectRun(id) {
  if (!id) return;
  state.selectedId = id;
  state.focus = null;
  state.detail = null;
  renderRuns();
  loadDetail(id);
}

async function refresh() {
  if (state.loading) return;
  state.loading = true;
  document.getElementById('refreshBtn').disabled = true;
  try {
    const payload = await api('/api/fleet/runs?limit=50');
    state.runs = Array.isArray(payload.runs) ? payload.runs : [];
    if (!state.selectedId || !state.runs.some(run => runId(run) === state.selectedId)) {
      state.selectedId = state.runs.length ? runId(state.runs[0]) : null;
    }
    showError('');
    renderRuns();
    postCount();
    if (state.selectedId) await loadDetail(state.selectedId);
    else { state.detail = null; renderDetail(); }
  } catch (error) {
    showError(error.message);
  } finally {
    state.loading = false;
    document.getElementById('refreshBtn').disabled = false;
  }
}

async function stopRun(id) {
  if (!confirm('Stop this Fleet run and every active worker?')) return;
  try {
    const payload = await api('/api/fleet/runs/' + encodeURIComponent(id) + '/stop', { method: 'POST' });
    state.detail = payload.run || state.detail;
    renderDetail();
    await refresh();
  } catch (error) { showError(error.message); }
}

async function deleteRun(id) {
  if (!confirm('Delete this Fleet, every worker chat, and its private worktrees?')) return;
  try {
    await api('/api/fleet/runs/' + encodeURIComponent(id), { method: 'DELETE' });
    state.selectedForDelete.delete(id);
    if (state.selectedId === id) {
      state.selectedId = null;
      state.detail = null;
    }
    await refresh();
  } catch (error) { showError(error.message); }
}

async function deleteSelectedRuns() {
  const ids = state.runs
    .filter(run => isDeletable(run) && state.selectedForDelete.has(runId(run)))
    .map(runId);
  if (!ids.length) return;
  const label = ids.length === 1 ? 'this Fleet' : 'these ' + ids.length + ' Fleets';
  if (!confirm('Delete ' + label + ', every worker chat, and their private worktrees?')) return;
  state.deletingMany = true;
  renderRuns();
  try {
    await api('/api/fleet/runs', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_ids: ids }),
    });
    if (ids.includes(state.selectedId)) {
      state.selectedId = null;
      state.detail = null;
    }
    state.selectedForDelete.clear();
    showError('');
  } catch (error) {
    showError(error.message);
  } finally {
    state.deletingMany = false;
    await refresh();
    renderRuns();
  }
}

async function retryRun(id) {
  try {
    const payload = await api('/api/fleet/runs/' + encodeURIComponent(id) + '/retry', { method: 'POST' });
    const retried = payload.run || {};
    state.selectedId = runId(retried) || id;
    state.detail = retried;
    renderDetail();
    await refresh();
  } catch (error) { showError(error.message); }
}

async function retryLeg(id, legId) {
  const retryKey = id + ':' + legId;
  state.pendingLegRetries.add(retryKey);
  ++state.detailSeq;
  if (id === state.selectedId) renderDetail();
  try {
    const payload = await api(
      '/api/fleet/runs/' + encodeURIComponent(id) + '/legs/' +
      encodeURIComponent(legId) + '/retry',
      { method: 'POST' },
    );
    state.pendingLegRetries.delete(retryKey);
    if (id !== state.selectedId) return;
    ++state.detailSeq;
    state.detail = payload.run || state.detail;
    showError('');
    renderDetail();
    await refresh();
  } catch (error) {
    state.pendingLegRetries.delete(retryKey);
    if (id !== state.selectedId) return;
    ++state.detailSeq;
    showError(error.message);
    renderDetail();
  }
}

async function handoffLeg(id, legId, provider) {
  const handoffKey = id + ':' + legId;
  state.pendingHandoffs.add(handoffKey);
  ++state.detailSeq;
  if (id === state.selectedId) renderDetail();
  try {
    const payload = await api(
      '/api/fleet/runs/' + encodeURIComponent(id) + '/legs/' +
      encodeURIComponent(legId) + '/handoff',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider }),
      },
    );
    state.pendingHandoffs.delete(handoffKey);
    if (id !== state.selectedId) return;
    ++state.detailSeq;
    state.detail = payload.run || state.detail;
    showError('');
    renderDetail();
    await refresh();
  } catch (error) {
    state.pendingHandoffs.delete(handoffKey);
    if (id !== state.selectedId) return;
    ++state.detailSeq;
    showError(error.message);
    renderDetail();
  }
}

function schedulePoll() {
  if (state.timer) clearTimeout(state.timer);
  const delay = state.visible ? 2000 : 12000;
  state.timer = setTimeout(async () => {
    await refresh();
    schedulePoll();
  }, delay);
}

window.addEventListener('message', event => {
  if (event.origin !== window.location.origin || event.source !== parent) return;
  const message = event.data || {};
  if (message.type !== 'serena-fleet-visible') return;
  state.visible = Boolean(message.visible);
  if (state.visible) refresh();
  schedulePoll();
});
document.getElementById('refreshBtn').addEventListener('click', refresh);
document.getElementById('selectAllRuns').addEventListener('change', event => {
  setAllRunDeleteSelections(event.currentTarget.checked);
});
document.getElementById('bulkDeleteBtn').addEventListener('click', deleteSelectedRuns);
refresh();
schedulePoll();
</script>
</body>
</html>
"""
