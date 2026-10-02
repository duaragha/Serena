import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../ui/web.py',import.meta.url),'utf8');
const start=source.indexOf('async function handoffSession(');
const end=source.indexOf('// === HANDOFF FEATURE END ===',start);
assert.ok(start>=0 && end>start);

for(const mounted of [true,false])for(const target of ['claude','codex','gemini'])test(`new ${target} handoff avoids terminal paste (mounted=${mounted})`,async()=>{
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
    startLiveTerminal:async(sid,options)=>{calls.push(['create',options]);if(mounted)termSessions.set(sid,{structured:true});},
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
  assert.match(calls.at(-1)[1],mounted?/Ready to create/:/Could not open.*Handoff was not sent/);
});

function linkedThread({structured=true,focusedSid}={}){
  const calls=[];
  const fetched=[];
  const termSessions=new Map();
  const claude={session_id:'source',agent:'claude',group:'g1',display_title:'Named chat',last_timestamp:'2026-09-26T10:00:00Z'};
  const codex={session_id:'sibling',agent:'codex',group:'g1',display_title:'Named chat',last_timestamp:'2026-09-26T11:00:00Z'};
  const pool=[claude,codex];
  const context=vm.createContext({
    window:{SERENA:{structuredWorkspace:structured}},
    document:{getElementById:()=>({classList:{add(){},remove(){}},textContent:''})},
    showToast:()=>({update:(...args)=>calls.push(['toast',...args])}),
    _resolveHandoffSid:async sid=>sid,
    _findClientSession:sid=>pool.find(s=>s.session_id===sid),
    sessionSource:pool,sessions:pool,
    fetch:async(url,init)=>{fetched.push(JSON.parse(init.body));return {ok:true,json:async()=>({ok:true,prompt:'Brief',cwd:'/project'})};},
    _agentLabel:agent=>agent,
    _pseudoSessions:[],
    setSessionSource(){},_applyClientGroup(){},_markActive(){},setTermStatus(){},_startPseudoReconciler(){},
    openConv:sid=>calls.push(['open',sid]),
    _gtkSplitActive:false,_gtkSplitSids:[],_activeTerms:new Set(),
    startLiveTerminal:async(sid,options)=>{calls.push(['create',options]);termSessions.set(sid,{structured:true});},
    _feedTerminalWhenReady:async sid=>{calls.push(['feed',sid]);return true;},
    termSessions,
    ...(focusedSid?{focusedSid}:{}),
  });
  vm.runInContext(source.slice(start,end),context);
  return {context,calls,fetched};
}

for(const target of ['claude','codex'])test(`handing off to ${target} spawns a new ${target} chat even when the thread already holds one`,async()=>{
  const {context,calls,fetched}=linkedThread();
  await context.handoffSession('source',target);
  const create=calls.find(call=>call[0]==='create');
  assert.ok(create,'a new chat must be spawned');
  assert.equal(create[1].agent,target);
  assert.equal(create[1].isNew,true);
  assert.ok(!calls.some(call=>call[0]==='open'),'must not land on an existing chat');
  assert.equal(fetched.length,1);
  assert.equal(fetched[0].target_agent,target);
  const pseudo=context._pseudoSessions[0];
  assert.equal(pseudo.agent,target);
  assert.deepEqual([...pseudo.pending_group_member_sids].sort(),['sibling','source']);
});

test('a handoff is briefed from the chat in front when it belongs to the thread',async()=>{
  const {context,fetched}=linkedThread({focusedSid:'sibling'});
  await context.handoffSession('source','codex');
  assert.deepEqual(fetched,[{source_sid:'sibling',target_agent:'codex'}]);
});

test('with no chat in front a handoff is briefed from the other side of the thread',async()=>{
  const {context,fetched}=linkedThread();
  await context.handoffSession('source','codex');
  assert.deepEqual(fetched,[{source_sid:'source',target_agent:'codex'}]);
  const second=linkedThread();
  await second.context.handoffSession('source','claude');
  assert.deepEqual(second.fetched,[{source_sid:'sibling',target_agent:'claude'}]);
});
