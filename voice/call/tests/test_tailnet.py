from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from voice.call import orchestrator, tailnet
from voice.call.brain import DeterministicBrainStub
from voice.call.orchestrator import CallRuntime, CallSession
from voice.call.tailnet import TailnetPathMeasurement
from voice.call.tests.test_fake_e2e import (
    FakeSocket,
    FakeSTT,
    ImmediateEndpoint,
)
from voice.call.tts import DeterministicTTSStub


def test_tailnet_peer_normalization_is_cgnat_and_tailnet_ipv6_only() -> None:
    assert tailnet.normalize_tailnet_peer("100.64.0.1") == "100.64.0.1"
    assert tailnet.normalize_tailnet_peer("::ffff:100.127.255.255") == "100.127.255.255"
    assert tailnet.normalize_tailnet_peer("[fd7a:115c:a1e0::7]") == "fd7a:115c:a1e0::7"
    assert tailnet.normalize_tailnet_peer("100.128.0.1") is None
    assert tailnet.normalize_tailnet_peer("127.0.0.1") is None
    assert tailnet.normalize_tailnet_peer("phone.example.com") is None


@pytest.mark.parametrize(
    "current_address, relay, peer_relay, expected, evidence_fragment",
    [
        ("192.0.2.1:41641", "tor", "", "direct", "192.0.2.1:41641"),
        ("", "tor", "", "relay", "DERP(tor)"),
        ("", "tor", "100.64.0.9:4000:vni:7", "relay", "peer relay"),
    ],
)
def test_probe_reads_current_tailscale_route(
    monkeypatch,
    current_address: str,
    relay: str,
    peer_relay: str,
    expected: str,
    evidence_fragment: str,
) -> None:
    captured: list[str] = []
    status = {
        "Peer": {
            "node": {
                "TailscaleIPs": ["100.64.0.2"],
                "Active": True,
                "CurAddr": current_address,
                "Relay": relay,
                "PeerRelay": peer_relay,
            }
        }
    }

    class Process:
        returncode = 0

        async def communicate(self):
            return json.dumps(status).encode(), None

    async def create(*args, **kwargs):
        captured.extend(args)
        assert kwargs["stdout"] == asyncio.subprocess.PIPE
        return Process()

    monkeypatch.setattr(tailnet, "find_tailscale_cli", lambda: "/tailscale")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    measurement = asyncio.run(tailnet.probe_tailscale_path("100.64.0.2"))

    assert measurement.path == expected
    assert measurement.source == "tailscale_probe"
    assert evidence_fragment in measurement.evidence
    assert captured == ["/tailscale", "status", "--json"]


def test_call_pong_carries_server_measured_route(monkeypatch, tmp_path: Path) -> None:
    async def scenario() -> None:
        measurements = [
            TailnetPathMeasurement(
                "direct",
                "tailscale_probe",
                "active peer via 192.0.2.1:41641",
                time.monotonic_ns(),
            ),
            TailnetPathMeasurement(
                "relay",
                "tailscale_probe",
                "active peer via DERP(tor)",
                time.monotonic_ns(),
            ),
        ]

        async def probe(peer: str | None) -> TailnetPathMeasurement:
            assert peer == "100.64.0.2"
            return measurements.pop(0)

        monkeypatch.setattr(orchestrator, "probe_tailscale_path", probe)
        socket = FakeSocket()
        metrics = tmp_path / "tailnet-path.jsonl"
        runtime = CallRuntime(
            stt=FakeSTT(),
            brain=DeterministicBrainStub(),
            tts=DeterministicTTSStub(),
            endpoint_factory=ImmediateEndpoint,
            metrics_path=metrics,
        )
        session = CallSession(socket, runtime, peer_host="100.64.0.2")
        await session._handle_control(
            {"type": "call.start", "call_id": "path-call", "generation": 0}
        )
        assert session._tailnet_probe_task is not None
        await session._tailnet_probe_task

        await session._handle_control(
            {"type": "ping", "nonce": "ping-1", "sent_at_us": 123}
        )
        assert len(session._pending_rtt_probes) == 1
        sample_id, pending = next(iter(session._pending_rtt_probes.items()))
        assert sample_id != "ping-1"
        await pending.pong_finished.wait()

        controls = [
            json.loads(item) for item in socket.sent if isinstance(item, str)
        ]
        pong = next(item for item in controls if item["type"] == "pong")
        assert pong["nonce"] == "ping-1"
        assert pong["sample_id"] == sample_id
        assert pong["path"] == "relay"
        assert pong["path_source"] == "tailscale_probe"
        assert pong["sent_at_us"] == 123
        assert pong["server_processing_us"] >= 0

        await session._handle_control(
            {
                "type": "rtt.report",
                "rtt_ms": 25.0,
                "path": "direct",
                "path_source": "client_config",
                "sample_id": sample_id,
            }
        )
        for _ in range(20):
            rows = [json.loads(line) for line in metrics.read_text().splitlines()]
            if any(row["event"] == "network.rtt" for row in rows):
                break
            await asyncio.sleep(0.01)

        await session._handle_control(
            {
                "type": "rtt.report",
                "rtt_ms": 25.0,
                "path": "relay",
                "path_source": "tailscale_probe",
                "sample_id": sample_id,
            }
        )
        await session._handle_control(
            {
                "type": "rtt.report",
                "rtt_ms": 1.0,
                "path": "direct",
                "path_source": "client_config",
                "sample_id": "forged",
            }
        )
        rows = [json.loads(line) for line in metrics.read_text().splitlines()]
        assert any(
            row["event"] == "network.path_probe"
            and row["path"] == "relay"
            and row["path_source"] == "tailscale_probe"
            for row in rows
        )
        assert any(
            row["event"] == "network.rtt"
            and row["network_sample_id"] == sample_id
            and row["path"] == "relay"
            and row["path_source"] == "tailscale_probe"
            for row in rows
        )
        assert any(
            row["event"] == "network.rtt_rejected"
            and row["network_sample_id"] == sample_id
            and row["reason"] == "unknown_or_replayed_sample"
            for row in rows
        )
        assert any(
            row["event"] == "network.rtt_rejected"
            and row["network_sample_id"] == "forged"
            for row in rows
        )
        await session.close()

    asyncio.run(scenario())


