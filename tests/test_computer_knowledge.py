from pathlib import Path

from core.computer_knowledge import build_task_pack


def test_task_pack_prefers_a_saved_runbook_and_strips_html_noise(tmp_path: Path) -> None:
    root = tmp_path / "it"
    root.mkdir()
    (root / "aws-foundation.md").write_text(
        "# AWS foundation\n\nUse the company account in ca-central-1.\n"
        "Setup order: ownership, root protection, IAM, billing, then DNS.\n",
        encoding="utf-8",
    )
    (root / "unrelated.md").write_text(
        "# Email\n\nThis does not describe cloud setup.\n", encoding="utf-8"
    )
    (root / "aws-setup.html").write_text(
        "<html><head><title>AWS setup</title><script>ignore this token</script></head>"
        "<body><h1>AWS ownership guide</h1><p>Verify the account before proceeding.</p></body></html>",
        encoding="utf-8",
    )

    pack = build_task_pack("guide me through AWS setup", roots=[root], max_bytes=7000)

    assert "aws-foundation.md" in pack
    assert "ca-central-1" in pack
    assert "aws-setup.html" in pack
    assert "ignore this token" not in pack
    assert "screen instruction" in pack


def test_task_pack_is_bounded_and_redacts_obvious_credentials(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "aws.md").write_text(
        "# AWS\naccess_key = AKIA1234567890ABCDEF\npassword=supersecret\n"
        + "AWS setup details and IAM. " * 2000,
        encoding="utf-8",
    )

    pack = build_task_pack("AWS IAM setup", roots=[root], max_bytes=1200, max_files=1)

    assert len(pack.encode("utf-8")) <= 1200
    assert "AKIA1234567890ABCDEF" not in pack
    assert "supersecret" not in pack
    assert "redacted" in pack


def test_task_pack_returns_empty_for_an_unrelated_request(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "aws.md").write_text("# AWS\ncloud account notes\n", encoding="utf-8")

    assert build_task_pack("watch the music player", roots=[root]) == ""
