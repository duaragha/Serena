import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {File} from 'node:buffer';
import {WorkspaceConnection} from '../ui/static/workspace-connection.mjs';

globalThis.crypto ??= webcrypto;
const storage = () => {
  const values = new Map();
  return {getItem: k => values.get(k), setItem: (k,v) => values.set(k,v)};
};
const response = data => ({ok: true, json: async () => data});

for(const action of ['submit','queue_input'])for(const reload of [false,true])test(`receipt cleanup storage failure retains ${action} identity (reload=${reload})`,async()=>{
  const saved=storage(),write=saved.setItem,calls=[],receipts=new Map();
  let failCleanup=false,executions=0;
  saved.setItem=(key,value)=>{
    if(failCleanup && key.startsWith('serena-workspace-pending:') && value==='{}'){
      failCleanup=false;throw Error('receipt storage unavailable');
    }
    write(key,value);
  };
  const options={sessionId:'exact',token:'token',storage:saved,receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    calls.push(JSON.parse(options.body));
    const id=calls.at(-1).request_id;
    if(!receipts.has(id))receipts.set(id,{turn:{id:`accepted-${++executions}`}});
    if(calls.length===1)failCleanup=true;
    return response({ok:true,result:receipts.get(id)});
  }};
  let connection=new WorkspaceConnection(options);
  const payload={inputs:[{type:'text',text:'one message'}],...(action==='queue_input'?{expectedTurnId:'running'}:{})};
  await assert.rejects(connection.command(action,payload),/receipt storage unavailable/);
  if(reload){connection.dispose();connection=new WorkspaceConnection(options);}
  assert.equal((await connection.command(action,payload)).turn.id,'accepted-1');
  assert.equal(calls[0].request_id,calls[1].request_id);
  assert.equal(executions,1);
  await connection.command(action,payload);
  assert.notEqual(calls[1].request_id,calls[2].request_id);
  assert.equal(executions,2);
  connection.dispose();
});

test('queued follow-up carries active identity and reuses lost receipt after reload',async()=>{
  const saved=storage(),calls=[];
  const options={sessionId:'exact',token:'token',storage:saved,receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    calls.push(JSON.parse(options.body));
    if(calls.length===1)throw Error('lost queue receipt');
    return response({ok:true,result:{turn:{id:'queued'}}});
  }};
  let connection=new WorkspaceConnection(options);
  const message={text:'follow-up',files:[],expectedTurnId:'running'};
  await assert.rejects(connection.controls().queueInput(message),/lost queue receipt/);
  connection.dispose();connection=new WorkspaceConnection(options);
  await assert.rejects(connection.controls().submit({text:'different'}),/unconfirmed queued message/);
  assert.equal(calls.length,1);
  assert.equal((await connection.controls().submit({text:message.text})).turn.id,'queued');
  assert.deepEqual(calls[0],calls[1]);
  assert.equal(calls[0].action,'queue_input');
  assert.deepEqual(calls[0].payload,{inputs:[{type:'text',text:'follow-up'}],expectedTurnId:'running'});
  connection.dispose();
});

test('explicit queue recovery uses saved payload without the current draft or uploads',async()=>{
  const saved=storage(),calls=[];
  const options={sessionId:'exact',token:'token',storage:saved,receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    calls.push(JSON.parse(options.body));
    if(calls.length===1)throw Error('lost');
    return response({ok:true,result:{turn:{id:'accepted'}}});
  }};
  let connection=new WorkspaceConnection(options);
  await assert.rejects(connection.controls().queueInput({text:'original',expectedTurnId:'first'}),/lost/);
  connection.dispose();connection=new WorkspaceConnection(options);
  const pending=connection.controls().pendingQueuedInputs();
  assert.equal(pending.length,1);assert.equal(calls.length,1);
  assert.equal(pending[0].payload.inputs[0].text,'original');
  pending[0].payload.inputs[0].text='local mutation';
  const id=pending[0].requestId;
  assert.equal((await connection.controls().retryQueuedInput(id)).turn.id,'accepted');
  assert.deepEqual(calls[0],calls[1]);
  assert.deepEqual(connection.controls().pendingQueuedInputs(),[]);
  await assert.rejects(connection.controls().retryQueuedInput(id),/no longer pending/);
  assert.equal(calls.length,2);connection.dispose();
});

