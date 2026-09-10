import assert from 'node:assert/strict';
import test from 'node:test';
import {ClaudeSdkSession} from '../core/workspace_claude_sdk.mjs';

function fixture(overrides={}) {
  const calls=[], outputs=[];
  let setup, finish;
  const end=new Promise(done=>{finish=done;});
  const stream={
    async *[Symbol.asyncIterator]() { await end; },
    initializationResult:async()=>({models:[]}),
    close(){finish();},
    applyFlagSettings:async value=>{calls.push(value);return {};},
  };
  const sdk={getSessionInfo:async()=>({sessionId:'exact',cwd:'/project'}),
    query(value){setup=value;value.options.spawnClaudeCodeProcess({});return stream;},...overrides};
  const session=new ClaudeSdkSession({sdk,sessionId:'exact',cwd:'/project',
    options:{resume:'wrong',forkSession:true},spawnOwned:()=>{calls.push('spawn');return {};},
    publish:message=>outputs.push(message),request:async()=>({action:'decline'})});
  return {session,calls,outputs,stream,get setup(){return setup;}};
}

function transitionFixture(){
  const f=fixture(), inbox=[];let wake,closed=false;
  f.stream[Symbol.asyncIterator]=async function*(){while(!closed){if(inbox.length)yield inbox.shift();else await new Promise(done=>{wake=done;});}};
  f.stream.close=()=>{closed=true;wake?.();};
  f.emit=message=>{inbox.push(message);wake?.();wake=null;};
  return f;
}

const clearedId='11111111-2222-4333-8444-555555555555';
test('explicit fresh creation fixes the reserved UUID and never resumes or forks',async()=>{
  const f=fixture({getSessionInfo:async()=>undefined});
  f.session.sessionId=clearedId;
  f.session.options={continue:true,resume:'wrong',forkSession:true,persistSession:false,resumeSessionAt:'old-message'};
  await f.session.create();
  const options=f.setup.options;
  assert.equal(options.sessionId,clearedId);
  assert.equal(options.resume,undefined);
  assert.equal(options.continue,false);
  assert.equal(options.forkSession,false);
  assert.equal(options.persistSession,true);
  assert.equal(options.resumeSessionAt,undefined);
  assert.equal(f.session.state,'ready');
  assert.deepEqual(f.calls,['spawn']);
  await assert.rejects(f.session.create(),/twice/);
  await f.session.close();
});

test('fresh creation rejects existing identity and invalid UUID without spawning',async()=>{
  const f=fixture();
  await assert.rejects(f.session.create(),/reserved UUID/);
  f.session.sessionId=clearedId;
  await assert.rejects(f.session.create(),/already exists/);
  assert.deepEqual(f.calls,[]);
  await assert.rejects(f.session.create(),/twice/);
});

test('clear blocks input until exact handoff acknowledgement and retains the runtime',async()=>{
  const f=transitionFixture();await f.session.open();
  const clear=f.session.beginClear();
  const input=(await f.setup.prompt.next()).value;
  assert.equal(input.message.content,'/clear');assert.equal(input.session_id,'exact');
  assert.throws(()=>f.session.send({type:'user',session_id:'exact'}),/not ready/);
  await assert.rejects(f.session.beginClear(),/not ready/);
  f.emit({type:'result',subtype:'success',is_error:false,session_id:clearedId,user_message_uuid:input.uuid});
  assert.deepEqual(await clear,{sessionId:clearedId});
  assert.equal(f.session.sessionId,'exact');assert.equal(f.session.state,'awaiting-handoff');
  assert.deepEqual(f.outputs,[]);
  await assert.rejects(f.session.commitClear('other'),/Exact pending/);
  assert.throws(()=>f.session.send({type:'user',session_id:clearedId}),/not ready/);
  assert.deepEqual(await f.session.commitClear(clearedId),{sessionId:clearedId});
  assert.equal(f.session.sessionId,clearedId);assert.equal(f.outputs[0].session_id,clearedId);
  assert.deepEqual(f.calls,['spawn']);
  assert.throws(()=>f.session.send({type:'user',session_id:'exact'}),/exact/);
  f.session.send({type:'user',session_id:clearedId,message:{role:'user',content:'next'}});
  assert.equal((await f.setup.prompt.next()).value.session_id,clearedId);
  await f.session.close();
});

