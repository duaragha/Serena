from __future__ import annotations

from dataclasses import dataclass

import pytest
from flask import Flask

from ui import fleet_web


class FakeSupervisor:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.run = {
            "run_id": "fleet-test-1",
            "task": "repair the parser",
            "activity": "coding",
            "state": "running",
            "origin_session_id": "origin-session",
            "agent_count": 2,
            "chat_count": 2,
            "progress": {"completed": 1, "total": 8},
            "phases": [
                {
                    "index": 0,
                    "name": "discover",
                    "display_name": "Research",
                    "state": "running",
                    "legs": [
                        {
                            "leg_id": "discover-sol",
                            "worker_key": "codex:0",
                            "worker_label": "Codex 1",
                            "assignment": "parser ownership",
                            "role": "investigator",
                            "runtime": "codex",
                            "model": "gpt-5.6-sol",
                            "effort": "xhigh",
                            "state": "running",
                            "current_attempt": {
                                "number": 1,
                                "state": "running",
                                "session_id": "worker-session",
                                "actual_model": "gpt-5.6-sol",
                                "actual_effort": "xhigh",
                            },
                        },
                        {
                            "leg_id": "discover-opus",
                            "role": "architecture-scout",
                            "runtime": "claude",
                            "model": "opus",
                            "effort": "xhigh",
                            "state": "failed",
                            "retry_requested": False,
                            "current_attempt": {
                                "number": 1,
                                "state": "failed",
                                "session_id": "claude-worker-session",
                                "actual_model": "<synthetic>",
                                "actual_effort": "xhigh",
                                "error": "API Error: ENOTFOUND",
                            },
                        },
                    ],
                }
            ],
        }

    def list_runs(self, limit=50):
        self.calls.append(("list", limit))
        return [{key: value for key, value in self.run.items() if key != "phases"}]

    def get_run(self, run_id):
        self.calls.append(("get", run_id))
        if run_id.startswith("fleet-done-"):
            return {**self.run, "run_id": run_id, "state": "completed"}
        return self.run if run_id == self.run["run_id"] else None

    def start_run(self, task, **kwargs):
        self.calls.append(("start", task, kwargs))
        return {**self.run, "task": task, **kwargs}

    def stop_run(self, run_id):
        self.calls.append(("stop", run_id))
        if run_id == "missing":
            raise KeyError(run_id)
        return {**self.run, "state": "stopped"}

    def retry_run(self, run_id):
        self.calls.append(("retry", run_id))
        if run_id == "running":
            raise RuntimeError("run is still active")
        return {**self.run, "run_id": "fleet-test-2", "state": "queued"}

    def delete_run(self, run_id):
        self.calls.append(("delete", run_id))
        if run_id == "running":
            raise RuntimeError("stop this Fleet first")
        if run_id == "missing":
            raise KeyError(run_id)
        return {"run_id": run_id, "state": "deleted", "deleted_chat_count": 2}

    def preflight_delete_run(self, run_id):
        self.calls.append(("preflight-delete", run_id))
        if run_id == "fleet-unrecovered":
            raise RuntimeError("Fleet has unrecovered worker changes")
        return self.get_run(run_id)

    def retry_leg(self, run_id, leg_id):
        self.calls.append(("retry-leg", run_id, leg_id))
        if run_id != self.run["run_id"]:
            raise KeyError(run_id)
        return {**self.run, "state": "running"}

    def handoff_leg(self, run_id, leg_id, provider):
        self.calls.append(("handoff-leg", run_id, leg_id, provider))
        if run_id != self.run["run_id"]:
            raise KeyError(run_id)
        return {**self.run, "state": "running"}

    def inspect_run(self, run_id, focus="", *, event_limit=50):
        self.calls.append(("inspect", run_id, focus, event_limit))
        if run_id != self.run["run_id"] or focus == "missing":
            raise KeyError(run_id)
        return {
            "run_id": run_id,
            "focus": focus or None,
            "work_units": [],
            "workers": [],
            "events": [],
        }


@pytest.fixture()
def fleet_client(monkeypatch):
    supervisor = FakeSupervisor()
    monkeypatch.setattr(fleet_web, "_supervisor", lambda: supervisor)
    app = Flask(__name__)
    app.register_blueprint(fleet_web.fleet_bp)
    return app.test_client(), supervisor


