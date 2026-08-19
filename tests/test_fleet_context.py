from __future__ import annotations

from core.fleet_context import budget_context, redact_text, redact_value
from core.fleet_policy import build_policy, builtin_config
from core.fleet_store import FleetStore


def test_secret_filter_covers_headers_tokens_environment_and_private_keys():
    source = """Authorization: Bearer abc.def.ghi
OPENAI_API_KEY=sk-example-value
github_pat_abcdefghijklmnopqrstuvwxyz123456
-----BEGIN PRIVATE KEY-----
not-real-secret
-----END PRIVATE KEY-----"""

    redacted, count = redact_text(source)

    assert count == 4
    assert "abc.def.ghi" not in redacted
    assert "sk-example-value" not in redacted
    assert "github_pat_" not in redacted
    assert "not-real-secret" not in redacted
    assert redacted.count("[redacted:") == 4

    payload, field_count = redact_value(
        {
            "api_token": "unstructured-value",
            "nested": {"password": "short"},
            "token_budget": 8_000,
            "output_tokens": 900,
        }
    )
    assert field_count == 2
    assert payload == {
        "api_token": "[redacted:sensitive_field]",
        "nested": {"password": "[redacted:sensitive_field]"},
        "token_budget": 8_000,
        "output_tokens": 900,
    }


def test_secret_filter_covers_provider_keys_and_inline_assignments_without_metadata_noise():
    source = """provider key sk-proj-0123456789abcdefghijklmnop
anthropic sk-ant-api03-0123456789abcdefghijklmnop
request token=plain-secret-value
token_budget=8000
output_tokens=900"""

    redacted, count = redact_text(source)

    assert count == 3
    assert "sk-proj-" not in redacted
    assert "sk-ant-api03-" not in redacted
    assert "plain-secret-value" not in redacted
    assert "token_budget=8000" in redacted
    assert "output_tokens=900" in redacted


def test_context_budget_is_inspectable_and_preserves_authoritative_history():
    secret = "Authorization: Bearer hidden-token"
    text, receipt = budget_context(
        [("worker-a", "a" * 4_000 + "\n" + secret), ("worker-b", "b" * 4_000)],
        budget_chars=3_000,
    )

    assert len(text) <= 3_000
    assert "hidden-token" not in text
    assert receipt.strategy == "bounded_excerpts"
    assert receipt.omitted_chars > 0
    assert receipt.redaction_count == 1
    assert receipt.full_history_preserved is True
    assert len(receipt.source_sha256) == 64


def test_context_receipt_is_durable_and_visible_on_attempt_status(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    policy = build_policy(
        "research",
        "inspect the bounded source",
        config=builtin_config(),
        provider_mode="codex",
        worker_count=1,
    ).to_dict()
    run = store.create_run(
        task="inspect the bounded source",
        activity="research",
        cwd=str(tmp_path),
        origin_session_id=None,
        origin_agent="codex",
        dry_run=False,
        policy=policy,
    )
    attempt = store.begin_attempt(run["phases"][0]["legs"][0]["leg_id"])
    _text, receipt = budget_context([("source", "safe")], budget_chars=2_000)
    store.record_context_receipt(attempt["attempt_id"], receipt.to_dict())

    reopened = FleetStore(tmp_path / "fleet.sqlite3")
    snapshot = reopened.get_run(run["run_id"])
    assert snapshot is not None
    saved = snapshot["phases"][0]["legs"][0]["current_attempt"]["context_receipt"]
    assert saved["strategy"] == "full"
    assert saved["source_sha256"] == receipt.source_sha256
    assert saved["full_history_preserved"] is True


def test_weighted_sources_get_proportional_share_of_a_tight_budget():
    """A related peer keeps more of the window than an unrelated one."""

    related = "R" * 40_000
    unrelated = "U" * 40_000
    delivered, receipt = budget_context(
        [("related peer", related), ("unrelated peer", unrelated)],
        budget_chars=20_000,
        weights=[4.0, 1.0],
    )

    assert receipt.strategy == "bounded_excerpts"
    assert delivered.count("R") > delivered.count("U") * 2
    # The omission is still recorded honestly against the full source text.
    assert receipt.omitted_chars > 0
    assert receipt.full_history_preserved is True


def test_missing_weights_keep_the_previous_equal_split():
    left = "L" * 40_000
    right = "G" * 40_000
    delivered, _receipt = budget_context(
        [("left", left), ("right", right)], budget_chars=20_000
    )

    # Equal split, give or take the label lengths in each section header.
    assert abs(delivered.count("L") - delivered.count("G")) <= 10