test('clear refuses consumed but unfinished input',async()=>{
  const f=transitionFixture();await f.session.open();
  f.session.send({type:'user',session_id:'exact',message:{role:'user',content:'running'}});
  const sent=(await f.setup.prompt.next()).value;
  f.emit({type:'result',session_id:'exact',user_message_uuid:'unrelated'});
  await new Promise(done=>setTimeout(done,0));
  await assert.rejects(f.session.beginClear(),/pending inputs/);
  assert.equal(f.session.state,'ready');assert.equal(f.session.pending.length,0);
  assert(f.session.outstanding.has(sent.uuid));
  await f.session.close();
});

for(const bad of ['wrong-receipt','same-id','error','conflicting-id'])test(`clear rejects ${bad} without allowing input`,async()=>{
  const f=transitionFixture();await f.session.open();
  const clear=f.session.beginClear(), input=(await f.setup.prompt.next()).value;
  if(bad==='conflicting-id')f.emit({type:'system',session_id:'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'});
  f.emit({type:'result',subtype:'success',is_error:bad==='error',session_id:bad==='same-id'?'exact':clearedId,
    user_message_uuid:bad==='wrong-receipt'?'unrelated':input.uuid});
  await assert.rejects(clear,/clear/);
  assert.equal(f.session.state,'unavailable');assert.equal(f.session.sessionId,'exact');
  assert.throws(()=>f.session.send({type:'user',session_id:clearedId}),/not ready/);
  await assert.rejects(f.session.close());
});

test('closing a pending clear rejects its waiter without retry',async()=>{
  const f=transitionFixture();await f.session.open();
  const clear=f.session.beginClear();
  const rejected=assert.rejects(clear,/closed during transition/);
  await f.session.close();await rejected;
  assert.deepEqual(f.calls,['spawn']);assert.equal(f.session.transition,null);
});

test('handoff publication failure never re-enables input or repeats clear',async()=>{
  const f=transitionFixture();await f.session.open();
  await assert.rejects(f.session.control('beginClear'),/Unsupported/);
  const clear=f.session.beginClear(),input=(await f.setup.prompt.next()).value;
  f.emit({type:'result',subtype:'success',session_id:clearedId,user_message_uuid:input.uuid});
  await clear;
  f.session.publish=async()=>{throw Error('Journal unavailable');};
  await assert.rejects(f.session.commitClear(clearedId),/Journal unavailable/);
  assert.equal(f.session.state,'unavailable');
  assert.throws(()=>f.session.send({type:'user',session_id:clearedId}),/not ready/);
  await assert.rejects(f.session.beginClear(),/not ready/);
  assert.deepEqual(f.calls,['spawn']);await f.session.close();
});

test('explicit exact resume, one spawn, inputs and public controls',async()=>{
  const f=fixture();
  assert.deepEqual(f.calls,[]);
  await f.session.open();
  assert.equal(f.setup.options.resume,'exact');
  assert.equal(f.setup.options.forkSession,false);
  assert.throws(()=>f.setup.options.spawnClaudeCodeProcess({}),/Duplicate/);
  await assert.rejects(f.session.open(),/twice/);
  f.session.send({type:'user',session_id:'exact',message:{role:'user',content:'hello'}});
  assert.equal((await f.setup.prompt.next()).value.message.content,'hello');
  assert.throws(()=>f.session.send({type:'user',session_id:'other'}),/exact/);
  await f.session.control('applyFlagSettings',{effortLevel:'high'});
  await assert.rejects(f.session.control('close'),/Unsupported/);
  assert.deepEqual(f.calls,['spawn',{effortLevel:'high'}]);
  await f.session.close();
  assert.equal((await f.setup.prompt.next()).done,true);
});

