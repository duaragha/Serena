import {WorkspacePane} from './workspace-pane.mjs';
import {WorkspaceConnection} from './workspace-connection.mjs';

const boot = JSON.parse(document.querySelector('#workspace-boot').textContent);
const connection = new WorkspaceConnection({
  ...boot,
  receive: event => {
    const accepted = pane.receive(event);
    if (accepted) reportState();
    return accepted;
  },
  error: error => connectionFailed(error),
});
const controls = connection.controls();
controls.listSessions = (query, offset=0) => connection.request('/sessions?' + new URLSearchParams({provider:boot.provider.toLowerCase(),q:query,offset}));
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
function reportState() {
  if (pane.conversation.status === 'unavailable') showRetry();
  if (parent !== window) parent.postMessage({type:'serena-workspace-state',sid:boot.sessionId,state:pane.conversation.status},location.origin);
}
window.addEventListener('message', e => {
  if (e.origin === location.origin && e.source === parent && e.data?.type === 'serena-workspace-focus') pane.input.focus();
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
button.disabled = false;
window.addEventListener('pagehide', () => { connection.dispose(); pane.dispose(); });