def test_fleet_view_is_isolated_and_uses_true_attempt_identity(fleet_client):
    client, _supervisor = fleet_client

    response = client.get("/fleet/view")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Fleet runs" in page
    assert 'id="selectAllRuns"' in page
    assert 'id="bulkDeleteBtn"' in page
    assert "selectedForDelete: new Set()" in page
    assert "function deleteSelectedRuns()" in page
    assert "DELETABLE_STATES.has(runState(run))" in page
    assert "displayModel(attempt, leg)" in page
    assert "attempt.actual_effort || leg.effort" in page
    assert "serena-fleet-open-session" in page
    assert "textContent" in page
    assert "const index = fallbackIndex" in page
    assert "phase.display_name || phase.name" in page
    assert "run.current_phase_display" in page
    assert "item.completed_at || item.ended_at" in page
    assert "retry agent" in page
    assert "retry queued" in page
    assert "LEG_RETRY_RUN_STATES.has(runState(run))" in page
    assert "legRetryPending(leg)" in page
    assert "pendingLegRetries" in page
    assert "continue with " in page
    assert "switching to " in page
    assert "pendingHandoffs" in page
    assert "handoffLeg(runId(run)" in page
    assert "if (id !== state.selectedId) return" in page
    assert "function topologyText(run)" in page
    assert "function routingInfo(run)" in page
    assert "policy.requested_provider_mode" in page
    assert "policy.provider_mode" in page
    assert "codex only" in page
    assert "claude only" in page
    assert "actualModelConfirmed(attempt)" in page
    assert "' requested'" in page
    assert "leg.worker_label || leg.worker_key" in page
    assert "'owns: ' + assignment" in page
    assert "' agent steps'" in page
    assert "done + '/' + legs.length + ' agent steps'" in page
    assert "function renderWorkUnits(run, open = true)" in page
    assert "Work units · " in page
    assert "owner_worker_key" in page
    assert "file ownership: " in page
    assert "waiting_for_capacity" in page
    assert "capacityTime(capacityWait.not_before" in page
    assert "context_receipt" in page
    assert "full history preserved" in page
    assert "clear focus" in page
    assert "phase_executions" in page
    assert "evidence_receipts" in page
    assert "evidence ' + (latestReceipt.accepted ? 'accepted' : 'recorded')" in page
    assert "verdict.changed_paths" in page
    assert "function renderIsolation(run, open = false)" in page
    assert "dirty base paths" in page
    assert "integration · " in page
    assert "function renderSupervision(run, open = false)" in page
    assert "heartbeat " in page
    assert "stall retries " in page
    assert "lease expired" in page


def test_fleet_polling_preserves_scroll_and_panel_state(fleet_client):
    client, _supervisor = fleet_client

    page = client.get("/fleet/view").get_data(as_text=True)
    runs_render = page.split("function renderRuns()", 1)[1].split(
        "\nfunction attemptFor", 1
    )[0]
    detail_render = page.split("function renderDetail()", 1)[1].split(
        "\nasync function loadDetail", 1
    )[0]

    assert "function scrollPosition(node)" in page
    assert "function restoreScrollPosition(node, position)" in page
    assert runs_render.index("const priorScroll = scrollPosition(list)") < runs_render.index(
        "list.replaceChildren()"
    )
    assert runs_render.index("list.replaceChildren()") < runs_render.index(
        "restoreScrollPosition(list, priorScroll)"
    )
    assert detail_render.index("const phaseScroll = scrollPosition(priorPhases)") < (
        detail_render.index("root.replaceChildren()")
    )
    assert detail_render.index("root.replaceChildren()") < detail_render.index(
        "restoreScrollPosition(phases, phaseScroll)"
    )
    assert "root.querySelectorAll('details[data-panel]')" in detail_render
    assert "panelState.has('work-units')" in detail_render
    assert "panelState.has('isolation')" in detail_render
    assert "panelState.has('supervision')" in detail_render


def test_list_and_get_runs_keep_full_supervisor_shape(fleet_client):
    client, supervisor = fleet_client

    listed = client.get("/api/fleet/runs?limit=500")
    detail = client.get("/api/fleet/runs/fleet-test-1")

    assert listed.status_code == 200
    assert listed.get_json()["runs"][0]["run_id"] == "fleet-test-1"
    assert supervisor.calls[0] == ("list", 100)
    payload = detail.get_json()["run"]
    attempt = payload["phases"][0]["legs"][0]["current_attempt"]
    assert attempt["actual_model"] == "gpt-5.6-sol"
    assert attempt["actual_effort"] == "xhigh"
    assert attempt["session_id"] == "worker-session"


