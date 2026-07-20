from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from voice.call.process_worker import (
    CancellableModelProcess,
    ModelProcessError,
    _trusted_bubblewrap,
)


def test_cancel_terminates_obsolete_model_work_before_next_request() -> None:
    async def scenario() -> None:
        worker = CancellableModelProcess("test", {})
        try:
            first_meta = await worker.warm()
            first_pid = first_meta["pid"]
            obsolete = asyncio.create_task(
                worker.request(
                    {"delay": 5.0, "value": "obsolete"}, generation=1
                )
            )
            for _ in range(100):
                if worker._active_generation == 1:
                    break
                await asyncio.sleep(0.01)
            assert worker._active_generation == 1

            started = time.monotonic()
            assert await worker.cancel(1)
            with pytest.raises(ModelProcessError, match="stopped during inference"):
                await obsolete
            assert time.monotonic() - started < 2.0

            response = await worker.request(
                {"delay": 0.0, "value": "current"}, generation=2
            )
            assert response["value"] == "current"
            assert worker.metadata["pid"] != first_pid
        finally:
            worker.close()

    asyncio.run(scenario())


def test_model_readiness_timeout_terminates_hung_child() -> None:
    async def scenario() -> None:
        worker = CancellableModelProcess(
            "test",
            {"ready_delay": 5.0},
            startup_timeout=1.0,
        )
        started = time.monotonic()
        try:
            with pytest.raises(ModelProcessError, match="readiness timed out"):
                await worker.warm()
            assert time.monotonic() - started < 2.5
            assert not worker.warmed
        finally:
            worker.close()

    asyncio.run(scenario())


def test_model_worker_can_use_a_dedicated_python_without_package_bootstrap() -> None:
    async def scenario() -> None:
        worker = CancellableModelProcess(
            "test",
            {},
            python_executable=sys.executable,
        )
        try:
            metadata = await worker.warm()
            assert metadata["pid"] > 0
            response = await worker.request(
                {"delay": 0.0, "value": "external"}, generation=1
            )
            assert response["value"] == "external"
        finally:
            worker.close()

    asyncio.run(scenario())


def test_dedicated_model_python_rejects_a_wrapper(tmp_path: Path) -> None:
    wrapper = tmp_path / "python"
    wrapper.write_text("#!/bin/sh\nexec python3 \"$@\"\n", encoding="utf-8")
    wrapper.chmod(0o700)

    with pytest.raises(ValueError, match="same trusted Python binary"):
        CancellableModelProcess(
            "test",
            {},
            python_executable=wrapper,
        )


@pytest.mark.skipif(
    sys.platform.startswith("linux") and _trusted_bubblewrap() is None,
    reason="Linux local-model isolation requires bubblewrap",
)
def test_network_disabled_worker_attests_and_blocks_inet_sockets() -> None:
    worker = CancellableModelProcess(
        "test",
        {"probe_network": True},
        network_disabled=True,
    )
    try:
        metadata = worker.warm_sync()
        assert metadata["network_blocked"] is True
        assert metadata["network_isolation"] != "none"
        assert worker.network_isolation == metadata["network_isolation"]
    finally:
        worker.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group assertion")
def test_model_worker_close_kills_its_descendant_group(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "model-child.pid"
    worker = CancellableModelProcess(
        "test",
        {"child_pid_file": str(child_pid_file)},
    )
    metadata = worker.warm_sync()
    child_pid = int(metadata["child_pid"])
    assert child_pid_file.read_text(encoding="ascii") == str(child_pid)

    worker.close()

    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        try:
            if Path(f"/proc/{child_pid}/stat").read_text().split()[2] == "Z":
                break
        except (OSError, IndexError):
            break
        time.sleep(0.02)
    else:
        os.kill(child_pid, 9)
        pytest.fail(f"model descendant {child_pid} survived worker close")


def test_model_inference_timeout_terminates_hung_child_and_restarts() -> None:
    async def scenario() -> None:
        worker = CancellableModelProcess(
            "test",
            {},
            inference_timeout=0.1,
        )
        try:
            first_pid = (await worker.warm())["pid"]
            with pytest.raises(ModelProcessError, match="inference timed out"):
                await worker.request(
                    {"delay": 5.0, "value": "hung"}, generation=1
                )
            assert not worker.warmed

            response = await worker.request(
                {"delay": 0.0, "value": "current"}, generation=2
            )
            assert response["value"] == "current"
            assert worker.metadata["pid"] != first_pid
        finally:
            worker.close()

    asyncio.run(scenario())


def test_cancelled_queued_request_never_reaches_inference() -> None:
    async def scenario() -> None:
        worker = CancellableModelProcess("test", {})
        try:
            active = asyncio.create_task(
                worker.request({"delay": 0.5, "value": "active"}, generation=10)
            )
            for _ in range(100):
                if worker._active_generation == 10:
                    break
                await asyncio.sleep(0.01)
            queued = asyncio.create_task(
                worker.request({"delay": 5.0, "value": "queued"}, generation=20)
            )
            for _ in range(100):
                if 20 in worker._pending_generations:
                    break
                await asyncio.sleep(0.01)
            assert await worker.cancel(20)
            assert (await active)["value"] == "active"
            with pytest.raises(ModelProcessError, match="canceled before inference"):
                await queued

            current = await worker.request(
                {"delay": 0.0, "value": "current"}, generation=30
            )
            assert current["value"] == "current"
        finally:
            worker.close()

    asyncio.run(scenario())


def test_network_isolated_worker_survives_across_asyncio_event_loops() -> None:
    if _trusted_bubblewrap() is None:
        pytest.skip("bubblewrap is unavailable")
    worker = CancellableModelProcess("test", {}, network_disabled=True)
    try:
        asyncio.run(worker.warm())
        process = worker._process
        assert process is not None
        pid = process.pid

        # Per-websocket event loops retire their default executor on close.
        # The model's dedicated owner thread must keep bubblewrap alive.
        time.sleep(0.05)
        assert worker.warmed is True
        response = asyncio.run(
            worker.request({"value": "still-warm"}, generation=1)
        )

        assert response["value"] == "still-warm"
        assert worker._process is not None
        assert worker._process.pid == pid
    finally:
        worker.close()
