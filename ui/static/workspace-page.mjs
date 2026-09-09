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
  error: error => pane.error(error),
});
const controls = connection.controls();
controls.openFork = sid => {
  if (typeof sid !== 'string' || !/^[a-f0-9-]{36}$/.test(sid) || sid === boot.sessionId) throw Error('Invalid fork identity');
  if (parent !== window) parent.postMessage({type:'serena-workspace-open-fork',sid:boot.sessionId,target:sid},location.origin);
  else location.assign('/workspace/' + encodeURIComponent(sid));
};
const pane = new WorkspacePane(document.querySelector('#workspace-pane'), {
  sessionId: boot.sessionId, provider: boot.provider, controls,
});
function reportState() {
  if (parent !== window) parent.postMessage({type:'serena-workspace-state',sid:boot.sessionId,state:pane.conversation.status},location.origin);
}
window.addEventListener('message', e => {
  if (e.origin === location.origin && e.source === parent && e.data?.type === 'serena-workspace-focus') pane.input.focus();
});
const button = document.querySelector('#workspace-connect');
button.addEventListener('click', async () => {
  button.disabled = true;
  try {
    await connection.connect();
    button.hidden = true;
    pane.input.focus();
    if (['Codex', 'Claude'].includes(boot.provider)) connection.controls().models().catch(error => pane.error(error));
  } catch (error) {
    pane.error(error);
    if (parent !== window) parent.postMessage({type:'serena-workspace-state',sid:boot.sessionId,state:'unavailable'},location.origin);
    button.disabled = false;
  }
});
window.addEventListener('pagehide', () => { connection.dispose(); pane.dispose(); });
