import asyncio
import os
import sys

import pytest

from core.workspace_diagnostics import claude_doctor


@pytest.mark.skipif(os.name == "nt", reason="POSIX subprocess fixture; Windows uses the separately tested job gate")
@pytest.mark.parametrize("case", ["output", "timeout", "overflow"])
def test_diagnostics_are_bounded_redacted_and_reaped(tmp_path, monkeypatch, case):
    async def run():
        original = asyncio.create_subprocess_exec
        processes = []
        code = {"output": "print('OPENAI_API_KEY=sk-private');raise SystemExit(4)",
                "timeout": "import time;time.sleep(60)", "overflow": "print('x'*2000)"}[case]

        async def spawn(*command, **options):
            assert command == ("/native/claude", "doctor")
            assert not options["env"].get("OPENAI_API_KEY")
            assert options["env"]["CI"] == "1"
            process = await original(sys.executable, "-c", code, **options)
            processes.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        if case == "output":
            result = await claude_doctor("/native/claude", tmp_path, {**os.environ, "OPENAI_API_KEY": "sk-private"})
            assert result["exitCode"] == 4 and "sk-private" not in result["output"]
            assert "[REDACTED]" in result["output"]
        else:
            with pytest.raises(RuntimeError, match="timed out" if case == "timeout" else "output limit"):
                await claude_doctor("/native/claude", tmp_path, os.environ, timeout=0.2 if case == "timeout" else 5, max_bytes=100)
        assert len(processes) == 1 and processes[0].returncode is not None

    asyncio.run(run())
