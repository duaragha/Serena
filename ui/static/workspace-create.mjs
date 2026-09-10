const boot = JSON.parse(document.getElementById('workspace-creation-boot').textContent);
const submit = document.getElementById('creation-submit');
const open = document.getElementById('creation-open');
const status = document.getElementById('creation-status');
const context = document.getElementById('creation-context');
const warning = document.getElementById('creation-warning');
const seeded = boot.seeded === true;
let seedReady = !seeded;
context.hidden = document.getElementById('creation-context-label').hidden = !seeded;
document.getElementById('creation-project').value = boot.cwd;
const key = 'serena-workspace-create:' + boot.source;
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
let record;
let busy = false;
let invalidRecord = false;
function save() { sessionStorage.setItem(key, JSON.stringify(record)); }
function showTarget() {
  submit.hidden = true;
  open.hidden = false;
  status.textContent = 'Session ' + record.target;
  warning.textContent = record.initial_error || '';
}
try {
  const saved = sessionStorage.getItem(key);
  if (saved) {
    record = JSON.parse(saved);
    if (!uuid(record.request_id) || record.cwd !== boot.cwd || record.provider !== boot.provider
        || (seeded && (typeof record.seed !== 'string' || !record.seed)) || (!seeded && record.seed)
        || (record.target && !uuid(record.target))) throw Error('Creation record needs recovery.');
    if (record.target) showTarget();
    else { submit.textContent = 'Check creation'; status.textContent = 'Previous creation request retained.'; }
  }
  if (seeded && record?.seed) { context.value = record.seed; seedReady = true; }
  if (seeded && !record) { status.textContent = 'Waiting for initial context'; submit.textContent = 'Create and send'; }
  submit.disabled = !seedReady;
} catch (error) { invalidRecord = true; submit.disabled = true; status.textContent = error.message; }
window.addEventListener('message', event => {
  if (invalidRecord || !seeded || parent === window || event.source !== parent || event.origin !== location.origin
      || event.data?.type !== 'serena-workspace-seed' || event.data.sid !== boot.source) return;
  try {
    const seed = event.data.seed;
    if (typeof seed !== 'string' || !seed || seed.includes('\0') || new TextEncoder().encode(seed).length > 1024 * 1024)
      throw Error('Initial context is invalid or exceeds 1 MiB.');
    if (record && record.seed !== seed) throw Error('Initial context differs from the saved creation request.');
    record ||= {request_id:crypto.randomUUID(), provider:boot.provider, cwd:boot.cwd, seed};
    save();
    context.value = seed;
    seedReady = true;
    if (!record.target && !busy) { submit.disabled = false; submit.textContent = 'Create and send'; status.textContent = ''; }
  } catch (error) { seedReady = false; submit.disabled = true; warning.textContent = error.message; }
});
submit.addEventListener('click', async () => {
  if (invalidRecord || busy || submit.disabled || !seedReady) return;
  busy = true;
  submit.disabled = true;
  try {
    record ||= {request_id: crypto.randomUUID(), provider: boot.provider, cwd: boot.cwd};
    save(); // Never send if the exact retry identity cannot survive reload.
    status.textContent = 'Creating session...';
    const response = await fetch('/api/workspace/create', {method:'POST', headers:{
      'Content-Type':'application/json', 'X-Serena-Workspace-Token':boot.token},
    body:JSON.stringify({request_id:record.request_id, provider:record.provider, cwd:record.cwd, confirmed:true,
      ...(seeded ? {seed:record.seed} : {})})});
    const result = await response.json();
    if (!response.ok || !result.ok) throw Error(result.error || 'Creation has not been confirmed.');
    const target = result.result;
    if (!target || !uuid(target.session_id) || target.provider !== record.provider || target.cwd !== record.cwd)
      throw Error('Creation returned an unexpected identity or project.');
    record.target = target.session_id;
    if (result.initial_message && !result.initial_message.ok)
      record.initial_error = result.initial_message.error || 'Initial context delivery is unconfirmed; it has not been retried.';
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
