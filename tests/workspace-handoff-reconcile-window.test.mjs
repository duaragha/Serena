import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../ui/web.py', import.meta.url), 'utf8');
const start = source.indexOf('function _pseudoCandidateTs(');
const end = source.indexOf('\nfunction _markActive(', start);
assert(start >= 0 && end > start);

function harness(pseudo) {
  const fetched = [];
  const context = vm.createContext({
    _pseudoSessions: [pseudo], fetched,
    fetch: async (url, init) => {
      fetched.push([url, JSON.parse(init.body)]);
      return {ok: true, json: async () => ({ok: true, group_id: 'g_thread'})};
    },
    _defaultCwd: () => '/home/raghav',
    _pendingLinkMembers: p => p.pending_group_member_sids || [p.pending_group_link_with],
    _patchClientSession() {}, _applyClientGroup() {}, _fdPairResolved: {},
    _activeTerms: new Set(), _activeMeta: new Map(), _gtkReadyTerms: new Set(),
    window: {}, termSessions: new Map(), currentSessionId: null, focusedSid: null,
    _gtkCodeSid: null, _gtkSplitSids: null, _migrateGtkRuntimeState() {},
    _resolvedPseudoSids: new Map(), _dropClientSession() {}, renderSessionList() {},
    allSessions: [], loadSessions: async () => {}, currentProject: null,
  });
  vm.runInContext(source.slice(start, end), context);
  return context;
}

const handoff = () => ({
  session_id: 'new-handoff', agent: 'codex', cwd: '/home/raghav',
  first_timestamp: '2026-10-01T13:29:00.000Z', pending_rename_title: 'Task Cleanup',
  pending_group_link_with: 'src', pending_group_member_sids: ['src', 'sibling'],
});

test('an unresolved handoff does not adopt a session that starts an hour later elsewhere', async () => {
  const pseudo = handoff();
  const context = harness(pseudo);
  await context._reconcilePseudos([{session_id: 'bench', agent: 'codex',
    cwd: '/tmp/bench/runA/gpt-6.1-sol-xhigh', first_timestamp: '2026-10-01T14:43:00.978000+00:00'}]);
  assert.equal(pseudo.resolved_session_id, undefined);
  assert.deepEqual(context.fetched, []);
});

test('an unresolved handoff does not adopt a later same-directory session either', async () => {
  const pseudo = handoff();
  const context = harness(pseudo);
  await context._reconcilePseudos([{session_id: 'later', agent: 'codex', cwd: '/home/raghav',
    first_timestamp: '2026-10-01T14:43:00.000Z'}]);
  assert.equal(pseudo.resolved_session_id, undefined);
});

test('a handoff never claims a session that already has a group or a live external runtime', async () => {
  for (const extra of [{group: 'g_other'}, {external_runtime_active: true}]) {
    const pseudo = handoff();
    const context = harness(pseudo);
    await context._reconcilePseudos([{session_id: 'taken', agent: 'codex', cwd: '/elsewhere',
      first_timestamp: '2026-10-01T13:29:05.000Z', ...extra}]);
    assert.equal(pseudo.resolved_session_id, undefined);
  }
});

test('a handoff still resolves to its fresh session when the recorded cwd differs', async () => {
  const pseudo = handoff();
  const context = harness(pseudo);
  await context._reconcilePseudos([{session_id: 'real', agent: 'codex', cwd: 'C:/Users/ragha',
    first_timestamp: '2026-10-01T13:29:05.503Z'}]);
  assert.equal(pseudo.resolved_session_id, 'real');
  assert.deepEqual(context.fetched.map(([url]) => url), ['/api/rename/real', '/api/group/link']);
  assert.deepEqual(context.fetched[1][1].session_ids.sort(), ['real', 'sibling', 'src']);
});
