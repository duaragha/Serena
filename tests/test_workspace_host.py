import asyncio
import io
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from core.workspace_codex import CodexWorkspace
from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from core.workspace_rpc import WorkspaceRpc
from ui.workspace_web import workspace_blueprint


def test_rewind_is_explicit_deduplicated_and_refuses_background_work(tmp_path):
    calls = []
    class RewindOwner(Owner):
        async def revert_history(self, **payload):
            calls.append(payload)
            return {'session_id': self.sid, 'files_changed': False}
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'rewind.db'),
                         resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
                         factories={'codex': RewindOwner})
    payload = {'before_turn_id': 'turn', 'expected_latest_turn_id': 'last', 'confirmed': True}
    try:
        with pytest.raises(ValueError, match='attach'):
            host.command('exact', 'before', 'revert_history', payload)
        assert not calls
        host.attach('exact')
        host._work_reservations['exact'] = 'job'
        assert not host.command('exact', 'reserved', 'revert_history', payload)['ok']
        host._work_reservations.clear()
        host._bridge_queues['exact'] = ['queued']
        assert not host.command('exact', 'queued', 'revert_history', payload)['ok']
        host._bridge_queues.clear()
        assert not host.command('exact', 'bad', 'revert_history', {**payload, 'threadId': 'foreign'})['ok']
        first = host.command('exact', 'once', 'revert_history', payload)
        assert first['ok']
        assert host.command('exact', 'once', 'revert_history', payload) == first
        assert calls == [payload]
    finally:
        host.shutdown()


def test_rename_requires_owner_and_preserves_receipt_without_duplicate_native_write(tmp_path):
    calls = []
    class RenameOwner(Owner):
        async def rename(self, name):
            calls.append(('native', self.sid, name))
            return {'session_id': self.sid, 'name': name}
    def register(target):
        calls.append(('catalog', target))
        return {'display_title': target['confirmed_native_name']}
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'rename.db'),
                         resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
                         factories={'codex': RenameOwner}, register_fork=register)
    try:
        with pytest.raises(ValueError, match='attach'):
            host.command('exact', 'before', 'rename_session', {'name': 'New title'})
        assert not calls
        host.attach('exact')
        host._sessions['exact'][0].cwd = tmp_path
        assert not host.command('exact', 'bad', 'rename_session', {'name': 'Title', 'threadId': 'foreign'})['ok']
        first = host.command('exact', 'once', 'rename_session', {'name': 'New title'})
        assert first['ok'] and first['result']['catalog']['indexed']
        assert host.command('exact', 'once', 'rename_session', {'name': 'New title'}) == first
        assert calls == [('native', 'exact', 'New title'), ('catalog', {'session_id': 'exact', 'provider': 'codex', 'cwd': str(tmp_path), 'confirmed_native_name': 'New title'})]
        assert not host._sessions['exact'][0].sent
    finally:
        host.shutdown()


@pytest.mark.parametrize("mode", ["click", "heartbeat", "busy", "pinned", "switch_chat", "different_split"])
def test_only_explicit_linked_pane_click_sleeps_idle_sibling(tmp_path, mode):
    class Transport:
        suspended = False
        async def pause_idle(self):
            self.suspended = True
            return True
        def wake(self):
            self.suspended = False

    class PowerOwner(Owner):
        async def open(self):
            await super().open()
            self.rpc = Transport()
        async def list_background_tasks(self):
            return {"data": []}

    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "power.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "codex", "cwd": str(tmp_path)},
        factories={"codex": PowerOwner})
    view = "11111111-1111-4111-8111-111111111111"
    try:
        for sid in ("source", "peer", "unrelated"):
            host.attach(sid)
            host.note_view_context(sid, {"view_id": view, "sequence": 1, "visible": True,
                                        "focused": False, "draft": False, "pinned": mode == "pinned",
                                        "split_sids": [] if mode == "different_split" and sid == "peer"
                                        else ["source", "peer"] if sid != "unrelated" else []})
        if mode == "busy":
            host._sessions["peer"][0].state = "running"
        data = {"view_id": view, "sequence": 2, "visible": True, "focused": True,
                "draft": False, "pinned": mode == "pinned", "split_sids": ["source", "peer"],
                "sleep_peers": mode != "heartbeat"}
        host.note_view_context("source", data)
        host.note_view_context("source", {**data, "sequence": 3, "sleep_peers": False,
                                          "focused": mode != "switch_chat", "visible": mode != "switch_chat"})
        host._dispatch(asyncio.sleep(.15), 2)
        assert host._sessions["peer"][0].rpc.suspended is (mode == "click")
        assert not host._sessions["source"][0].rpc.suspended
        assert not host._sessions["unrelated"][0].rpc.suspended
    finally:
        host.shutdown()


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("blocker", [None, "running", "draft", "focused", "pinned", "unknown_pin",
                                    "stale", "questions", "reserved", "queued", "background", "rpc_failure", "race"])
def test_native_sleep_admission_and_focus_wake(tmp_path, provider, blocker):
    class Transport:
        suspended = False
        pauses = 0
        async def pause_idle(self):
            self.suspended = True
            self.pauses += 1
            return True
        def wake(self):
            self.suspended = False

    class SleepOwner(Owner):
        async def open(self):
            await super().open()
            self.transport = Transport()
            self.rpc = self.transport
            self.client = SimpleNamespace(transport=SimpleNamespace(rpc=self.transport))
        async def list_background_tasks(self):
            if blocker == "rpc_failure":
                raise RuntimeError("unavailable")
            if blocker == "race":
                self.state = "running"
            return {"data": [{"processId": "active"}] if blocker == "background" else []}

    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "sleep.db"),
        resolve=lambda sid: {"session_id": sid, "provider": provider, "cwd": str(tmp_path)},
        factories={provider: SleepOwner})
    app = Flask(__name__)
    app.register_blueprint(workspace_blueprint(host, token="s" * 32))
    client = app.test_client()
    headers = {"X-Serena-Workspace-Token": "s" * 32}
    view = "11111111-1111-4111-8111-111111111111"
    try:
        assert client.post("/api/workspace/exact/sleep", json={"sleeping": True}).status_code == 403
        assert client.post("/api/workspace/exact/sleep", json={"sleeping": True}, headers=headers,
                           environ_overrides={"REMOTE_ADDR": "192.0.2.1"}).status_code == 403
        assert client.post("/api/workspace/exact/sleep", json={"sleeping": "true"}, headers=headers).status_code == 400
        assert not host.set_sleep("exact", True)["ok"] and host._loop is None
        assert host.events("exact")["runtime"] is None and host._loop is None
        host.attach("exact")
        data = {"view_id": view, "sequence": 1, "visible": True,
                "focused": blocker == "focused", "draft": blocker == "draft", "pinned": blocker == "pinned"}
        if blocker == "unknown_pin":
            data.pop("pinned")
        host.note_view_context("exact", data)
        owner = host._sessions["exact"][0]
        if blocker == "running":
            owner.state = "running"
        if blocker == "questions":
            owner.questions = {"pending": {}}
        if blocker == "stale":
            host._views["exact"][view]["seen"] -= 10
        if blocker == "reserved":
            host._work_reservations["exact"] = "job"
        if blocker == "queued":
            host._bridge_queues["exact"] = ["queued"]
        assert not owner.transport.suspended  # Opening/reporting never sleeps.
        result = client.post("/api/workspace/exact/sleep", json={"sleeping": True}, headers=headers).json
        assert result["ok"] is (blocker is None)
        assert owner.transport.pauses == (1 if blocker is None else 0)
        if blocker is None:
            assert host.observe("exact")["sleeping"]
            assert host.events("exact")["runtime"] == {"session_id": "exact", "sleeping": True}
            assert owner.transport.suspended and owner.transport.pauses == 1
            host.note_view_context("exact", {**data, "sequence": 2, "focused": True})
            assert not owner.transport.suspended
            assert host.events("exact")["runtime"] == {"session_id": "exact", "sleeping": False}
    finally:
        host._bridge_queues.clear()
        host.shutdown()


@pytest.mark.parametrize('provider', ['codex', 'claude', 'gemini'])
def test_composer_queue_is_fifo_editable_cancellable_and_deduplicated(tmp_path, provider):
    calls = []

    class QueuedOwner(Owner):
        async def open(self):
            await super().open()
            self.state, self.active_turn = 'running', 'existing'

        async def submit(self, inputs, options=None):
            assert self.state == 'ready'
            self.state, self.active_turn = 'running', f'turn-{len(calls)}'
            calls.append((inputs, options))
            await self.publish({'method': 'turn/completed', 'params': {
                'turn': {'id': self.active_turn, 'status': 'completed'}}})
            turn = self.active_turn
            self.state, self.active_turn = 'ready', None
            return {'turn': {'id': turn}}

    journal = WorkspaceJournal(tmp_path / 'queue.db')
    host = WorkspaceHost(journal=journal,
        resolve=lambda sid: {'session_id': sid, 'provider': provider, 'cwd': str(tmp_path)},
        factories={provider: QueuedOwner})
    try:
        host.attach('exact')
        payload = {'inputs': [{'type': 'text', 'text': 'first'}], 'options': {'model': 'selected'}}
        first = host.command('exact', 'first', 'enqueue', payload)
        assert first['ok'] and first['result']['queued']
        assert host.command('exact', 'first', 'enqueue', payload) == first
        second = host.command('exact', 'second', 'enqueue', {'inputs': [{'type': 'text', 'text': 'second'}]})
        third = host.command('exact', 'third', 'enqueue', {'inputs': [{'type': 'text', 'text': 'third'}]})
        assert second['ok'] and third['ok'] and calls == []
        assert host.command('exact', 'edit', 'edit_queued_bridge', {
            'request_id': first['result']['id'], 'expected_prompt': 'first', 'prompt': 'edited'})['ok']
        assert host.command('exact', 'cancel', 'cancel_queued_bridge', {'request_id': second['result']['id']})['ok']
        recovered = journal.recoverable_bridge_queue('exact', provider)
        assert [r['prompt'] for r in recovered] == ['edited', 'third']
        assert recovered[0]['message'] == {'inputs': [{'type': 'text', 'text': 'edited'}], 'options': {'model': 'selected'}}

        async def finish():
            owner = host._sessions['exact'][0]
            owner.state, owner.active_turn = 'ready', None

        host._dispatch(finish(), 5)
        deadline = time.monotonic() + 5
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(.02)
        assert calls == [([{'type': 'text', 'text': 'edited'}], {'model': 'selected'}),
                         ([{'type': 'text', 'text': 'third'}], None)]
        assert journal.recoverable_bridge_queue('exact', provider) == []
    finally:
        host.shutdown()


def test_claude_queued_input_is_session_bound_and_deduplicated(tmp_path):
    calls = []

    class QueueOwner(Owner):
        async def open(self):
            await super().open()
            self.state, self.active_turn = "running", "active"

        async def queue_input(self, inputs, *, expected_turn_id):
            calls.append((self.sid, inputs, expected_turn_id))
            return {"turn": {"id": "queued"}}

    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "queue.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)},
        factories={"claude": QueueOwner})
    payload = {"inputs": [{"type": "text", "text": "follow-up"}], "expectedTurnId": "active"}
    try:
        host.events("exact")
        assert not calls
        host.attach("exact")
        result = host.command("exact", "queue-request", "queue_input", payload)
        assert result["ok"] and result["result"]["turn"]["id"] == "queued"
        assert host.command("exact", "queue-request", "queue_input", payload) == result
        assert calls == [("exact", payload["inputs"], "active")]
        stale = host.command("exact", "stale", "queue_input", {**payload, "expectedTurnId": "old"})
        assert not stale["ok"] and stale["retryable"]
        assert not host.command("exact", "invalid", "queue_input", {**payload, "options": {"model": "other"}})["ok"]
        assert len(calls) == 1
    finally:
        host.shutdown()


class Owner:
    instances = []

    def __init__(self, *, session_id, cwd, publish):
        self.sid, self.publish = session_id, publish
        self.cwd = cwd
        self.state, self.active_turn = "opening", None
        self.sent, self.closed = [], False
        self.instances.append(self)

    async def open(self):
        await asyncio.sleep(0.04)
        self.state = "ready"
        await self.publish(
            {"method": "workspace/history", "params": {"thread": {"id": self.sid, "turns": []}}}
        )

    async def submit(self, inputs, options=None):
        self.sent.append(inputs)
        await asyncio.sleep(0.04)
        await self.publish(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": self.sid,
                    "turnId": "t",
                    "itemId": "a",
                    "delta": "adapter fixture output",
                },
            }
        )
        return {"turn": {"id": "turn-1"}}

    async def close(self):
        self.closed = True


def test_observation_never_starts_or_replaces_an_owner(tmp_path):
    owners = []

    class ObservedOwner(Owner):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            owners.append(self)

    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "observe.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "codex", "cwd": str(tmp_path)},
        factories={"codex": ObservedOwner})
    app = Flask(__name__)
    app.register_blueprint(workspace_blueprint(host, token="s" * 40))
    client = app.test_client()
    headers = {"X-Serena-Workspace-Token": "s" * 40}
    rows = [{"session_id": "exact", "title": "Owned"}, {"session_id": "other", "title": "Closed"}]
    try:
        assert client.get("/api/workspace/exact/observe").status_code == 403
        assert client.get("/api/workspace/exact/observe", headers=headers).json == {
            "observing": False, "session_id": "exact"}
        assert host._loop is None and not owners
        assert host.decorate_runtime_sessions(rows) == rows
        assert host._loop is None and not owners
        host.attach("exact")
        for state in ["ready", "running", "completed", "failed", "interrupted"]:
            owners[0].state = state
            result = client.get("/api/workspace/exact/observe", headers=headers).json
            assert result["observing"] and result["session_id"] == "exact"
            assert result["state"] == state
            decorated = host.decorate_runtime_sessions(rows)
            assert decorated[0]["workspace_runtime"]["state"] == state
            assert decorated[0]["workspace_runtime"]["ok"]
            assert decorated[1] == rows[1]
            assert "workspace_runtime" not in rows[0]
        for state in ["opening", "closed", "unavailable"]:
            owners[0].state = state
            assert not host.observe("exact")["observing"]
            if state != "opening":
                assert not host.decorate_runtime_sessions(rows)[0]["workspace_runtime"]["ok"]
        assert not host.observe("other")["observing"]
        assert len(owners) == 1 and not owners[0].closed
    finally:
        host.shutdown()
    assert not host.observe("exact")["observing"]


