/** Isolated local-command persistence/resume proof; no authentication or inference. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {ClaudeSdkSession} from '../core/workspace_claude_sdk.mjs';

const [sdkPath,cliPath]=process.argv.slice(2);
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
  console.log(`PASS: ${children.length} sequential native processes reaped, no user authentication, sessions or settings used`);
} finally {
  seed?.close();
  await driver?.close().catch(()=>{});
  for(const child of children)if(child.exitCode===null)child.kill('SIGTERM');
  await Promise.allSettled(exits);
  clearTimeout(deadline);
  await rm(root,{recursive:true,force:true});
}
