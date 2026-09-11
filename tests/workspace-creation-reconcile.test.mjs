import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../ui/web.py', import.meta.url), 'utf8');
const start = source.indexOf('async function _reconcilePseudos(');
const end = source.indexOf('\nfunction _markActive(', start);
assert(start >= 0 && end > start);

test('structured pseudo cannot be claimed by a newer unrelated native session in the same directory', async () => {
  const pseudo = {session_id:'new-exact',agent:'codex',cwd:'/project',structured_pending:true,
    first_timestamp:'2026-09-09T00:00:00Z',pending_rename_title:'Keep this name',fd_expires:1};
  const context = vm.createContext({_pseudoSessions:[pseudo]});
  vm.runInContext(source.slice(start,end),context);
  await context._reconcilePseudos([{session_id:'unrelated-native',agent:'codex',cwd:'/project',
    first_timestamp:'2026-09-10T00:00:00Z'}]);
  assert.equal(context._pseudoSessions.length,1);
  assert.equal(pseudo.session_id,'new-exact');
  assert.equal(pseudo.pending_rename_title,'Keep this name');
  assert.equal(pseudo.resolved_session_id,undefined);
});
