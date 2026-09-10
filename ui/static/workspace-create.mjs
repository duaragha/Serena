const boot = JSON.parse(document.getElementById('workspace-creation-boot').textContent);
const submit = document.getElementById('creation-submit');
const open = document.getElementById('creation-open');
const status = document.getElementById('creation-status');
document.getElementById('creation-project').value = boot.cwd;
const key = 'serena-workspace-create:' + boot.source;
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
let record;
let busy = false;
function save() { sessionStorage.setItem(key, JSON.stringify(record)); }
function showTarget() {
  submit.hidden = true;
  open.hidden = false;
  status.textContent = 'Session ' + record.target;
}
try {
  const saved = sessionStorage.getItem(key);
  if (saved) {
    record = JSON.parse(saved);
    if (!uuid(record.request_id) || record.cwd !== boot.cwd || record.provider !== boot.provider
        || (record.target && !uuid(record.target))) throw Error('Creation record needs recovery.');
    if (record.target) showTarget();
    else { submit.textContent = 'Check creation'; status.textContent = 'Previous creation request retained.'; }
  }
  submit.disabled = false;
} catch (error) { status.textContent = error.message; }
submit.addEventListener('click', async () => {
  if (busy || submit.disabled) return;
  busy = true;
  submit.disabled = true;
  try {
    record ||= {request_id: crypto.randomUUID(), provider: boot.provider, cwd: boot.cwd};
    save(); // Never send if the exact retry identity cannot survive reload.
    status.textContent = 'Creating session...';
    const response = await fetch('/api/workspace/create', {method:'POST', headers:{
      'Content-Type':'application/json', 'X-Serena-Workspace-Token':boot.token},
    body:JSON.stringify({request_id:record.request_id, provider:record.provider, cwd:record.cwd, confirmed:true})});
    const result = await response.json();
    if (!response.ok || !result.ok) throw Error(result.error || 'Creation has not been confirmed.');
    const target = result.result;
    if (!target || !uuid(target.session_id) || target.provider !== record.provider || target.cwd !== record.cwd)
      throw Error('Creation returned an unexpected identity or project.');
    record.target = target.session_id;
    save();
    showTarget();
  } catch (error) {
    status.textContent = error.message;
    submit.textContent = 'Check creation';
  } finally { busy = false; submit.disabled = false; }
});
open.addEventListener('click', () => {
  if (!record?.target) return;
  if (window.parent === window) location.assign('/workspace/' + record.target);
  else window.parent.postMessage({type:'serena-workspace-open-created', sid:boot.source, target:record.target}, location.origin);
});
