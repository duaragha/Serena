"""Per-unit delivery scoping: library/tests/docs-only units owe no delivery."""

from __future__ import annotations

import pytest

from fleet.contracts import _completion_contract, build_work_unit_contracts


def _unit(title, description, declared_paths=()):
    return _completion_contract(
        "coding", description, title=title, declared_paths=list(declared_paths)
    )


@pytest.mark.parametrize(
    "text",
    [
        "restart the auth service",
        "scale the microservices",
        "deploy the new build",
        "the deployment failed last night",
        "redeploying after the rollback",
        "add a /health endpoint",
        "document the API endpoints",
        "add a login route",
        "fix the message bridge",
        "bridging to the legacy queue",
        "ship the release",
        "rebuild the docker image",
        "apply the terraform plan",
        "promote to production",
        "verify the live surface",
    ],
)
def test_deploy_signals_keep_all_three_requirements(text):
    contract = _unit("work", text)
    assert len(contract["delivery_requirements"]) == 3
    assert contract["delivery_scope"]["deliverable"] is True
    assert contract["delivery_scope"]["signals"]
    assert contract["delivery_scope"]["reason"]


@pytest.mark.parametrize(
    "path",
    [
        "Dockerfile",
        "docker/Dockerfile.dev",
        "docker-compose.yml",
        "infra/main.tf",
        "units/serena.service",
        "scripts/deploy.sh",
        "k8s/deployment.yaml",
        "helm/chart/values.yaml",
        "Procfile",
        "fly.toml",
    ],
)
def test_deploy_paths_keep_all_three_requirements(path):
    contract = _unit("work", f"own only {path}", declared_paths=[path])
    assert len(contract["delivery_requirements"]) == 3
    assert contract["delivery_scope"]["deliverable"] is True


@pytest.mark.parametrize(
    "paths",
    [
        ["fleet/parser.py"],
        ["core/token.py", "core/lex.py"],
        ["README.md"],
        ["docs/guide.md", "docs/ops.md"],
        ["tests/test_parser.py"],
        ["tests/test_a.py", "tests/test_b.py"],
        ["docs/guide.md", "tests/test_docs.py"],
    ],
)
def test_only_paths_scope_delivery_out_with_auditable_reason(paths):
    contract = _unit("work", f"own only {', '.join(paths)}", declared_paths=paths)
    assert contract["delivery_requirements"] == []
    scope = contract["delivery_scope"]
    assert scope["deliverable"] is False
    assert "no deployable surface" in scope["reason"]
    assert scope["signals"] == []


@pytest.mark.parametrize(
    "text",
    [
        "Fix a typo in the onboarding guide",
        "Update the README with the new flags",
        "Write documentation for the config file",
        "Document the retry behavior",
        "Add unit tests for the parser",
        "Extend test coverage for the lexer",
        "Write regression tests for the outage",
        "Refactor the token parser",
        "Extract a helper module for dates",
        "Pure function cleanup, no functional changes",
        "Changelog refresh, docs only",
        "Update the user guide",
    ],
)
def test_only_framing_scopes_delivery_out_without_paths(text):
    contract = _unit("work", text)
    assert contract["delivery_requirements"] == []
    assert contract["delivery_scope"]["deliverable"] is False
    assert "no deployable surface" in contract["delivery_scope"]["reason"]


@pytest.mark.parametrize(
    "text",
    [
        "the first bounded unit",
        "implement bounded behavior",
        "Fix the parser bug",
        "Add a feature to the dashboard",
        "Read the README then implement the fix",
        "Fix the parser, see README for context",
        "test",
    ],
)
def test_ambiguous_units_fail_closed_with_delivery_owed(text):
    contract = _unit("work", text)
    assert len(contract["delivery_requirements"]) == 3
    assert contract["delivery_scope"]["deliverable"] is True
    assert "fails closed" in contract["delivery_scope"]["reason"]


def test_docs_prose_never_fires_a_deploy_signal():
    contract = _unit("work", "Write documentation for the config file")
    assert contract["delivery_scope"]["signals"] == []
    assert contract["delivery_requirements"] == []


def test_deploy_signal_beats_docs_framing():
    contract = _unit("work", "Deploy the docs site to production")
    assert len(contract["delivery_requirements"]) == 3


def test_research_contracts_unchanged():
    contract = _completion_contract("research", "survey the deploy options")
    assert contract["delivery_requirements"] == []
    assert contract["delivery_scope"]["deliverable"] is False


def test_empty_description_keeps_backward_compatible_requirements():
    assert len(_completion_contract("coding", "")["delivery_requirements"]) == 3


def test_build_contracts_scopes_each_unit_independently(tmp_path):
    (tmp_path / "fleet").mkdir()
    (tmp_path / "fleet" / "parser.py").write_text("x = 1\n")
    workstreams = [
        {"id": "ws-1", "title": "lib", "description": "Own only fleet/parser.py."},
        {
            "id": "ws-2",
            "title": "svc",
            "description": "Deploy the auth service to production.",
        },
    ]
    workers = [
        {"worker_key": "agent:a", "assignment_ids": ["ws-1"]},
        {"worker_key": "agent:b", "assignment_ids": ["ws-2"]},
    ]
    units = build_work_unit_contracts("coding", workstreams, workers, cwd=tmp_path)
    by_id = {unit["id"]: unit for unit in units}
    assert by_id["ws-1"]["completion_contract"]["delivery_requirements"] == []
    assert by_id["ws-1"]["completion_contract"]["delivery_scope"]["deliverable"] is False
    assert len(by_id["ws-2"]["completion_contract"]["delivery_requirements"]) == 3
    # Acceptance, evidence, and constraints are untouched by scoping.
    for unit in units:
        completion = unit["completion_contract"]
        assert len(completion["acceptance_criteria"]) == 3
        assert completion["required_evidence"]
        assert completion["constraints"]
        assert completion["stop_conditions"]