test('disconnect is explicit and lost response reuses exact receipt after reload',async()=>{
  const saved=storage(),calls=[];
  const options={sessionId:'exact',token:'token',storage:saved,receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    assert(url.includes('/exact/'));calls.push(JSON.parse(options.body));
    if(calls.length===1)throw Error('lost');
    return response({ok:true,result:{disconnected:true,session_id:'exact'}});
  }};
  let conn=new WorkspaceConnection(options);conn.dispose();assert.equal(calls.length,0);
  conn=new WorkspaceConnection(options);
  await assert.rejects(conn.controls().disconnectSession(),/lost/);conn.dispose();
  conn=new WorkspaceConnection(options);
  assert.equal((await conn.controls().disconnectSession()).disconnected,true);
  assert.deepEqual(calls[0],calls[1]);assert.equal(calls[0].action,'disconnect_session');
  assert.deepEqual(calls[0].payload,{confirmed:true});conn.dispose();assert.equal(calls.length,2);
});

test('clear is explicit, response loss reuses its receipt, and reload preserves target',async()=>{
  const saved=storage(),calls=[];
  const options={sessionId:'source',token:'token',storage:saved,receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    calls.push(JSON.parse(options.body));
    if(calls.length===1)throw Error('response lost');
    return response({ok:true,result:{session_id:'11111111-1111-4111-8111-111111111111',provider:'claude'}});
  }};
  let conn=new WorkspaceConnection(options);
  assert.equal(conn.controls().lastClear(),null);assert.equal(calls.length,0);
  await assert.rejects(conn.controls().clearSession(),/response lost/);conn.dispose();
  conn=new WorkspaceConnection(options);
  const target=await conn.controls().clearSession();
  assert.deepEqual(calls[0],calls[1]);assert.equal(calls[0].action,'clear_session');
  assert.deepEqual(calls[0].payload,{confirmed:true});conn.dispose();
  conn=new WorkspaceConnection(options);
  assert.deepEqual(conn.controls().lastClear(),target);assert.equal(calls.length,2);conn.dispose();
});

test('plugin reload is explicit and retains its receipt after response loss',async()=>{
  const calls=[];
  const conn=new WorkspaceConnection({sessionId:'exact',token:'token',storage:storage(),receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    assert(url.includes('/exact/'));
    calls.push(JSON.parse(options.body));
    if(calls.length===1)throw Error('response lost');
    return response({ok:true,result:{data:[],plugins:[],error_count:0}});
  }});
  assert.equal(calls.length,0);
  await assert.rejects(conn.controls().reloadPlugins(),/response lost/);
  await conn.controls().reloadPlugins();
  assert.deepEqual(calls[0],calls[1]);
  assert.equal(calls[0].action,'reload_plugins');
  assert.deepEqual(calls[0].payload,{});
  conn.dispose();assert.equal(calls.length,2);
});

test('history retry retains cursor but permits a new receipt after confirmed read failure',async()=>{
  const calls=[];
  const conn=new WorkspaceConnection({sessionId:'exact',token:'token',storage:storage(),receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    calls.push(JSON.parse(options.body));
    return response(calls.length===1?{ok:false,retryable:true,error:'history read timed out'}:{ok:true,result:{turns:[],historyCursor:null}});
  }});
  await assert.rejects(conn.controls().loadEarlier('opaque'),/timed out/);
  await conn.controls().loadEarlier('opaque');
  assert.notEqual(calls[0].request_id,calls[1].request_id);
  assert.deepEqual(calls.map(c=>[c.action,c.payload]),[['load_earlier',{cursor:'opaque'}],['load_earlier',{cursor:'opaque'}]]);
  conn.dispose();
});

