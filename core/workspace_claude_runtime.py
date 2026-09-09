"""Resolve the provisioned public SDK without installing or launching anything."""
import json
import os
import shutil
from functools import partial
from pathlib import Path


def runtime_paths(*, root=None, env=None):
    env = os.environ if env is None else env
    root = Path(root or Path(__file__).resolve().parents[1])
    runtime = Path(env.get("SERENA_WORKSPACE_RUNTIME_ROOT") or root / "runtimes" / "claude-sdk")
    sdk = runtime / "node_modules" / "@anthropic-ai" / "claude-agent-sdk" / "sdk.mjs"
    try:
        manifest = json.loads((runtime / "package.json").read_text(encoding="utf-8"))
        installed = json.loads((sdk.parent / "package.json").read_text(encoding="utf-8"))
        expected = manifest["dependencies"]["@anthropic-ai/claude-agent-sdk"]
        if installed["name"] != "@anthropic-ai/claude-agent-sdk" or installed["version"] != expected or not sdk.is_file():
            raise ValueError("SDK identity mismatch")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError("Claude workspace SDK is missing or does not match its pinned runtime dependency") from error
    configured = env.get("SERENA_WORKSPACE_NODE")
    node = shutil.which(configured or "node", path=env.get("PATH", ""))
    if not node:
        raise RuntimeError("Claude workspace requires its provisioned Node runtime")
    return sdk.resolve(), Path(node).resolve()


def client_factory(**kwargs):
    from core.workspace_claude_client import ClaudeTypeScriptClient

    sdk, node = runtime_paths(**kwargs)
    return partial(ClaudeTypeScriptClient, sdk_path=sdk, node_path=node)
