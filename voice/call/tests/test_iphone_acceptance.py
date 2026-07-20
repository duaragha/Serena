from __future__ import annotations

import json
from pathlib import Path

import voice.call.iphone_acceptance as acceptance


def test_prepare_stages_one_private_append_window(tmp_path: Path, monkeypatch) -> None:
    token = tmp_path / "chat_token"
    token.write_text("private-token-value", encoding="utf-8")
    token.chmod(0o600)
    handoff = tmp_path / "handoff.json"
    metrics = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(acceptance, "CHAT_TOKEN", token)
    monkeypatch.setattr(acceptance, "_service_state", lambda _name: "active")
    monkeypatch.setattr(
        acceptance,
        "_json_url",
        lambda _url: {
            "ok": True,
            "pid": 123,
            "started": 1.0,
            "billing": {"auth_mode": "subscription_oauth_guarded"},
        },
    )
    monkeypatch.setattr(acceptance, "_probe_app", lambda _url: 200)
    monkeypatch.setattr(
        acceptance,
        "capture_metrics_cursor",
        lambda _path: {"ok": True, "path": str(metrics), "offset": 0},
    )
    monkeypatch.setattr(
        acceptance,
        "subscription_auth_evidence",
        lambda: {"ok": True, "failures": []},
    )
    monkeypatch.setattr(
        acceptance,
        "brain_daemon_evidence",
        lambda: {"ok": True, "pid": 123, "failures": []},
    )
    monkeypatch.setattr(
        acceptance,
        "brain_health_evidence",
        lambda: {"ok": True, "pid": 123, "failures": []},
    )
    monkeypatch.setattr(
        acceptance,
        "capture_baseline",
        lambda **_kwargs: {"ok": True, "failures": [], "captured_at": 1.0},
    )

    result = acceptance.prepare(handoff_path=handoff, metrics_path=metrics)

    assert result["ok"] is True
    assert result["criteria"]["require_reconnect"] is True
    staged = json.loads(handoff.read_text(encoding="utf-8"))
    assert staged["token_ready"] is True
    assert staged["cost_baseline"]["ok"] is True
    assert handoff.stat().st_mode & 0o077 == 0


def test_verify_closes_both_gates_and_requires_roam(tmp_path: Path, monkeypatch) -> None:
    handoff = tmp_path / "handoff.json"
    report_path = tmp_path / "report.json"
    handoff.write_text(
        json.dumps({"schema_version": 2, "ok": True, "metrics": {"ok": True}})
    )
    rows = [{"event": "call.start", "call_id": "one-call"}]
    monkeypatch.setattr(
        acceptance,
        "load_metrics_append",
        lambda _path, _cursor: (rows, {"ok": True, "rows": 1}),
    )
    monkeypatch.setattr(
        acceptance,
        "analyze_call",
        lambda *_args, **_kwargs: {
            "acceptance_claim": True,
            "cold_open": {"pass": True},
            "reconnects": 1,
            "failures": [],
        },
    )
    monkeypatch.setattr(
        acceptance,
        "subscription_auth_evidence",
        lambda: {"ok": True, "failures": []},
    )
    monkeypatch.setattr(
        acceptance,
        "brain_daemon_evidence",
        lambda: {"ok": True, "failures": []},
    )
    monkeypatch.setattr(
        acceptance,
        "brain_health_evidence",
        lambda: {"ok": True, "failures": []},
    )
    monkeypatch.setattr(
        acceptance,
        "analyze_cost_objective",
        lambda *_args, **_kwargs: {
            "acceptance_claim": True,
            "automated_pass": True,
            "failures": [],
        },
    )

    result = acceptance.verify(
        handoff_path=handoff,
        report_path=report_path,
        metrics_path=tmp_path / "metrics.jsonl",
        heard_clean=True,
        billing_dashboard_clear=True,
    )

    assert result["ok"] is True
    assert result["cold_open_pass"] is True
    assert result["call_integrity_pass"] is True
    assert result["cost_pass"] is True
    assert json.loads(report_path.read_text(encoding="utf-8"))["call_id"] == "one-call"


