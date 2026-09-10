"""Exercise native installation diagnostics in an unsigned disposable profile."""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.workspace_diagnostics import claude_doctor


async def main():
    binary = shutil.which("claude")
    assert binary, "Installed Claude CLI required"
    with tempfile.TemporaryDirectory(prefix="serena-diagnostics-proof-") as directory:
        root = Path(directory)
        env = {**os.environ, "HOME": str(root), "USERPROFILE": str(root),
               "CLAUDE_CONFIG_DIR": str(root / "config")}
        result = await claude_doctor(binary, root, env)
        assert result["command"] == "claude doctor" and result["exitCode"] == 0, result
        assert "Claude Code doctor" in result["output"] and "Running:" in result["output"], result
        print(json.dumps(result, ensure_ascii=True))
    print("PASS: native doctor completed with exit 0 through bounded runner; unsigned temporary profile removed; no coding session or repair started")


if __name__ == "__main__":
    asyncio.run(main())
