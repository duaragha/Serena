import assert from 'node:assert/strict';
import test from 'node:test';
import {ClaudeSdkChannel} from '../core/workspace_claude_channel.mjs';

function fixture() {
  const messages=[],calls=[];
  let options;
  const channel=new ClaudeSdkChannel({write:message=>messages.push(message),sessionOptions:{sessionId:'exact'},
    createSession:value=>{
      options=value;
      calls.push('create');
      return {open:async()=>({ready:true}),create:async()=>{calls.push('native-create');return {ready:true};},send:value=>calls.push(value),
        control:async(method,...args)=>({method,args}),close:async()=>calls.push('close')};
    }});
  return {channel,messages,calls,get options(){return options;}};
}

test('creation is explicit, parameter-free and cannot replace an existing owner',async()=>{
  const f=fixture();
  await f.channel.receive({id:1,method:'create',params:{resume:'other'}});
  assert.ok(f.messages.at(-1).error);
  assert.deepEqual(f.calls,[]);
  await f.channel.receive({id:2,method:'create',params:{}});
  assert.deepEqual(f.calls,['create','native-create']);
  for(const method of ['create','open']){
    await f.channel.receive({id:3,method,params:{}});
    assert.match(f.messages.at(-1).error.message,/already exists/);
  }
  assert.deepEqual(f.calls,['create','native-create']);
  await f.channel.close();
});

test('clear handoff has explicit private methods with exact payloads',async()=>{
  const f=fixture();
  await f.channel.receive({id:1,method:'begin_clear',params:{}});
  assert.match(f.messages.at(-1).error.message,/open session/);
  assert.deepEqual(f.calls,[]);
  await f.channel.receive({id:2,method:'open'});
  const target='11111111-2222-4333-8444-555555555555';
  f.channel.session.beginClear=async name=>{f.calls.push(['clear',name]);return {sessionId:target};};
  f.channel.session.commitClear=async sid=>{assert.equal(sid,target);f.calls.push('commit');return {sessionId:sid};};
  await f.channel.receive({id:3,method:'begin_clear',params:{unexpected:true}});
  assert.ok(f.messages.at(-1).error);
  await f.channel.receive({id:'bad-name',method:'begin_clear',params:{name:42}});
  assert.ok(f.messages.at(-1).error);
  await f.channel.receive({id:4,method:'begin_clear',params:{name:'Next work'}});
  assert.deepEqual(f.messages.at(-1).result,{sessionId:target});
  await f.channel.receive({id:5,method:'commit_clear',params:{sessionId:target,unexpected:true}});
  assert.ok(f.messages.at(-1).error);
  await f.channel.receive({id:6,method:'commit_clear',params:{sessionId:target}});
  assert.deepEqual(f.calls,['create',['clear','Next work'],'commit']);
});

test('pending permission prevents native clear dispatch',async()=>{
  const f=fixture();
  await f.channel.receive({id:1,method:'open'});
  f.channel.session.beginClear=async()=>assert.fail('must not clear');
  const question=f.channel.ask('canUseTool',{toolName:'Bash'});
  const rejected=assert.rejects(question,/owner closed/);
  await new Promise(done=>setImmediate(done));
  await f.channel.receive({id:2,method:'begin_clear',params:{}});
  assert.match(f.messages.at(-1).error.message,/pending approvals/);
  await f.channel.close();
  await rejected;
});

test('no automatic open; native events and explicit session controls share channel',async()=>{
  const f=fixture();
  assert.deepEqual(f.calls,[]);
  await f.channel.receive({id:1,method:'open'});
  assert.equal(f.options.sessionId,'exact');
  await f.options.publish({type:'assistant',session_id:'exact'});
  await f.channel.receive({id:2,method:'send',params:{message:{type:'user',session_id:'exact'}}});
  await f.channel.receive({id:3,method:'control',params:{method:'supportedAgents',args:[]}});
  assert.deepEqual(f.messages[1],{method:'claude/message',params:{message:{type:'assistant',session_id:'exact'}}});
  assert.deepEqual(f.messages[3],{id:3,result:{method:'supportedAgents',args:[]}});
  await f.channel.receive({id:4,method:'open'});
  assert.match(f.messages.at(-1).error.message,/already exists/);
  assert.equal(f.calls.filter(value=>value==='create').length,1);
});

