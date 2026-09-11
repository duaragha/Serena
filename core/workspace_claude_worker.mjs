/** Private child process for WorkspaceRpc. Only an explicit open launches Claude. */
import {spawn} from 'node:child_process';
import {resolve} from 'node:path';
import {createInterface} from 'node:readline';
import {pathToFileURL} from 'node:url';
import {ClaudeSdkSession} from './workspace_claude_sdk.mjs';
import {ClaudeSdkChannel} from './workspace_claude_channel.mjs';

const [sdkPath,cliPath,sessionId,cwd]=process.argv.slice(2);
if (!sdkPath || !cliPath || !sessionId || !cwd) throw new Error('SDK, CLI, exact session and cwd are required');
const sdk=await import(pathToFileURL(resolve(sdkPath)).href);
let child,exit,closing;
const write=message=>new Promise((done,reject)=>{
  process.stdout.write(JSON.stringify(message)+'\n',error=>error?reject(error):done());
});
async function reap() {
  if (!child || !exit) return;
  let terminate,kill;
  try {
    terminate=setTimeout(()=>child.kill('SIGTERM'),2000);
    kill=setTimeout(()=>child.kill('SIGKILL'),4000);
    await exit;
  } finally {clearTimeout(terminate);clearTimeout(kill);}
}
const channel=new ClaudeSdkChannel({write,
  sessionOptions:{sdk,sessionId,cwd,
    options:{pathToClaudeCodeExecutable:resolve(cliPath),env:{...process.env,ELECTRON_RUN_AS_NODE:''},
      settingSources:['user','project','local'],systemPrompt:{type:'preset',preset:'claude_code'},
      includePartialMessages:true},
    spawnOwned:options=>{
      if(child) throw new Error('Native process already exists');
      child=spawn(options.command,options.args,{cwd:options.cwd,env:options.env,stdio:['pipe','pipe','pipe']});
      child.stderr.pipe(process.stderr,{end:false});
      exit=new Promise((done,reject)=>{child.once('exit',(code,signal)=>done({code,signal}));child.once('error',reject);});
      exit.catch(()=>{});
      // The host reserves a lease before open; this event binds its actual child.
      write({method:'claude/process',params:{pid:child.pid}}).catch(()=>shutdown());
      return child;
    },
  },
  createSession:options=>{
    const session=new ClaudeSdkSession(options);
    const close=session.close.bind(session);
    session.close=async()=>{
      const closed=close();
      const reaped=reap();
      const results=await Promise.allSettled([closed,reaped]);
      const failed=results.find(value=>value.status==='rejected');
      if(failed)throw failed.reason;
    };
    return session;
  },
});
function shutdown() {
  if(!closing) closing=channel.close().catch(error=>{process.stderr.write(`${error.message}\n`);process.exitCode=1;});
  return closing;
}
process.stdout.on('error',()=>{shutdown();process.stdin.destroy();});
process.once('SIGTERM',()=>{shutdown();process.stdin.destroy();});
process.once('SIGINT',()=>{shutdown();process.stdin.destroy();});
const lines=createInterface({input:process.stdin,crlfDelay:Infinity});
const active=new Set();
try {
  for await(const line of lines) {
    // Do not await a control here: it may be waiting for an approval on stdin.
    const operation=Promise.resolve().then(()=>channel.receive(JSON.parse(line)))
      .catch(error=>write({method:'workspace/error',params:{reason:error.message}}))
      .catch(()=>shutdown());
    active.add(operation);
    operation.finally(()=>active.delete(operation));
  }
} finally {
  await shutdown();
  await Promise.allSettled(active);
}