def test_native_runtime_context_is_read_only_and_local(tmp_path, monkeypatch):
    from ui import web

    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "context.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "codex", "cwd": str(tmp_path)},
        factories={"codex": Owner})
    monkeypatch.setitem(web.app.extensions, "workspace_host", host)
    monkeypatch.setattr(web, "_native_runtime_context", lambda: None)
    monkeypatch.setattr(web.pty_terminal, "runtime_context_snapshot", lambda: {"runtimes": []})
    monkeypatch.setattr(web, "_decorate_runtime_entry", lambda entry: dict(entry))
    try:
        client = web.app.test_client()
        assert client.get('/api/runtime-context').json['runtimes'] == []
        assert host._loop is None
        host.attach('exact')
        owner = host._sessions['exact'][0]
        for state, turn, alive, busy in [
            ('ready', None, True, False), ('running', 'turn', True, True),
            ('uncertain', None, True, True), ('opening', None, True, True),
            ('ready', 'turn', True, True), ('closed', None, False, False),
            ('unavailable', None, False, False),
        ]:
            owner.state, owner.active_turn = state, turn
            context = client.get('/api/runtime-context').json
            assert context['runtimes'] == [{
                'sid': 'exact', 'agent': 'codex', 'cwd': str(tmp_path),
                'alive': alive, 'state': state, 'busy': busy,
                'reserved': False, 'owner': 'workspace', 'draft': False, 'draft_known': False,
                'pending_interactions': False, 'model': '', 'effort': ''}]
            assert context['sessions'] == context['runtimes']
            assert not context['focused_sid'] and not context['window_active']
        host._bridge_queues['exact'] = ['pending']
        assert host.runtime_context_snapshot()['runtimes'][0]['reserved']
        assert client.get('/api/runtime-context', environ_overrides={
            'REMOTE_ADDR': '100.100.100.100'}).status_code == 403
        assert not owner.sent and not owner.closed and len(host._sessions) == 1
    finally:
        host.shutdown()
    assert host.runtime_context_snapshot() == {'runtimes': []}


def test_view_context_auth_order_expiry_and_draft_retention(tmp_path, monkeypatch):
    import core.workspace_host as module
    clock = [100.0]
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'views.db'),
        resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
        factories={'codex': Owner})
    app = Flask(__name__)
    app.register_blueprint(workspace_blueprint(host, token='s' * 40))
    client = app.test_client()
    headers = {'X-Serena-Workspace-Token': 's' * 40}
    data = {'view_id': '11111111-1111-4111-8111-111111111111', 'sequence': 1,
            'focused': True, 'visible': True, 'draft': True}
    try:
        assert client.post('/api/workspace/exact/view-context', json=data).status_code == 403
        assert not client.post('/api/workspace/exact/view-context', json=data, headers=headers).json['ok']
        assert host._loop is None
        host.attach('exact')
        assert client.post('/api/workspace/exact/view-context', json=data, headers=headers).json['ok']
        context = host.runtime_context_snapshot()
        assert context['focused_sid'] == 'exact' and context['window_active']
        assert context['runtimes'][0]['draft'] and context['runtimes'][0]['draft_known']
        stale = {**data, 'sequence': 0, 'draft': False}
        assert host.note_view_context('exact', stale)['stale']
        assert host.runtime_context_snapshot()['runtimes'][0]['draft']
        for invalid in [{**data, 'focused': 'yes'}, {**data, 'visible': False},
                        {**data, 'sequence': True}, {**data, 'text': 'private draft'},
                        {**data, 'closed': 'yes'}, {**data, 'closed': True},
                        {**data, 'view_id': 'invalid'}]:
            assert client.post('/api/workspace/exact/view-context', json=invalid, headers=headers).status_code == 400
        clock[0] += 7
        context = host.runtime_context_snapshot()
        assert not context['focused_sid'] and not context['window_active']
        assert context['runtimes'][0]['draft'] and not context['runtimes'][0]['draft_known']
        host.note_view_context('exact', {**data, 'sequence': 2, 'draft': False, 'focused': False})
        assert not host.runtime_context_snapshot()['runtimes'][0]['draft']
        assert len(host._sessions) == 1 and not host._sessions['exact'][0].sent
    finally:
        host.shutdown()


def test_closed_views_retire_constraints_without_closing_owner(tmp_path):
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'closed-views.db'),
        resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
        factories={'codex': Owner})
    data = {'view_id': '11111111-1111-4111-8111-111111111111', 'sequence': 1,
            'focused': False, 'visible': False, 'draft': True, 'pinned': False}
    try:
        host.attach('exact')
        owner = host._sessions['exact'][0]
        host.note_view_context('exact', data)
        host._views['exact'][data['view_id']]['seen'] -= 10
        fresh = {**data, 'view_id': '22222222-2222-4222-8222-222222222222', 'draft': False, 'visible': True}
        host.note_view_context('exact', fresh)
        assert host._sleep_blocker('exact') and host._work_admission_error('exact')
        assert not host.runtime_context_snapshot()['runtimes'][0]['draft_known']
        assert host.note_view_context('exact', {**data, 'sequence': 3, 'closed': True})['closed']
        assert not host._sleep_blocker('exact') and not host._work_admission_error('exact')
        context = host.runtime_context_snapshot()['runtimes'][0]
        assert context['draft_known'] and not context['draft']
        assert host.note_view_context('exact', {**data, 'sequence': 2})['stale']
        assert len(host._active_views('exact')) == 1
        host.note_view_context('exact', {**data, 'sequence': 4})
        assert host._sleep_blocker('exact') and host._work_admission_error('exact')
        assert len(host._active_views('exact')) == 2
        assert owner is host._sessions['exact'][0] and not owner.sent and not owner.closed
    finally:
        host.shutdown()


def test_closed_view_cursors_do_not_consume_live_view_slots(tmp_path):
    from uuid import uuid4
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'view-slots.db'),
        resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
        factories={'codex': Owner})
    try:
        host.attach('exact')
        base = {'sequence': 1, 'focused': False, 'visible': False, 'draft': False}
        for _ in range(40):
            data = {**base, 'view_id': str(uuid4())}
            host.note_view_context('exact', data)
            host.note_view_context('exact', {**data, 'sequence': 2, 'closed': True})
        assert not host._active_views('exact')
        for _ in range(32):
            host.note_view_context('exact', {**base, 'view_id': str(uuid4())})
        with pytest.raises(ValueError, match='Too many views'):
            host.note_view_context('exact', {**base, 'view_id': str(uuid4())})
    finally:
        host.shutdown()


def test_split_context_stays_with_its_focused_owner(tmp_path, monkeypatch):
    from ui import web
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'split.db'),
        resolve=lambda sid: {'session_id': sid, 'provider': 'claude' if sid == 'left' else 'codex',
                             'cwd': str(tmp_path)}, factories={'claude': Owner, 'codex': Owner})
    monkeypatch.setitem(web.app.extensions, 'workspace_host', host)
    monkeypatch.setattr(web, '_native_runtime_context', lambda: {
        'focused_sid': 'old', 'focused_at': 1, 'split_pair': ['old', 'wrong'], 'runtimes': []})
    monkeypatch.setattr(web.pty_terminal, 'runtime_context_snapshot', lambda: {'runtimes': []})
    monkeypatch.setattr(web, '_decorate_runtime_entry', lambda row: dict(row))
    data = {'view_id': '11111111-1111-4111-8111-111111111111', 'sequence': 1,
            'focused': True, 'visible': True, 'draft': False}
    try:
        host.attach('left')
        host.attach('right')
        host.note_view_context('left', data)
        client = web.app.test_client()
        context = client.get('/api/runtime-context').json
        assert context['focused_sid'] == 'left' and context['split_pair'] == []
        host.note_view_context('left', {**data, 'sequence': 2, 'split_sids': ['left', 'right']})
        context = client.get('/api/runtime-context').json
        assert context['focused_sid'] == 'left' and context['split_pair'] == ['left', 'right']
        for split in [['right'], ['left', 'left'], 'left', ['left', 1]]:
            with pytest.raises(ValueError):
                host.note_view_context('left', {**data, 'sequence': 3, 'split_sids': split})
        host._sessions['right'][0].state = 'closed'
        assert host.runtime_context_snapshot()['split_pair'] == []
        assert len(host._sessions) == 2 and all(not owner.sent for owner, _ in host._sessions.values())
    finally:
        host.shutdown()


def test_runtime_busy_includes_native_questions_and_claude_background_work(tmp_path):
    from types import SimpleNamespace

    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'activity.db'),
        resolve=lambda sid: {'session_id': sid, 'provider': 'claude', 'cwd': str(tmp_path)},
        factories={'claude': Owner})
    try:
        host.attach('exact')
        owner = host._sessions['exact'][0]
        owner.settings = {'model': 'native-model', 'reasoningEffort': 'high'}
        for attribute in ['questions', 'elicitations']:
            setattr(owner, attribute, {'pending': {}})
            row = host.runtime_context_snapshot()['runtimes'][0]
            assert row['busy'] and row['pending_interactions']
            assert row['model'] == 'native-model' and row['effort'] == 'high'
            setattr(owner, attribute, {})
        for status, busy in [('running', True), ('queued', True), (None, True),
                             ('completed', False), ('failed', False), ('stopped', False), ('killed', False)]:
            owner.events = SimpleNamespace(tasks={'background': {'status': status}})
            row = host.runtime_context_snapshot()['runtimes'][0]
            assert row['busy'] is busy and not row['pending_interactions']
        assert not owner.sent and not owner.closed
    finally:
        host.shutdown()


def test_native_work_reservation_is_exact_and_blocks_competing_input(tmp_path):
    class WorkOwner(Owner):
        async def list_background_tasks(self):
            return {'data': []}
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'work.db'),
        resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
        factories={'codex': WorkOwner})
    item = '11111111-1111-4111-8111-111111111111'
    other = '22222222-2222-4222-8222-222222222222'
    view = {'view_id': item, 'sequence': 1, 'focused': True, 'visible': True, 'draft': False}
    try:
        assert not host.reserve_work('exact', item)['ok'] and host._loop is None
        host.attach('exact')
        assert not host.reserve_work('exact', item)['ok']
        host.note_view_context('exact', view)
        assert host.reserve_work('exact', item)['ok']
        assert host.reserve_work('exact', item)['ok']
        assert not host.reserve_work('exact', other)['ok']
        assert host.runtime_context_snapshot()['runtimes'][0]['reserved']
        for action in ['submit', 'shell_command', 'clear_session', 'disconnect_session', 'set_permissions']:
            result = host.command('exact', 'blocked-' + action, action, {})
            assert not result['ok'] and result['retryable']
        assert not host.bridge('exact', 'codex', 'competing message', 'competing')['ok']
        assert not host.release_work('exact', other)
        owner = host._sessions['exact'][0]
        owner.active_turn = 'running'
        assert not host.release_work('exact', item)
        owner.active_turn = None
        owner.questions = {'pending': {}}
        assert not host.release_work('exact', item)
        owner.questions = {}
        owner.state = 'unavailable'
        owner.can_retry_attachment = lambda: True
        assert not host.attach('exact')['ok']
        assert host._sessions['exact'][0] is owner
        owner.state = 'ready'
        assert host.release_work('exact', item)
        assert not host.runtime_context_snapshot()['runtimes'][0]['reserved']
        assert not owner.sent and not owner.closed
    finally:
        host.shutdown()


def test_work_admission_rechecks_draft_after_native_background_rpc(tmp_path):
    entered, finish = threading.Event(), threading.Event()
    class WorkOwner(Owner):
        async def list_background_tasks(self):
            entered.set()
            await asyncio.to_thread(finish.wait, 5)
            return {'data': []}
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'race.db'),
        resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
        factories={'codex': WorkOwner})
    item = '11111111-1111-4111-8111-111111111111'
    view = {'view_id': item, 'sequence': 1, 'focused': True, 'visible': True, 'draft': False}
    try:
        host.attach('exact')
        host.note_view_context('exact', view)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(host.reserve_work, 'exact', item)
            assert entered.wait(5)
            host.note_view_context('exact', {**view, 'sequence': 2, 'draft': True})
            finish.set()
            assert not result.result(5)['ok']
        assert not host._work_reservations
    finally:
        finish.set()
        host.shutdown()


@pytest.mark.parametrize('blocker', ['question', 'turn', 'draft', 'stale', 'queue', 'background', 'rpc-error'])
def test_native_work_reservation_refuses_unsafe_admission(tmp_path, blocker):
    class WorkOwner(Owner):
        async def list_background_tasks(self):
            if blocker == 'rpc-error':
                raise RuntimeError('not available')
            return {'data': [{}] if blocker == 'background' else []}
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'blocked.db'),
        resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
        factories={'codex': WorkOwner})
    item = '11111111-1111-4111-8111-111111111111'
    try:
        host.attach('exact')
        host.note_view_context('exact', {'view_id': item, 'sequence': 1, 'focused': True,
                                        'visible': True, 'draft': blocker == 'draft'})
        owner = host._sessions['exact'][0]
        if blocker == 'question':
            owner.questions = {'approval': {}}
        elif blocker == 'turn':
            owner.active_turn = 'ongoing'
        elif blocker == 'stale':
            host._views['exact'][item]['seen'] -= 10
        elif blocker == 'queue':
            host._bridge_queues['exact'] = ['pending']
        assert not host.reserve_work('exact', item)['ok']
        assert not host._work_reservations and not owner.sent
    finally:
        host.shutdown()


