"""Phone briefs buy an approval-held task, never immediate execution.

Contract tests isolate ingress from the task store; integration tests exercise
the real Markdown queue, triage, and concurrent approval deduplication.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from core import webhook_ingress as webhooks
from core.webhook_signing import WebhookReplayStore, sign
from memory import store

SECRET = "test-task-webhook-secret"


@pytest.fixture
def ingress(tmp_path):
    return webhooks.default_ingress(
        path=tmp_path / "ingress.sqlite3", secret=SECRET,
        replay_store=WebhookReplayStore(tmp_path / "replays.sqlite3"),
    )


@pytest.fixture
def enqueue(monkeypatch):
    enqueue = Mock(return_value={"id": 42, "state": "ready"})
    monkeypatch.setattr(store, "enqueue_task", enqueue)
    return enqueue


def post(ingress, payload, *, now=1000):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return ingress.handle("task", raw, sign(raw, SECRET, timestamp=now).headers(), now=now)


@pytest.mark.parametrize("hint", ["project", "repository"])
def test_task_waits_for_approval_and_passes_only_reviewed_fields(ingress, enqueue, hint):
    held = post(ingress, {"text": "fix the login error and add a regression test",
                          hint: "serena", "priority": "high"})
    assert held.status == 202
    assert ingress.routes == ("bluebubbles", "notify", "ping", "task")
    enqueue.assert_not_called()
    assert ingress.pending()[0]["route"] == "task"
    approved = ingress.approve(held.delivery_id, actor="raghav", now=1001)
    assert approved.accepted
    assert approved.reason == "queued task 42 (ready)"
    enqueue.assert_called_once_with(
        "fix the login error and add a regression test", project_hint="serena",
        priority="high", source_id=f"webhook:{held.delivery_id}",
    )
    assert not ingress.pending()
    row = ingress.history(route="task")[0]
    assert row["approved_by"] == "raghav"
    assert row["body_raw"] is None
    assert ingress.approve(held.delivery_id, actor="raghav").accepted
    enqueue.assert_called_once()


def test_thin_brief_uses_store_triage_and_default_metadata(ingress, enqueue):
    enqueue.return_value = {"id": 42, "state": "needs_triage"}
    held = post(ingress, {"text": "locket is broken"})
    result = ingress.approve(held.delivery_id, actor="raghav")
    assert result.reason == "queued task 42 (needs_triage)"
    enqueue.assert_called_once_with(
        "locket is broken", project_hint=None, priority="normal",
        source_id=f"webhook:{held.delivery_id}",
    )


@pytest.mark.parametrize("payload", [
    {}, {"text": ""}, {"text": "  "}, {"text": 1}, {"text": []},
    {"text": "x" * 4001}, {"text": "x", "project": {}},
    {"text": "x", "project": "x" * 513}, {"text": "x", "repository": ""},
    {"text": "x", "project": "one", "repository": "two"},
    {"text": "x", "priority": []}, {"text": "x", "priority": True},
    {"text": "x", "priority": "urgent"}, {"text": "x", "priority": None},
    {"text": "x", "state": "ready"}, {"text": "x", "source_id": "forged"},
    {"text": "x", "project": "repo\nstate: ready"}, {"text": "bad\x00text"},
    {"text": "\ud800"}, b'{"text":"one","text":"two"}',
    b'{"text":NaN}', b'{"text":Infinity}', b'[]', b'{', b'\xff',
    b'{"text":' + b'[' * 1500 + b'0' + b']' * 1500 + b'}',
])
def test_malformed_task_is_durably_refused_before_hold(ingress, enqueue, payload):
    result = post(ingress, payload)
    assert result.status == 400
    assert not ingress.pending()
    assert ingress.history()[0]["decision"] == "rejected"
    assert ingress.history()[0]["body_raw"] is None
    enqueue.assert_not_called()


@pytest.mark.parametrize("route,body,status", [
    ("task", b"x" * (webhooks.MAX_TASK_BODY_BYTES + 1), 413),
    ("task", b"x" * (webhooks.MAX_BODY_BYTES + 1), 413),
    ("unknown", b"malformed", 404),
])
def test_size_and_unknown_route_are_refused_before_json_parse(ingress, monkeypatch, route, body, status):
    parser = Mock(side_effect=AssertionError("must not parse"))
    monkeypatch.setattr(webhooks.json, "loads", parser)
    result = ingress.handle(route, body, {}, now=1000)
    assert result.status == status
    parser.assert_not_called()


def test_task_authentication_and_one_shot_consumption(ingress, enqueue):
    raw = b'{"text":"locket is broken"}'
    assert ingress.handle("task", raw, {}, now=1000).status == 401
    headers = sign(raw, SECRET, timestamp=1000).headers()
    assert ingress.handle("task", raw + b" ", headers, now=1000).status == 401
    assert ingress.handle("task", raw, headers, now=1000).status == 202
    assert ingress.handle("task", raw, headers, now=1001).status == 401
    enqueue.assert_not_called()


def test_unicode_approval_uses_exact_body_not_truncated_preview(ingress, enqueue):
    text = "😀" * 3000
    raw = json.dumps({"text": text}, ensure_ascii=False).encode()
    held = post(ingress, raw)
    assert held.status == 202
    assert len(ingress.pending()[0]["payload_json"]) == 16000
    assert ingress.approve(held.delivery_id, actor="raghav").accepted
    assert enqueue.call_args.args == (text,)


def test_concurrent_approval_passes_same_durable_dedupe_identity(ingress, enqueue):
    barrier = Barrier(2)

    def finish(*args, **kwargs):
        barrier.wait(timeout=5)
        return {"id": 42, "state": "ready"}

    enqueue.side_effect = finish
    held = post(ingress, {"text": "fix login and add a regression test"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ingress.approve, held.delivery_id, actor="raghav") for _ in range(2)]
        assert all(future.result(timeout=10).accepted for future in futures)
    assert enqueue.call_count == 2
    assert {call.kwargs["source_id"] for call in enqueue.call_args_list} == {f"webhook:{held.delivery_id}"}


def test_enqueue_failure_is_audited_without_false_acceptance(ingress, enqueue):
    enqueue.side_effect = OSError("queue unavailable")
    held = post(ingress, {"text": "fix login and add a regression test"})
    result = ingress.approve(held.delivery_id, actor="raghav")
    assert result.status == 500
    assert ingress.history(route="task")[0]["decision"] == "rejected"


def test_cli_routes_pending_approve_and_history(ingress, enqueue, monkeypatch):
    import cli

    monkeypatch.setattr(cli, "_ingress", lambda: ingress)
    runner = CliRunner()
    routes = runner.invoke(cli.main, ["webhook", "routes"])
    assert routes.exit_code == 0
    assert "task" in routes.output.splitlines()
    held = post(ingress, {"text": "fix login and add a regression test"})
    pending = runner.invoke(cli.main, ["webhook", "pending"])
    assert pending.exit_code == 0
    assert held.delivery_id in pending.output and "task" in pending.output
    approval = runner.invoke(cli.main, ["webhook", "approve", held.delivery_id, "--actor", "raghav"])
    assert approval.exit_code == 0, approval.output
    assert json.loads(approval.output)["decision"] == "accepted"
    history = runner.invoke(cli.main, ["webhook", "history", "--route", "task"])
    assert history.exit_code == 0
    assert "accepted" in history.output and "queued task 42" in history.output


def test_http_task_requires_local_approval(ingress, enqueue, monkeypatch):
    from flask import Flask

    from ui import webhook_web

    monkeypatch.setattr(webhook_web, "_ingress", lambda: ingress)
    app = Flask(__name__)
    app.register_blueprint(webhook_web.webhook_bp)
    client = app.test_client()
    raw = b'{"text":"fix login and add a regression test"}'
    held = client.post("/webhooks/task", data=raw,
                       headers=sign(raw, SECRET, timestamp=int(time.time())).headers())
    assert held.status_code == 202
    delivery_id = held.get_json()["delivery_id"]
    enqueue.assert_not_called()
    endpoint = f"/api/webhooks/{delivery_id}/approve"
    assert client.post(endpoint, json={"actor": "raghav"},
                       environ_base={"REMOTE_ADDR": "203.0.113.1"}).status_code == 403
    assert client.post(endpoint, json={}).status_code == 400
    assert client.post(endpoint, json={"actor": "raghav"}).status_code == 200


@pytest.mark.parametrize("text,state", [
    ("locket is broken", "needs_triage"),
    ("Please fix it because it is still broken today", "needs_triage"),
    ("Add a settings screen with a working dark mode toggle", "ready"),
    ("😀" * 3000, "needs_triage"),
])
def test_real_task_queue_roundtrip(ingress, monkeypatch, tmp_path, text, state):
    monkeypatch.setattr(store, "MEMORY_DIR", tmp_path / "memory")
    # Approval must bypass the legacy MemoryV2 proposal path entirely.
    monkeypatch.setattr(store, "_active_v2_store", Mock(side_effect=AssertionError("legacy write")))
    raw = json.dumps({"text": text, "project": "serena", "priority": "high"},
                     ensure_ascii=False).encode()
    held = post(ingress, raw)
    assert held.status == 202
    assert not (tmp_path / "memory" / "task").exists()
    # Reopen the ingress to prove the hold survives the receiving instance.
    ingress = webhooks.default_ingress(path=ingress.path, secret=SECRET)
    assert ingress.approve(held.delivery_id, actor="raghav").accepted
    tasks = store.list_memories("task")
    assert len(tasks) == 1
    assert tasks[0]["state"] == state
    assert tasks[0]["content"] == text
    assert tasks[0]["priority"] == "high"
    assert tasks[0]["project_hint"] == "serena"
    assert tasks[0]["source_id"] == f"webhook:{held.delivery_id}"
    assert ingress.approve(held.delivery_id, actor="raghav").accepted
    assert len(store.list_memories("task")) == 1


@pytest.mark.parametrize("hint", ["project", "repository"])
@pytest.mark.parametrize("separator", ["\n", "\v", "\f", "\r", "\x1c", "\x1d",
                                       "\x1e", "\x85", "\u2028", "\u2029", "---"])
def test_metadata_injection_is_rejected_before_hold(ingress, hint, separator):
    result = post(ingress, {"text": "locket is broken", hint: f"repo{separator}state: ready"})
    assert result.status == 400
    assert not ingress.pending()


def test_concurrent_approvals_create_one_real_task(ingress, monkeypatch, tmp_path):
    monkeypatch.setattr(store, "MEMORY_DIR", tmp_path / "memory")
    original = store.enqueue_task
    barrier = Barrier(2)

    def enqueue_together(*args, **kwargs):
        barrier.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "enqueue_task", enqueue_together)
    held = post(ingress, {"text": "Add a settings screen with a working dark mode toggle"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ingress.approve, held.delivery_id, actor="raghav") for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert all(result.accepted for result in results)
    assert results[0].reason == results[1].reason
    tasks = store.list_memories("task")
    assert len(tasks) == 1
    assert tasks[0]["state"] == "ready"
    assert tasks[0]["source_id"] == f"webhook:{held.delivery_id}"


def test_a_denied_delivery_never_runs_and_drops_its_body(ingress, enqueue, monkeypatch):
    import cli

    held = post(ingress, {"text": "fix login and add a regression test"})
    with pytest.raises(webhooks.WebhookIngressError):
        ingress.deny(held.delivery_id, actor="")
    monkeypatch.setattr(cli, "_ingress", lambda: ingress)
    denied = CliRunner().invoke(cli.main, ["webhook", "deny", held.delivery_id, "--actor", "raghav"])
    assert denied.exit_code == 0, denied.output
    assert json.loads(denied.output)["decision"] == "rejected"
    assert not ingress.pending()
    row = ingress.history(route="task")[0]
    assert row["reason"] == "denied by raghav"
    with ingress._connect() as connection:
        stored = connection.execute(
            "SELECT body_raw FROM webhook_deliveries WHERE delivery_id = ?", (held.delivery_id,)
        ).fetchone()
    assert stored["body_raw"] is None
    assert ingress.approve(held.delivery_id, actor="raghav").decision == "rejected"
    enqueue.assert_not_called()
    with pytest.raises(webhooks.WebhookIngressError):
        ingress.deny(held.delivery_id, actor="raghav")
