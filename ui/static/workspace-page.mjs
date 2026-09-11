import {WorkspacePane} from './workspace-pane.mjs';
import {WorkspaceConnection} from './workspace-connection.mjs';

const boot = JSON.parse(document.querySelector('#workspace-boot').textContent);
const connection = new WorkspaceConnection({
  ...boot,
  receive: event => {
    const accepted = pane.receive(event);
    const catalog=event?.event;
    if(accepted && catalog?.method==='workspace/catalog' && catalog.params?.indexed===true
      && catalog.params.session_id===boot.sessionId && typeof catalog.params.display_title==='string'
      && parent!==window){
      parent.postMessage(catalog.params.native_rename===true
        ? {type:'serena-workspace-title-changed',sid:boot.sessionId}
        : {type:'serena-workspace-catalog',sid:boot.sessionId,title:catalog.params.display_title},location.origin);
    }
    if (accepted) reportState();
    return accepted;
  },
  error: error => connectionFailed(error),
  runtime: runtime => pane.setSleeping(runtime?.sleeping === true),
});
const controls = connection.controls();
if(parent!==window)controls.newConversation=title=>{
  parent.postMessage({type:'serena-workspace-new-conversation',sid:boot.sessionId,title},location.origin);
};
const renameSession=controls.renameSession;
controls.renameSession=async name=>{
  const result=await renameSession(name);
  if(result.catalog?.indexed!==true)throw Error(`Native name saved, but Serena title synchronization failed: ${result.catalog?.error || 'catalog unavailable'}`);
  if(parent!==window)parent.postMessage({type:'serena-workspace-title-changed',sid:boot.sessionId},location.origin);
  return result;
};
controls.diagnostics = () => connection.command('diagnostics',{});
controls.accountStatus = () => connection.command('account_status',{});
controls.accountRateLimits = () => connection.command('account_rate_limits',{});
controls.accountTokenUsage = scope => connection.command('account_token_usage',scope==='session'?{scope:'session'}:{});
controls.accountLogin = () => connection.command('account_login',{});
controls.cancelAccountLogin = loginId => connection.command('account_login_cancel',{loginId});
controls.accountLogout = () => connection.command('account_logout',{confirmed:true});
controls.resetSavedSetting = failureId => connection.command('reset_saved_setting',{failure_id:failureId,confirmed:true});
controls.listSessions = (query, offset=0, archived=false) => connection.request('/sessions?' + new URLSearchParams({provider:boot.provider.toLowerCase(),q:query,offset,archived}));
if(boot.provider==='Codex')controls.restoreArchive=async(sid,reconcile=false,requestId=null)=>{
  if(typeof sid!=='string' || !/^[a-f0-9-]{36}$/.test(sid))throw Error('Invalid session identity');
  const target=new WorkspaceConnection({sessionId:sid,token:boot.token,receive:()=>{},error:()=>{}});
  try{return await target.restoreArchive({reconcile,requestId});}finally{target.dispose();}
};
if(boot.provider==='Codex')controls.reconcileArchive=async(sid,requestId)=>{
  if(typeof sid!=='string' || !/^[a-f0-9-]{36}$/.test(sid) || typeof requestId!=='string')throw Error('Invalid archive recovery identity');
  const target=new WorkspaceConnection({sessionId:sid,token:boot.token,receive:()=>{},error:()=>{}});
  try{return await target.archiveSession({reconcile:true,requestId});}finally{target.dispose();}
};
if(boot.provider==='Codex')controls.deleteSavedSession=async(sid,{reconcile=false,requestId=null}={})=>{
  if(typeof sid!=='string' || !/^[a-f0-9-]{36}$/.test(sid))throw Error('Invalid delete session identity');
  const target=new WorkspaceConnection({sessionId:sid,token:boot.token,receive:()=>{},error:()=>{}});
  try{return await target.deleteSession({reconcile,requestId});}finally{target.dispose();}
};
controls.openSession = sid => {
  if(typeof sid!=='string' || !/^[a-f0-9-]{36}$/.test(sid))throw Error('Invalid session identity');
  if(parent!==window)parent.postMessage({type:'serena-workspace-open-session',sid:boot.sessionId,target:sid},location.origin);
  else location.assign('/workspace/'+encodeURIComponent(sid));
};
controls.openCleared = sid => {
  if(typeof sid!=='string' || !/^[a-f0-9-]{36}$/.test(sid) || sid===boot.sessionId)throw Error('Invalid cleared session identity');
  if(parent!==window)parent.postMessage({type:'serena-workspace-open-cleared',sid:boot.sessionId,target:sid},location.origin);
  else location.assign('/workspace/'+encodeURIComponent(sid));
};
controls.openFork = sid => {
  if (typeof sid !== 'string' || !/^[a-f0-9-]{36}$/.test(sid) || sid === boot.sessionId) throw Error('Invalid fork identity');
  if (parent !== window) parent.postMessage({type:'serena-workspace-open-fork',sid:boot.sessionId,target:sid},location.origin);
  else location.assign('/workspace/' + encodeURIComponent(sid));
};
const pane = new WorkspacePane(document.querySelector('#workspace-pane'), {
  sessionId: boot.sessionId, provider: boot.provider, controls,
});
let intersects=true;
let viewContext=null;
let splitSids=[];
let pinned=null;
let lastContextSignature='',lastContextAt=0;
const contextKey=`serena-workspace-view:${boot.sessionId}`;
try {
  const saved=JSON.parse(sessionStorage.getItem(contextKey) || 'null');
  viewContext=saved && typeof saved.view_id==='string' && Number.isSafeInteger(saved.sequence)
    ? saved : {view_id:crypto.randomUUID(),sequence:0};
} catch { /* Context telemetry must not interfere with the session. */ }
function reportContext(closing=false,sleepPeers=false) {
  closing=closing===true;
  if(!viewContext || pane.disposed)return;
  const visible=!closing && intersects && document.visibilityState==='visible';
  const state={visible,focused:visible && document.hasFocus(),split_sids:visible ? splitSids : [],
    ...(closing ? {closed:true} : {}),
    ...(typeof pinned==='boolean' ? {pinned} : {}),
    ...(sleepPeers===true && !connection.storageFailure && visible && document.hasFocus() && pinned===false && splitSids.length>1
      ? {sleep_peers:true} : {}),
    draft:!!(connection.storageFailure || pane.input.value.trim() || pane.files.length || pane.selectedSkills.length || pane.selectedApps.length)};
  const signature=JSON.stringify(state),now=performance.now();
  if(!closing && sleepPeers!==true && signature===lastContextSignature && now-lastContextAt<1800)return;
  const data={view_id:viewContext.view_id,sequence:++viewContext.sequence,...state};
  try {sessionStorage.setItem(contextKey,JSON.stringify(viewContext));} catch {return;}
  lastContextSignature=signature;lastContextAt=now;
  fetch(connection.base+'/view-context', {
    method:'POST',credentials:'same-origin',keepalive:closing,
    headers:{'Content-Type':'application/json','X-Serena-Workspace-Token':boot.token},
    body:JSON.stringify(data),
  }).catch(()=>{});
}
function refreshContext() {
  if(parent!==window)parent.postMessage({type:'serena-workspace-context-request',sid:boot.sessionId},location.origin);
  reportContext();
}
const contextTimer=setInterval(refreshContext,2000);
document.addEventListener('input',reportContext);
window.addEventListener('blur',reportContext);
const updateVisibility=()=>{
  connection.setVisible(intersects && document.visibilityState==='visible');
  reportContext();
};
const visibilityObserver=new IntersectionObserver(entries=>{
  intersects=entries.some(entry=>entry.isIntersecting);
  updateVisibility();
});
visibilityObserver.observe(pane.root);
document.addEventListener('visibilitychange',updateVisibility);
function reportFocus() {
  refreshContext();
  if(parent!==window && intersects && document.visibilityState==='visible' && document.hasFocus())
    parent.postMessage({type:'serena-workspace-focused',sid:boot.sessionId},location.origin);
}
document.addEventListener('focusin',reportFocus);
document.addEventListener('pointerdown',reportFocus);
document.addEventListener('pointerdown',event=>{
  if(event.isTrusted)setTimeout(()=>reportContext(false,true),0);
});
window.addEventListener('focus',reportFocus);
function reportState() {
  if (pane.conversation.status === 'unavailable') {
    if(pane.conversation.error==='Conversation deleted'){
      button.hidden=true;button.disabled=true;button.textContent='Conversation deleted';
    }else showRetry();
  }
  if (parent !== window) parent.postMessage({type:'serena-workspace-state',sid:boot.sessionId,state:pane.conversation.status},location.origin);
}
window.addEventListener('message', e => {
  if(e.origin===location.origin && e.source===parent && e.data?.type==='serena-workspace-layout'
    && e.data.sid===boot.sessionId && Array.isArray(e.data.split_sids)
    && e.data.split_sids.length<=4 && e.data.split_sids.every(sid=>typeof sid==='string')){
    splitSids=e.data.split_sids;
    pinned=typeof e.data.pinned==='boolean' ? e.data.pinned : null;
    reportContext();return;
  }
  if (e.origin === location.origin && e.source === parent && e.data?.type === 'serena-workspace-focus') pane.input.focus();
  if(e.origin!==location.origin || e.source!==parent || e.data?.type!=='serena-workspace-handoff' || e.data.sid!==boot.sessionId)return;
  const {requestId,text}=e.data;
  if(typeof requestId!=='string' || !requestId || typeof text!=='string')return;
  try{connection.requireReceipts();}catch(error){
    pane.error(error);
    parent.postMessage({type:'serena-workspace-handoff-result',sid:boot.sessionId,requestId,result:{ok:false,error:error.message}},location.origin);
    return;
  }
  connection.request('/handoff',{provider:boot.provider.toLowerCase(),prompt:text,request_id:requestId}).then(async result=>{
    parent.postMessage({type:'serena-workspace-handoff-result',sid:boot.sessionId,requestId,result},location.origin);
    // Observe the existing owner; do not submit the draft or reattach again.
    await connection.poll();
    if(result.ok || result.pending)button.hidden=true;
  }).catch(error=>{
    pane.error(error);
    parent.postMessage({type:'serena-workspace-handoff-result',sid:boot.sessionId,requestId,result:{ok:false,error:error.message}},location.origin);
  });
});
const button = document.querySelector('#workspace-connect');
if(pane.clearedSession)button.textContent='Resume original conversation';
function showRetry() {
  button.hidden = false;
  button.disabled = false;
  button.textContent = 'Retry connection';
}
function connectionFailed(error) {
  pane.error(error);
  showRetry();
}
button.addEventListener('click', async () => {
  button.disabled = true;
  try {
    await connection.connect();
    if (pane.conversation.status === 'unavailable') {
      showRetry();
      return;
    }
    button.hidden = true;
    if(pane.clearedSession){
      controls.forgetClear();
      pane.clearedSession=null;
      pane.render();
    }
    pane.input.focus();
    if (['Codex', 'Claude'].includes(boot.provider)) connection.controls().models().catch(error => pane.error(error));
  } catch (error) {
    connectionFailed(error);
    if (parent !== window) parent.postMessage({type:'serena-workspace-state',sid:boot.sessionId,state:'unavailable'},location.origin);
    button.disabled = false;
  }
});
// Reopening a view may read its existing owner, but must never resume a process.
if(pane.clearedSession)button.disabled=false;
else connection.observe().then(observing=>{
  button.hidden=observing && pane.conversation.status!=='unavailable';
  button.disabled=false;
}).catch(connectionFailed);
window.addEventListener('pagehide', () => {
  clearInterval(contextTimer);document.removeEventListener('input',reportContext);
  window.removeEventListener('blur',reportContext);
  reportContext(true);
  document.removeEventListener('focusin',reportFocus);document.removeEventListener('pointerdown',reportFocus);
  window.removeEventListener('focus',reportFocus);
  visibilityObserver.disconnect();document.removeEventListener('visibilitychange',updateVisibility);
  connection.dispose();pane.dispose();
});
window.addEventListener('pageshow', event => {
  if(event.persisted && pane.disposed)location.reload();
});