test('saved fork survives reload and registration retries never create a new fork',async()=>{
  const saved=storage(),calls=[];
  const options={sessionId:'exact',token:'token',storage:saved,receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    const body=JSON.parse(options.body);calls.push(body);
    return response({ok:true,result:{session_id:'fork',indexed:body.action==='register_fork'}});
  }};
  let conn=new WorkspaceConnection(options);
  const fork=await conn.controls().forkSession();conn.dispose();
  conn=new WorkspaceConnection(options);
  assert.deepEqual(conn.controls().lastFork(),fork);
  const recovered=await conn.controls().recoverFork(fork.request_id);
  assert.equal(recovered.request_id,fork.request_id);
  assert(recovered.indexed);
  assert.deepEqual(calls.map(item=>item.action),['fork_session','register_fork']);
  assert.deepEqual(calls[1].payload,{fork_request_id:calls[0].request_id});
  conn.controls().clearForkReceipt();assert.equal(conn.controls().lastFork(),null);conn.dispose();
});

test('fork retry after lost response or reload keeps exact receipt; busy refusal permits later intent',async()=>{
  const saved=storage(),calls=[];
  let mode='lost';
  const options={sessionId:'exact',token:'token',storage:saved,receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    calls.push(JSON.parse(options.body));
    if(mode==='lost')throw Error('response lost');
    if(mode==='busy')return response({ok:false,retryable:true,error:'busy'});
    return response({ok:true,result:{session_id:'fork',indexed:true}});
  }};
  let conn=new WorkspaceConnection(options);
  await assert.rejects(conn.controls().forkSession(),/response lost/);conn.dispose();
  conn=new WorkspaceConnection(options);mode='busy';
  await assert.rejects(conn.controls().forkSession(),/busy/);
  assert.deepEqual(calls[0],calls[1]);
  mode='ready';assert.equal((await conn.controls().forkSession()).session_id,'fork');
  assert.notEqual(calls[1].request_id,calls[2].request_id);
  assert(calls.every(call=>call.action==='fork_session' && Object.keys(call.payload).length===0));
  conn.dispose();assert.equal(calls.length,3);
});

test('interrupt carries displayed turn identity to the exact session',async()=>{
  const calls=[];
  const conn=new WorkspaceConnection({sessionId:'exact',token:'token',storage:storage(),receive:()=>{},error:()=>{},fetcher:async(url,options)=>{calls.push([url,JSON.parse(options.body)]);return response({ok:true,result:{}});}});
  await conn.controls().interrupt('displayed');conn.dispose();
  assert.equal(calls[0][0],'/api/workspace/exact/commands');
  assert.equal(calls[0][1].action,'interrupt');
  assert.deepEqual(calls[0][1].payload,{expectedTurnId:'displayed'});
});

test('queue editing routes replacement and original text through a stable control receipt',async()=>{
  const calls=[];
  const conn=new WorkspaceConnection({sessionId:'exact',token:'token',storage:storage(),receive:()=>{},error:()=>{},fetcher:async(url,options)=>{calls.push(JSON.parse(options.body));throw Error('response lost');}});
  for(let i=0;i<2;i++)await assert.rejects(conn.controls().editQueuedBridge('q','new','old'),/response lost/);
  assert.deepEqual(calls[0],calls[1]);
  assert.equal(calls[0].action,'edit_queued_bridge');
  assert.deepEqual(calls[0].payload,{request_id:'q',prompt:'new',expected_prompt:'old'});
  conn.dispose();
});

