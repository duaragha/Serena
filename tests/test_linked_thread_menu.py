"""Linked-thread menu in the real sidebar renderer; no provider launches."""

from tests.test_workspace_browser import workspace  # noqa: F401  (shared fixture)


def test_any_single_member_can_leave_a_linked_thread_of_three(workspace):  # noqa: F811
    """A linked thread shows as one row, so "Unlink this" could only remove the
    member heading it. Each member now has its own removal entry."""
    page, calls, errors, rows = workspace
    # Whichever member heads the folded thread is the only row to click.
    row = page.locator('.session-row[data-sid^="00000000-0000-4000-8000-"]').first
    row.click(button="right")
    menu = page.locator(".ctx-menu-item")
    labels = menu.all_inner_texts()
    for agent in ("Claude", "Codex", "Gemini"):
        assert any(label.startswith(f"Remove {agent} from thread") for label in labels), labels
    assert not any(label.startswith("Unlink this") for label in labels)
    gemini = rows[2]["session_id"]
    with page.expect_request(lambda req: req.url.endswith("/api/group/unlink")) as request:
        menu.filter(has_text="Remove Gemini from thread").click()
    assert request.value.post_data_json == {"session_id": gemini}
    assert not errors