def test_verify_fails_without_a_network_roam(tmp_path: Path, monkeypatch) -> None:
    handoff = tmp_path / "handoff.json"
    handoff.write_text(
        json.dumps({"schema_version": 2, "ok": True, "metrics": {"ok": True}})
    )
    monkeypatch.setattr(
        acceptance,
        "load_metrics_append",
        lambda _path, _cursor: (
            [{"event": "call.start", "call_id": "one-call"}],
            {"ok": True},
        ),
    )
    monkeypatch.setattr(
        acceptance,
        "analyze_call",
        lambda *_args, **_kwargs: {
            "acceptance_claim": True,
            "cold_open": {"pass": True},
            "reconnects": 0,
            "failures": [],
        },
    )
    monkeypatch.setattr(
        acceptance,
        "subscription_auth_evidence",
        lambda: {"ok": True, "failures": []},
    )
    monkeypatch.setattr(
        acceptance,
        "brain_daemon_evidence",
        lambda: {"ok": True, "failures": []},
    )
    monkeypatch.setattr(
        acceptance,
        "brain_health_evidence",
        lambda: {"ok": True, "failures": []},
    )
    monkeypatch.setattr(
        acceptance,
        "analyze_cost_objective",
        lambda *_args, **_kwargs: {
            "acceptance_claim": True,
            "automated_pass": True,
            "failures": [],
        },
    )

    result = acceptance.verify(
        handoff_path=handoff,
        report_path=tmp_path / "report.json",
        metrics_path=tmp_path / "metrics.jsonl",
        heard_clean=True,
        billing_dashboard_clear=True,
    )

    assert result["ok"] is False
    assert result["call_integrity_pass"] is False
    assert result["failures"] == ["the call did not prove one network-roam reconnect"]


def test_verify_keeps_cost_gate_open_without_dashboard_attestation(
    tmp_path: Path, monkeypatch
) -> None:
    handoff = tmp_path / "handoff.json"
    handoff.write_text(
        json.dumps({"schema_version": 2, "ok": True, "metrics": {"ok": True}})
    )
    monkeypatch.setattr(
        acceptance,
        "load_metrics_append",
        lambda _path, _cursor: (
            [{"event": "call.start", "call_id": "one-call"}],
            {"ok": True},
        ),
    )
    monkeypatch.setattr(
        acceptance,
        "analyze_call",
        lambda *_args, **_kwargs: {
            "acceptance_claim": True,
            "cold_open": {"pass": True},
            "reconnects": 1,
            "failures": [],
        },
    )
    monkeypatch.setattr(
        acceptance,
        "subscription_auth_evidence",
        lambda: {"ok": True, "failures": []},
    )
    monkeypatch.setattr(
        acceptance,
        "brain_daemon_evidence",
        lambda: {"ok": True, "failures": []},
    )
    monkeypatch.setattr(
        acceptance,
        "brain_health_evidence",
        lambda: {"ok": True, "failures": []},
    )
    monkeypatch.setattr(
        acceptance,
        "analyze_cost_objective",
        lambda *_args, **kwargs: {
            "acceptance_claim": kwargs["billing_dashboard_clear"],
            "automated_pass": True,
            "failures": (
                []
                if kwargs["billing_dashboard_clear"]
                else ["post-call billing dashboard attestation is missing"]
            ),
        },
    )

    result = acceptance.verify(
        handoff_path=handoff,
        report_path=tmp_path / "report.json",
        metrics_path=tmp_path / "metrics.jsonl",
        heard_clean=True,
    )

    assert result["ok"] is False
    assert result["call_integrity_pass"] is True
    assert result["cost_pass"] is False
    assert result["failures"] == ["post-call billing dashboard attestation is missing"]