@pytest.mark.parametrize('uncertain', ['', 'submit', 'receipt'])
def test_reserved_submission_is_durable_and_interrupt_is_turn_bound(tmp_path, uncertain, monkeypatch):
    class WorkOwner(Owner):
        interrupts = 0
        async def list_background_tasks(self):
            return {'data': []}
        async def submit(self, inputs, options=None):
            self.sent.append(inputs)
            if uncertain == 'submit':
                raise TimeoutError('native acknowledgement lost')
            self.active_turn, self.state = 'exact-turn', 'running'
            return {'turn': {'id': self.active_turn}}
        async def interrupt(self):
            self.interrupts += 1
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'submit.db'),
        resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
        factories={'codex': WorkOwner})
    item = '11111111-1111-4111-8111-111111111111'
    dispatch = '22222222-2222-4222-8222-222222222222'
    try:
        host.attach('exact')
        host.note_view_context('exact', {'view_id': item, 'sequence': 1, 'focused': True,
                                        'visible': True, 'draft': False})
        assert host.reserve_work('exact', item)['ok']
        if uncertain == 'receipt':
            def lost_receipt(*args):
                raise OSError('receipt write failed')
            monkeypatch.setattr(host.journal, 'finish_command', lost_receipt)
        assert not host.submit_work('exact', dispatch, 'wrong owner', dispatch)['ok']
        result = host.submit_work('exact', item, 'accepted job', dispatch, start_offset=12)
        repeated = host.submit_work('exact', item, 'accepted job', dispatch, start_offset=200)
        assert result['start_offset'] == repeated['start_offset'] == 12
        stored = WorkspaceJournal(host.journal.path).command_record('exact', 'work:' + item + ':' + dispatch)
        assert stored['payload']['start_offset'] == 12
        owner = host._sessions['exact'][0]
        assert owner.sent == [[{'type': 'text', 'text': 'accepted job'}]]
        assert not host.interrupt_work('exact', dispatch)['ok']
        with pytest.raises(ValueError):
            host.submit_work('exact', item, 'changed prompt', dispatch)
        for invalid_offset in [-1, True, '12']:
            with pytest.raises(ValueError):
                host.submit_work('exact', item, 'accepted job', dispatch, start_offset=invalid_offset)
        if uncertain:
            assert result['uncertain'] and repeated['uncertain']
            assert not host.release_work('exact', item)
            assert not host.submit_work('exact', item, 'accepted job', item)['ok']
            assert not host.interrupt_work('exact', item)['ok']
            # Re-reading an orphan durable claim must restore the uncertainty guard.
            host._work_turns.clear()
            assert host.submit_work('exact', item, 'accepted job', dispatch)['uncertain']
            assert not host.release_work('exact', item)
        else:
            assert result == repeated and result['turn_id'] == 'exact-turn'
            assert host.interrupt_work('exact', item)['ok'] and owner.interrupts == 1
            owner.active_turn = 'different-turn'
            assert not host.interrupt_work('exact', item)['ok'] and owner.interrupts == 1
            owner.active_turn, owner.state = None, 'ready'
            assert host.release_work('exact', item)
    finally:
        host.shutdown()


def test_pending_work_receipt_blocks_restart_and_new_dispatch(tmp_path):
    journal = WorkspaceJournal(tmp_path / 'restart-work.db')
    item = '11111111-1111-4111-8111-111111111111'
    key = 'work:' + item + ':' + item
    journal.claim_command('exact', key, {'action': 'work_submit'})
    assert journal.has_pending_work('exact') and not journal.has_pending_work('other')
    host = WorkspaceHost(journal=WorkspaceJournal(journal.path),
                         resolve=lambda sid: pytest.fail('Unconfirmed work must not resolve or attach'))
    try:
        with pytest.raises(ValueError, match='work dispatch is unconfirmed'):
            host.attach('exact')
        assert not host._sessions
    finally:
        host.shutdown()
    journal.finish_command('exact', key, {'ok': False, 'committed': False})
    assert not journal.has_pending_work('exact')
    host = WorkspaceHost(journal=journal,
        resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
        factories={'codex': Owner})
    try:
        host.attach('exact')
        journal.claim_command('exact', key + ':next', {'action': 'work_submit'})
        assert not host.reserve_work('exact', item)['ok']
        host._work_reservations['exact'] = item
        assert not host.release_work('exact', item)
        assert not host.submit_work('exact', item, 'must not submit',
                                    '22222222-2222-4222-8222-222222222222')['ok']
        assert not host._sessions['exact'][0].sent
    finally:
        host.shutdown()


def test_browser_login_controls_are_receipted_and_subscription_only(tmp_path):
    calls = []
    class AccountOwner(Owner):
        async def list_background_tasks(self):
            return {"data": []}

        async def login_account(self):
            calls.append((self.sid, "login"))
            return {"loginId": "one", "status": "pending"}
        async def cancel_account_login(self, login_id):
            calls.append((self.sid, login_id))
            return {"status": "cancelled"}
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "login.db"),
                         resolve=lambda sid: {"session_id": sid, "provider": "codex", "cwd": str(tmp_path)},
                         factories={"codex": AccountOwner})
    try:
        host.attach("exact")
        assert calls == []
        result = host.command("exact", "login-once", "account_login", {})
        assert result["ok"]
        assert host.command("exact", "login-once", "account_login", {}) == result
        assert not host.command("exact", "bad", "account_login", {"apiKey": "forbidden"})["ok"]
        assert host.command("exact", "cancel-once", "account_login_cancel", {"loginId": "one"})["ok"]
        assert calls == [("exact", "login"), ("exact", "one")]
        assert host._codex_account_login is None
        assert not host._sessions["exact"][0].sent
    finally:
        host.shutdown()


def test_browser_login_retires_stale_owners_blocks_writes_and_reconnects_after_completion(tmp_path):
    calls = []
    instances = []

    class AccountOwner(Owner):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.questions = {}
            self.active_agent_threads = set()
            instances.append(self)

        async def list_background_tasks(self):
            return {"data": []}

        async def login_account(self):
            calls.append((self.sid, "login"))
            return {"loginId": "native-login", "status": "pending",
                    "authUrl": "https://auth.openai.com/authorize?state=test"}

        async def account_status(self):
            return {"account": None, "login": {"loginId": "native-login", "status": "pending"}}

        async def close(self):
            await super().close()
            self.state = "closed"

        def can_retry_attachment(self):
            return self.state == "closed" and self.closed

    host = WorkspaceHost(
        journal=WorkspaceJournal(tmp_path / "login-sync.db"),
        resolve=lambda sid: {
            "session_id": sid,
            "provider": "codex",
            "cwd": str(tmp_path),
        },
        factories={"codex": AccountOwner},
    )
    try:
        host.attach("exact")
        host.attach("other")
        original_other = host._sessions["other"][0]
        result = host.command("exact", "login", "account_login", {})
        assert result == {
            "ok": True,
            "result": {
                "loginId": "native-login",
                "status": "pending",
                "authUrl": "https://auth.openai.com/authorize?state=test",
                "disconnectedSessionCount": 1,
            },
        }
        assert original_other.closed and original_other.state == "closed"
        assert not host.observe("other")["observing"]
        with pytest.raises(ValueError, match="browser sign-in"):
            host.attach("other")
        with pytest.raises(ValueError, match="browser sign-in"):
            host.create(
                "11111111-1111-4111-8111-111111111111",
                "codex",
                str(tmp_path),
                confirmed=True,
            )
        assert len(instances) == 2
        methods = [item["event"]["method"] for item in host.events("other")["events"]]
        assert methods[-1] == "workspace/transportClosed"
        blocked = host.command(
            "exact", "blocked-submit", "submit", {"inputs": [{"type": "text", "text": "do not send"}]}
        )
        assert not blocked["ok"] and blocked["retryable"]
        assert "browser sign-in" in blocked["error"] and not host._sessions["exact"][0].sent
        assert "browser sign-in" in host.bridge("exact", "codex", "do not bridge", "blocked")["message"]
        assert "browser sign-in" in host._work_admission_error("exact")
        assert host._codex_account_login == {
            "sid": "exact", "login_id": "native-login", "disconnected": ("other",)
        }
        status = host.command("exact", "status", "account_status", {})
        assert status["result"]["login"]["disconnectedSessionCount"] == 1
        host._dispatch(host._publish("exact", {
            "method": "workspace/error", "params": {"reason": "Unrelated event error"}
        }), 5)
        host._dispatch(host._publish("exact", {
            "method": "account/login/completed",
            "params": {"loginId": "another-login", "success": True},
        }), 5)
        assert host._codex_account_login is not None
        host._dispatch(host._publish("exact", {
            "method": "account/login/completed",
            "params": {"loginId": "native-login", "success": True},
        }), 5)
        assert host._codex_account_login is None
        assert host._sessions["other"][0] is original_other
        assert not original_other.sent
        reconnected = host.attach("other")
        assert reconnected["ok"] and host._sessions["other"][0] is not original_other
        assert len(instances) == 3
        assert calls == [("exact", "login")]
    finally:
        host.shutdown()


def test_browser_login_early_completion_settles_after_command_releases_account_lock(tmp_path):
    class AccountOwner(Owner):
        async def list_background_tasks(self):
            return {"data": []}

        async def login_account(self):
            asyncio.create_task(self.publish({
                "method": "account/login/completed",
                "params": {"loginId": "early", "success": True},
            }))
            await asyncio.sleep(0)
            return {"loginId": "early", "status": "succeeded"}

    host = WorkspaceHost(
        journal=WorkspaceJournal(tmp_path / "early-login.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "codex", "cwd": str(tmp_path)},
        factories={"codex": AccountOwner},
    )
    try:
        host.attach("exact")
        result = host.command("exact", "login", "account_login", {})
        assert result["ok"] and result["result"]["status"] == "succeeded"

        async def settled():
            async with asyncio.timeout(2):
                while host._codex_account_login is not None:
                    await asyncio.sleep(0.01)

        host._dispatch(settled(), 5)
        assert host._codex_account_login is None
    finally:
        host.shutdown()


@pytest.mark.parametrize("blocker", ["running", "background", "reserved", "pending"])
def test_browser_login_refuses_busy_codex_owners_without_closing_them(tmp_path, blocker):
    calls = []

    class AccountOwner(Owner):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.questions = {}
            self.active_agent_threads = set()

        async def list_background_tasks(self):
            return {"data": ([{"processId": "work"}]
                             if blocker == "background" and self.sid == "other" else [])}

        async def login_account(self):
            calls.append(self.sid)
            return {"loginId": "must-not-start", "status": "pending"}

    host = WorkspaceHost(
        journal=WorkspaceJournal(tmp_path / f"login-{blocker}.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "codex", "cwd": str(tmp_path)},
        factories={"codex": AccountOwner},
    )
    try:
        host.attach("exact")
        host.attach("other")
        if blocker == "running":
            host._sessions["other"][0].state = "running"
        elif blocker == "reserved":
            host._work_reservations["other"] = "job"
        elif blocker == "pending":
            host.journal.claim_command("other", "unfinished", {"action": "submit", "payload": {}})
        result = host.command("exact", "login", "account_login", {})
        assert not result["ok"] and result["retryable"]
        assert not calls and host._codex_account_login is None
        assert not host._sessions["exact"][0].closed
        assert not host._sessions["other"][0].closed
    finally:
        host.shutdown()


@pytest.mark.parametrize("blocker", [None, "running", "background", "reserved", "pending"])
def test_account_logout_is_confirmed_deduplicated_and_covers_every_live_codex_owner(tmp_path, blocker):
    calls = []

    class AccountOwner(Owner):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.questions = {}
            self.active_agent_threads = set()

        async def list_background_tasks(self):
            return {
                "data": (
                    [{"processId": "work"}]
                    if blocker == "background" and self.sid == "other"
                    else []
                )
            }

        async def logout_account(self):
            calls.append(self.sid)
            return {"loggedOut": True}

    host = WorkspaceHost(
        journal=WorkspaceJournal(tmp_path / "logout.db"),
        resolve=lambda sid: {
            "session_id": sid,
            "provider": "codex",
            "cwd": str(tmp_path),
        },
        factories={"codex": AccountOwner},
    )
    try:
        host.attach("exact")
        host.attach("other")
        if blocker == "running":
            host._sessions["other"][0].state = "running"
        elif blocker == "reserved":
            host._work_reservations["other"] = "job"
        elif blocker == "pending":
            host.journal.claim_command(
                "other", "unfinished", {"action": "submit", "payload": {}}
            )
        assert not host.command("exact", "bad", "account_logout", {})["ok"]
        result = host.command(
            "exact", "logout-once", "account_logout", {"confirmed": True}
        )
        if blocker is None:
            assert result == {
                "ok": True,
                "result": {"loggedOut": True, "sessionCount": 2},
            }
            assert host.command(
                "exact", "logout-once", "account_logout", {"confirmed": True}
            ) == result
            assert calls == ["exact", "other"]
        else:
            assert not result["ok"] and result["retryable"]
            assert calls == []
        assert not host._sessions["exact"][0].sent
        assert not host._sessions["other"][0].sent
    finally:
        host.shutdown()


def test_session_usage_scope_is_readonly_and_cannot_choose_another_session(tmp_path):
    calls = []
    class UsageOwner(Owner):
        async def thread_token_usage(self):
            calls.append(self.sid)
            return {'threadUsage': None}
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'usage.db'),
                         resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
                         factories={'codex': UsageOwner})
    try:
        host.attach('exact')
        host._work_reservations['exact'] = 'job'
        result = host.command('exact', 'read', 'account_token_usage', {'scope': 'session'})
        assert result['ok'] and calls == ['exact']
        assert host.command('exact', 'read', 'account_token_usage', {'scope': 'session'}) == result
        for index, payload in enumerate([{'scope': 'foreign'}, {'scope': 'session', 'threadId': 'other'}]):
            assert not host.command('exact', f'bad-{index}', 'account_token_usage', payload)['ok']
        assert calls == ['exact'] and not host._sessions['exact'][0].sent
    finally:
        host.shutdown()


def test_sessions_http_lists_native_owner_without_a_mounted_pane(tmp_path, monkeypatch):
    from ui import web

    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "sidebar.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "codex", "cwd": str(tmp_path)},
        factories={"codex": Owner})
    rows = [{"session_id": "exact", "agent": "codex", "title": "Background coding"}]
    monkeypatch.setitem(web.app.extensions, "workspace_host", host)
    monkeypatch.setattr(web, "list_sessions", lambda **kwargs: rows)
    monkeypatch.setattr(web, "_include_permanent_serena_session", lambda value: value)
    monkeypatch.setattr(web, "_decorate_sessions", lambda value: value)
    try:
        client = web.app.test_client()
        assert client.get('/api/sessions').json == rows
        assert host._loop is None
        host.attach('exact')
        owner = host._sessions['exact'][0]
        owner.state, owner.active_turn = 'running', 'work-in-progress'
        result = client.get('/api/sessions').json
        assert result[0]['workspace_runtime'] == {
            'ok': True, 'session_id': 'exact', 'provider': 'codex',
            'state': 'running', 'turn_id': 'work-in-progress', 'sleeping': False}
        assert host._sessions['exact'][0] is owner and not owner.sent and not owner.closed
        owner.state = 'unavailable'
        assert not client.get('/api/sessions').json[0]['workspace_runtime']['ok']
        assert not owner.closed and 'workspace_runtime' not in rows[0]
    finally:
        host.shutdown()


@pytest.mark.parametrize('action', ['account_status', 'account_rate_limits', 'account_token_usage', 'hooks', 'apps'])
def test_account_status_requires_explicit_owner_and_rejects_mutations(tmp_path, action):
    calls = []
    class AccountOwner(Owner):
        async def account_status(self):
            calls.append(self.sid)
            return {"account": None, "requiresOpenaiAuth": True, "credentialsVerified": False}
        account_rate_limits = account_status
        account_token_usage = account_status
        list_hooks = account_status
        list_apps = account_status
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "account.db"),
                         resolve=lambda sid: {"session_id": sid, "provider": "codex", "cwd": str(tmp_path)},
                         factories={"codex": AccountOwner})
    try:
        with pytest.raises(ValueError, match="attach"):
            host.command("exact", "before", action, {})
        assert not calls
        host.attach("exact")
        result = host.command("exact", "account-once", action, {})
        assert result["ok"] and result["result"]["account"] is None
        assert host.command("exact", "account-once", action, {}) == result
        assert not host.command("exact", "invalid", action, {"refreshToken": True})["ok"]
        assert calls == ["exact"] and not host._sessions["exact"][0].sent
    finally:
        host.shutdown()


