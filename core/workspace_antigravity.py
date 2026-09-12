"""Gemini workspace using Antigravity's native CLI conversation stream."""

import asyncio
import csv
import io
import json
import os
import shutil
from pathlib import Path
from uuid import UUID, uuid4

from core.billing import strip_metered_auth_env
from core.gemini_scanner import read_turns, resumable_conversation_path, transcript_path
from core.workspace_admission import reject_unregistered_provider
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpc, WorkspaceRpcError


class AntigravityStream(WorkspaceRpc):
    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                data = json.loads(line)
                if not isinstance(data, dict) or not isinstance(data.get('event'), str):
                    raise ValueError('Invalid Antigravity stream event')
                await self.events.put(data)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._failure = str(error)
        finally:
            self._failure = self._failure or 'Antigravity output closed'
            await self.events.put({'event': 'transportClosed', 'reason': self._failure})

    async def prompt(self, inputs):
        await self._send({'event': 'user', 'message': {'content': inputs}})


class AntigravityWorkspace:
    native_stream = True
    def __init__(self, *, session_id, cwd, publish, binary=None, transport_factory=AntigravityStream,
                 lease_factory=SessionLease, history_reader=None):
        if not session_id.startswith('new:') and str(UUID(session_id)) != session_id:
            raise ValueError('Exact Gemini conversation ID required')
        self.session_id, self.cwd, self.publish = session_id, Path(cwd).resolve(strict=True), publish
        self.binary = binary or shutil.which('agy') or str(Path.home() / '.local/bin/agy')
        self.transport_factory, self.lease_factory = transport_factory, lease_factory
        self.history_reader = history_reader or self._history
        self.rpc, self._lease, self._reader = None, None, None
        self._control = asyncio.Lock()
        self.state, self.active_turn, self.questions = 'closed', None, {}
        self._items, self._settings, self._models = {}, {}, []
        self._mode = None
        self._background_possible = False

    async def event(self, method, params):
        await self.publish({'method': method, 'params': {'threadId': self.session_id, **params}})

    def _history(self):
        path = transcript_path(self.session_id)
        if path is None:
            raise ValueError('Gemini saved history is unavailable; the native conversation was not changed')
        turns = []
        for index, record in enumerate(read_turns(path)):
            if record['role'] == 'user' or not turns:
                turns.append({'id': f'history-{index}', 'status': 'completed', 'items': []})
            item = {'id': f'history-item-{index}', 'type': 'userMessage' if record['role'] == 'user' else 'agentMessage',
                    'text': record['text'], 'providerOriginal': record}
            if record['role'] == 'user':
                item['content'] = [{'type': 'text', 'text': record['text']}]
            if record.get('tool_name'):
                item.update(type='acpToolCall', tool=record['tool_name'], input=json.loads(record['tool_input'] or '{}'), status='completed')
            turns[-1]['items'].append(item)
        return turns

    async def _start(self, checkpoint=None):
        self.rpc = self.transport_factory()
        args = [self.binary, '--input-format', 'stream-json', '--output-format', 'stream-json']
        if checkpoint is None:
            args += ['--conversation', self.session_id]
        for key, flag in (('model', '--model'), ('reasoningEffort', '--effort')):
            if self._settings.get(key):
                args += [flag, self._settings[key]]
        if self._mode:
            args += ['--mode', self._mode]
        self._lease.launching()
        await self.rpc.start(args, cwd=self.cwd, env=strip_metered_auth_env(dict(os.environ)))
        self._lease.bind(self.rpc.process.pid)
        first = await asyncio.wait_for(self.rpc.events.get(), 60)
        if first.get('event') == 'transportClosed':
            detail = ''.join(getattr(self.rpc, 'stderr', ()))[-500:] or first.get('reason', '')
            raise RuntimeError('Gemini startup failed: ' + detail)
        sid = first.get('conversation_id')
        if first.get('event') != 'init' or not isinstance(sid, str) or str(UUID(sid)) != sid:
            raise ValueError('Gemini did not confirm a native conversation')
        init = first.get('init', {})
        if not isinstance(init.get('cwd'), str) or Path(init['cwd']).resolve() != self.cwd:
            raise ValueError('Gemini initialized a different project')
        if checkpoint:
            await checkpoint({'session_id': sid, 'provider': 'gemini', 'cwd': str(self.cwd)})
            self._lease = self._lease.transfer_after_transition(sid)
            self.session_id = sid
            from core.metadata import set_gemini_workspace
            await asyncio.to_thread(set_gemini_workspace, sid, str(self.cwd))
        elif sid != self.session_id:
            raise ValueError('Gemini resumed a different conversation; no prompt was sent')
        if self._settings.get('model') and init.get('model') != self._settings['model']:
            raise ValueError('Gemini did not confirm the requested model')
        if init.get('model'):
            self._settings['model'] = init['model']
        self._reader = asyncio.create_task(self._consume())

    async def open(self):
        async with self._control:
            path = resumable_conversation_path(self.session_id)
            if path is None:
                raise ValueError('Exact native Gemini conversation is unavailable')
            reject_unregistered_provider(self.session_id, self.cwd, path, 'agy')
            history = await asyncio.to_thread(self.history_reader)
            self._lease = self.lease_factory(self.session_id)
            self.state = 'opening'
            try:
                await self._start()
                await self.event('workspace/history', {'thread': {'id': self.session_id, 'turns': history}, 'provider': 'gemini'})
                self.state = 'ready'
            except BaseException:
                await self._close()
                raise

    async def create(self, *, checkpoint):
        async with self._control:
            if not self.session_id.startswith('new:') or self._lease is not None:
                raise ValueError('Gemini creation was already attempted')
            self._lease = self.lease_factory(self.session_id)
            self.state = 'opening'
            try:
                await self._start(checkpoint)
                await self.event('workspace/history', {'thread': {'id': self.session_id, 'turns': []}, 'provider': 'gemini'})
                self.state = 'ready'
            except BaseException:
                await self._close()
                raise

    async def _metadata(self, *args):
        process = await asyncio.create_subprocess_exec(self.binary, *args, cwd=self.cwd,
            env=strip_metered_auth_env(dict(os.environ)), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(process.communicate(), 30)
            if process.returncode:
                raise ValueError('Gemini catalog query failed: ' + err.decode(errors='replace')[-500:])
            return out.decode('utf-8')
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def list_models(self):
        async with self._control:
            rows = csv.reader(io.StringIO(await self._metadata('models')), delimiter='\t')
            self._models = [{'model': row[0], 'displayName': row[1],
                             'supportedReasoningEfforts': []} for row in rows if len(row) == 2]
            selected = json.loads(await self._metadata('--print', '/model', '--output-format', 'json'))
            current = selected.get('command', {}).get('data', {})
            if not self._settings.get('model') and current.get('id'):
                self._settings['model'] = current['id']
            result = {'data': self._models, 'settings': dict(self._settings)}
            await self.event('workspace/models', result)
            return result

    async def list_commands(self):
        result = json.loads(await self._metadata('--print', '/help', '--output-format', 'json'))
        # Native CLI control commands cannot run inside stream-json. Skills can.
        commands = result.get('command', {}).get('data', {}).get('commands', [])
        skills = json.loads(await self._metadata('--print', '/skills', '--output-format', 'json'))
        return {'data': [c for c in commands if c.get('name') in {'help', 'skills', 'usage', 'config', 'model', 'effort'}]
                + [{'name': s['name'], 'description': s.get('description', '')}
                   for s in skills.get('command', {}).get('data', {}).get('skills', []) if isinstance(s.get('name'), str)]}

    async def list_session_modes(self):
        return {'id': 'mode', 'name': 'Session mode', 'currentValue': self._mode or 'default',
                'options': [{'value': 'default', 'name': 'Default'}, {'value': 'plan', 'name': 'Plan'},
                            {'value': 'accept-edits', 'name': 'Accept edits'}]}

    async def set_session_mode(self, mode):
        async with self._control:
            if self.state != 'ready' or mode not in {'default', 'plan', 'accept-edits'}:
                raise ValueError('Finish the Gemini turn before changing mode')
            if self._background_possible:
                raise ValueError('Stop Gemini background work before changing mode')
            await self._stop_stream()
            self._mode = None if mode == 'default' else mode
            try:
                await self._start()
            except BaseException:
                await self._close()
                raise
            return await self.list_session_modes()

    async def submit(self, inputs, *, options=None):
        async with self._control:
            if self.state != 'ready' or self.active_turn:
                raise ValueError('Gemini is not ready for input')
            if not isinstance(inputs, list) or not inputs or any(not isinstance(i, dict) or set(i) != {'type', 'text'}
                    or i['type'] != 'text' or not isinstance(i['text'], str) for i in inputs):
                raise ValueError('Gemini stream requires text or attached-file references')
            options = options or {}
            if not isinstance(options, dict) or set(options) - {'model'}:
                raise ValueError('Unsupported Gemini turn options')
            model = options.get('model')
            if model is not None and not isinstance(model, str):
                raise ValueError('Choose an advertised Gemini model')
            if model and model not in {m['model'] for m in self._models}:
                raise ValueError('Choose an advertised Gemini model')
            if model and model != self._settings.get('model'):
                if self._background_possible:
                    raise ValueError('Stop Gemini background work before changing model')
                await self._stop_stream()
                self._settings['model'] = model
            if self.rpc is None:
                try:
                    await self._start()
                except BaseException:
                    await self._close()
                    raise
            self.active_turn = str(uuid4())
            turn = self.active_turn
            self._items = {}
            self.state = 'running'
            await self.event('workspace/settings', dict(self._settings))
            await self.event('turn/started', {'turn': {'id': turn, 'status': 'inProgress'}})
            await self.event('item/completed', {'turnId': turn, 'item': {'id': turn + '-user', 'type': 'userMessage',
                'content': inputs}})
            try:
                command = inputs[0]['text'].strip() if len(inputs) == 1 else ''
                if command in {'/help', '/skills', '/usage', '/config', '/model', '/effort'}:
                    try:
                        result = json.loads(await self._metadata('--print', command, '--output-format', 'json'))
                    except Exception as error:
                        result = {'status': 'ERROR', 'error': str(error), 'response': ''}
                    await self.rpc.events.put({'event': 'result', 'result': {**result, 'conversation_id': self.session_id}})
                    return {'turn': {'id': turn}}
                await self.rpc.prompt(inputs)
            except BaseException:
                self.state = 'unavailable'
                await self.event('workspace/transportClosed', {'reason': 'Gemini input delivery is unconfirmed; it will not be resent'})
                raise
            return {'turn': {'id': turn}}

    async def _consume(self):
        try:
            while True:
                event = await self.rpc.events.get()
                if event.get('event') == 'transportClosed':
                    raise WorkspaceRpcError(event.get('reason', 'Gemini stream closed'))
                data = event.get('step_update') if event.get('event') == 'step_update' else event.get('result')
                if not isinstance(data, dict) or data.get('conversation_id') != self.session_id:
                    raise ValueError('Gemini event belongs to another conversation')
                if not self.active_turn:
                    raise ValueError('Gemini emitted output without an admitted turn')
                turn = self.active_turn
                if event['event'] == 'result':
                    if data.get('response') and not any(i['type'] == 'agentMessage' for i in self._items.values()):
                        await self.event('item/completed', {'turnId': turn, 'item': {'id': turn + '-reply',
                            'type': 'agentMessage', 'text': data['response']}})
                    status = 'completed' if data.get('status') == 'SUCCESS' else 'failed'
                    self.state, self.active_turn = 'ready', None
                    await self.event('turn/completed', {'turn': {'id': turn, 'status': status, 'providerOriginal': data,
                        **({'error': {'message': data['error']}} if data.get('error') else {})}})
                    continue
                index = data.get('step_index')
                if type(index) is not int or index < 0:
                    raise ValueError('Invalid Gemini step index')
                kind = data.get('step_type')
                if kind == 'user_input':
                    continue
                if kind == 'checkpoint':
                    await self.event('workspace/native', {'providerOriginal': event})
                    continue
                key = f'{turn}-step-{index}'
                item = self._items.setdefault(key, {'id': key, 'type': 'agentMessage' if kind == 'agent_response' else 'acpToolCall', 'text': ''})
                item['text'] += data.get('text_delta') or ''
                item['providerOriginal'] = data
                if kind != 'agent_response':
                    tool = data.get('tool_info') or {}
                    if data.get('subagent_info') or tool.get('name') in {'run_command', 'invoke_subagent', 'schedule', 'browser_subagent'}:
                        self._background_possible = True
                    item.update(tool=tool.get('name') or data.get('tool_name') or kind,
                                input=tool.get('parameters', {}), output=tool.get('output', ''),
                                status='failed' if tool.get('error') else ('completed' if data.get('state') == 'DONE' else 'inProgress'))
                await self.event('item/completed', {'turnId': turn, 'item': dict(item)})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.state = 'unavailable'
            await self.event('workspace/transportClosed', {'reason': str(error)})

    async def _stop_stream(self):
        if self._reader:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None
        if self.rpc:
            try:
                await self.rpc.close()
            except BaseException:
                self.state = 'unavailable'
                raise
            if self.rpc.process is not None:
                raise ValueError('Gemini process exit is unconfirmed')
            self.rpc = None

    async def interrupt(self):
        async with self._control:
            turn = self.active_turn
            self.state = 'cancelling'
            try:
                await self._stop_stream()
            except BaseException:
                self.state = 'unavailable'
                raise
            self.active_turn, self.state = None, 'ready'
            self._background_possible = False
            if turn:
                await self.event('turn/completed', {'turn': {'id': turn, 'status': 'interrupted'}})
            return {}

    async def _close(self):
        self.state = 'unavailable'
        await self._stop_stream()
        if self._lease:
            self._lease.release()
            self._lease = None
        self.active_turn, self.state = None, 'closed'

    async def close(self):
        async with self._control:
            await self._close()

    def can_retry_attachment(self):
        return self.state in {'closed', 'unavailable'} and self.rpc is None and self._lease is None
