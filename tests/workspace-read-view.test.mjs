import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const web = readFileSync(new URL('../ui/web.py', import.meta.url), 'utf8');
const start = web.indexOf('async function loadReadTranscript(sid, force) {');
const end = web.indexOf('\nfunction setConvMode(mode)', start);
assert(start >= 0 && end > start);
const source = web.slice(start, end);

function fixture(fetch) {
  const elements = Object.fromEntries(['convBody','convTitle','convMeta'].map(id=>[id,{innerHTML:'',textContent:''}]));
  const refresh = [];
  const context = vm.createContext({fetch, currentSessionId:'exact', convMode:'read', _convLoaded:new Set(),
    document:{getElementById:id=>elements[id]}, _isPseudoSid:()=>false, _patchClientSession:()=>{},
    formatTokens:String, esc:String, linkifyPaths:String, _isSerenaVoiceSession:()=>false,
    _scheduleExternalReadRefresh:sid=>refresh.push(sid), _stopExternalReadRefresh:()=>refresh.push('stop')});
  vm.runInContext(source, context);
  return {context,elements,refresh};
}

test('pending read state refreshes into actual transcript without caching a permanent empty view',async()=>{
  let data={title:'My chat',messages:[],native_persistence_pending:true};
  const {context,elements,refresh}=fixture(async()=>({ok:true,json:async()=>data}));
  await context.loadReadTranscript('exact',false);
  assert.match(elements.convBody.innerHTML,/Native transcript not indexed yet/);
  assert.equal(elements.convTitle.textContent,'My chat');
  assert.deepEqual(refresh,['exact']);
  data={title:'My chat',messages:[{role:'assistant',text:'Native output'}],agent:'codex'};
  await context.loadReadTranscript('exact',true);
  assert.match(elements.convBody.innerHTML,/Native output/);
  assert.deepEqual(refresh,['exact','stop']);
});

test('HTTP failure does not overwrite title or cache a successful read',async()=>{
  const {context,elements}=fixture(async()=>({ok:false,json:async()=>({error:'Not found'})}));
  elements.convTitle.textContent='Existing title';
  await context.loadReadTranscript('exact',false);
  assert.match(elements.convBody.innerHTML,/Error loading/);
  assert.equal(elements.convTitle.textContent,'Existing title');
  assert.equal(context._convLoaded.has('exact'),false);
});

test('failed request for previous chat cannot overwrite current view',async()=>{
  let reject;
  const {context,elements}=fixture(()=>new Promise((resolve,fail)=>{reject=fail;}));
  const request=context.loadReadTranscript('exact',false);
  context.currentSessionId='other';elements.convBody.innerHTML='Other conversation';
  reject(Error('connection lost'));
  await request;
  assert.equal(elements.convBody.innerHTML,'Other conversation');
});
