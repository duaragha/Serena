import json
from pathlib import Path

import pytest

from core.workspace_claude_runtime import runtime_paths


def provision(tmp_path):
    runtime = tmp_path / "runtimes" / "claude-sdk"
    sdk = runtime / "node_modules" / "@anthropic-ai" / "claude-agent-sdk" / "sdk.mjs"
    sdk.parent.mkdir(parents=True)
    sdk.write_text("export {};", encoding="utf-8")
    (runtime / "package.json").write_text(json.dumps({"dependencies": {"@anthropic-ai/claude-agent-sdk": "0.3.266"}}))
    (sdk.parent / "package.json").write_text(json.dumps({"name": "@anthropic-ai/claude-agent-sdk", "version": "0.3.266"}))
    return runtime, sdk


def test_resolves_pinned_dependency_without_launch(monkeypatch, tmp_path):
    _, sdk = provision(tmp_path)
    monkeypatch.setattr("core.workspace_claude_runtime.shutil.which", lambda name, path: "/native/node")
    assert runtime_paths(root=tmp_path, env={}) == (sdk, Path("/native/node").resolve())


def test_missing_or_wrong_sdk_fails_without_fallback(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="SDK is missing"):
        runtime_paths(root=tmp_path, env={})
    _, sdk = provision(tmp_path)
    (sdk.parent / "package.json").write_text(json.dumps({"name": "@anthropic-ai/claude-agent-sdk", "version": "0.0.0"}))
    with pytest.raises(RuntimeError, match="pinned"):
        runtime_paths(root=tmp_path, env={})


def test_explicit_runtime_location_and_node_are_honored(monkeypatch, tmp_path):
    runtime, sdk = provision(tmp_path)
    calls = []

    def which(name, path):
        calls.append((name, path))
        return "/packaged/node" if name == "/packaged/node" else None

    monkeypatch.setattr("core.workspace_claude_runtime.shutil.which", which)
    result = runtime_paths(root=tmp_path / "missing", env={"SERENA_WORKSPACE_RUNTIME_ROOT": str(runtime),
                                                          "SERENA_WORKSPACE_NODE": "/packaged/node", "PATH": "/known"})
    assert result[0] == sdk
    assert calls == [("/packaged/node", "/known")]
    with pytest.raises(RuntimeError, match="Node runtime"):
        runtime_paths(root=tmp_path, env={"SERENA_WORKSPACE_NODE": "/missing"})