def test_project_diff_requires_existing_owner_and_ignores_caller_paths(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('core.workspace_diff.read_project_diff', lambda root: calls.append(root) or {'staged': '', 'unstaged': '', 'untracked': '', 'omitted': []})
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'diff.db'),
                         resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
                         factories={'codex': Owner})
    try:
        with pytest.raises(ValueError, match='attach'):
            host.command('exact', 'before', 'project_diff', {})
        assert not calls
        host.attach('exact')
        assert not host.command('exact', 'bad', 'project_diff', {'cwd': '/other'})['ok']
        assert host.command('exact', 'read', 'project_diff', {})['ok']
        assert [str(root) for root in calls] == [str(tmp_path)]
        assert not host._sessions['exact'][0].sent
    finally:
        host.shutdown()


@pytest.mark.parametrize('mode', ['plan', 'default', None, 'invalid'])
@pytest.mark.parametrize('failure', [False, True])
def test_explicit_resume_restores_saved_mode_before_admission(tmp_path, mode, failure):
    calls = []
    class ModeOwner(Owner):
        async def open(self):
            calls.append('open')
            await super().open()
        async def set_session_mode(self, value):
            calls.append(value)
            if failure:
                raise ValueError('Native mode restoration rejected')
            self.settings = {'collaborationMode': value}
    journal = WorkspaceJournal(tmp_path / 'resume-mode.db')
    if mode is not None:
        journal.append('exact', {'method': 'workspace/settings', 'params': {'collaborationMode': mode}})
    host = WorkspaceHost(journal=journal,
                         resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
                         factories={'codex': ModeOwner})
    try:
        host.events('exact')
        assert calls == []
        result = host.attach('exact')
        if mode == 'invalid':
            assert not result['ok'] and calls == []
        elif failure and mode is not None:
            assert not result['ok'] and host._sessions['exact'][0].closed
        else:
            assert result['ok']
            assert calls == (['open'] if mode is None else ['open', mode])
            host.attach('exact')
            assert calls.count('open') == 1
            if mode == 'plan':
                assert host._work_admission_error('exact') == 'Native session is in Plan mode'
    finally:
        host.shutdown()


@pytest.mark.parametrize('value', [None, 'friendly', 'invalid'])
@pytest.mark.parametrize('failure', [False, True])
def test_personality_restores_only_saved_exact_session_and_closes_on_failure(tmp_path, value, failure):
    calls = []
    class PersonalityOwner(Owner):
        async def set_personality(self, selected):
            calls.append(selected)
            if failure:
                raise ValueError('Unsupported personality')
            return {'currentValue': selected}
    journal = WorkspaceJournal(tmp_path / 'personality.db')
    journal.append('other', {'method':'workspace/settings','params':{'personality':'pragmatic'}})
    if value is not None:
        journal.append('exact', {'method':'workspace/settings','params':{'personality':value}})
    host = WorkspaceHost(journal=journal, resolve=lambda sid: {'session_id':sid,'provider':'codex','cwd':str(tmp_path)},
                         factories={'codex':PersonalityOwner})
    try:
        host.events('exact')
        assert not calls
        result = host.attach('exact')
        assert result['ok'] is (value is None or (value == 'friendly' and not failure))
        assert calls == (['friendly'] if value == 'friendly' else [])
        if failure and value == 'friendly':
            assert host._sessions['exact'][0].closed
        if result['ok']:
            host._work_reservations['exact'] = {'item_id':'job'}
            assert not host.command('exact','blocked','set_personality',{'value':'none'})['ok']
    finally:
        host.shutdown()


def test_agent_history_previews_only_parent_owned_uploads(tmp_path):
    from PIL import Image

    history = []
    class AgentOwner(Owner):
        async def inspect_agent(self, thread_id, cursor):
            return {'thread': {'id': thread_id, 'turns': history}, 'historyCursor': None}
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'media.db'),
                         resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
                         factories={'codex': AgentOwner})
    try:
        raw = io.BytesIO()
        Image.new('RGB', (8, 8), 'green').save(raw, format='PNG')
        parts = []
        tokens = []
        for sid in ['exact', 'foreign']:
            record = host.uploads.save(sid, 'photo.png', io.BytesIO(raw.getvalue()), 'image/png')
            tokens.append(record['token'])
            parts.extend(host.uploads.codex_inputs(sid, [{'type': 'upload', 'token': record['token']}]))
        history.append({'id': 'turn', 'items': [{'type': 'userMessage', 'content': parts}]})
        host.attach('exact')
        reply = host.command('exact', 'media', 'inspect_agent', {'thread_id': 'child', 'cursor': None})
        assert reply['ok']
        assert reply['result']['thread']['id'] == 'child'
        decorated = reply['result']['thread']['turns'][0]['items'][0]['content']
        assert decorated[0]['previewToken'] == tokens[0]
        assert 'previewToken' not in decorated[1]
        assert all('previewToken' not in part for part in parts)
        assert list(host._sessions) == ['exact']
        assert not host._sessions['exact'][0].sent
    finally:
        host.shutdown()


@pytest.mark.parametrize('value', [None, 'priority'])
@pytest.mark.parametrize('failure', [False, True])
def test_saved_speed_restores_before_admission_and_refuses_failed_restore(tmp_path, value, failure):
    calls = []
    class SpeedOwner(Owner):
        async def open(self):
            result = await super().open()
            self.settings = {'model': 'resumed-model'}
            return result
        async def set_speed_tier(self, tier, model):
            calls.append((tier, model))
            if failure:
                raise ValueError('Unsupported speed')
    journal = WorkspaceJournal(tmp_path / 'speed.db')
    journal.append('exact', {'method': 'workspace/speed', 'params': {'model': 'old-model', 'value': value}})
    journal.append('exact', {'method': 'workspace/settings', 'params': {'serviceTier': None}})
    journal.append('exact', {'method': 'workspace/settings', 'params': {'personality': 'none', 'personalityConfirmed': False}})
    assert journal.saved_codex_personality('exact') is None
    assert journal.saved_codex_speed('other') is None
    host = WorkspaceHost(journal=journal, resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)}, factories={'codex': SpeedOwner})
    try:
        assert host.attach('exact')['ok'] is (not failure)
        assert calls == [(value, 'resumed-model')]
        if failure:
            assert host._sessions['exact'][0].closed
    finally:
        host.shutdown()


@pytest.mark.parametrize('setting', ['speed', 'personality'])
def test_failed_saved_setting_recovery_is_exact_confirmed_and_does_not_launch(tmp_path, setting):
    opened = []
    class RecoveryOwner(Owner):
        async def open(self):
            opened.append(self.sid)
            await super().open()
            self.settings = {'model': 'resumed'}
        async def set_speed_tier(self, *args):
            raise ValueError('Unsupported speed')
        async def set_personality(self, *args):
            raise ValueError('Unsupported personality')
        async def set_session_mode(self, mode):
            assert mode == 'plan'
        async def close(self):
            await super().close()
            self.state = 'closed'
        def can_retry_attachment(self):
            return self.state == 'closed' and self.closed
    journal = WorkspaceJournal(tmp_path / 'recovery.db')
    event = ({'method': 'workspace/speed', 'params': {'model': 'old', 'value': 'priority'}} if setting == 'speed'
             else {'method': 'workspace/settings', 'params': {'personality': 'friendly'}})
    journal.append('exact', event)
    journal.append('other', event)
    journal.append('exact', {'method': 'workspace/settings', 'params': {'collaborationMode': 'plan'}})
    host = WorkspaceHost(journal=journal, resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)}, factories={'codex': RecoveryOwner})
    try:
        failed = host.attach('exact')
        assert not failed['ok']
        recovery = failed['setting_recovery']
        assert recovery['setting'] == setting
        assert host._sessions['exact'][0].state == 'closed'
        payload = {'failure_id': recovery['failure_id'], 'confirmed': True}
        for index, bad in enumerate([{**payload, 'confirmed': False}, {**payload, 'failure_id': 'stale'}, {**payload, 'setting': 'mode'}]):
            assert not host.command('exact', f'bad-{index}', 'reset_saved_setting', bad)['ok']
        host._work_reservations['exact'] = 'job'
        assert not host.command('exact', 'reserved', 'reset_saved_setting', payload)['ok']
        host._work_reservations.clear()
        result = host.command('exact', 'reset-once', 'reset_saved_setting', payload)
        assert result['ok'] and result['result']['reconnectRequired']
        assert host.command('exact', 'reset-once', 'reset_saved_setting', payload) == result
        assert opened == ['exact']
        reader = journal.saved_codex_speed if setting == 'speed' else journal.saved_codex_personality
        assert reader('exact') is None and reader('other') is not None
        assert journal.saved_codex_mode('exact') == 'plan'
        assert host.attach('exact')['ok']
        assert opened == ['exact', 'exact']
        assert not host._sessions['exact'][0].sent
    finally:
        host.shutdown()


def test_agent_attachments_validate_parent_scope_and_deduplicate(tmp_path):
    calls = []
    class AgentOwner(Owner):
        async def steer_agent(self, **payload):
            calls.append(payload)
            return {'accepted': True}
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'agent-files.db'),
                         resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
                         factories={'codex': AgentOwner})
    try:
        token = host.uploads.save('exact', 'notes.txt', io.BytesIO(b'notes'))['token']
        foreign = host.uploads.save('foreign', 'notes.txt', io.BytesIO(b'secret'))['token']
        host.attach('exact')
        payload = {'thread_id': 'child', 'expected_turn_id': 'turn', 'inputs': [{'type': 'upload', 'token': token}]}
        assert host.command('exact', 'send', 'steer_agent', payload)['ok']
        assert host.command('exact', 'send', 'steer_agent', payload)['ok']
        assert len(calls) == 1
        assert calls[0]['thread_id'] == 'child' and calls[0]['expected_turn_id'] == 'turn'
        assert calls[0]['inputs'] == host.uploads.codex_inputs('exact', payload['inputs'])
        for index, bad in enumerate([
            [{'type': 'upload', 'token': foreign}], [{'type': 'localImage', 'path': '/etc/passwd'}], [],
        ]):
            assert not host.command('exact', f'bad-{index}', 'steer_agent', {**payload, 'inputs': bad})['ok']
        assert len(calls) == 1 and list(host._sessions) == ['exact']
    finally:
        host.shutdown()


def test_agent_reads_use_existing_parent_even_when_job_reserved(tmp_path):
    calls = []
    class AgentOwner(Owner):
        async def list_agents(self, cursor):
            calls.append(('list', self.sid, cursor))
            return {'data': [], 'nextCursor': None}
        async def inspect_agent(self, thread_id, cursor):
            calls.append(('inspect', self.sid, thread_id, cursor))
            return {'thread': {'id': thread_id, 'turns': []}}
        async def interrupt_agent(self, **payload):
            calls.append(('stop', self.sid, payload))
            return {'requested': True}
        async def steer_agent(self, **payload):
            calls.append(('steer', self.sid, payload))
            return {'accepted': True}
        async def continue_agent(self, **payload):
            calls.append(('continue', self.sid, payload))
            return {'accepted': True}
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'agents.db'),
                         resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
                         factories={'codex': AgentOwner})
    try:
        host.attach('exact')
        host._work_reservations['exact'] = {'item_id': 'job'}
        assert not calls
        assert host.command('exact', 'list', 'agents', {'cursor': None})['ok']
        assert host.command('exact', 'read', 'inspect_agent', {'thread_id': 'child', 'cursor': None})['ok']
        assert not host.command('exact', 'bad', 'inspect_agent', {'thread_id': 'child', 'cursor': None, 'cwd': '/other'})['ok']
        assert calls == [('list', 'exact', None), ('inspect', 'exact', 'child', None)]
        payload = {'thread_id': 'child', 'expected_turn_id': 'child-turn', 'confirmed': True}
        assert host.command('exact', 'stop', 'interrupt_agent', payload)['ok']
        assert host.command('exact', 'stop', 'interrupt_agent', payload)['ok']
        assert calls[2:] == [('stop', 'exact', payload)]
        message = {'thread_id': 'child', 'expected_turn_id': 'child-turn', 'text': 'Focus on tests'}
        assert not host.command('exact', 'reserved-steer', 'steer_agent', message)['ok']
        host._work_reservations.clear()
        assert host.command('exact', 'steer', 'steer_agent', message)['ok']
        assert host.command('exact', 'steer', 'steer_agent', message)['ok']
        assert calls[3:] == [('steer', 'exact', message)]
        continuation = {'thread_id': 'child', 'expected_latest_turn_id': 'old', 'text': 'Continue', 'confirmed': True}
        host._work_reservations['exact'] = {'item_id': 'job'}
        assert not host.command('exact', 'reserved-continue', 'continue_agent', continuation)['ok']
        host._work_reservations.clear()
        host._bridge_queues['exact'] = ['queued']
        assert not host.command('exact', 'queued-continue', 'continue_agent', continuation)['ok']
        host._bridge_queues.clear()
        assert host.command('exact', 'continue', 'continue_agent', continuation)['ok']
        assert host.command('exact', 'continue', 'continue_agent', continuation)['ok']
        assert calls[4:] == [('continue', 'exact', continuation)]
        assert list(host._sessions) == ['exact']
        assert not host._sessions['exact'][0].sent
        owner = host._sessions['exact'][0]
        owner.active_agent_threads = {'child'}
        assert host._sleep_blocker('exact') == 'Delegated agent work is active or uncertain'
        assert host._work_admission_error('exact') == 'Delegated agent work is active or uncertain'
    finally:
        host.shutdown()


def test_goal_controls_are_exact_receipted_and_job_guarded(tmp_path):
    calls = []
    class GoalOwner(Owner):
        async def get_goal(self):
            return {'goal': None}
        async def update_goal(self, **payload):
            calls.append((self.sid, payload))
            return {'goal': None}
        async def clear_goal(self, **payload):
            calls.append((self.sid, payload))
            return {'goal': None}
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'goals.db'),
                         resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
                         factories={'codex': GoalOwner})
    payload = {'expected': None, 'confirmed': True, 'changes': {'status': 'paused'}}
    try:
        host.attach('exact')
        assert not calls
        assert host.command('exact', 'read', 'goal', {})['ok']
        assert host.command('exact', 'update', 'update_goal', payload)['ok']
        assert host.command('exact', 'update', 'update_goal', payload)['ok']
        assert calls == [('exact', payload)]
        assert not host.command('exact', 'bad', 'goal', {'threadId': 'other'})['ok']
        host._work_reservations['exact'] = {'item_id': 'job'}
        assert not host.command('exact', 'blocked', 'update_goal', payload)['ok']
        assert host.command('exact', 'read-reserved', 'goal', {})['ok']
        host._work_reservations.clear()
        host._bridge_queues['exact'] = ['pending']
        assert not host.command('exact', 'queued', 'clear_goal', {'expected': None, 'confirmed': True})['ok']
        assert calls == [('exact', payload)]
        host._bridge_queues.clear()
    finally:
        host.shutdown()