test('event inspector reads exact-session pages without moving live replay or sending commands',async()=>{
  const calls=[];
  const conn=new WorkspaceConnection({sessionId:'exact',token:'token',storage:storage(),receive:()=>{},error:()=>{},fetcher:async(url,options)=>{calls.push([url,options.method]);return response({events:[],cursor:200,has_more:false});}});
  conn.cursor=500;
  await conn.controls().events(200);
  assert.equal(conn.cursor,500);
  assert.throws(()=>conn.controls().events(-1),/cursor/);
  conn.dispose();
  assert.deepEqual(calls,[['/api/workspace/exact/events?after=200','GET']]);
});

test('steering targets the displayed turn and never falls back to submit', async () => {
  const calls=[];
  const conn=new WorkspaceConnection({sessionId:'exact',token:'secret',storage:storage(),receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    calls.push(JSON.parse(options.body)); return response({ok:true,result:{}});
  }});
  await assert.rejects(conn.controls().steer({text:'change direction'}),/identity/);
  assert.equal(calls.length,0);
  await conn.controls().steer({text:'change direction',expectedTurnId:'running-1'});
  assert.equal(calls[0].action,'steer');
  assert.deepEqual(calls[0].payload,{inputs:[{type:'text',text:'change direction'}],expectedTurnId:'running-1'});
  conn.dispose();
});

test('construction has no launch; explicit connect replays and dispose sends nothing', async () => {
  const calls = [], events = [];
  const conn = new WorkspaceConnection({sessionId:'exact',token:'secret',storage:storage(),
    receive:e=>events.push(e),error:e=>{throw e;},fetcher:async(url,options)=>{
      calls.push([url,options]);
      return response(url.endsWith('/attach') ? {ok:true} : {events:[{sequence:1,event:{method:'history'}}],has_more:false});
    }});
  assert.equal(calls.length,0);
  await conn.connect();
  conn.dispose();
  assert.equal(calls.length,2);
  assert.equal(events.length,1);
  assert.equal(calls[0][1].headers['X-Serena-Workspace-Token'],'secret');
  assert.equal(calls[1][0],'/api/workspace/exact/events?after=0');
});

test('lost send response retains request ID across view reload, confirmed next send is new',async()=>{
  const saved=storage(), ids=[];
  const make = fetcher => new WorkspaceConnection({sessionId:'exact',token:'secret',storage:saved,receive:()=>{},error:()=>{},fetcher});
  const payload={inputs:[{type:'text',text:'once'}]};
  const first=make(async(url,options)=>{ids.push(JSON.parse(options.body).request_id);throw Error('lost response');});
  await assert.rejects(first.command('submit',payload),/lost response/);
  first.dispose();
  const second=make(async(url,options)=>{ids.push(JSON.parse(options.body).request_id);return response({ok:true,result:{}});});
  await second.command('submit',payload);
  await second.command('submit',payload);
  second.dispose();
  assert.equal(ids[0],ids[1]);
  assert.notEqual(ids[1],ids[2]);
});

for(const method of ['submit','queueInput'])test(`${method} files upload once and stable attachment IDs survive a lost send response`,async()=>{
  const calls=[];
  let lose=true;
  const conn=new WorkspaceConnection({sessionId:'s',token:'s',storage:storage(),receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    calls.push([url,options]);
    if(url.endsWith('/uploads')) {
      assert.equal(options.body.get('file').name,'notes.txt');
      assert.equal(options.headers['Content-Type'],undefined);
      return response({ok:true,upload:{token:'bound-token'}});
    }
    if(lose){lose=false;throw Error('lost');}
    return response({ok:true,result:{}});
  }});
  const files=[new File(['contents'],'notes.txt',{type:'text/plain'})];
  await assert.rejects(conn.controls()[method]({text:'read',files,expectedTurnId:'active'}),/lost/);
  await conn.controls()[method]({text:'read',files,expectedTurnId:'active'});
  conn.dispose();
  assert.equal(calls.filter(([url])=>url.endsWith('/uploads')).length,1);
  const sends=calls.filter(([url])=>url.endsWith('/commands')).map(([,options])=>JSON.parse(options.body));
  assert.deepEqual(sends[0],sends[1]);
  assert.deepEqual(sends[0].payload.inputs,[{type:'text',text:'read'},{type:'upload',token:'bound-token'}]);
});