test('missing or mismatched session never spawns',async()=>{
  for(const info of [undefined,{sessionId:'other'},{sessionId:'exact',cwd:'/wrong'}]) {
    const f=fixture({getSessionInfo:async()=>info});
    await assert.rejects(f.session.open(),/unavailable/);
    assert.deepEqual(f.calls,[]);
  }
});

test('fork is bound to owned identity and cannot spawn or redirect the driver',async()=>{
  const seen=[];
  const f=fixture({forkSession:async(...args)=>{seen.push(args);return {sessionId:'fork'};}});
  await assert.rejects(f.session.control('forkSession'),/not ready/);
  await f.session.open();
  await assert.rejects(f.session.control('forkSession',{dir:'/other'}),/owned session/);
  assert.deepEqual(await f.session.control('forkSession'),{sessionId:'fork'});
  assert.deepEqual(seen,[['exact',{dir:'/project'}]]);
  assert.equal(f.session.sessionId,'exact');
  assert.deepEqual(f.calls,['spawn']);
  await f.session.close();
});

test('close during lookup cannot launch a late process',async()=>{
  let resolveInfo;
  const f=fixture({getSessionInfo:()=>new Promise(done=>{resolveInfo=done;})});
  const opening=f.session.open();
  await f.session.close();
  resolveInfo({sessionId:'exact'});
  await assert.rejects(opening,/cancelled/);
  assert.deepEqual(f.calls,[]);
});

test('approval and elicitation reach caller with cancellation identity intact',async()=>{
  const f=fixture();
  const signal=new AbortController().signal;
  const options={signal,requestId:'request-1'};
  const received=[];
  f.session.request=async(...args)=>{received.push(args);return {action:'accept',content:{answer:'yes'}};};
  await f.session.open();
  const request={serverName:'local',message:'choose',mode:'form'};
  const answer=await f.setup.options.onElicitation(request,options);
  assert.equal(answer.content.answer,'yes');
  assert.deepEqual(received,[['elicitation',request,options]]);
  f.session.request=async()=>null;
  await assert.rejects(f.setup.options.onElicitation(request,options),/explicit/);
  const denial={behavior:'deny',message:'user declined'};
  f.session.request=async(...args)=>{received.push(args);return denial;};
  assert.equal(await f.setup.options.canUseTool('Bash',{command:'pwd'},options),denial);
  assert.deepEqual(received[1],['canUseTool',{toolName:'Bash',input:{command:'pwd'}},options]);
  await f.session.close();
});

test('foreign native output fails closed before publishing',async()=>{
  const f=fixture();
  let emit;
  const ready=new Promise(done=>{emit=done;});
  f.stream[Symbol.asyncIterator]=async function*(){await ready;yield {type:'assistant',session_id:'other'};};
  await f.session.open();
  emit();
  await assert.rejects(f.session.done,/different session/);
  assert.deepEqual(f.outputs,[]);
  assert.equal(f.session.state,'unavailable');
  await assert.rejects(f.session.close(),/different session/);
});

test('initialization failure closes the stream and never retries',async()=>{
  const f=fixture();
  f.stream.initializationResult=async()=>{throw new Error('native initialize failed');};
  await assert.rejects(f.session.open(),/native initialize failed/);
  await f.session.done;
  assert.equal(f.session.state,'unavailable');
  await assert.rejects(f.session.open(),/twice/);
  assert.deepEqual(f.calls,['spawn']);
  assert.throws(()=>f.session.send({type:'user',session_id:'exact'}),/not ready/);
});

test('native stream ending unexpectedly invalidates controls',async()=>{
  const f=fixture();
  await f.session.open();
  f.stream.close();
  await assert.rejects(f.session.done,/ended unexpectedly/);
  await assert.rejects(f.session.control('supportedAgents'),/not ready/);
  assert.deepEqual(f.calls,['spawn']);
});