def test_codex_mode_controls_are_explicit_receipted_and_job_guarded(tmp_path):
    calls = []
    class ModeOwner(Owner):
        async def list_session_modes(self):
            calls.append(('list', self.sid))
            return {'currentValue': None, 'options': [{'name': 'Plan', 'value': 'plan'}]}
        async def set_session_mode(self, mode):
            calls.append((mode, self.sid))
            return {'currentValue': mode, 'options': []}
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'modes.db'),
                         resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
                         factories={'codex': ModeOwner})
    try:
        with pytest.raises(ValueError, match='attach'):
            host.command('exact', 'before', 'session_modes', {})
        assert not calls
        host.attach('exact')
        assert host.command('exact', 'list', 'session_modes', {})['ok']
        result = host.command('exact', 'set', 'set_session_mode', {'mode': 'plan'})
        assert result['ok']
        assert host.command('exact', 'set', 'set_session_mode', {'mode': 'plan'}) == result
        host._sessions['exact'][0].settings = {'collaborationMode': 'plan'}
        assert host._work_admission_error('exact') == 'Native session is in Plan mode'
        host._work_reservations['exact'] = {'item_id': 'job'}
        assert not host.command('exact', 'job', 'set_session_mode', {'mode': 'default'})['ok']
        assert calls == [('list', 'exact'), ('plan', 'exact')]
        assert not host._sessions['exact'][0].sent
    finally:
        host.shutdown()


def test_diagnostics_route_is_exact_receipted_and_never_submits(tmp_path):
    calls = []

    class DiagnosticOwner(Owner):
        async def diagnostics(self):
            calls.append(self.sid)
            return {"command": "claude doctor", "exitCode": 3, "output": "Native warning"}

    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "doctor.db"),
                         resolve=lambda sid: {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)},
                         factories={"claude": DiagnosticOwner})
    try:
        assert not calls
        host.attach("exact")
        result = host.command("exact", "doctor-once", "diagnostics", {})
        assert result["ok"] and result["result"]["exitCode"] == 3
        assert host.command("exact", "doctor-once", "diagnostics", {}) == result
        assert not host.command("exact", "invalid", "diagnostics", {"repair": True})["ok"]
        assert calls == ["exact"]
        assert not host._sessions["exact"][0].sent
    finally:
        host.shutdown()


def test_gemini_explicit_owner_uses_acp_mapping_and_deduplicates_delivery(tmp_path):
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "gemini.db"),
                         resolve=lambda sid: {"session_id": sid, "provider": "gemini", "cwd": str(tmp_path)},
                         factories={"gemini": Owner})
    try:
        assert not host._sessions
        host.events("exact")
        assert not host._sessions
        assert host.attach("exact")["provider"] == "gemini"
        owner = host._sessions["exact"][0]
        document = host.uploads.save("exact", "notes.txt", io.BytesIO(b"notes"))
        payload = {"inputs": [{"type": "upload", "token": document["token"]}]}
        receipt = host.command("exact", "one", "submit", payload)
        assert receipt["ok"]
        assert host.command("exact", "one", "submit", payload) == receipt
        assert len(owner.sent) == 1
        assert owner.sent[0][0]["type"] == "resource_link"
        assert owner.sent[0][0]["name"] == "notes.txt"
        assert host.attach("exact")["provider"] == "gemini"
        assert host._sessions["exact"][0] is owner
    finally:
        host.shutdown()


@pytest.mark.parametrize("blocked", [None, "running", "background", "queued", "confirmation", "cleanup"])
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_explicit_disconnect_preserves_history_and_never_stops_other_owner(tmp_path, blocked, provider):
    class DisconnectOwner(Owner):
        closes = 0

        async def list_background_tasks(self):
            return {"data": [{}] if blocked == "background" else []}

        async def close(self):
            self.closes += 1
            self.state = "closed"

        def can_retry_attachment(self):
            return self.state == "closed" and blocked != "cleanup"

    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "close.db"),
                         resolve=lambda sid: {"session_id": sid, "provider": provider, "cwd": str(tmp_path)},
                         factories={provider: DisconnectOwner})
    try:
        host.attach("source")
        host.attach("other")
        owner = host._sessions["source"][0]
        if blocked == "running":
            owner.state = "running"
        if blocked == "queued":
            host._bridge_queues["source"] = ["pending"]
        history = host.events("source")["events"]
        payload = {"confirmed": blocked != "confirmation"}
        result = host.command("source", "disconnect", "disconnect_session", payload)
        assert result["ok"] is (blocked is None)
        assert host.command("source", "disconnect", "disconnect_session", payload) == result
        assert owner.closes == (1 if blocked in {None, "cleanup"} else 0)
        assert host._sessions["other"][0].closes == 0
        assert host.events("source")["events"][:len(history)] == history
        if blocked is None:
            assert host.attach("source")["ok"]
            assert host._sessions["source"][0] is not owner
    finally:
        host.shutdown()


@pytest.mark.parametrize("failure", [None, "checkpoint", "handoff", "receipt"])
def test_clear_checkpoint_exact_owner_routing_and_no_replay(tmp_path, monkeypatch, failure):
    target = "11111111-2222-4333-8444-555555555555"
    clears = []

    class ClearOwner(Owner):
        async def begin_clear(self):
            clears.append(self.sid)
            self.state = "awaiting-handoff"
            return {"session_id": target, "provider": "claude", "cwd": str(tmp_path)}

        async def commit_clear(self, sid, *, publish):
            assert value.journal.has_pending_clear("source")
            assert "source" not in value._sessions
            assert value._sessions[sid][0] is self
            self.sid, self.publish = sid, publish
            if failure == "handoff":
                raise RuntimeError("ack lost")
            await publish({"method": "workspace/history", "params": {"thread": {"id": sid, "turns": []}}})
            self.state = "ready"

    value = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "clear.db"),
                          resolve=lambda sid: {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)},
                          factories={"claude": ClearOwner})
    try:
        value.attach("source")
        original = value._sessions["source"][0]
        if failure in {"checkpoint", "receipt"}:
            def fail(*args):
                raise OSError("disk full")
            monkeypatch.setattr(value.journal, "prepare_clear" if failure == "checkpoint" else "complete_clear", fail)
        result = value.command("source", "clear-1", "clear_session", {"confirmed": True})
        assert result["ok"] is (failure is None)
        assert value.command("source", "clear-1", "clear_session", {"confirmed": True}) == result
        assert clears == ["source"]
        if failure:
            assert original.state == "unavailable"
        else:
            assert value.attach(target)["ok"]
            assert value._sessions[target][0] is original
            prior_source = value.events("source")
            assert value.command(target, "new-input", "submit", {"inputs": [{"type": "text", "text": "hello"}]})["ok"]
            assert value.events("source") == prior_source
            assert value.events(target)["events"][-1]["event"]["params"]["threadId"] == target
            assert not value.journal.has_pending_clear("source")
        reopened = WorkspaceJournal(value.journal.path)
        assert reopened.command_receipt("source", "clear-1", {"action": "clear_session", "payload": {"confirmed": True}}) == (True, result)
        if failure != "checkpoint":
            assert reopened.clear_target(target)["committed"] is (failure is None)
    finally:
        value.shutdown()


@pytest.mark.parametrize('failure', [None, 'background', 'agent', 'cleanup', 'creation', 'checkpoint', 'receipt'])
def test_codex_clear_releases_writer_before_durable_creation(tmp_path, monkeypatch, failure):
    from uuid import uuid4
    target = str(uuid4())
    calls = []

    class ClearOwner(Owner):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.questions = {}
            self.active_agent_threads = set()

        @property
        def session_id(self):
            return self.sid

        async def list_background_tasks(self):
            return {'data': [{}] if failure == 'background' else []}

        async def close(self):
            calls.append(('close', self.sid))
            self.state = 'closed'

        def can_retry_attachment(self):
            return self.state == 'closed' and failure != 'cleanup'

        async def create(self, *, checkpoint):
            assert host._sessions['source'][0].state == 'closed'
            calls.append(('create', self.sid))
            if failure == 'creation':
                raise RuntimeError('native response lost')
            await checkpoint({'session_id': target, 'provider': 'codex', 'cwd': str(tmp_path)})
            self.sid = target
            self.state = 'ready'

    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'codex-clear.db'),
                         resolve=lambda sid: {'session_id': sid, 'provider': 'codex', 'cwd': str(tmp_path)},
                         factories={'codex': ClearOwner})
    try:
        host.attach('source')
        original = host._sessions['source'][0]
        if failure == 'agent':
            original.active_agent_threads.add('child')
        if failure in {'checkpoint', 'receipt'}:
            def fail(*args):
                raise OSError('disk full')
            monkeypatch.setattr(host.journal, 'prepare_clear' if failure == 'checkpoint' else 'complete_clear', fail)
        result = host.command('source', 'once', 'clear_session', {'confirmed': True})
        assert result['ok'] is (failure is None)
        before = list(calls)
        assert host.command('source', 'once', 'clear_session', {'confirmed': True}) == result
        assert calls == before
        if failure in {'background', 'agent'}:
            assert calls == []
        elif failure == 'cleanup':
            assert calls == [('close', 'source')]
        else:
            assert calls[0] == ('close', 'source') and calls[1][0] == 'create'
            assert len(calls) == 2
            if failure != 'creation':
                assert host._sessions[target][0] is not original
                assert host.journal.creation_target(calls[1][1][4:])['committed']
            if failure is None:
                assert result['result']['session_id'] == target
                assert host.journal.clear_target(target)['committed']
    finally:
        host.shutdown()


@pytest.mark.parametrize("rename_failure", [False, True])
def test_codex_named_clear_renames_exact_created_owner_without_repeating_creation(tmp_path, rename_failure):
    from uuid import uuid4

    target = str(uuid4())
    calls = []

    class NamedClearOwner(Owner):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.questions = {}
            self.active_agent_threads = set()

        @property
        def session_id(self):
            return self.sid

        async def list_background_tasks(self):
            return {"data": []}

        async def close(self):
            calls.append(("close", self.sid))
            self.state = "closed"

        def can_retry_attachment(self):
            return self.state == "closed"

        async def create(self, *, checkpoint):
            calls.append(("create", self.sid))
            await checkpoint({"session_id": target, "provider": "codex", "cwd": str(tmp_path)})
            self.sid = target
            self.state = "ready"

        async def rename(self, name):
            calls.append(("rename", self.sid, name))
            if rename_failure:
                raise RuntimeError("rename offline")
            return {"session_id": self.sid, "name": name}

    host = WorkspaceHost(
        journal=WorkspaceJournal(tmp_path / "named-clear.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "codex", "cwd": str(tmp_path)},
        factories={"codex": NamedClearOwner},
    )
    payload = {"confirmed": True, "name": "Next work"}
    try:
        host.attach("source")
        result = host.command("source", "named", "clear_session", payload)
        assert result["ok"]
        assert result["result"]["session_id"] == target
        assert result["result"]["requestedName"] == "Next work"
        assert result["result"]["nameConfirmed"] is (not rename_failure)
        assert ("nameError" in result["result"]) is rename_failure
        before = list(calls)
        assert host.command("source", "named", "clear_session", payload) == result
        assert calls == before
        assert [call[0] for call in calls] == ["close", "create", "rename"]
        pending = host.describe_pending_session(target)
        assert pending["title"] == ("New Codex conversation" if rename_failure else "Next work")
    finally:
        host.shutdown()


def test_claude_named_clear_uses_confirmed_native_transition_without_replay(tmp_path):
    target = "11111111-2222-4333-8444-555555555555"
    calls = []

    class NamedClearOwner(Owner):
        async def begin_clear(self, name=None):
            calls.append(("clear", self.sid, name))
            self.state = "awaiting-handoff"
            return {
                "session_id": target,
                "provider": "claude",
                "cwd": str(tmp_path),
                "requestedName": name,
                "nameConfirmed": True,
            }

        async def commit_clear(self, sid, *, publish):
            calls.append(("commit", sid))
            self.sid = sid
            self.state = "ready"
            await publish({"method": "workspace/history", "params": {"thread": {"id": sid, "turns": []}}})

    host = WorkspaceHost(
        journal=WorkspaceJournal(tmp_path / "claude-named-clear.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)},
        factories={"claude": NamedClearOwner},
    )
    payload = {"confirmed": True, "name": "Next work"}
    try:
        host.attach("source")
        result = host.command("source", "named", "clear_session", payload)
        assert result["ok"]
        assert result["result"]["session_id"] == target
        assert result["result"]["requestedName"] == "Next work"
        assert result["result"]["nameConfirmed"] is True
        assert host.command("source", "named", "clear_session", payload) == result
        assert calls == [("clear", "source", "Next work"), ("commit", target)]
        assert host.describe_pending_session(target)["title"] == "Next work"
    finally:
        host.shutdown()


def test_clear_requires_confirmation_idle_owner_and_no_queued_bridge(tmp_path):
    value = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "clear.db"),
                          resolve=lambda sid: {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)},
                          factories={"claude": Owner})
    try:
        value.attach("source")
        for index, payload in enumerate(({}, {"confirmed": False}, {"confirmed": 1})):
            assert not value.command("source", str(index), "clear_session", payload)["ok"]
        value._bridge_queues["source"] = ["bridge:queued"]
        result = value.command("source", "queued", "clear_session", {"confirmed": True})
        assert not result["ok"] and result["retryable"]
        assert value._sessions["source"][0].state == "ready"
    finally:
        value.shutdown()


def test_unfinished_clear_checkpoint_blocks_source_resume_without_launch(tmp_path):
    journal = WorkspaceJournal(tmp_path / "clear.db")
    payload = {"action": "clear_session", "payload": {"confirmed": True}}
    journal.claim_command("source", "clear", payload)
    journal.prepare_clear("source", "clear", {"session_id": "11111111-2222-4333-8444-555555555555",
                                             "provider": "claude", "cwd": str(tmp_path)})
    value = WorkspaceHost(journal=WorkspaceJournal(journal.path), resolve=lambda sid: pytest.fail("must not resolve"))
    try:
        with pytest.raises(ValueError, match="unconfirmed"):
            value.attach("source")
        result = value.command("source", "clear", "clear_session", {"confirmed": True})
        assert result["uncertain"] and not value._sessions
    finally:
        value.shutdown()