test('approval replies remain processable while another control waits',async()=>{
  const f=fixture();
  await f.channel.receive({id:1,method:'open'});
  f.channel.session.control=()=>f.options.request('elicitation',{message:'Choose'},{requestId:'native'});
  const waiting=f.channel.receive({id:2,method:'control',params:{method:'reloadPlugins',args:[]}});
  await new Promise(done=>setImmediate(done));
  const question=f.messages.at(-1);
  assert.equal(question.method,'claude/elicitation');
  assert.equal(question.params.nativeRequestId,'native');
  await f.channel.receive({id:question.id,result:{action:'accept',content:{choice:2}}});
  await waiting;
  assert.deepEqual(f.messages.at(-1),{id:2,result:{action:'accept',content:{choice:2}}});
  await assert.rejects(f.channel.receive({id:question.id,result:{action:'accept'}}),/no longer pending/);
});

test('native cancellation retires request and rejects late answer',async()=>{
  const f=fixture();
  const abort=new AbortController();
  const answer=f.channel.ask('canUseTool',{toolName:'Bash'},{signal:abort.signal});
  await new Promise(done=>setImmediate(done));
  const id=f.messages[0].id;
  abort.abort();
  await assert.rejects(answer,/cancelled/);
  assert.deepEqual(f.messages.at(-1),{method:'serverRequest/resolved',params:{requestId:id}});
  await assert.rejects(f.channel.receive({id,result:{behavior:'allow'}}),/no longer pending/);
});

test('permission suggestions and selected updates survive JSON channel unchanged',async()=>{
  const f=fixture();
  const suggestion={type:'addRules',rules:[{toolName:'Bash',ruleContent:'pwd'}],behavior:'allow',destination:'localSettings'};
  const pending=f.channel.ask('canUseTool',{toolName:'Bash',input:{command:'pwd'}},{toolUseID:'tool-1',suggestions:[suggestion]});
  await new Promise(done=>setImmediate(done));
  const question=JSON.parse(JSON.stringify(f.messages.at(-1)));
  assert.deepEqual(question.params.options.suggestions,[suggestion]);
  const result={behavior:'allow',updatedInput:{command:'pwd'},updatedPermissions:[suggestion]};
  await f.channel.receive(JSON.parse(JSON.stringify({id:question.id,result})));
  assert.deepEqual(await pending,result);
  assert.equal(f.channel.pending.size,0);
});

test('owner close rejects outstanding questions without accepting them',async()=>{
  const f=fixture();
  await f.channel.receive({id:1,method:'open'});
  const answer=f.channel.ask('elicitation',{message:'Choose'});
  const rejected=assert.rejects(answer,/owner closed/);
  await f.channel.receive({id:2,method:'close'});
  await rejected;
  assert.equal(f.channel.pending.size,0);
  await f.channel.receive({id:3,method:'send',params:{message:{}}});
  assert.match(f.messages.at(-1).error.message,/closed/);
});

test('immediate native cancellation never emits a stale question',async()=>{
  const f=fixture();
  const abort=new AbortController();
  const answer=f.channel.ask('elicitation',{message:'Choose'},{signal:abort.signal});
  abort.abort();
  await assert.rejects(answer,/cancelled/);
  assert.equal(f.messages.some(message=>message.method==='claude/elicitation'),false);
});

test('native stream failure is reported without creating a new owner',async()=>{
  const f=fixture();
  let fail;
  const create=f.channel.createSession;
  f.channel.createSession=options=>({...create(options),done:new Promise((resolve,reject)=>{fail=reject;})});
  await f.channel.receive({id:1,method:'open'});
  fail(new Error('Native stream disconnected'));
  await new Promise(done=>setImmediate(done));
  assert.deepEqual(f.messages.at(-1),{method:'workspace/error',params:{reason:'Native stream disconnected'}});
  assert.deepEqual(f.calls,['create']);
});
