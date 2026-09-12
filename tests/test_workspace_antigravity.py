import asyncio
from types import SimpleNamespace

import pytest

from core import workspace_antigravity as native

SID = '77777777-7777-4777-8777-777777777777'


class Lease:
    def __init__(self, sid):
        self.sid, self.closed = sid, False
    def launching(self):
        pass
    def bind(self, pid):
        pass
    def transfer_after_transition(self, sid):
        self.sid = sid
        return self
    def release(self):
        self.closed = True


class Stream:
    def __init__(self):
        self.events = asyncio.Queue()
        self.sent, self.args, self.process = [], [], None
    async def start(self, args, *, cwd, env):
        self.args = args
        self.process = SimpleNamespace(pid=1234)
        await self.events.put({'event': 'init', 'conversation_id': SID, 'init': {'cwd': str(cwd)}})
    async def prompt(self, inputs):
        self.sent.append(inputs)
    async def close(self):
        self.process = None


@pytest.fixture
def build(tmp_path, monkeypatch):
    from core import metadata
    monkeypatch.setattr(metadata, 'set_gemini_workspace', lambda *args: None)
    db = tmp_path / f'{SID}.db'
    db.touch()
    monkeypatch.setattr(native, 'resumable_conversation_path', lambda sid: db)
    monkeypatch.setattr(native, 'reject_unregistered_provider', lambda *args: None)
    def make(sid=SID, stream=Stream):
        events = []
        async def publish(event):
            events.append(event)
        owner = native.AntigravityWorkspace(session_id=sid, cwd=tmp_path, publish=publish,
            binary='/test/agy', transport_factory=stream, lease_factory=Lease, history_reader=lambda: [])
        return owner, events
    return make


def test_resume_streams_steps_without_duplicate_final_and_retains_owner(build):
    async def run():
        owner, events = build()
        try:
            await owner.open()
            assert owner.rpc.args[-2:] == ['--conversation', SID]
            assert not owner.rpc.sent
            await owner.submit([{'type': 'text', 'text': 'hello'}])
            for text, state in [('one ', 'ACTIVE'), ('two', 'DONE')]:
                await owner.rpc.events.put({'event': 'step_update', 'step_update': {
                    'conversation_id': SID, 'step_index': 1, 'step_type': 'agent_response',
                    'state': state, 'text_delta': text}})
            await owner.rpc.events.put({'event': 'step_update', 'step_update': {
                'conversation_id': SID, 'step_index': 2, 'step_type': 'tool', 'state': 'DONE',
                'tool_info': {'name': 'view_file', 'parameters': {'path': 'file'}, 'output': 'contents'}}})
            await owner.rpc.events.put({'event': 'result', 'result': {
                'conversation_id': SID, 'status': 'SUCCESS', 'response': 'one two'}})
            while owner.active_turn:
                await asyncio.sleep(0)
            items = [e['params']['item'] for e in events if e['method'] == 'item/completed']
            assert items[0]['content'] == [{'type': 'text', 'text': 'hello'}]
            assert [i['text'] for i in items if i['type'] == 'agentMessage'] == ['one ', 'one two']
            assert items[-1]['type'] == 'acpToolCall' and items[-1]['output'] == 'contents'
            process = owner.rpc.process
            await owner.submit([{'type': 'text', 'text': 'again'}])
            assert owner.rpc.process is process and len(owner.rpc.sent) == 2
            await owner.interrupt()
            assert owner.state == 'ready' and owner.rpc is None
            await owner.submit([{'type': 'text', 'text': 'after stop'}])
            assert owner.rpc.args[-2:] == ['--conversation', SID]
        finally:
            await owner.close()
    asyncio.run(run())


def test_creation_checkpoints_native_identity_before_publishing(build):
    async def run():
        owner, events = build('new:88888888-8888-4888-8888-888888888888')
        targets = []
        async def checkpoint(target):
            assert not events
            targets.append(target)
        try:
            await owner.create(checkpoint=checkpoint)
            assert owner.session_id == SID and targets[0]['provider'] == 'gemini'
            assert '--conversation' not in owner.rpc.args
        finally:
            await owner.close()
        assert owner.can_retry_attachment()
    asyncio.run(run())


def test_wrong_identity_never_receives_prompt(build):
    async def run():
        owner, events = build('99999999-9999-4999-8999-999999999999')
        with pytest.raises(ValueError, match='different conversation'):
            await owner.open()
        assert owner.can_retry_attachment()
    asyncio.run(run())


def test_cleanup_failure_blocks_retry_and_new_input(build):
    class Broken(Stream):
        async def close(self):
            raise RuntimeError('still alive')
    async def run():
        owner, events = build(stream=Broken)
        await owner.open()
        await owner.submit([{'type':'text', 'text':'work'}])
        with pytest.raises(RuntimeError, match='still alive'):
            await owner.interrupt()
        assert not owner.can_retry_attachment()
        with pytest.raises(ValueError, match='not ready'):
            await owner.submit([{'type':'text', 'text':'duplicate'}])
        owner.rpc.close = lambda: Stream.close(owner.rpc)
        await owner.close()
    asyncio.run(run())


def test_model_validation_and_attachment_blocks_do_not_send(build):
    async def run():
        owner, _ = build()
        try:
            await owner.open()
            with pytest.raises(ValueError, match='advertised'):
                await owner.submit([{'type':'text','text':'hello'}], options={'model':'invented'})
            with pytest.raises(ValueError, match='text or attached'):
                await owner.submit([{'type':'image','data':'unvalidated'}])
            assert not owner.rpc.sent
        finally:
            await owner.close()
    asyncio.run(run())


def test_host_creation_receipt_is_reused_for_gemini(build, tmp_path):
    from core.workspace_host import WorkspaceHost
    from core.workspace_journal import WorkspaceJournal
    owners = []
    def factory(**kwargs):
        owner = native.AntigravityWorkspace(**kwargs, binary='/test/agy', transport_factory=Stream,
                                           lease_factory=Lease, history_reader=lambda: [])
        owners.append(owner)
        return owner
    host = WorkspaceHost(journal=WorkspaceJournal(tmp_path / 'events.db'), resolve=lambda sid: {},
                         factories={'gemini': factory})
    try:
        receipt = host.create('88888888-8888-4888-8888-888888888888', 'gemini', str(tmp_path), confirmed=True)
        assert receipt['ok'], receipt
        again = host.create('88888888-8888-4888-8888-888888888888', 'gemini', str(tmp_path), confirmed=True)
        assert again == receipt and len(owners) == 1
        assert owners[0].session_id == SID
    finally:
        host.shutdown()


def test_headless_transcript_and_confirmed_workspace_rebuild_catalog(tmp_path, monkeypatch):
    import json

    from core import gemini_scanner, metadata
    db = tmp_path / f'{SID}.db'
    db.touch()
    transcript = tmp_path / 'transcript.jsonl'
    transcript.write_text(json.dumps({'type':'USER_INPUT', 'created_at':'2026-09-12T10:00:00Z',
                                     'content':'<USER_REQUEST>Native headless message</USER_REQUEST>'}) + '\n')
    monkeypatch.setattr(gemini_scanner, '_history_entries', lambda: [])
    monkeypatch.setattr(gemini_scanner, 'transcript_path', lambda sid: transcript)
    monkeypatch.setattr(metadata, 'get_meta', lambda sid: {'gemini_workspace': str(tmp_path)})
    result = gemini_scanner.parse_gemini_metadata(db)
    assert result.cwd == str(tmp_path) and result.first_message == 'Native headless message'
    assert result.message_count == 1
