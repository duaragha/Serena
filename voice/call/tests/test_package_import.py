from __future__ import annotations

import subprocess
import sys


def test_package_import_does_not_eagerly_load_call_runtime() -> None:
    script = (
        "import sys; import voice.call; "
        "assert 'voice.call.orchestrator' not in sys.modules; "
        "assert 'core.work_jobs' not in sys.modules"
    )

    subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=True,
        cwd=".",
    )


def test_runtime_exports_still_load_lazily() -> None:
    import voice.call

    assert callable(voice.call.handle_websocket)
    assert callable(voice.call.warm_default_runtime_background)