def test_inspect_endpoint_forwards_bounded_focus(fleet_client):
    client, supervisor = fleet_client

    response = client.get("/api/fleet/runs/fleet-test-1/inspect?focus=ws-2&events=500")

    assert response.status_code == 200
    assert response.get_json()["inspection"]["focus"] == "ws-2"
    assert ("inspect", "fleet-test-1", "ws-2", 100) in supervisor.calls
    missing = client.get("/api/fleet/runs/fleet-test-1/inspect?focus=missing")
    assert missing.status_code == 404


def test_start_run_passes_supported_facade_options(fleet_client):
    client, supervisor = fleet_client

    response = client.post(
        "/api/fleet/runs",
        json={
            "task": "  inspect this repository  ",
            "activity": "research",
            "provider_mode": "codex",
            "worker_count": 3,
            "cwd": "/tmp/project",
            "origin_session_id": "origin",
            "origin_agent": "claude",
            "dry_run": True,
        },
    )

    assert response.status_code == 200
    assert supervisor.calls == [
        (
            "start",
            "inspect this repository",
            {
                "activity": "research",
                "provider_mode": "codex",
                "worker_count": 3,
                "cwd": "/tmp/project",
                "origin_session_id": "origin",
                "origin_agent": "claude",
                "dry_run": True,
            },
        )
    ]


def test_start_rejects_empty_task(fleet_client):
    client, supervisor = fleet_client

    response = client.post("/api/fleet/runs", json={"task": "   "})

    assert response.status_code == 400
    assert response.get_json()["error"] == "task is required"
    assert supervisor.calls == []


def test_stop_retry_and_missing_run_errors_are_explicit(fleet_client):
    client, supervisor = fleet_client

    stopped = client.post("/api/fleet/runs/fleet-test-1/stop")
    retried = client.post("/api/fleet/runs/fleet-test-1/retry")
    missing = client.post("/api/fleet/runs/missing/stop")
    conflict = client.post("/api/fleet/runs/running/retry")
    retried_leg = client.post("/api/fleet/runs/fleet-test-1/legs/discover-opus/retry")
    handed_off = client.post(
        "/api/fleet/runs/fleet-test-1/legs/discover-opus/handoff",
        json={"provider": "codex"},
    )
    unknown = client.get("/api/fleet/runs/unknown")
    deleted = client.delete("/api/fleet/runs/fleet-test-1")
    delete_conflict = client.delete("/api/fleet/runs/running")

    assert stopped.get_json()["run"]["state"] == "stopped"
    assert retried.get_json()["run"]["run_id"] == "fleet-test-2"
    assert missing.status_code == 404
    assert conflict.status_code == 409
    assert conflict.get_json()["error"] == "run is still active"
    assert retried_leg.status_code == 200
    assert handed_off.status_code == 200
    assert unknown.status_code == 404
    assert deleted.get_json()["run"]["state"] == "deleted"
    assert delete_conflict.status_code == 409
    assert ("stop", "fleet-test-1") in supervisor.calls
    assert ("retry", "fleet-test-1") in supervisor.calls
    assert ("retry-leg", "fleet-test-1", "discover-opus") in supervisor.calls
    assert ("delete", "fleet-test-1") in supervisor.calls
    assert (
        "handoff-leg",
        "fleet-test-1",
        "discover-opus",
        "codex",
    ) in supervisor.calls


def test_bulk_delete_preflights_and_deletes_every_terminal_run(fleet_client):
    client, supervisor = fleet_client

    response = client.delete(
        "/api/fleet/runs",
        json={"run_ids": ["fleet-done-1", "fleet-done-2", "fleet-done-1"]},
    )

    assert response.status_code == 200
    assert response.get_json()["deleted_count"] == 2
    assert supervisor.calls.index(("preflight-delete", "fleet-done-1")) < supervisor.calls.index(
        ("delete", "fleet-done-1")
    )
    assert supervisor.calls.index(("preflight-delete", "fleet-done-2")) < supervisor.calls.index(
        ("delete", "fleet-done-1")
    )
    assert ("delete", "fleet-done-1") in supervisor.calls
    assert ("delete", "fleet-done-2") in supervisor.calls