def test_route_probe_does_not_block_call_control_worker(monkeypatch, tmp_path: Path) -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        calls = 0

        async def probe(peer: str | None) -> TailnetPathMeasurement:
            nonlocal calls
            calls += 1
            if calls > 1:
                await release.wait()
            return TailnetPathMeasurement(
                "direct",
                "tailscale_probe",
                "active peer via 192.0.2.1:41641",
                time.monotonic_ns(),
            )

        monkeypatch.setattr(orchestrator, "probe_tailscale_path", probe)
        runtime = CallRuntime(
            stt=FakeSTT(),
            brain=DeterministicBrainStub(),
            tts=DeterministicTTSStub(),
            endpoint_factory=ImmediateEndpoint,
            metrics_path=tmp_path / "nonblocking-path.jsonl",
        )
        session = CallSession(FakeSocket(), runtime, peer_host="100.64.0.2")
        await session._handle_control(
            {"type": "call.start", "call_id": "nonblocking", "generation": 0}
        )
        assert session._tailnet_probe_task is not None
        await session._tailnet_probe_task

        await asyncio.wait_for(
            session._handle_control(
                {"type": "ping", "nonce": "slow-route", "sent_at_us": 1}
            ),
            timeout=0.05,
        )
        await asyncio.wait_for(
            session._handle_control(
                {"type": "ping", "nonce": "slow-route-2", "sent_at_us": 2}
            ),
            timeout=0.05,
        )
        pending = list(session._pending_rtt_probes.values())
        assert len(pending) == 2
        assert pending[0].path_task is not pending[1].path_task
        assert not pending[0].pong_finished.is_set()
        assert not pending[1].pong_finished.is_set()
        await asyncio.sleep(0)
        assert calls == 3
        release.set()
        await asyncio.wait_for(
            asyncio.gather(*(item.pong_finished.wait() for item in pending)),
            timeout=1,
        )
        await session.close()

    asyncio.run(scenario())


def test_active_probe_limit_survives_early_reports(monkeypatch, tmp_path: Path) -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        calls = 0

        async def probe(peer: str | None) -> TailnetPathMeasurement:
            nonlocal calls
            calls += 1
            if calls > 1:
                await release.wait()
            return TailnetPathMeasurement(
                "direct",
                "tailscale_probe",
                "active peer via 192.0.2.1:41641",
                time.monotonic_ns(),
            )

        monkeypatch.setattr(orchestrator, "probe_tailscale_path", probe)
        socket = FakeSocket()
        runtime = CallRuntime(
            stt=FakeSTT(),
            brain=DeterministicBrainStub(),
            tts=DeterministicTTSStub(),
            endpoint_factory=ImmediateEndpoint,
            metrics_path=tmp_path / "bounded-path.jsonl",
        )
        session = CallSession(socket, runtime, peer_host="100.64.0.2")
        await session._handle_control(
            {"type": "call.start", "call_id": "bounded", "generation": 0}
        )
        assert session._tailnet_probe_task is not None
        await session._tailnet_probe_task

        for index in range(64):
            await session._handle_control(
                {"type": "ping", "nonce": f"flood-{index}", "sent_at_us": index}
            )
        await asyncio.sleep(0)
        accepted = list(session._pending_rtt_probes.items())
        for sample_id, _ in accepted:
            await session._handle_control(
                {
                    "type": "rtt.report",
                    "rtt_ms": 1.0,
                    "path": "direct",
                    "path_source": "client_config",
                    "sample_id": sample_id,
                }
            )
        assert not session._pending_rtt_probes

        for index in range(64, 130):
            await session._handle_control(
                {"type": "ping", "nonce": f"flood-{index}", "sent_at_us": index}
            )
        await asyncio.sleep(0)

        unresolved = [
            task
            for task in session._background
            if task.get_name() == "call-tailnet-path-sample" and not task.done()
        ]
        controls = [
            json.loads(item) for item in socket.sent if isinstance(item, str)
        ]
        assert not session._pending_rtt_probes
        assert len(session._tailnet_sample_tasks) == 64
        assert calls == 65
        assert len(unresolved) == 64
        assert sum(
            item.get("type") == "error" and item.get("code") == "rtt_busy"
            for item in controls
        ) == 66

        release.set()
        await asyncio.wait_for(
            asyncio.gather(
                *(
                    pending.pong_finished.wait()
                    for _, pending in accepted
                )
            ),
            timeout=1,
        )
        await session.close()

    asyncio.run(scenario())
