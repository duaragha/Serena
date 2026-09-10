/** Isolated public TypeScript SDK control probe; no prompts or user sessions. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const [sdkPath, cliPath] = process.argv.slice(2);
assert(sdkPath && cliPath, 'Supply an installed sdk.mjs and the installed Claude CLI path');
const {query} = await import(pathToFileURL(resolve(sdkPath)).href);
const root = await mkdtemp(join(tmpdir(), 'serena-ts-control-proof-'));
const env = Object.fromEntries(Object.keys(process.env).map(key => [key, '']));
Object.assign(env, {PATH: process.env.PATH, HOME: root, CLAUDE_CONFIG_DIR: join(root, 'config'), XDG_CONFIG_HOME: join(root, 'xdg')});
let child, stopInput, stream, drain;
let exit;
const deadline = setTimeout(() => { console.error('Control probe exceeded 45s'); stream?.close(); child?.kill('SIGTERM'); process.exitCode=1; }, 45000);
async function* input() { await new Promise(done => { stopInput=done; }); }
try {
  stream=query({prompt:input(),options:{
    cwd:root, pathToClaudeCodeExecutable:resolve(cliPath), env,
    settingSources:[], tools:[], strictMcpConfig:true,
    onElicitation:async()=>({action:'decline'}),
    spawnClaudeCodeProcess:options=>{
      assert(!child,'Only one owned CLI is allowed');
      child=spawn(options.command,options.args,{cwd:options.cwd,env:options.env,stdio:['pipe','pipe','pipe']});
      child.stderr.resume();
      exit=new Promise((done,reject)=>{child.once('exit',(code,signal)=>done({code,signal}));child.once('error',reject);});
      return child;
    },
  }});
  drain=(async()=>{for await(const message of stream) assert.notEqual(message.type,'assistant','No inference output expected');})();
  // Attach a rejection handler immediately while preserving the eventual error.
  drain.catch(()=>{});
  const initialized=await stream.initializationResult();
  assert(Array.isArray(initialized.models) && initialized.models.length);
  await stream.applyFlagSettings({effortLevel:'high'});
  await stream.applyFlagSettings({effortLevel:null});
  const agents=await stream.supportedAgents();
  const skills=await stream.reloadSkills();
  const plugins=await stream.reloadPlugins();
  assert(Array.isArray(agents));
  assert(skills && typeof skills==='object');
  assert(Array.isArray(plugins.commands) && Array.isArray(plugins.plugins) && Array.isArray(plugins.mcpServers));
  console.log('PASS: public TypeScript SDK initialization, session-only effort apply/clear, agent inventory, skill and plugin reload');
  console.log('No user prompt, inference, resume, copied authentication or user settings; elicitation registered but not exercised');
} finally {
  stopInput?.(); stream?.close();
  try { await drain; } finally {
    let ended;
    try { if(exit)ended=await exit; }
    finally { clearTimeout(deadline);await rm(root,{recursive:true,force:true}); }
    if(ended){assert.equal(ended.code,0,`CLI exited unexpectedly: ${JSON.stringify(ended)}`);console.log(`PASS: owned CLI reaped: ${JSON.stringify(ended)}`);}
  }
}
