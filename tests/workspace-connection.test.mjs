import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {WorkspaceConnection} from '../ui/static/workspace-connection.mjs';

globalThis.crypto ??= webcrypto;
const storage = () => {
  const values = new Map();
  return {getItem: k => values.get(k), setItem: (k,v) => values.set(k,v)};
};
const response = data => ({ok: true, json: async () => data});

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

test('attachments fail honestly without losing them through a text-only send',()=>{
  let called=false;
  const conn=new WorkspaceConnection({sessionId:'s',token:'s',storage:storage(),receive:()=>{},error:()=>{},fetcher:()=>{called=true;}});
  assert.throws(()=>conn.controls().submit({text:'photo',files:[{name:'a.png'}]}),/uploads/);
  conn.dispose();
  assert.equal(called,false);
});

test('a rejected render leaves the replay cursor before the failed event',async()=>{
  const errors=[];
  const conn=new WorkspaceConnection({sessionId:'s',token:'s',storage:storage(),receive:()=>false,error:e=>errors.push(e.message),fetcher:async()=>response({events:[{sequence:1,event:{method:'bad'}}],has_more:false})});
  await conn.poll();
  conn.dispose();
  assert.equal(conn.cursor,0);
  assert.match(errors[0],/not accepted/);
});