def test_pending_clear_catalog_filters_deduplicates_and_retires_after_indexing(tmp_path):
    from core.config import claude_project_dir_for

    journal = WorkspaceJournal(tmp_path / "clear.db")
    target = {"session_id": "11111111-2222-4333-8444-555555555555", "provider": "claude", "cwd": str(tmp_path)}
    journal.claim_command("source", "clear", {"action": "clear_session", "payload": {"confirmed": True}})
    journal.prepare_clear("source", "clear", target)
    host = WorkspaceHost(journal=journal, resolve=lambda sid: pytest.fail("catalog must not attach"))
    try:
        assert host.include_pending_sessions([]) == []
        journal.complete_clear("source", "clear")
        rows = host.include_pending_sessions([], projects=[claude_project_dir_for(str(tmp_path))])
        assert len(rows) == 1 and rows[0]["session_id"] == target["session_id"]
        assert rows[0]["native_persistence_pending"] and rows[0]["created_at"]
        assert host.include_pending_sessions([], projects=["another-project"]) == []
        actual = {"session_id": target["session_id"], "display_title": "Actual title"}
        assert host.include_pending_sessions([actual]) == [actual]
        assert host.include_pending_sessions([]) == []  # Deleted indexed chat cannot reappear.
        assert not host._sessions and host._loop is None
    finally:
        host.shutdown()


@pytest.mark.parametrize("case", ["missing", "persisted", "ambiguous"])
def test_pending_delete_retains_recovery_and_rejects_owned_target(tmp_path, monkeypatch, case):
    import json

    from core import indexer, metadata
    from core.workspace_catalog import register_fork
    from core.workspace_lease import SessionLease, SessionOwnedError

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("SERENA_RUNTIME_LEASE_DIR", str(tmp_path / "leases"))
    monkeypatch.setattr(indexer, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(indexer, "DB_PATH", tmp_path / "data/index.db")
    monkeypatch.setattr(indexer, "_INDEX_LOCK_PATH", tmp_path / "data/index.lock")
    monkeypatch.setattr(metadata, "METADATA_DIR", tmp_path / "meta")
    monkeypatch.setattr(metadata, "METADATA_PATH", tmp_path / "legacy.json")
    monkeypatch.setattr(metadata, "_migrated", False)
    sid = "11111111-2222-4333-8444-555555555555"
    journal = WorkspaceJournal(tmp_path / "workspace.db")
    target = {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)}
    journal.claim_command("source", "clear", {"action": "clear_session", "payload": {"confirmed": True}})
    journal.prepare_clear("source", "clear", target)
    host = WorkspaceHost(journal=journal, resolve=lambda sid: pytest.fail("must not launch"), register_fork=register_fork)
    assert host.delete_pending_session(sid, source="proof") is None
    journal.complete_clear("source", "clear")
    journal.append(sid, {"method": "proof", "params": {"text": "retained"}})
    metadata.set_custom_title(sid, "Keep title")
    if case != "missing":
        transcript = tmp_path / "claude/projects/project" / f"{sid}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"type": "user", "cwd": str(tmp_path), "message": {"content": "Native history"}}) + "\n")
        if case == "ambiguous":
            other = transcript.parent.parent / "other" / transcript.name
            other.parent.mkdir()
            other.write_bytes(transcript.read_bytes())
    lease = SessionLease(sid)
    try:
        with pytest.raises(SessionOwnedError):
            host.delete_pending_session(sid, source="proof")
        assert journal.uncataloged_clears() and metadata.get_meta(sid)["custom_title"] == "Keep title"
    finally:
        lease.release()
    if case == "ambiguous":
        with pytest.raises(ValueError, match="ambiguous"):
            host.delete_pending_session(sid, source="proof")
        assert journal.uncataloged_clears() and metadata.get_meta(sid)["custom_title"] == "Keep title"
        assert not list(tmp_path.rglob("recovery.json"))
        return
    assert host.delete_pending_session(sid, source="proof")
    assert not journal.uncataloged_clears() and host.describe_pending_session(sid) is None
    assert not metadata.get_meta(sid)
    assert journal.read(sid)["events"][0]["event"]["params"]["text"] == "retained"
    manifests = list(tmp_path.rglob("recovery.json"))
    assert len(manifests) == 1 and json.loads(manifests[0].read_text())["metadata"]["custom_title"] == "Keep title"
    assert host.delete_pending_session(sid, source="proof") is None
    assert host._loop is None and not host._sessions


def test_completed_clear_indexes_only_persisted_committed_target_and_retries_failure(tmp_path):
    journal = WorkspaceJournal(tmp_path / "catalog.db")
    target = {"session_id": "11111111-2222-4333-8444-555555555555", "provider": "claude", "cwd": str(tmp_path)}
    attempts = []

    def register(value):
        attempts.append(value)
        if len(attempts) == 1:
            raise ValueError("Transcript not flushed yet")

    host = WorkspaceHost(journal=journal, resolve=lambda sid: pytest.fail("must not launch"), register_fork=register)
    sid = target["session_id"]
    complete = {"method": "turn/completed", "params": {"threadId": sid, "turn": {"id": "t"}}}

    async def exercise():
        await host._publish(sid, complete)
        assert not attempts
        journal.claim_command("source", "clear", {"action": "clear_session", "payload": {"confirmed": True}})
        journal.prepare_clear("source", "clear", target)
        await host._publish(sid, complete)
        assert not attempts  # The native clear receipt itself is not a new turn.
        journal.complete_clear("source", "clear")
        await host._publish(sid, {"method": "workspace/history", "params": {}})
        assert not attempts
        await host._publish(sid, complete)
        assert journal.uncataloged_clears()
        failure = journal.read(sid)["events"][-1]["event"]
        assert failure["method"] == "workspace/catalog" and not failure["params"]["indexed"]
        await host._publish(sid, complete)
        assert not journal.uncataloged_clears()
        await host._publish(sid, complete)
        assert attempts == [target, target]
        assert sum(item["event"]["method"] == "turn/completed" for item in journal.read(sid)["events"]) == 5

    asyncio.run(exercise())
    assert host._loop is None and not host._sessions


def test_native_catalog_flush_retry_is_bounded_and_keeps_pending_identity(tmp_path):
    from core.workspace_catalog import NativeTranscriptPending

    journal = WorkspaceJournal(tmp_path / "retry.db")
    sid = "11111111-2222-4333-8444-555555555555"
    target = {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)}
    journal.claim_command("source", "clear", {"action": "clear_session", "payload": {"confirmed": True}})
    journal.prepare_clear("source", "clear", target)
    journal.complete_clear("source", "clear")
    calls = []

    def register(value):
        calls.append(value)
        raise NativeTranscriptPending("Waiting for native flush")

    host = WorkspaceHost(journal=journal, resolve=lambda sid: pytest.fail("no launch"), register_fork=register)
    event = {"method": "turn/completed", "params": {"turn": {"providerOriginal": {"user_message_uuid": "exact-prompt"}}}}
    asyncio.run(host._publish(sid, event))
    assert calls == [{**target, "prompt_id": "exact-prompt"}] * 5
    assert journal.uncataloged_clears()
    assert journal.read(sid)["events"][0]["event"] == event
    assert journal.read(sid)["events"][-1]["event"]["params"]["retryable"]
    assert not host._sessions and host._loop is None


def test_clear_preflight_failure_does_not_disable_unchanged_owner(tmp_path):
    class BusyOwner(Owner):
        async def begin_clear(self):
            raise RuntimeError("Background task still running")
    value = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "clear.db"),
                          resolve=lambda sid: {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)},
                          factories={"claude": BusyOwner})
    try:
        value.attach("source")
        result = value.command("source", "clear", "clear_session", {"confirmed": True})
        assert not result["ok"]
        assert value._sessions["source"][0].state == "ready"
        assert not value.journal.has_pending_clear("source")
    finally:
        value.shutdown()


def test_clear_never_replaces_an_existing_target_owner(tmp_path):
    target = "11111111-2222-4333-8444-555555555555"
    class CollisionOwner(Owner):
        async def begin_clear(self):
            self.state = "awaiting-handoff"
            return {"session_id": target, "provider": "claude", "cwd": str(tmp_path)}
        async def commit_clear(self, *args, **kwargs):
            pytest.fail("must not acknowledge an occupied identity")
    value = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "clear.db"),
                          resolve=lambda sid: {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)},
                          factories={"claude": CollisionOwner})
    try:
        value.attach(target)
        existing = value._sessions[target][0]
        value.attach("source")
        result = value.command("source", "clear", "clear_session", {"confirmed": True})
        assert not result["ok"] and "already has" in result["error"]
        assert value._sessions[target][0] is existing and existing.state == "ready"
        assert value._sessions["source"][0].state == "unavailable"
    finally:
        value.shutdown()


@pytest.fixture
def host(tmp_path):
    Owner.instances = []
    value = WorkspaceHost(
        journal=WorkspaceJournal(tmp_path / "events.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "codex", "cwd": str(tmp_path)},
        factories={"codex": Owner},
    )
    yield value
    value.shutdown()


def test_explicit_reattach_only_replaces_verified_cleaned_owner(host):
    host.attach("exact")
    first = Owner.instances[-1]
    first.state = "unavailable"
    assert not host.attach("exact")["ok"]
    assert Owner.instances == [first] and not first.closed
    first.can_retry_attachment = lambda: False
    assert not host.attach("exact")["ok"]
    assert Owner.instances == [first] and not first.closed
    first.can_retry_attachment = lambda: True
    # Reads and repeated commands never invoke the explicit attach path.
    host.events("exact")
    assert Owner.instances == [first]
    assert host.attach("exact")["ok"]
    assert len(Owner.instances) == 2
    assert Owner.instances[-1].sid == "exact" and not first.closed
    assert not first.sent and not Owner.instances[-1].sent
    assert host.attach("exact")["ok"]
    assert len(Owner.instances) == 2


def test_unconfirmed_receipt_survives_explicit_owner_replacement(host):
    host.attach("exact")
    payload = {"action": "submit", "payload": {"inputs": [{"type": "text", "text": "do not repeat"}]}}
    host.journal.claim_command("exact", "uncertain", payload)
    first = Owner.instances[-1]
    first.state = "closed"
    first.can_retry_attachment = lambda: True
    assert host.attach("exact")["ok"]
    result = host.command("exact", "uncertain", "submit", payload["payload"])
    assert not result["ok"] and result["uncertain"]
    assert not Owner.instances[-1].sent


def test_interrupt_rejects_stale_displayed_turn_and_replays_receipt_without_stopping_new_turn(host):
    host.attach("exact")
    owner = host._sessions["exact"][0]
    calls = []
    async def interrupt():
        calls.append(owner.active_turn)
        return {"interrupted": True}
    owner.interrupt = interrupt
    owner.active_turn = "current"
    stale = host.command("exact", "stale", "interrupt", {"expectedTurnId": "old"})
    assert not stale["ok"] and "no longer active" in stale["error"]
    assert calls == []
    current = host.command("exact", "stop", "interrupt", {"expectedTurnId": "current"})
    assert current["ok"] and calls == ["current"]
    owner.active_turn = "next"
    assert host.command("exact", "stop", "interrupt", {"expectedTurnId": "current"}) == current
    assert calls == ["current"]
    invalid = host.command("exact", "invalid", "interrupt", {"expectedTurnId": None})
    assert not invalid["ok"]


def test_older_history_uses_exact_owner_cursor_and_receipt(host):
    host.attach("exact")
    owner = Owner.instances[-1]
    calls = []
    async def older(cursor):
        calls.append((owner.sid, cursor))
        return {"turns": [], "historyCursor": None}
    owner.load_earlier = older
    assert not host.command("exact", "bad-history", "load_earlier", {"cursor": 1})["ok"]
    payload = {"cursor": "opaque"}
    result = host.command("exact", "history", "load_earlier", payload)
    assert result["ok"] and calls == [("exact", "opaque")]
    assert host.command("exact", "history", "load_earlier", payload) == result
    assert len(calls) == 1


def test_failed_history_read_can_retry_without_mutating_work(host):
    host.attach("exact")
    owner = Owner.instances[-1]
    calls = []
    async def older(cursor):
        calls.append(cursor)
        if len(calls) == 1:
            raise TimeoutError("read timed out")
        return {"turns": [], "historyCursor": None}
    owner.load_earlier = older
    payload = {"cursor": "opaque"}
    failed = host.command("exact", "read-1", "load_earlier", payload)
    assert not failed["ok"] and failed["retryable"]
    assert host.command("exact", "read-1", "load_earlier", payload) == failed
    assert calls == ["opaque"]
    assert host.command("exact", "read-2", "load_earlier", payload)["ok"]
    assert calls == ["opaque", "opaque"] and not owner.sent


def test_permission_mode_control_requires_explicit_boolean_confirmation(tmp_path):
    calls = []
    class PermissionOwner(Owner):
        async def set_permissions(self, mode, confirmed):
            calls.append((self.sid, mode, confirmed))
            return {"mode": mode}
    value = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "permissions.db"), resolve=lambda sid: {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)}, factories={"claude": PermissionOwner})
    try:
        value.attach("exact")
        assert not value.command("exact", "invalid", "set_permissions", {"mode": "plan", "confirmed": "false"})["ok"]
        payload = {"mode": "plan", "confirmed": False}
        first = value.command("exact", "set", "set_permissions", payload)
        assert first["ok"]
        assert value.command("exact", "set", "set_permissions", payload) == first
        assert calls == [("exact", "plan", False)]
        assert not PermissionOwner.instances[-1].sent
    finally:
        value.shutdown()


def test_context_control_reads_attached_claude_without_query(tmp_path):
    class ContextOwner(Owner):
        async def context_usage(self):
            return {"model": "native", "percentage": 15}
    value = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "context.db"), resolve=lambda sid: {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)}, factories={"claude": ContextOwner})
    try:
        with pytest.raises(ValueError, match="Explicitly attach"):
            value.command("exact", "before", "context_usage", {})
        value.attach("exact")
        assert value.command("exact", "read", "context_usage", {})["result"] == {"model": "native", "percentage": 15}
        assert not value.command("exact", "invalid", "context_usage", {"query": True})["ok"]
        assert not ContextOwner.instances[-1].sent
    finally:
        value.shutdown()


