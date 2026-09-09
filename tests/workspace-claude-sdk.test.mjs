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