test('background controls use exact process ID and disposal sends no stop',async()=>{
  const calls=[];
  const conn=new WorkspaceConnection({sessionId:'exact',token:'secret',storage:storage(),receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    calls.push([url,JSON.parse(options.body)]);
    return response({ok:true,result:{data:[]}});
  }});
  assert.equal(calls.length,0);
  await conn.controls().backgroundTasks();
  await conn.controls().terminateBackgroundTask('native-p');
  conn.dispose();
  assert.deepEqual(calls.map(([url,body])=>[url,body.action,body.payload]),[
    ['/api/workspace/exact/commands','background_tasks',{}],
    ['/api/workspace/exact/commands','terminate_background_task',{processId:'native-p'}],
  ]);
});

test('skill-only sends and steering keep native selections and the expected turn',async()=>{
  const calls=[];
  const conn=new WorkspaceConnection({sessionId:'exact',token:'s',storage:storage(),receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    calls.push(JSON.parse(options.body));return response({ok:true,result:{}});
  }});
  await conn.controls().submit({text:'',options:{skills:['/skill']}});
  await conn.controls().steer({text:'',options:{skills:['/skill']},expectedTurnId:'working'});
  assert.deepEqual(calls.map(({action,payload})=>({action,payload})),[
    {action:'submit',payload:{inputs:[{type:'text',text:''}],options:{skills:['/skill']}}},
    {action:'steer',payload:{inputs:[{type:'text',text:''}],skills:['/skill'],expectedTurnId:'working'}},
  ]);
  conn.dispose();
});

test('MCP controls target only the selected session and never run on disposal',async()=>{
  const calls=[];
  const conn=new WorkspaceConnection({sessionId:'claude-exact',token:'s',storage:storage(),receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    calls.push([url,JSON.parse(options.body)]);return response({ok:true,result:{data:[]}});
  }});
  assert.deepEqual(calls,[]);
  await conn.controls().mcpServers();
  await conn.controls().mcpServerControl('local','disable');
  conn.dispose();
  assert.deepEqual(calls.map(([url,body])=>[url,body.action,body.payload]),[
    ['/api/workspace/claude-exact/commands','mcp_servers',{}],
    ['/api/workspace/claude-exact/commands','mcp_server_control',{name:'local',action:'disable'}],
  ]);
});

test('Codex MCP login and reload keep exact session and uncertain login receipt',async()=>{
  const saved=storage(),calls=[];
  let fail=true;
  const conn=new WorkspaceConnection({sessionId:'codex-exact',token:'s',storage:saved,receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    const body=JSON.parse(options.body);calls.push([url,body]);
    if(fail)throw Error('lost reply');
    return response({ok:true,result:{status:'pending'}});
  }});
  assert.deepEqual(calls,[]);
  await assert.rejects(conn.controls().mcpLogin('local'),/lost reply/);
  fail=false;
  await conn.controls().mcpLogin('local');
  assert.equal(calls[0][1].request_id,calls[1][1].request_id);
  await conn.controls().mcpReload();
  conn.dispose();
  assert.deepEqual(calls.map(([url,body])=>[url,body.action,body.payload]),[
    ['/api/workspace/codex-exact/commands','mcp_login',{name:'local'}],
    ['/api/workspace/codex-exact/commands','mcp_login',{name:'local'}],
    ['/api/workspace/codex-exact/commands','mcp_reload',{}],
  ]);
});