def test_host_routes_skill_steering_to_exact_existing_owner(host):
    host.attach("exact")
    owner = Owner.instances[-1]
    received = []
    async def steer(inputs, *, expected_turn_id, skills):
        received.append((inputs, expected_turn_id, skills))
        return {"turnId": expected_turn_id}
    owner.steer = steer
    result = host.command("exact", "skill-steer", "steer", {"inputs": [{"type": "text", "text": ""}], "expectedTurnId": "active", "skills": ["/skill"]})
    assert result["ok"]
    assert received == [([{"type": "text", "text": ""}], "active", ["/skill"])]
    assert not owner.sent


def test_codex_mcp_discovery_uses_attached_owner_without_claude_mutations(host):
    host.attach("exact")
    owner = Owner.instances[-1]
    calls = []
    async def inventory(verbose=False):
        calls.append(verbose)
        return {"data": [{"name": "local", "status": "unknown"}]}
    owner.list_mcp_servers = inventory
    assert host.command("exact", "list-mcp", "mcp_servers", {})["result"] == {"data": [{"name": "local", "status": "unknown"}]}
    assert host.command("exact", "verbose-mcp", "mcp_servers", {"verbose": True})["result"] == {"data": [{"name": "local", "status": "unknown"}]}
    assert not host.command("exact", "bad-verbose", "mcp_servers", {"verbose": 1})["ok"]
    assert not host.command("exact", "bad-mutation", "mcp_server_control", {"name": "local", "action": "disable"})["ok"]
    assert calls == [False, True]
    assert not owner.sent


def test_codex_mcp_actions_are_explicit_and_receipted(host):
    host.attach("exact")
    owner = Owner.instances[-1]
    calls = []
    async def login(name):
        calls.append(name)
        return {"status": "pending"}
    async def reload():
        calls.append("reload")
        return {"data": []}
    owner.login_mcp, owner.reload_mcp = login, reload
    assert calls == []
    result = host.command("exact", "login", "mcp_login", {"name": "local"})
    assert result["ok"]
    assert host.command("exact", "login", "mcp_login", {"name": "local"}) == result
    assert calls == ["local"]
    assert host.command("exact", "reload", "mcp_reload", {})["ok"]
    assert not host.command("exact", "bad-reload", "mcp_reload", {"threadId": "other"})["ok"]
    owner.state = "running"
    result = host.command("exact", "busy", "mcp_login", {"name": "local"})
    assert not result["ok"] and result["retryable"]
    assert calls == ["local", "reload"]


def test_codex_mcp_setting_requires_exact_payload_and_reuses_receipt(host):
    host.attach("exact")
    owner = Owner.instances[-1]
    calls = []
    async def setting(name, enabled):
        calls.append((name, enabled))
        return {"data": [], "effectiveEnabled": enabled}
    owner.set_mcp_enabled = setting
    assert not host.command("exact", "bad", "set_mcp_enabled", {"name": "local", "enabled": False, "filePath": "/other"})["ok"]
    assert not host.command("exact", "bad-type", "set_mcp_enabled", {"name": "local", "enabled": "false"})["ok"]
    result = host.command("exact", "setting", "set_mcp_enabled", {"name": "local", "enabled": False})
    assert result["ok"]
    assert host.command("exact", "setting", "set_mcp_enabled", {"name": "local", "enabled": False}) == result
    owner.state = "running"
    busy = host.command("exact", "busy", "set_mcp_enabled", {"name": "local", "enabled": True})
    assert not busy["ok"] and busy["retryable"]
    assert calls == [("local", False)]


def test_codex_skill_setting_requires_exact_payload_and_reuses_receipt(host):
    host.attach("exact")
    owner = Owner.instances[-1]
    calls = []
    async def setting(path, enabled):
        calls.append((path, enabled))
        return {"data": [], "effectiveEnabled": enabled}
    owner.set_skill_enabled = setting
    payload = {"path": "/native/SKILL.md", "enabled": False}
    first = host.command("exact", "skill", "set_skill_enabled", payload)
    assert first["ok"]
    assert host.command("exact", "skill", "set_skill_enabled", payload) == first
    assert not host.command("exact", "bad-skill", "set_skill_enabled", {**payload, "name": "injected"})["ok"]
    owner.state = "running"
    rejected = host.command("exact", "busy-skill", "set_skill_enabled", payload)
    assert not rejected["ok"] and rejected["retryable"]
    assert calls == [("/native/SKILL.md", False)]


def test_codex_extended_controls_route_exact_owner_and_respect_reservation(host):
    host.attach("exact")
    owner = Owner.instances[-1]
    calls = []

    async def read(name):
        calls.append((name, {}))
        return {"kind": name}

    owner.config_diagnostics = lambda: read("config_diagnostics")
    owner.experimental_features = lambda: read("experimental_features")
    owner.memory_settings = lambda: read("memory_settings")
    owner.guardian_denial = lambda: calls.append(("guardian_denial", {})) or {
        "denial": None
    }
    owner.detect_external_imports = lambda: read("detect_external_imports")

    async def mutation(action_name, **payload):
        calls.append((action_name, payload))
        return {"kind": action_name}

    owner.set_experimental_feature = lambda **payload: mutation(
        "set_experimental_feature", **payload
    )
    owner.set_memory_mode = lambda **payload: mutation("set_memory_mode", **payload)
    owner.set_memory_defaults = lambda **payload: mutation(
        "set_memory_defaults", **payload
    )
    owner.approve_guardian_denial = lambda **payload: mutation(
        "approve_guardian_denial", **payload
    )
    owner.submit_feedback = lambda **payload: mutation("submit_feedback", **payload)
    owner.import_external_items = lambda **payload: mutation(
        "import_external_items", **payload
    )

    reads = (
        "config_diagnostics",
        "experimental_features",
        "memory_settings",
        "guardian_denial",
        "detect_external_imports",
    )
    host._work_reservations["exact"] = "job"
    for index, action in enumerate(reads):
        result = host.command("exact", f"reserved-read-{index}", action, {})
        assert result["ok"]
    assert [name for name, _ in calls] == list(reads)

    mutations = [
        (
            "set_experimental_feature",
            {"name": "proof", "enabled": True, "confirmed": True},
        ),
        ("set_memory_mode", {"mode": "disabled", "confirmed": True}),
        (
            "set_memory_defaults",
            {
                "use_memories": True,
                "generate_memories": False,
                "confirmed": True,
            },
        ),
        (
            "approve_guardian_denial",
            {"review_id": "review", "confirmed": True},
        ),
        (
            "submit_feedback",
            {
                "classification": "bug",
                "reason": "reason",
                "include_logs": False,
                "confirmed": True,
            },
        ),
        (
            "import_external_items",
            {"candidate_ids": ["candidate"], "confirmed": True},
        ),
    ]
    for index, (action, payload) in enumerate(mutations):
        result = host.command("exact", f"reserved-write-{index}", action, payload)
        assert not result["ok"] and result["retryable"]
    assert [name for name, _ in calls] == list(reads)

    host._work_reservations.clear()
    for index, (action, payload) in enumerate(mutations):
        result = host.command("exact", f"write-{index}", action, payload)
        assert result["ok"] and result["result"] == {"kind": action}
        assert host.command("exact", f"write-{index}", action, payload) == result
    assert [name for name, _ in calls] == list(reads) + [
        action for action, _ in mutations
    ]
    assert not host.command(
        "exact",
        "bad-feature",
        "set_experimental_feature",
        {"name": "proof", "enabled": "true", "confirmed": True},
    )["ok"]
    assert not host.command(
        "exact",
        "bad-import",
        "import_external_items",
        {"candidate_ids": "candidate", "confirmed": True},
    )["ok"]
    assert not owner.sent


def test_mcp_controls_require_attach_and_replay_without_repeating(tmp_path):
    calls = []
    class McpOwner(Owner):
        async def list_mcp_servers(self):
            return {"data": []}
        async def control_mcp_server(self, name, action):
            calls.append((self.sid, name, action))
            return {"data": []}
    value = WorkspaceHost(
        journal=WorkspaceJournal(tmp_path / "mcp.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)},
        factories={"claude": McpOwner},
    )
    try:
        with pytest.raises(ValueError, match="Explicitly attach"):
            value.command("exact", "before", "mcp_servers", {})
        value.attach("exact")
        assert value.command("exact", "list", "mcp_servers", {})["result"] == {"data": []}
        payload = {"name": "local", "action": "reconnect"}
        first = value.command("exact", "retry", "mcp_server_control", payload)
        assert first["ok"]
        assert value.command("exact", "retry", "mcp_server_control", payload) == first
        assert calls == [("exact", "local", "reconnect")]
        assert not value.command("exact", "bad", "mcp_server_control", {"name": "local"})["ok"]
        assert not McpOwner.instances[-1].sent
    finally:
        value.shutdown()


def test_command_discovery_is_provider_scoped_and_never_submits(tmp_path):
    class CommandOwner(Owner):
        reloads = 0

        async def list_commands(self):
            return {"data": [{"name": "context"}]}

        async def reload_skills(self):
            self.reloads += 1
            return {"data": [{"name": "fresh"}]}

        async def reload_plugins(self):
            self.reloads += 1
            return {"data": [{"name": "plugin-command"}], "plugins": [], "error_count": 0}

    value = WorkspaceHost(
        journal=WorkspaceJournal(tmp_path / "commands.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "claude", "cwd": str(tmp_path)},
        factories={"claude": CommandOwner},
    )
    try:
        value.attach("exact")
        assert value.command("exact", "list", "commands", {})["result"] == {
            "data": [{"name": "context"}]
        }
        assert not value.command("exact", "bad", "commands", {"run": "context"})["ok"]
        refreshed = value.command("exact", "reload", "reload_skills", {})
        assert refreshed["result"] == {"data": [{"name": "fresh"}]}
        assert value.command("exact", "reload", "reload_skills", {}) == refreshed
        assert CommandOwner.instances[-1].reloads == 1
        assert not value.command("exact", "bad-reload", "reload_skills", {"path": "/elsewhere"})["ok"]
        plugins = value.command("exact", "plugins", "reload_plugins", {})
        assert plugins["ok"]
        assert value.command("exact", "plugins", "reload_plugins", {}) == plugins
        assert CommandOwner.instances[-1].reloads == 2
        assert not value.command("exact", "bad-plugins", "reload_plugins", {"path": "/elsewhere"})["ok"]
        CommandOwner.instances[-1].state = "running"
        busy = value.command("exact", "busy-plugins", "reload_plugins", {})
        assert not busy["ok"] and busy["retryable"]
        assert CommandOwner.instances[-1].reloads == 2
        CommandOwner.instances[-1].state = "ready"
        assert value.command("exact", "later-plugins", "reload_plugins", {})["ok"]
        assert not CommandOwner.instances[-1].sent
    finally:
        value.shutdown()


@pytest.mark.parametrize("registration_fails", [False, True])
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_fork_receipt_keeps_identity_even_if_indexing_fails(tmp_path, registration_fails, provider):
    class ForkOwner(Owner):
        forks = 0

        async def fork_session(self):
            self.forks += 1
            return {"session_id": "new-fork", "provider": provider, "cwd": str(tmp_path)}

    registered = []

    def register(target):
        registered.append(target)
        if registration_fails:
            raise RuntimeError("catalog unavailable")

    value = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "fork.db"),
                          resolve=lambda sid: {"session_id": sid, "provider": provider, "cwd": str(tmp_path)},
                          factories={provider: ForkOwner}, register_fork=register)
    try:
        value.attach("exact")
        assert not value.command("exact", "bad", "fork_session", {"session_id": "other"})["ok"]
        ForkOwner.instances[-1].state = "running"
        refused = value.command("exact", "busy", "fork_session", {})
        assert not refused["ok"] and refused["retryable"] and ForkOwner.instances[-1].forks == 0
        ForkOwner.instances[-1].state = "ready"
        receipt = value.command("exact", "fork", "fork_session", {})
        assert receipt["ok"] and receipt["result"]["session_id"] == "new-fork"
        assert receipt["result"]["indexed"] is not registration_fails
        assert value.command("exact", "fork", "fork_session", {}) == receipt
        assert ForkOwner.instances[-1].forks == len(registered) == 1
        assert "new-fork" not in value._sessions
        assert not ForkOwner.instances[-1].sent
        assert not value.command("exact", "foreign", "register_fork", {"fork_request_id": "missing"})["ok"]
        assert len(registered) == 1
        if registration_fails:
            failed = value.command("exact", "registration-failed", "register_fork", {"fork_request_id": "fork"})
            assert not failed["ok"] and failed["retryable"]
        registration_fails = False
        ForkOwner.instances[-1].state = "running"
        recovered = value.command("exact", "recover", "register_fork", {"fork_request_id": "fork"})
        assert recovered["ok"] and recovered["result"]["indexed"]
        assert recovered["result"]["session_id"] == "new-fork"
        writes = len(registered)
        assert value.command("exact", "recover", "register_fork", {"fork_request_id": "fork"}) == recovered
        assert len(registered) == writes and ForkOwner.instances[-1].forks == 1
    finally:
        value.shutdown()


@pytest.mark.parametrize("checkpoint_count", [0, 1, 2])
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_interrupted_fork_receipt_recovers_only_unique_checkpoint(tmp_path, checkpoint_count, provider):
    path = tmp_path / "interrupted.db"
    journal = WorkspaceJournal(path)
    journal.claim_command("exact", "fork", {"action": "fork_session", "payload": {}})
    target = {"session_id": "saved-fork", "provider": provider, "cwd": str(tmp_path)}
    for _ in range(checkpoint_count):
        journal.append("exact", {"method": "workspace/sessionForked", "params": {"threadId": "exact", "requestId": "fork", "fork": target}})
    registered = []
    # Owner has no fork method: recovery must never call the native creator.
    value = WorkspaceHost(journal=WorkspaceJournal(path),
                          resolve=lambda sid: {"session_id": sid, "provider": provider, "cwd": str(tmp_path)},
                          factories={provider: Owner}, register_fork=registered.append)
    try:
        value.attach("exact")
        if checkpoint_count == 2:
            with pytest.raises(ValueError, match="ambiguous"):
                value.command("exact", "fork", "fork_session", {})
        else:
            receipt = value.command("exact", "fork", "fork_session", {})
            if checkpoint_count:
                assert receipt == {"ok": True, "result": {**target, "indexed": True}}
                assert value.command("exact", "fork", "fork_session", {}) == receipt
            else:
                assert receipt["uncertain"] and not receipt["ok"]
        assert registered == ([target] if checkpoint_count == 1 else [])
        assert "saved-fork" not in value._sessions
    finally:
        value.shutdown()


