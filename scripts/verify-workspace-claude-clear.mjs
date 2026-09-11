/** Probe native clear identity/persistence using isolated local commands only. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {mkdtemp,rm} from 'node:fs/promises';
import {homedir,tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {ClaudeSdkSession} from '../core/workspace_claude_sdk.mjs';

const [sdkPath,cliPath,pythonPath]=process.argv.slice(2);
assert(sdkPath && cliPath);
const root=await mkdtemp(join(tmpdir(),'workspace-clear-proof-'));
const path=process.env.PATH;
const proofPythonPath=process.env.SERENA_PROOF_PYTHONPATH;
const proofBrowserChannel=process.env.SERENA_PROOF_BROWSER_CHANNEL;
const browsers=process.env.PLAYWRIGHT_BROWSERS_PATH || join(homedir(),'.cache','ms-playwright');
for(const key of Object.keys(process.env))delete process.env[key];
Object.assign(process.env,{PATH:path,HOME:root,CLAUDE_CONFIG_DIR:join(root,'config'),XDG_CONFIG_HOME:join(root,'xdg'),ANTHROPIC_BASE_URL:'http://127.0.0.1:9',
  ...(proofBrowserChannel?{SERENA_PROOF_BROWSER_CHANNEL:proofBrowserChannel}:{})});
const sdk=await import(pathToFileURL(resolve(sdkPath)).href);
const children=[],exits=[];
let seed,stream,driver,stopped=false,wake;
const queue=[],results=[],messages=[];
const options={cwd:root,pathToClaudeCodeExecutable:resolve(cliPath),settingSources:[],tools:[],strictMcpConfig:true,env:{...process.env},
  spawnClaudeCodeProcess:options=>{
    const child=spawn(options.command,options.args,{cwd:options.cwd,env:options.env,stdio:['pipe','pipe','pipe']});
    child.stderr.resume();children.push(child);
    const exit=new Promise((resolve,reject)=>{child.once('exit',(code,signal)=>resolve({code,signal}));child.once('error',reject);});
    exit.catch(()=>{});exits.push(exit);return child;
  }};
async function* input(){while(!stopped){if(queue.length)yield queue.shift();else await new Promise(resolve=>{wake=resolve;});}}
function send(text,sid){queue.push({type:'user',uuid:randomUUID(),session_id:sid,parent_tool_use_id:null,message:{role:'user',content:text}});wake?.();}
async function waitResult(count){const end=Date.now()+20000;while(results.length<count && Date.now()<end)await new Promise(resolve=>setTimeout(resolve,20));assert(results.length>=count,'Native local command did not complete');return results[count-1];}
try{
  seed=sdk.query({prompt:'/effort high',options});
  let original;
  for await(const message of seed){if(message.session_id)original=message.session_id;if(message.type==='result'){assert.equal(message.total_cost_usd,0);assert.equal(message.num_turns,0);}}
  seed.close();await exits[0];assert(original);
  const before=await sdk.getSessionMessages(original,{dir:root});
  stream=sdk.query({prompt:input(),options:{...options,resume:original}});
  const reader=(async()=>{for await(const message of stream){messages.push(message);if(message.type==='result')results.push(message);}})();
  reader.catch(()=>{});
  const info=await stream.initializationResult();
  console.log(JSON.stringify({original,clearAdvertised:(info.commands||[]).some(command=>command.name==='clear')}));
  send('/clear',original);
  const cleared=await waitResult(1);
  assert.equal(cleared.total_cost_usd,0);assert.equal(cleared.num_turns,0);
  const clearedInfo=await sdk.getSessionInfo(cleared.session_id,{dir:root});
  const clearedHistory=await sdk.getSessionMessages(cleared.session_id,{dir:root});
  assert.notEqual(cleared.session_id,original);
  console.log(JSON.stringify({clearSession:cleared.session_id,totalCost:cleared.total_cost_usd,modelTurns:cleared.num_turns,
    persistedBeforeNextPrompt:clearedInfo?.sessionId===cleared.session_id,messagesAfterClear:clearedHistory.length,
    identities:[...new Set(messages.map(message=>message.session_id).filter(Boolean))]}));
  send('/effort low',cleared.session_id);
  const next=await waitResult(2);
  assert.equal(next.total_cost_usd,0);assert.equal(next.num_turns,0);
  stopped=true;wake?.();stream.close();await reader;await exits[1];
  const oldHistory=await sdk.getSessionMessages(original,{dir:root});
  const nextHistory=await sdk.getSessionMessages(next.session_id,{dir:root});
  assert.deepEqual(oldHistory,before,'Clear must preserve original conversation records');
  assert.equal(next.session_id,cleared.session_id);
  const originalIds=new Set(before.map(message=>message.uuid));
  assert(nextHistory.every(message=>!originalIds.has(message.uuid)),'Old conversation records leaked into new history');
  console.log(JSON.stringify({nextSession:next.session_id,changedIdentity:next.session_id!==original,originalMessagesBefore:before.length,originalMessagesAfter:oldHistory.length,nextMessages:nextHistory.length}));
  const output=[];
  driver=new ClaudeSdkSession({sdk,sessionId:original,cwd:root,options,
    spawnOwned:options.spawnClaudeCodeProcess,publish:message=>output.push(message),
    request:async()=>{throw new Error('Local clear unexpectedly requested permission');}});
  await driver.open();
  const nativePid=children.at(-1).pid;
  const transition=await driver.beginClear('Named clear proof');
  assert.equal(driver.state,'awaiting-handoff');assert.equal(driver.sessionId,original);
  assert.equal(transition.requestedName,'Named clear proof');assert.equal(transition.nameConfirmed,true);
  assert.equal((await sdk.getSessionInfo(transition.sessionId,{dir:root})).customTitle,'Named clear proof');
  assert.throws(()=>driver.send({type:'user',session_id:transition.sessionId}),/not ready/);
  await assert.rejects(driver.commitClear(original),/Exact pending/);
  await driver.commitClear(transition.sessionId);
  const uuid=randomUUID();
  driver.send({type:'user',uuid,session_id:transition.sessionId,parent_tool_use_id:null,message:{role:'user',content:'/effort low'}});
  const end=Date.now()+20000;
  while(!output.some(message=>message.type==='result' && message.user_message_uuid===uuid) && Date.now()<end)await new Promise(resolve=>setTimeout(resolve,20));
  const final=output.find(message=>message.type==='result' && message.user_message_uuid===uuid);
  assert(final);assert.equal(final.session_id,transition.sessionId);assert.equal(final.total_cost_usd,0);assert.equal(final.num_turns,0);
  assert.equal(children.at(-1).pid,nativePid);assert.equal(children.length,3);
  await driver.close();await exits[2];
  assert.deepEqual(await sdk.getSessionMessages(original,{dir:root}),before);
  console.log('PASS: production SDK driver verified the named target, paused at the new identity until exact acknowledgement; same native process accepted subsequent input; original history preserved');
  if(pythonPath){
    const proof=spawn(resolve(pythonPath),[resolve('scripts/verify-workspace-claude-clear-transport.py'),resolve(sdkPath),resolve(cliPath),process.execPath,root,original],
      {env:{...process.env,PLAYWRIGHT_BROWSERS_PATH:browsers,...(proofPythonPath?{PYTHONPATH:proofPythonPath}:{})},stdio:'inherit'});
    const code=await new Promise((done,reject)=>{proof.once('exit',done);proof.once('error',reject);});
    assert.equal(code,0,'Real worker transport clear proof failed');
    assert.deepEqual(await sdk.getSessionMessages(original,{dir:root}),before);
  }
  console.log('PASS: native clear identity/persistence observed with zero inference and isolated home; no user session touched');
}finally{
  stopped=true;wake?.();seed?.close();stream?.close();
  await driver?.close().catch(()=>{});
  for(const child of children)if(child.exitCode===null && child.signalCode===null)child.kill('SIGKILL');
  await Promise.allSettled(exits);await rm(root,{recursive:true,force:true});
}
