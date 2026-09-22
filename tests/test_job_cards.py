"""One Telegram card per job: posted once, edited quietly, re-posted at the end."""

from __future__ import annotations

import pytest

from memory import store


@pytest.fixture
def line(tmp_path, monkeypatch):
    from core import job_cards, telegram_line

    monkeypatch.setattr(store, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(job_cards, "enabled", lambda: True)
    calls = []
    ids = iter(range(100, 200))

    def send_text(text, **kw):
        calls.append(("send", text, kw))
        return next(ids)

    monkeypatch.setattr(telegram_line, "send_text", send_text)
    monkeypatch.setattr(telegram_line, "edit_text",
                        lambda mid, text, **kw: calls.append(("edit", mid, text, kw)) or True)
    monkeypatch.setattr(telegram_line, "delete_message",
                        lambda mid: calls.append(("delete", mid)) or True)
    return calls


def _run(*states):
    names = ("discover", "execute", "verify", "finalize")
    return {"phases": [{"name": n, "state": s} for n, s in zip(names, states)]}


LABEL = "In Unified (liquid glass ui in chats)"


def test_render_shows_project_steps_and_buttons():
    from core import job_cards

    text, buttons = job_cards.render(87, LABEL, _run("completed", "running", "", ""), "running")
    assert text.splitlines()[0] == "🟡 <b>#87 · Unified</b>"
    assert "liquid glass ui in chats" in text
    assert "✅ research   ⏳ <b>code</b>   ▫️ review   ▫️ fixes" in text
    assert buttons == []

    text, buttons = job_cards.render(87, LABEL, None, "failed", reason="tests <failed>")
    assert text.startswith("🔴 <b>#87 · Unified</b>  —  failed")
    assert "<blockquote expandable>tests &lt;failed&gt;</blockquote>" in text
    assert buttons == [[{"text": "🔁 retry", "callback_data": "retry:87", "style": "success"}]]

    text, buttons = job_cards.render(87, LABEL, None, "merged",
                                     url="https://github.com/o/r/pull/9")
    assert "✅ fixes" in text and "merged" in text.splitlines()[0]
    assert buttons == [[{"text": "open PR", "url": "https://github.com/o/r/pull/9",
                         "style": "primary"}]]


def test_a_job_buzzes_on_start_and_end_and_edits_quietly_between(line):
    from core import job_cards

    task = {"id": 5}
    assert job_cards.show(task, _run("running", "", "", ""), "running", LABEL)
    assert job_cards.show(task, _run("running", "", "", ""), "running", LABEL)  # unchanged
    assert job_cards.show(task, _run("completed", "running", "", ""), "running", LABEL)
    assert job_cards.show(task, None, "merged", LABEL, url="https://github.com/o/r/pull/1")

    kinds = [call[0] for call in line]
    assert kinds == ["send", "edit", "send", "delete"]
    first, edit, final, delete = line
    assert first[2]["topic"] == "jobs" and not first[2].get("silent")
    assert edit[1] == 100  # the same message, rewritten
    assert final[2]["topic"] == "jobs" and not final[2].get("silent")
    assert delete == ("delete", 100)  # never two cards for one job


def test_cards_step_aside_when_the_line_cannot_edit(tmp_path, monkeypatch):
    from core import job_cards

    monkeypatch.setattr(store, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(job_cards, "enabled", lambda: False)
    assert job_cards.show({"id": 1}, None, "running", LABEL) is False
