/** Isolated local-command persistence/resume proof; no authentication or inference. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {mkdtemp,rm} from 'node:fs/promises';
import {homedir,tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {fileURLToPath} from 'node:url';
import {createInterface} from 'node:readline';
import {ClaudeSdkSession} from '../core/workspace_claude_sdk.mjs';
import {isolatedProofEnv} from './workspace-proof-env.mjs';

const [sdkPath,cliPath,pythonPath,formMode,electronPath,frozenPath]=process.argv.slice(2);
assert(!formMode || formMode==='--discovery-form','Unknown proof mode');
assert(sdkPath && cliPath,'SDK module and installed CLI paths required');
const root=await mkdtemp(join(tmpdir(),'serena-claude-driver-'));
const cleanEnv=isolatedProofEnv(process.env,root);
const browsers=process.env.PLAYWRIGHT_BROWSERS_PATH || join(homedir(),'.cache','ms-playwright');
const proofPythonPath=process.env.SERENA_PROOF_PYTHONPATH;
for(const key of Object.keys(process.env)) delete process.env[key];
Object.assign(process.env,cleanEnv,{PLAYWRIGHT_BROWSERS_PATH:browsers});
const sdk=await import(pathToFileURL(resolve(sdkPath)).href);
const children=[],exits=[];
let driver,seed;
const spawnOwned=options=>{
  assert(children.every(child=>child.exitCode!==null),'A previous native owner is still alive');
  const child=spawn(options.command,options.args,{cwd:options.cwd,env:options.env,stdio:['pipe','pipe','pipe']});
  child.stderr.resume();
  children.push(child);
  const exit=new Promise((done,reject)=>{child.once('exit',(code,signal)=>done({code,signal}));child.once('error',reject);});
  exit.catch(()=>{});exits.push(exit);
  return child;
};
const options={cwd:root,pathToClaudeCodeExecutable:resolve(cliPath),env:{...process.env},
  settingSources:[],tools:[],strictMcpConfig:true,spawnClaudeCodeProcess:spawnOwned};
const deadline=setTimeout(()=>{
  console.error('Native driver proof exceeded 90s');
  seed?.close();driver?.close().catch(()=>{});
  for(const child of children)if(child.exitCode===null)child.kill('SIGTERM');
  process.exitCode=1;
},90000);
try {
  seed=sdk.query({prompt:'/effort high',options});
  let sid;
  for await(const message of seed) {
    if(message.session_id)sid=message.session_id;
    if(message.type==='result') {
      assert.equal(message.total_cost_usd,0);
      assert.equal(message.num_turns,0);
    }
  }
  seed.close();
  assert.equal((await exits[0]).code,0);
  assert(sid,'Local command must persist a native session');
  let finishTurn;
  const queuedResults=[];
  let collectingQueued=false;
  const result=new Promise(done=>{finishTurn=done;});
  driver=new ClaudeSdkSession({sdk,sessionId:sid,cwd:root,options,spawnOwned,
    publish:message=>{if(message.type==='result'){if(collectingQueued)queuedResults.push(message);else finishTurn(message);}},
    request:async()=>{throw new Error('Unexpected interactive request');}});
  await driver.open();
  await driver.control('applyFlagSettings',{effortLevel:'high'});
  assert(Array.isArray(await driver.control('supportedAgents')));
  driver.send({type:'user',session_id:sid,uuid:randomUUID(),parent_tool_use_id:null,
    message:{role:'user',content:'/effort low'}});
  const completed=await Promise.race([result,driver.done.then(()=>{throw new Error('Stream ended before reply');})]);
  assert.equal(completed.session_id,sid);
  assert.equal(completed.total_cost_usd,0);
  assert.equal(completed.num_turns,0);
  collectingQueued=true;
  console.log('Native completion receipt fields:',JSON.stringify({
    keys:Object.keys(completed),outstanding:driver.outstanding.size,
  }));
  const queuedIds=[randomUUID(),randomUUID()];
  for(const [index,id] of queuedIds.entries())driver.send({type:'user',session_id:sid,uuid:id,parent_tool_use_id:null,
    message:{role:'user',content:index?'/effort medium':'/effort high'}});
  const queueDeadline=Date.now()+10000;
  while(driver.outstanding.size){
    assert(Date.now()<queueDeadline,`Queued acknowledgements missing: ${JSON.stringify({
      outstanding:driver.outstanding.size,results:queuedResults.map(message=>({
        keys:Object.keys(message),uuid:message.user_message_uuid,uuids:message.user_message_uuids,
      })),
    })}`);
    await Promise.race([new Promise(done=>setTimeout(done,10)),driver.done.then(()=>{throw new Error('Stream ended before queued replies');})]);
  }
  const acknowledged=queuedResults.flatMap(message=>message.user_message_uuids || [message.user_message_uuid]);
  assert.deepEqual([...new Set(acknowledged)].sort(),[...queuedIds].sort());
  for(const message of queuedResults){
    assert.equal(message.session_id,sid);
    assert.equal(message.total_cost_usd,0);
    assert.equal(message.num_turns,0);
  }
  console.log(`PASS: two queued native local inputs correlated to their exact UUIDs in ${queuedResults.length} result records, same session/PID, zero inference`);
  await driver.close();
  assert.equal(children.length,2,'One seed owner, then one exact resumed owner');
  assert.equal((await exits[1]).code,0);
  console.log('PASS: isolated zero-inference local session persisted, original CLI reaped, exact session resumed; driver input received matching native result, effort and agent controls acknowledged');
  const worker=spawn(process.execPath,[fileURLToPath(new URL('../core/workspace_claude_worker.mjs',import.meta.url)),
    resolve(sdkPath),resolve(cliPath),sid,root],{env:{...process.env},stdio:['pipe','pipe','pipe']});
  children.push(worker);
  worker.stderr.pipe(process.stderr,{end:false});
  const workerExit=new Promise((done,reject)=>{worker.once('exit',(code,signal)=>done({code,signal}));worker.once('error',reject);});
  exits.push(workerExit);workerExit.catch(()=>{});
  const pending=new Map();
  let sequence=0,nativePid,resolveResult;
  const nativeResult=new Promise(done=>{resolveResult=done;});
  const lines=createInterface({input:worker.stdout,crlfDelay:Infinity});
  const reader=(async()=>{
    for await(const line of lines) {
      const message=JSON.parse(line);
      if(message.method==='claude/process')nativePid=message.params.pid;
      else if(message.method==='claude/message' && message.params.message.type==='result')resolveResult(message.params.message);
      else if(pending.has(message.id)) {
        pending.get(message.id)(message);pending.delete(message.id);
      }
    }
    for(const done of pending.values())done({error:{message:'Worker output closed'}});
  })();
  reader.catch(()=>{});
  const call=(method,params={})=>new Promise(done=>{
    const id=++sequence;pending.set(id,done);
    worker.stdin.write(JSON.stringify({id,method,params})+'\n');
  });
  assert.match((await call('control',{method:'supportedAgents',args:[]})).error.message,/not open/);
  assert.equal(nativePid,undefined,'Starting the wrapper cannot launch a CLI');
  assert(!(await call('open')).error);
  assert(Number.isInteger(nativePid) && nativePid!==worker.pid,'Actual CLI PID must be exposed');
  assert(!(await call('send',{message:{type:'user',session_id:sid,uuid:randomUUID(),parent_tool_use_id:null,
    message:{role:'user',content:'/effort medium'}}})).error);
  const wireResult=await Promise.race([nativeResult,reader.then(()=>{throw new Error('Worker closed before native result');})]);
  assert.equal(wireResult.session_id,sid);
  assert.equal(wireResult.total_cost_usd,0);
  assert.equal(wireResult.num_turns,0);
  assert(!(await call('close')).error);
  assert.throws(()=>process.kill(nativePid,0),{code:'ESRCH'},'Native CLI must be reaped before close acknowledgement');
  worker.stdin.end();
  assert.equal((await workerExit).code,0);
  await reader;
  console.log('PASS: real JSONL worker performed no automatic launch, resumed the exact session, routed input/output, reported actual child PID and reaped it before acknowledging close');
  if(pythonPath) {
    const python=spawn(resolve(pythonPath),[fileURLToPath(new URL('./verify-workspace-claude-transport.py',import.meta.url)),
      resolve(sdkPath),resolve(cliPath),sid,root],{env:{...process.env,SERENA_EVIDENCE_KIND:'live',
        SERENA_PROOF_DISCOVERY_FORM:formMode?'1':'',
        ...(electronPath?{SERENA_WORKSPACE_NODE:resolve(electronPath),SERENA_WORKSPACE_NODE_MODE:'electron'}:{})},stdio:['ignore','inherit','inherit']});
    children.push(python);
    const pythonExit=new Promise((done,reject)=>{python.once('exit',(code,signal)=>done({code,signal}));python.once('error',reject);});
    exits.push(pythonExit);pythonExit.catch(()=>{});
    assert.equal((await pythonExit).code,0,'Python transport proof failed');
  }
  if(frozenPath) {
    assert(pythonPath && electronPath,'Frozen proof requires Python and Electron');
    const frozen=spawn(resolve(pythonPath),[fileURLToPath(new URL('./verify-workspace-frozen.py',import.meta.url)),
      resolve(frozenPath),resolve(sdkPath),resolve(electronPath),sid,root],{
      env:{...process.env,SERENA_EVIDENCE_KIND:'live',...(proofPythonPath?{SERENA_PROOF_PYTHONPATH:proofPythonPath}:{})},stdio:['ignore','inherit','inherit']});
    children.push(frozen);
    const frozenExit=new Promise((done,reject)=>{frozen.once('exit',(code,signal)=>done({code,signal}));frozen.once('error',reject);});
    exits.push(frozenExit);frozenExit.catch(()=>{});
    assert.equal((await frozenExit).code,0,'Frozen workspace proof failed');
  }
  const sourceHistory=await sdk.getSessionMessages(sid,{dir:root});
  assert(sourceHistory.length>0);
  const fork=await sdk.forkSession(sid,{dir:root,title:'Isolated workspace fork proof'});
  assert(fork.sessionId && fork.sessionId!==sid,'Fork must have its own persisted identity');
  const forkHistory=await sdk.getSessionMessages(fork.sessionId,{dir:root});
  assert.deepEqual(forkHistory.map(item=>item.message),sourceHistory.map(item=>item.message));
  const sourceIds=new Set(sourceHistory.map(item=>item.uuid));
  assert(forkHistory.every(item=>!sourceIds.has(item.uuid)),'Fork must remap message identities');
  assert.deepEqual(await sdk.getSessionMessages(sid,{dir:root}),sourceHistory,'Fork changed source history');
  let finishFork;
  const forkResult=new Promise(done=>{finishFork=done;});
  driver=new ClaudeSdkSession({sdk,sessionId:fork.sessionId,cwd:root,options,spawnOwned,
    publish:message=>{if(message.type==='result')finishFork(message);},
    request:async()=>{throw new Error('Unexpected fork interactive request');}});
  await driver.open();
  driver.send({type:'user',session_id:fork.sessionId,uuid:randomUUID(),parent_tool_use_id:null,
    message:{role:'user',content:'/effort high'}});
  const forkCompleted=await Promise.race([forkResult,driver.done.then(()=>{throw new Error('Fork stream closed before result');})]);
  assert.equal(forkCompleted.session_id,fork.sessionId);
  assert.equal(forkCompleted.num_turns,0);
  assert.equal(forkCompleted.total_cost_usd,0);
  await driver.close();
  assert.equal((await exits.at(-1)).code,0);
  assert.deepEqual(await sdk.getSessionMessages(sid,{dir:root}),sourceHistory,'Fork input changed source history');
  console.log('PASS: native fork preserved history with new message/session IDs, resumed exact fork and received local-command output without changing source');
  console.log('PASS: all isolated processes reaped; no user authentication, sessions or settings used');
} finally {
  seed?.close();
  await driver?.close().catch(()=>{});
  for(const child of children)if(child.exitCode===null)child.kill('SIGTERM');
  await Promise.allSettled(exits);
  clearTimeout(deadline);
  await rm(root,{recursive:true,force:true,maxRetries:20,retryDelay:250});
}
