"""Regression for HYD-03's clean integration checkout missing codegen output."""

import json
import os
import sys

import pytest

from fleet.isolation import _generated_types_preparation, run_test_gates


def test_generated_types_are_rebuilt_and_original_failure_is_retained(tmp_path):
    npm = tmp_path / "npm"
    npm.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        "p = pathlib.Path('storefrontapi.generated')\n"
        "if sys.argv[1:] == ['--ignore-scripts', 'run', 'codegen']:\n"
        "    p.write_text('generated locally')\n"
        "    sys.exit(0)\n"
        "if not p.exists():\n"
        "    print(\"app.ts: error TS2307: Cannot find module 'storefrontapi.generated' or its corresponding type declarations.\")\n"
        "    sys.exit(2)\n"
    )
    npm.chmod(0o755)
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"codegen": "local generator"}}))
    if os.name == "nt":
        pytest.skip("POSIX executable fixture")
    gate = run_test_gates(tmp_path, [[str(npm), "run", "typecheck"]])
    assert gate["ok"] is True
    receipt = gate["results"][0]["prerequisite_recovery"]
    assert receipt["original_failure"]["exit_code"] == 2
    assert receipt["preparation"]["exit_code"] == 0
    assert receipt["rechecked"] is True


@pytest.mark.parametrize("output", [
    "error TS2322: Type 'number' is not assignable to type 'string'.",
    "error TS2307: Cannot find module 'graphql'",
    "error TS2307: Cannot find module 'some-generated-package'",
])
def test_unrelated_failures_do_not_trigger_codegen(tmp_path, output):
    (tmp_path / "package.json").write_text('{"scripts":{"codegen":"anything"}}')
    assert _generated_types_preparation(tmp_path, ["npm", "run", "typecheck"], {
        "ok": False, "exit_code": 2, "output_tail": output,
    }) is None


@pytest.mark.parametrize("preparation_ok", [True, False])
def test_recovery_is_bounded_and_never_hides_persistent_failure(tmp_path, monkeypatch, preparation_ok):
    (tmp_path / "package.json").write_text('{"scripts":{"codegen":"generator"}}')
    calls = []

    def run(root, command, **kwargs):
        calls.append(command)
        if "codegen" in command:
            return {"ran": True, "ok": preparation_ok, "exit_code": 0 if preparation_ok else 1}
        return {"ran": True, "ok": False, "exit_code": 2,
                "output_tail": "error TS2307: Cannot find module 'storefrontapi.generated'"}

    monkeypatch.setattr("fleet.isolation.run_test_gate", run)
    gate = run_test_gates(tmp_path, [["npm", "run", "typecheck"], ["npm", "run", "lint"]])
    assert gate["ok"] is False
    assert len(calls) == (3 if preparation_ok else 2)
    assert gate["results"][0]["prerequisite_recovery"]["rechecked"] is preparation_ok


def test_missing_codegen_script_does_not_invent_preparation(tmp_path):
    assert _generated_types_preparation(tmp_path, ["npm", "run", "typecheck"], {
        "ok": False, "exit_code": 2,
        "output_tail": "error TS2307: Cannot find module 'storefrontapi.generated'",
    }) is None
