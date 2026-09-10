/** Isolated native fresh-session proof; no credentials or model inference. */
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
const root=await mkdtemp(join(tmpdir(),'workspace-claude-create-'));
const path=process.env.PATH;
const proofPythonPath=process.env.SERENA_PROOF_PYTHONPATH;
const browsers=process.env.PLAYWRIGHT_BROWSERS_PATH || join(homedir(),'.cache','ms-playwright');
for(const key of Object.keys(process.env))delete process.env[key];
Object.assign(process.env,{PATH:path,HOME:root,CLAUDE_CONFIG_DIR:join(root,'config'),
  XDG_CONFIG_HOME:join(root,'xdg'),ANTHROPIC_BASE_URL:'http://127.0.0.1:9'});
const sdk=await import(pathToFileURL(resolve(sdkPath)).href);
const sid=randomUUID(),children=[],exits=[],events=[];
const options={cwd:root,pathToClaudeCodeExecutable:resolve(cliPath),settingSources:[],tools:[],
  strictMcpConfig:true,env:{...process.env}};
function spawnOwned(options){
  const child=spawn(options.command,options.args,{cwd:options.cwd,env:options.env,stdio:['pipe','pipe','pipe']});
  child.stderr.resume();
  children.push(child);
  const exited=new Promise((done,reject)=>{child.once('exit',(code,signal)=>done({code,signal}));child.once('error',reject);});
  exited.catch(()=>{});exits.push(exited);
  return child;
}
function driver(){return new ClaudeSdkSession({sdk,sessionId:sid,cwd:root,options,spawnOwned,
  publish:message=>events.push(message),request:async()=>{throw Error('Unexpected permission request');}});}
let session=driver();
const deadline=setTimeout(()=>{for(const child of children)child.kill('SIGKILL');},45000);
try{
  assert.equal(await sdk.getSessionInfo(sid,{dir:root}),undefined);
  await session.create();
  assert.equal(session.state,'ready');
  assert.equal(children.length,1);
  const pid=children[0].pid;
  console.log(JSON.stringify({reservedSession:sid,initialized:true,eventsBeforeInput:events.length,
    persistedBeforeInput:Boolean(await sdk.getSessionInfo(sid,{dir:root}))}));
  const uuid=randomUUID();
  session.send({type:'user',uuid,session_id:sid,parent_tool_use_id:null,message:{role:'user',content:'/effort low'}});
  const until=Date.now()+20000;
  while(!events.some(message=>message.type==='result' && message.user_message_uuid===uuid) && Date.now()<until)
    await new Promise(done=>setTimeout(done,20));
  const result=events.find(message=>message.type==='result' && message.user_message_uuid===uuid);
  assert(result);
  assert.equal(result.session_id,sid);
  assert.equal(result.num_turns,0);
  assert.equal(result.total_cost_usd,0);
  assert.equal(children.length,1);
  assert.equal(children[0].pid,pid);
  assert(events.filter(message=>message.session_id).every(message=>message.session_id===sid));
  await session.close();await exits[0];
  const history=await sdk.getSessionMessages(sid,{dir:root});
  assert(history.length>0);
  const duplicate=driver();
  await assert.rejects(duplicate.create(),/already exists/);
  assert.equal(children.length,1);
  session=driver();
  await session.open();
  assert.equal(session.sessionId,sid);
  assert.equal(children.length,2);
  await session.close();await exits[1];
  assert.deepEqual(await sdk.getSessionMessages(sid,{dir:root}),history);
  console.log('PASS: explicit native UUID creation, same-process local input, exact output and persisted history; zero inference');
  console.log('PASS: duplicate creation refused before spawn; exact resume preserved history; children reaped');
  if(pythonPath){
    const proof=spawn(resolve(pythonPath),[resolve('scripts/verify-workspace-claude-create-transport.py'),
      resolve(sdkPath),resolve(cliPath),process.execPath,root],
      {env:{...process.env,PLAYWRIGHT_BROWSERS_PATH:browsers,...(proofPythonPath?{PYTHONPATH:proofPythonPath}:{})},stdio:'inherit'});
    const code=await new Promise((done,reject)=>{proof.once('exit',done);proof.once('error',reject);});
    assert.equal(code,0,'Native Python creation proof failed');
  }
}finally{
  clearTimeout(deadline);
  await session.close();
  for(const child of children)if(child.exitCode===null && child.signalCode===null)child.kill('SIGKILL');
  await Promise.allSettled(exits);
  await rm(root,{recursive:true,force:true});
}
