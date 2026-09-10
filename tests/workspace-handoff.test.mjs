import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../ui/web.py',import.meta.url),'utf8');
const start=source.indexOf('async function handoffSession(');
const end=source.indexOf('// === HANDOFF FEATURE END ===',start);
assert.ok(start>=0 && end>start);

for(const target of ['claude','codex'])test(`new ${target} handoff uses explicit seeded creation without terminal paste`,async()=>{
  const calls=[];
  const termSessions=new Map();
  const sourceChat={session_id:'source',agent:target==='claude'?'codex':'claude',display_title:'Named chat'};
  const context=vm.createContext({
    window:{SERENA:{structuredWorkspace:true}},
    document:{getElementById:()=>({classList:{add(){},remove(){}},textContent:''})},
    showToast:()=>({update:(...args)=>calls.push(['toast',...args])}),
    _resolveHandoffSid:async sid=>sid,
    _findClientSession:()=>sourceChat,
    sessionSource:[sourceChat],sessions:[sourceChat],
    fetch:async()=>({ok:true,json:async()=>({ok:true,prompt:'Required source context',cwd:'/project'})}),
    _agentLabel:agent=>agent,
    _pseudoSessions:[],
    setSessionSource(){},_applyClientGroup(){},_markActive(){},setTermStatus(){},_startPseudoReconciler(){},
    startLiveTerminal:async(sid,options)=>{calls.push(['create',options]);termSessions.set(sid,{structured:true});},
    _feedTerminalWhenReady:()=>{throw Error('Legacy terminal paste must not run');},
    termSessions,
  });
  vm.runInContext(source.slice(start,end),context);
  await context.handoffSession('source',target);
  const create=calls.find(call=>call[0]==='create')[1];
  assert.equal(create.seed,'Required source context');
  assert.equal(create.agent,target);
  assert.equal(create.isNew,true);
  assert.equal(context._pseudoSessions[0].pending_rename_title,'Named chat');
  assert.equal(context._pseudoSessions[0].pending_group_link_with,'source');
  assert.match(calls.at(-1)[1],/Ready to create/);
});
