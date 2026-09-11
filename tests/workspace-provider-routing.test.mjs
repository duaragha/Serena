import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../ui/web.py', import.meta.url), 'utf8');
const start = source.indexOf('async function startLiveTerminal(sid, opts)');
const end = source.indexOf('// A missing CLI/session', start);
const route = source.slice(start, end) + 'return "terminal"; }';

for (const agent of ['claude', 'codex', 'gemini']) {
  test(`${agent} selects its supported renderer for new and saved chats`, async () => {
    const context = vm.createContext({
      window:{SERENA:{structuredWorkspace:true}},
      _findClientSession:()=>({agent}),
      _isSerenaVoiceSession:()=>false, _isFleetSession:()=>false,
      _startStructuredPane:()=> 'structured',
    });
    vm.runInContext(route, context);
    for (const opts of [{}, {isNew:true,agent}]) {
      assert.equal(await context.startLiveTerminal('exact',opts), agent==='gemini'?'terminal':'structured');
    }
  });
}
