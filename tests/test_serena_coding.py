"""Her own coding sessions: finding his app, briefing, tracking, reporting."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

from core import serena_coding as sc


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "STATE_PATH", tmp_path / "state" / "serena-coding.json")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SERENA_CODING_BACKEND", raising=False)
    (tmp_path / "home").mkdir()
    # Never text him from a test.
    fake_line = types.SimpleNamespace(sent=[], send=lambda text, *, key="", topic="": (
        fake_line.sent.append(text) or True))
    monkeypatch.setitem(sys.modules, "core.phone_line", fake_line)
    monkeypatch.setattr(sys.modules["core"], "phone_line", fake_line, raising=False)


class _Proc:
    def __init__(self, cmdline, created):
        self.info = {"cmdline": cmdline, "create_time": created}


def _fake_psutil(monkeypatch, procs):
    monkeypatch.setitem(sys.modules, "psutil",
                        types.SimpleNamespace(process_iter=lambda _attrs: iter(procs)))


def test_backend_prefers_the_installed_apps_own_sidecar(monkeypatch):
    _fake_psutil(monkeypatch, [
        _Proc(["python", "-m", "sidecar", "--port", "9000"], 300),
        _Proc(["/tmp/.mount_SerenaXYZ/resources/sidecar/sidecar", "--port", "40277"], 100),
        _Proc(["bash"], 50),
    ])
    assert sc.backend_url() == "http://127.0.0.1:40277"


class _EnvProc(_Proc):
    def __init__(self, cmdline, created, env):
        super().__init__(cmdline, created)
        self._env = env

    def environ(self):
        return self._env


def test_backend_prefers_the_stable_app_over_serena_dev(monkeypatch):
    # Dev opens chats in structured panes that cannot attach to her terminal.
    _fake_psutil(monkeypatch, [
        _EnvProc(["/tmp/.mount_SerenaAb/resources/sidecar/serena-web-sidecar", "--port", "41000"],
                 500, {"SERENA_DESKTOP_CHANNEL": "dev", "SERENA_STRUCTURED_WORKSPACE": "1"}),
        _EnvProc(["/tmp/.mount_SerenaCd/resources/sidecar/serena-web-sidecar", "--port", "40277"],
                 100, {"SERENA_DESKTOP_CHANNEL": "stable", "SERENA_STRUCTURED_WORKSPACE": "0"}),
    ])
    assert sc.backend_url() == "http://127.0.0.1:40277"


def test_backend_falls_back_to_the_mobile_host(monkeypatch):
    _fake_psutil(monkeypatch, [_Proc(["bash"], 1)])
    assert sc.backend_url() == sc.FALLBACK_BACKEND


def test_backend_env_override_wins(monkeypatch):
    monkeypatch.setenv("SERENA_CODING_BACKEND", "http://127.0.0.1:5555/")
    assert sc.backend_url() == "http://127.0.0.1:5555"


def test_brief_carries_task_tag_repo_and_the_report_contract(tmp_path):
    text = sc.brief("Make the orb show thinking while the brain answers.",
                    repo=tmp_path, tag="serena-task-abc")
    assert text.startswith("[serena-task-abc]")
    assert "Make the orb show thinking" in text
    assert str(tmp_path) in text
    for marker in sc.DONE_MARKERS:
        assert marker in text
    assert "-m core.serena_coding notify" in text
    assert "/fleet" in text and "ask-codex" in text


def _claude_transcript(cwd: Path, name: str, first_text: str) -> Path:
    folder = sc._claude_project_dir(cwd)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"role": "user",
                                                             "content": first_text}}) + "\n")
    return path


def test_transcript_is_found_by_its_tag_not_by_recency(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    started = time.time()
    _claude_transcript(repo, "11111111-aaaa", "someone else's chat")
    mine = _claude_transcript(repo, "22222222-bbbb", "[serena-task-xyz] do the thing")
    found = sc._find_transcript("claude", repo, "serena-task-xyz", started)
    assert found == mine
    assert sc._session_id("claude", found) == "22222222-bbbb"
    assert sc._find_transcript("claude", repo, "serena-task-nope", started) is None


def test_tag_is_found_behind_a_large_session_hook_preamble(tmp_path):
    path = tmp_path / "t.jsonl"
    preamble = json.dumps({"type": "attachment", "content": "x" * 700_000})
    brief = json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "[serena-task-deep] You are running this task"}]}})
    path.write_text(preamble + "\n" + brief + "\n")
    assert sc._opens_with(path, "serena-task-deep")
    assert not sc._opens_with(path, "serena-task-else")


def test_a_chat_that_only_mentions_the_tag_is_not_adopted(tmp_path):
    # This is how the chat that built this feature got adopted and renamed.
    path = tmp_path / "t.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "check serena-task-abc"}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "[serena-task-abc] You are running"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "[serena-task-abc] quoted back"}]}},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    assert not sc._opens_with(path, "serena-task-abc")


def test_codex_rollouts_match_on_the_users_first_message(tmp_path):
    path = tmp_path / "rollout.jsonl"
    lines = [
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>...</environment_context>"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "[serena-task-cx] You are running"}]}},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    assert sc._opens_with(path, "serena-task-cx")


def test_last_words_come_from_the_tail_of_a_long_transcript(tmp_path):
    path = tmp_path / "t.jsonl"
    early = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "early words"}]}}
    late = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "DONE: shipped"}]}}
    filler = json.dumps({"type": "attachment", "content": "x" * 5000})
    path.write_text(json.dumps(early) + "\n" + (filler + "\n") * 300 + json.dumps(late) + "\n")
    assert sc._last_said(path, tail_bytes=64 * 1024) == "DONE: shipped"
    codex = tmp_path / "rollout.jsonl"
    codex.write_text(json.dumps({"type": "event_msg", "payload": {
        "type": "agent_message", "message": "BLOCKED: no PC"}}) + "\n")
    assert sc._last_said(codex) == "BLOCKED: no PC"


def test_codex_session_id_comes_from_the_rollout_name():
    path = Path("rollout-2026-09-22T10-00-00-0199aaaa-bbbb-cccc-dddd-eeeeffff0000.jsonl")
    assert sc._session_id("codex", path) == "0199aaaa-bbbb-cccc-dddd-eeeeffff0000"


def _record(tmp_path, transcript: Path | None) -> dict:
    return {"id": "serena-task-1", "title": "Serena: fix it", "project": "serena",
            "agent": "claude", "cwd": str(tmp_path), "opened_at": time.time() - 300,
            "session_id": "sid-1", "transcript": str(transcript) if transcript else None,
            "terminal_id": "t-1", "backend": "http://127.0.0.1:1"}


@pytest.mark.parametrize(("last", "age", "state"), [
    ("Shipped.\nDONE: orb shows thinking, PR https://x/1", 400, "done"),
    ("Tests will not run.\nBLOCKED: pytest needs the PC", 400, "blocked"),
    ("Which icon?\nNEEDS YOU: pick the icon", 400, "blocked"),
    ("I did not change anything.\n\nDONE", 400, "done"),
    ("**DONE:** shipped", 400, "done"),
    ("Done with the tests, moving on to the docs.", 10, "working"),
    ("Running the tests now.", 10, "working"),
    ("Running the tests now.", 400, "waiting"),
])
def test_status_reads_the_sessions_own_words(tmp_path, monkeypatch, last, age, state):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    os.utime(transcript, (time.time() - age, time.time() - age))
    lines = [{"type": "user", "message": {"role": "user", "content": "go"}},
             {"type": "assistant", "message": {"role": "assistant",
                                               "content": [{"type": "text", "text": last}]}},
             {"type": "assistant", "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "Bash", "input": {}}]}}]
    transcript.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    os.utime(transcript, (time.time() - age, time.time() - age))
    result = sc.status(_record(tmp_path, transcript))
    assert result["state"] == state
    if state in ("done", "blocked"):
        assert sc._MARKER.match(result["outcome"])


def test_a_stopped_session_reads_stopped_whatever_it_last_said(tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    record = {**_record(tmp_path, transcript), "stopped_at": time.time()}
    sc._save({record["id"]: record})
    assert sc.status(record)["state"] == "stopped"
    assert sc.list_sessions(include_finished=False) == []


def test_status_is_starting_before_the_transcript_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_find_transcript", lambda *a: None)
    assert sc.status(_record(tmp_path, None))["state"] == "starting"


def test_open_session_spawns_in_his_app_finds_and_titles_it(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = []

    def fake_request(method, path, body=None, *, timeout=30, base=None):
        calls.append((method, path, body, base))
        return {"ok": True, "terminal_id": "term-9"} if "spawn" in path else {"ok": True}

    fake_contract = types.SimpleNamespace(
        RepositoryResolutionError=ValueError,
        resolve_repository_root=lambda task, project_hint="": repo)
    monkeypatch.setitem(sys.modules, "core.coding_job_contract", fake_contract)
    monkeypatch.setattr(sc, "_request", fake_request)
    monkeypatch.setattr(sc, "backend_url", lambda: "http://127.0.0.1:40277")
    drained = []
    monkeypatch.setattr(sc, "_keep_flowing", lambda base, tid: drained.append((base, tid)))

    def fake_find(agent, cwd, tag, since):
        return _claude_transcript(cwd, "33333333-cccc", f"[{tag}] brief")

    monkeypatch.setattr(sc, "_find_transcript", fake_find)
    titled = {}
    fake_meta = types.SimpleNamespace(get_meta=lambda sid: {},
                                      set_custom_title=lambda sid, title: titled.update({sid: title}))
    monkeypatch.setitem(sys.modules, "core.metadata", fake_meta)
    monkeypatch.setattr(sys.modules["core"], "metadata", fake_meta, raising=False)
    record = sc.open_session("Add a status label to the orb", project="serena")
    # Titled in its metadata, which indexing picks up even before the app knows it.
    assert titled == {"33333333-cccc": record["title"]}

    spawn = calls[0]
    assert spawn[:2] == ("POST", "/api/spawn-terminal")
    assert spawn[2]["agent"] == "claude" and spawn[2]["cwd"] == str(repo)
    assert spawn[2]["seed"].startswith(f"[{record['id']}]")
    assert spawn[2]["client_session_id"] == record["id"]
    assert spawn[3] == "http://127.0.0.1:40277"
    # The pane is re-keyed to its real chat so opening it reuses this process.
    assert calls[1][:2] == ("POST", "/api/terminal-runtime/migrate")
    assert calls[1][2] == {"old_sid": record["id"], "new_sid": "33333333-cccc",
                           "terminal_id": "term-9"}
    assert calls[1][3] == "http://127.0.0.1:40277"
    assert calls[2][:2] == ("POST", "/api/rename/33333333-cccc")
    assert sc._load()[record["id"]]["session_id"] == "33333333-cccc"
    # He is told where to watch it the moment it opens.
    started = sys.modules["core.phone_line"].sent
    assert len(started) == 1 and "Serena app" in started[0] and record["title"] in started[0]
    assert "chat titled" in record["where"]
    assert drained == [("http://127.0.0.1:40277", "term-9")]
    assert record["terminal_id"] == "term-9" and record["session_id"] == "33333333-cccc"
    assert sc._record("status label")["id"] == record["id"]
    assert sc._record("33333333")["id"] == record["id"]


def test_unknown_session_is_an_error_not_a_guess():
    with pytest.raises(sc.CodingSessionError):
        sc._record("nothing like it")


def test_steer_uses_the_bridge_for_its_agent(tmp_path, monkeypatch):
    record = _record(tmp_path, None)
    record["agent"] = "codex"
    sc._save({record["id"]: record})
    seen = {}

    def fake_request(method, path, body=None, *, timeout=30, base=None):
        if path == "/api/spawn-terminal":
            seen["reopened"] = body
            return {"ok": True, "terminal_id": "t-2"}
        seen.update(path=path, body=body, base=base)
        return {"ok": True, "response": "on it"}

    drained = []
    monkeypatch.setattr(sc, "_request", fake_request)
    monkeypatch.setattr(sc, "_keep_flowing", lambda base, tid: drained.append(tid))
    result = sc.steer_session("fix it", "use the blue icon")
    # The app had reaped the pane; it is reopened on the same chat first.
    assert seen["reopened"]["session_id"] == "sid-1"
    assert drained == ["t-2"] and sc._load()[record["id"]]["terminal_id"] == "t-2"
    assert seen["path"] == "/api/codex-bridge"
    assert seen["body"]["target_sid"] == "sid-1"
    assert seen["base"] == record["backend"]
    assert result["reply"] == "on it"


def test_notify_uses_the_jobs_topic_and_survives_a_line_without_topics(monkeypatch):
    sent = []

    def with_topic(text, *, key="", topic=""):
        sent.append((text, key, topic))
        return True

    monkeypatch.setitem(sys.modules, "core.phone_line", types.SimpleNamespace(send=with_topic))
    monkeypatch.setattr(sys.modules["core"], "phone_line", sys.modules["core.phone_line"],
                        raising=False)
    assert sc.notify("DONE:   orb   fixed")
    assert sent[-1][0] == "DONE: orb fixed" and sent[-1][2] == "jobs"
    assert sent[-1][1].startswith("coding:")

    def no_topic(text, *, key=""):
        sent.append((text, key, None))
        return True

    monkeypatch.setitem(sys.modules, "core.phone_line", types.SimpleNamespace(send=no_topic))
    monkeypatch.setattr(sys.modules["core"], "phone_line", sys.modules["core.phone_line"],
                        raising=False)
    assert sc.notify("BLOCKED: tests")
    assert sent[-1] == ("BLOCKED: tests", sent[-1][1], None)


def test_write_tools_refuse_without_a_live_turn_from_him(monkeypatch):
    from core import brain_code_tools as tools

    monkeypatch.setattr(tools, "current_turn", lambda: {})
    result = asyncio.run(tools.open_coding_session.handler({"task": "build the thing properly"}))
    assert result.get("is_error")
    monkeypatch.setattr(tools, "current_turn", lambda: {"text": "go", "protocol": "soak"})
    result = asyncio.run(tools.stop_coding_session.handler({"session": "x"}))
    assert result.get("is_error")


def test_tool_errors_come_back_as_errors(monkeypatch):
    from core import brain_code_tools as tools

    monkeypatch.setattr(tools, "current_turn", lambda: {"text": "open it", "protocol": "voice"})

    def boom(*_a, **_k):
        raise sc.CodingSessionError("could not reach his Serena app")

    monkeypatch.setattr(sc, "open_session", boom)
    result = asyncio.run(tools.open_coding_session.handler(
        {"task": "add a status label to the orb", "project": "serena"}))
    assert result["is_error"] and "could not reach" in result["content"][0]["text"]