test('MCP settings retain exact session and request identity after a lost reply',async()=>{
  const calls=[];let fail=true;
  const conn=new WorkspaceConnection({sessionId:'codex-exact',token:'s',storage:storage(),receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    const body=JSON.parse(options.body);calls.push([url,body]);
    if(fail)throw Error('lost reply');
    return response({ok:true,result:{data:[],effectiveEnabled:false}});
  }});
  assert.deepEqual(calls,[]);
  await assert.rejects(conn.controls().setMcpEnabled('proof.dot',false),/lost reply/);
  fail=false;await conn.controls().setMcpEnabled('proof.dot',false);conn.dispose();
  assert.equal(calls.length,2);
  assert.equal(calls[0][1].request_id,calls[1][1].request_id);
  assert.equal(calls[0][0],'/api/workspace/codex-exact/commands');
  assert.equal(calls[0][1].action,'set_mcp_enabled');
  assert.deepEqual(calls[0][1].payload,{name:'proof.dot',enabled:false});
});

test('skill settings are explicit exact-path commands with stable retry identity',async()=>{
  const calls=[];
  let fail=true;
  const conn=new WorkspaceConnection({sessionId:'codex-exact',token:'s',storage:storage(),receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    const body=JSON.parse(options.body);calls.push([url,body]);
    if(fail)throw Error('lost reply');
    return response({ok:true,result:{data:[],effectiveEnabled:false}});
  }});
  assert.deepEqual(calls,[]);
  await assert.rejects(conn.controls().setSkillEnabled('/native/SKILL.md',false),/lost reply/);
  fail=false;
  await conn.controls().setSkillEnabled('/native/SKILL.md',false);
  conn.dispose();
  assert.equal(calls.length,2);
  assert.equal(calls[0][1].request_id,calls[1][1].request_id);
  assert.equal(calls[0][0],'/api/workspace/codex-exact/commands');
  assert.deepEqual(calls[0][1].payload,{path:'/native/SKILL.md',enabled:false});
  assert.equal(calls[0][1].action,'set_skill_enabled');
});

test('answer receipts persist only a fingerprint while preserving retry identity',async()=>{
  const saved=storage(), ids=[];
  const conn=new WorkspaceConnection({sessionId:'s',token:'s',storage:saved,receive:()=>{},error:()=>{},fetcher:async(url,options)=>{
    ids.push(JSON.parse(options.body).request_id);throw Error('lost');
  }});
  const content={action:'accept',content:{field:'private-form-value'}};
  await assert.rejects(conn.controls().answer(1,content),/lost/);
  assert.ok(!saved.getItem(conn.key).includes('private-form-value'));
  await assert.rejects(conn.controls().answer(1,content),/lost/);
  assert.equal(ids[0],ids[1]);
  conn.dispose();
});

test('a rejected render leaves the replay cursor before the failed event',async()=>{
  const errors=[];
  const conn=new WorkspaceConnection({sessionId:'s',token:'s',storage:storage(),receive:()=>false,error:e=>errors.push(e.message),fetcher:async()=>response({events:[{sequence:1,event:{method:'bad'}}],has_more:false})});
  await conn.poll();
  conn.dispose();
  assert.equal(conn.cursor,0);
  assert.match(errors[0],/not accepted/);
});

test('initial replay failure rejects attachment without cancelling the owner or advancing the cursor',async()=>{
  const calls=[], errors=[];
  let unavailable=true;
  const conn=new WorkspaceConnection({sessionId:'s',token:'s',storage:storage(),receive:()=>true,error:e=>errors.push(e),fetcher:async(url)=>{
    calls.push(url);
    if(url.endsWith('/attach')) return response({ok:true});
    if(unavailable) throw Error('history unavailable');
    return response({events:[{sequence:1,event:{method:'history'}}],has_more:false});
  }});
  try {
    await assert.rejects(conn.connect(),/history unavailable/);
    assert.equal(conn.cursor,0);
    assert.deepEqual(errors,[]);
    unavailable=false;
    await conn.connect();
    assert.equal(conn.cursor,1);
    assert.deepEqual(calls,['/api/workspace/s/attach','/api/workspace/s/events?after=0','/api/workspace/s/attach','/api/workspace/s/events?after=0']);
  } finally { conn.dispose(); }
});