def test_bulk_delete_rejects_active_or_invalid_selection_before_deleting(fleet_client):
    client, supervisor = fleet_client

    active = client.delete("/api/fleet/runs", json={"run_ids": ["fleet-test-1"]})
    empty = client.delete("/api/fleet/runs", json={"run_ids": []})
    invalid = client.delete("/api/fleet/runs", json={"run_ids": ["$bad"]})

    assert active.status_code == 409
    assert "wait for it to finish" in active.get_json()["error"]
    assert empty.status_code == 400
    assert invalid.status_code == 400
    assert not any(call[0] == "delete" for call in supervisor.calls)


def test_bulk_delete_rejects_unrecovered_selection_before_deleting_any_run(fleet_client):
    client, supervisor = fleet_client
    original_get = supervisor.get_run

    def get_run(run_id):
        if run_id == "fleet-unrecovered":
            return {**supervisor.run, "run_id": run_id, "state": "failed"}
        return original_get(run_id)

    supervisor.get_run = get_run
    response = client.delete(
        "/api/fleet/runs",
        json={"run_ids": ["fleet-done-1", "fleet-unrecovered"]},
    )

    assert response.status_code == 409
    assert "unrecovered worker changes" in response.get_json()["error"]
    assert not any(call[0] == "delete" for call in supervisor.calls)


def test_invalid_run_id_and_limit_do_not_reach_supervisor(fleet_client):
    client, supervisor = fleet_client

    bad_limit = client.get("/api/fleet/runs?limit=nope")
    bad_id = client.get("/api/fleet/runs/%24bad")
    bad_handoff = client.post(
        "/api/fleet/runs/fleet-test-1/legs/discover-opus/handoff",
        json={"provider": "other"},
    )

    assert bad_limit.status_code == 400
    assert bad_id.status_code == 400
    assert bad_handoff.status_code == 400
    assert supervisor.calls == []


def test_fleet_ui_and_actions_are_rejected_from_non_loopback_clients(fleet_client):
    client, supervisor = fleet_client

    view = client.get("/fleet/view", environ_base={"REMOTE_ADDR": "192.168.1.44"})
    start = client.post(
        "/api/fleet/runs",
        json={"task": "run privileged work", "cwd": "/home/raghav"},
        environ_base={"REMOTE_ADDR": "100.64.0.9"},
    )

    assert view.status_code == 403
    assert start.status_code == 403
    assert start.get_json()["error"] == "Fleet is available only from this computer"
    assert supervisor.calls == []


def test_missing_supervisor_is_a_clear_service_error(monkeypatch):
    monkeypatch.setattr(
        fleet_web,
        "_supervisor",
        lambda: (_ for _ in ()).throw(ImportError("not installed")),
    )
    app = Flask(__name__)
    app.register_blueprint(fleet_web.fleet_bp)

    response = app.test_client().get("/api/fleet/runs")

    assert response.status_code == 503
    assert "Fleet supervisor unavailable" in response.get_json()["error"]


def test_main_serena_shell_registers_fleet_and_opens_workers_read_only():
    from ui import web

    rules = {rule.rule for rule in web.app.url_map.iter_rules()}
    assert "/fleet/view" in rules
    assert "/api/fleet/runs/<run_id>/stop" in rules
    assert "/api/fleet/runs/<run_id>" in rules
    assert "/api/fleet/runs/<run_id>/legs/<leg_id>/retry" in rules
    assert "/api/fleet/runs/<run_id>/legs/<leg_id>/handoff" in rules
    assert 'data-tab="fleet"' in web.HTML
    assert 'id="fleetFrame"' in web.HTML
    assert "openConv(String(sid), { mode: 'read' })" in web.HTML
    assert "const showReadView = opts.mode !== 'live' || readOnly" in web.HTML
    assert "showReadView && convMode !== 'read'" in web.HTML
    assert "else if (showReadView && convMode !== 'read')" in web.HTML
    assert "data.agent === 'codex' ? 'Codex' : defaultAgentLabel" in web.HTML
    assert "!s.external_runtime_active && !s.fleet_worker" in web.HTML


@dataclass
class ExampleResult:
    run_id: str
    path: object


def test_jsonable_handles_facade_dataclasses_and_paths(tmp_path):
    payload = fleet_web._jsonable(ExampleResult("fleet-1", tmp_path))

    assert payload == {"run_id": "fleet-1", "path": str(tmp_path)}
