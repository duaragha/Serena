/** Isolated local-command persistence/resume proof; no authentication or inference. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {fileURLToPath} from 'node:url';
import {createInterface} from 'node:readline';
import {ClaudeSdkSession} from '../core/workspace_claude_sdk.mjs';

const [sdkPath,cliPath,pythonPath]=process.argv.slice(2);
assert(sdkPath && cliPath,'SDK module and installed CLI paths required');
const root=await mkdtemp(join(tmpdir(),'serena-claude-driver-'));
const path=process.env.PATH;
for(const key of Object.keys(process.env)) delete process.env[key];
Object.assign(process.env,{PATH:path,HOME:root,CLAUDE_CONFIG_DIR:join(root,'config'),XDG_CONFIG_HOME:join(root,'xdg')});
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
  console.error('Native driver proof exceeded 45s');
  seed?.close();driver?.close().catch(()=>{});
  for(const child of children)if(child.exitCode===null)child.kill('SIGTERM');
  process.exitCode=1;
},45000);
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
  const result=new Promise(done=>{finishTurn=done;});
  driver=new ClaudeSdkSession({sdk,sessionId:sid,cwd:root,options,spawnOwned,
    publish:message=>{if(message.type==='result')finishTurn(message);},
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
      resolve(sdkPath),resolve(cliPath),sid,root],{env:{...process.env,SERENA_EVIDENCE_KIND:'live'},stdio:['ignore','inherit','inherit']});
    children.push(python);
    const pythonExit=new Promise((done,reject)=>{python.once('exit',(code,signal)=>done({code,signal}));python.once('error',reject);});
    exits.push(pythonExit);pythonExit.catch(()=>{});
    assert.equal((await pythonExit).code,0,'Python transport proof failed');
  }
  console.log('PASS: all isolated processes reaped; no user authentication, sessions or settings used');
} finally {
  seed?.close();
  await driver?.close().catch(()=>{});
  for(const child of children)if(child.exitCode===null)child.kill('SIGTERM');
  await Promise.allSettled(exits);
  clearTimeout(deadline);
  await rm(root,{recursive:true,force:true});
}