def test_background_controls_require_attach_and_deduplicate_stop(host):
    with pytest.raises(ValueError, match="Explicitly attach"):
        host.command("exact", "before", "background_tasks", {})
    assert not Owner.instances
    host.attach("exact")
    calls = []

    async def tasks():
        return {"data": []}

    async def stop(process_id):
        calls.append(process_id)
        return {"terminated": True}

    owner = Owner.instances[0]
    owner.list_background_tasks = tasks
    owner.terminate_background_task = stop
    assert host.command("exact", "list", "background_tasks", {})["result"] == {"data": []}
    assert not host.command("exact", "bad", "terminate_background_task", {"pid": "p"})["ok"]
    for _ in range(2):
        assert host.command("exact", "stop", "terminate_background_task", {"processId": "p"})["ok"]
    assert calls == ["p"]
    assert not owner.closed


def test_answer_receipts_do_not_store_form_content(host):
    import sqlite3

    host.attach("exact")
    received = []

    async def answer(request_id, content):
        received.append(content)
        return {}

    Owner.instances[0].answer = answer
    payload = {
        "request_id": 1,
        "answer": {"action": "accept", "content": {"field": "private-form-value"}},
    }
    assert host.command("exact", "reply", "answer", payload)["ok"]
    assert host.command("exact", "reply", "answer", payload)["ok"]
    assert len(received) == 1
    with sqlite3.connect(host.journal.path) as conn:
        saved = conn.execute(
            "SELECT payload FROM workspace_commands WHERE request_id='reply'"
        ).fetchone()[0]
    assert "private-form-value" not in saved and "sha256" in saved


def test_polling_never_launches_and_concurrent_attach_has_one_owner(host):
    assert host.events("exact")["events"] == []
    assert host._loop is None
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: host.attach("exact"), range(6)))
    assert all(result["ok"] for result in results)
    assert len(Owner.instances) == 1
    assert len(host.events("exact")["events"]) == 1
    assert not Owner.instances[0].closed


def test_timeout_retry_does_not_cancel_or_send_again(host):
    assert host.attach("exact", timeout=0.001)["pending"]
    assert host.attach("exact")["ok"]
    payload = {"inputs": [{"type": "text", "text": "once"}]}
    assert host.command("exact", "stable", "submit", payload, timeout=0.001)["pending"]
    receipt = host.command("exact", "stable", "submit", payload)
    assert receipt["ok"]
    assert host.command("exact", "stable", "submit", payload) == receipt
    assert len(Owner.instances[0].sent) == 1
    with pytest.raises(ValueError, match="different content"):
        host.command("exact", "stable", "submit", {"inputs": []})
    assert host.events("other")["events"] == []


def test_crash_ambiguous_command_is_not_repeated(host):
    host.attach("exact")
    payload = {"inputs": [{"type": "text", "text": "once"}]}
    host.journal.claim_command("exact", "uncertain", {"action": "submit", "payload": payload})
    receipt = host.command("exact", "uncertain", "submit", payload)
    assert receipt["uncertain"]
    assert Owner.instances[0].sent == []


def test_image_preview_requires_auth_and_exact_session_without_launch(host):
    from PIL import Image

    image = io.BytesIO()
    Image.new("RGB", (8, 8), "green").save(image, format="PNG")
    raw = image.getvalue()
    record = host.uploads.save("exact", "image.png", io.BytesIO(raw))
    app = Flask(__name__)
    token = "s" * 40
    app.register_blueprint(workspace_blueprint(host, token=token))
    headers = {"X-Serena-Workspace-Token": token}
    url = f"/api/workspace/exact/attachments/{record['token']}"
    with app.test_client() as client:
        assert client.get(url, base_url="http://127.0.0.1").status_code == 403
        response = client.get(url, base_url="http://127.0.0.1", headers=headers)
        assert response.status_code == 200 and response.data == raw
        assert response.mimetype == "image/png"
        assert response.headers["Cache-Control"] == "no-store"
        assert (
            client.get(
                url.replace("/exact/", "/different/"), base_url="http://127.0.0.1", headers=headers
            ).status_code
            == 400
        )
    assert Owner.instances == [] and host._loop is None


def test_claude_input_routing_and_duplicate_receipt(host):
    import base64

    from PIL import Image

    host.factories = {"claude": Owner}
    host.resolve = lambda sid: {"session_id": sid, "provider": "claude", "cwd": "."}
    assert host.events("claude-exact")["events"] == []
    assert Owner.instances == []
    assert host.attach("claude-exact")["provider"] == "claude"
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "green").save(stream, format="PNG")
    raw = stream.getvalue()
    upload = host.uploads.save("claude-exact", "photo.png", io.BytesIO(raw))
    payload = {
        "inputs": [
            {"type": "text", "text": "look at this"},
            {"type": "upload", "token": upload["token"]},
        ]
    }
    receipt = host.command("claude-exact", "once", "submit", payload)
    assert receipt["ok"]
    assert host.command("claude-exact", "once", "submit", payload) == receipt
    owner = Owner.instances[0]
    assert owner.sid == "claude-exact"
    assert len(owner.sent) == 1
    assert base64.b64decode(owner.sent[0][1]["source"]["data"]) == raw
    host.attach("different")
    rejected = host.command("different", "not-yours", "submit", payload)
    assert not rejected["ok"]
    assert Owner.instances[1].sent == []
    assert not owner.closed


def test_explicit_shutdown_waits_for_admitted_attach_and_is_idempotent(host):
    assert host.attach("exact", timeout=0.001)["pending"]
    host.shutdown()
    assert len(Owner.instances) == 1
    assert Owner.instances[0].closed
    host.shutdown()
    with pytest.raises(RuntimeError, match="stopped"):
        host.attach("exact")


def test_resolver_mismatch_and_unattached_controls_never_spawn(host):
    host.resolve = lambda sid: {"session_id": "wrong", "provider": "codex"}
    with pytest.raises(ValueError, match="different session"):
        host.attach("exact")
    with pytest.raises(ValueError, match="attach"):
        host.command("exact", "r", "interrupt", {})
    assert Owner.instances == []


def test_http_authentication_replay_and_disconnect_are_non_cancelling(host):
    app = Flask(__name__)
    token = "s" * 40
    app.register_blueprint(workspace_blueprint(host, token=token))
    headers = {"X-Serena-Workspace-Token": token}
    with app.test_client() as client:
        assert (
            client.post("/api/workspace/exact/attach", base_url="http://127.0.0.1").status_code
            == 403
        )
        assert (
            client.post(
                "/api/workspace/exact/attach", base_url="http://evil.test", headers=headers
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/workspace/exact/attach",
                base_url="http://127.0.0.1",
                headers={**headers, "Origin": "https://evil.test"},
            ).status_code
            == 403
        )
        kwargs = {"base_url": "http://127.0.0.1", "headers": headers}
        assert client.get("/api/workspace/exact/events", **kwargs).json["events"] == []
        assert Owner.instances == []
        assert client.post("/api/workspace/exact/attach", **kwargs).json["ok"]
        data = {
            "request_id": "once",
            "action": "submit",
            "payload": {"inputs": [{"type": "text", "text": "hi"}]},
        }
        first = client.post("/api/workspace/exact/commands", json=data, **kwargs)
        assert first.json["ok"]
        assert client.post("/api/workspace/exact/commands", json=data, **kwargs).json == first.json
    assert not Owner.instances[0].closed
    with app.test_client() as reconnected:
        assert len(reconnected.get("/api/workspace/exact/events", **kwargs).json["events"]) == 2
    assert len(Owner.instances) == 1


def test_browser_composer_reaches_host_and_reloads_same_owner(host):
    playwright = pytest.importorskip("playwright.sync_api")
    from werkzeug.serving import make_server

    app = Flask(__name__, static_folder=str(Path(__file__).resolve().parents[1] / "ui/static"))
    app.register_blueprint(workspace_blueprint(host, token="s" * 40))

    @app.get("/")
    def page():
        return """<!doctype html><html><head><link rel="stylesheet" href="/static/workspace-pane.css"></head>
<body style="margin:0;background:#000"><button id="connect">Open session</button><main style="height:90vh" id="pane"></main>
<script type="module">
import {WorkspacePane} from '/static/workspace-pane.mjs';
import {WorkspaceConnection} from '/static/workspace-connection.mjs';
window.connection=new WorkspaceConnection({sessionId:'exact',token:'ssssssssssssssssssssssssssssssssssssssss',receive:e=>pane.receive(e),error:e=>pane.error(e)});
window.pane=new WorkspacePane(document.querySelector('#pane'),{sessionId:'exact',provider:'Codex',controls:connection.controls()});
document.querySelector('#connect').onclick=()=>connection.connect().catch(e=>pane.error(e));
window.addEventListener('pagehide',()=>connection.dispose());
</script></body></html>"""

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ.get("SERENA_PROOF_BROWSER"))
            page = browser.new_page(viewport={"width": 1200, "height": 800})
            page.set_default_timeout(5000)
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{server.server_port}")
            page.wait_for_function("window.connection !== undefined")
            assert Owner.instances == []
            page.get_by_role("button", name="Open session").click()
            page.wait_for_function("pane.conversation.status === 'ready' || !pane.alert.hidden")
            assert page.locator(".aw-error").is_hidden(), page.locator(".aw-error").inner_text()
            page.wait_for_function("pane.conversation.status === 'ready'")
            page.get_by_role("textbox", name="Message Codex").fill("through the actual HTTP host")
            page.locator("input[type=file]").set_input_files(
                {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"attached document"}
            )
            page.get_by_role("button", name="Send message", exact=True).click()
            page.get_by_text("adapter fixture output", exact=True).wait_for()
            assert Owner.instances[0].sent[0][0] == {
                "type": "text",
                "text": "through the actual HTTP host",
            }
            import json

            attachment = json.loads(
                Owner.instances[0].sent[0][1]["text"].removeprefix("User-attached file: ")
            )
            assert Path(attachment["path"]).read_bytes() == b"attached document"
            page.reload()
            page.get_by_role("button", name="Open session").click()
            page.get_by_text("adapter fixture output", exact=True).wait_for()
            assert len(Owner.instances) == 1
            assert not Owner.instances[0].closed
            assert not errors
            browser.close()
        assert not Owner.instances[0].closed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_uploaded_image_reaches_same_owner_and_cross_session_token_is_rejected(host):
    from PIL import Image

    image = io.BytesIO()
    Image.new("RGB", (8, 8), "green").save(image, format="PNG")
    image.seek(0)
    app = Flask(__name__)
    app.register_blueprint(workspace_blueprint(host, token="s" * 40))
    kwargs = {"base_url": "http://127.0.0.1", "headers": {"X-Serena-Workspace-Token": "s" * 40}}
    with app.test_client() as client:
        upload = client.post(
            "/api/workspace/exact/uploads", data={"file": (image, "photo.png")}, **kwargs
        )
        assert upload.status_code == 200, upload.json
        assert Owner.instances == []
        token = upload.json["upload"]["token"]
        assert client.post("/api/workspace/exact/attach", **kwargs).json["ok"]
        data = {
            "request_id": "image",
            "action": "submit",
            "payload": {"inputs": [{"type": "upload", "token": token}]},
        }
        assert client.post("/api/workspace/exact/commands", json=data, **kwargs).json["ok"]
        delivered = Owner.instances[0].sent[0][0]
        assert delivered["type"] == "localImage"
        with Image.open(delivered["path"]) as decoded:
            assert decoded.size == (8, 8)
        assert client.post("/api/workspace/other/attach", **kwargs).json["ok"]
        assert not client.post("/api/workspace/other/commands", json=data, **kwargs).json["ok"]
        assert Owner.instances[1].sent == []


PEER = r"""
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if 'id' not in m: continue
    method = m.get('method')
    if method == 'initialize': result = {'userAgent': 'controlled-test-peer'}
    elif method == 'thread/resume': result = {'thread': {'id': m['params']['threadId'], 'turns': []}}
    elif method == 'turn/start':
        result = {'turn': {'id': 't'}}
        print(json.dumps({'method': 'item/agentMessage/delta', 'params': {'threadId': m['params']['threadId'], 'delta': m['params']['input'][0]['text']}}), flush=True)
    else: result = {}
    print(json.dumps({'id': m['id'], 'result': result}), flush=True)
"""


def test_host_routes_real_bidirectional_pipes_into_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_RUNTIME_LEASE_DIR", str(tmp_path / "leases"))

    class PeerRpc(WorkspaceRpc):
        async def start(self, command, **kwargs):
            await super().start([sys.executable, "-u", "-c", PEER], **kwargs)

    class PeerCodex(CodexWorkspace):
        async def open(self):
            return await super().open(binary=sys.executable)

    owners = []

    def factory(**kwargs):
        owner = PeerCodex(rpc=PeerRpc(), **kwargs)
        owners.append(owner)
        return owner

    host = WorkspaceHost(
        journal=WorkspaceJournal(tmp_path / "journal.db"),
        resolve=lambda sid: {"session_id": sid, "provider": "codex", "cwd": str(tmp_path)},
        factories={"codex": factory},
    )
    try:
        assert host.attach("exact")["ok"]
        process = owners[0].rpc.process
        assert host.command(
            "exact", "one", "submit", {"inputs": [{"type": "text", "text": "through real pipes"}]}
        )["ok"]
        deadline = time.monotonic() + 3
        while (
            not any(
                e["event"]["method"] == "item/agentMessage/delta"
                for e in host.events("exact")["events"]
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        deltas = [
            e
            for e in host.events("exact")["events"]
            if e["event"]["method"] == "item/agentMessage/delta"
        ]
        assert deltas[0]["event"]["params"]["delta"] == "through real pipes"
        assert host.attach("exact")["ok"]
        assert owners[0].rpc.process is process
    finally:
        host.shutdown()
    assert process.returncode is not None
@pytest.mark.parametrize("pending", [False, True])
def test_confirmed_claude_rename_indexes_exact_owner_without_attach(tmp_path, pending):
    import asyncio
    from types import SimpleNamespace

    from core.workspace_catalog import NativeTranscriptPending
    from core.workspace_host import WorkspaceHost
    from core.workspace_journal import WorkspaceJournal

    calls = []
    def register(target):
        calls.append(target)
        if pending:
            raise NativeTranscriptPending("not flushed")
        return {'display_title': 'New name', 'native_rename': True}

    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / "rename.db"),
                         resolve=lambda sid: pytest.fail("must not attach"),
                         register_fork=register)
    host._sessions["exact"] = (SimpleNamespace(cwd=tmp_path), "claude")
    asyncio.run(host._publish("exact", {"method": "workspace/renameCompleted", "params": {"title": "New name"}}))
    assert calls == [{"session_id": "exact", "provider": "claude", "cwd": str(tmp_path), "expected_native_title": "New name", "confirmed_native_name": "New name"}] * (5 if pending else 1)
    result = host.events('exact')['events'][-1]['event']['params']
    assert result['indexed'] is not pending
    assert result.get('native_rename', False) is not pending
    assert host._loop is None
