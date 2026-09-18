'use strict';
const api = window.serenaReleases;
const el = id => document.getElementById(id);
let snapshot = null;
let status = null;
let busy = false;
let polling = false;
const selected = new Set();
const tested = new Set();

function error(message) { el('error').textContent = message || ''; el('error').hidden = !message; }
function validation() {
  if (!snapshot) return 'Release information is unavailable.';
  const chosen = new Set([...snapshot.installed, ...selected]);
  const added = snapshot.catalog.features.filter(f => selected.has(f.id) && !snapshot.installed.includes(f.id));
  if (!added.length) return 'No new features selected.';
  for (const feature of added) {
    const missing = feature.requires.filter(id => !chosen.has(id));
    if (missing.length) return `${feature.title} requires ${missing.map(id => snapshot.catalog.features.find(f => f.id === id).title).join(', ')}.`;
    if (!tested.has(feature.id)) return `Not marked as tested: ${feature.title}.`;
  }
  return '';
}
function controls() {
  const warning = validation();
  el('validation').textContent = warning || `${selected.size} selected`;
  const pending = status && status.run?.status !== 'completed';
  for (const id of ['verify', 'publish']) el(id).disabled = busy || !!warning || !!pending;
  el('refresh').disabled = busy;
  el('dismiss').disabled = busy;
  for (const input of el('features').querySelectorAll('input:not([data-shipped])')) input.disabled = busy;
}
function renderFeatures() {
  const host = el('features');
  host.replaceChildren();
  for (const f of snapshot.catalog.features) {
    const shipped = snapshot.installed.includes(f.id);
    if (shipped) { selected.delete(f.id); tested.delete(f.id); }
    const row = document.createElement('div'); row.className = 'feature';
    const include = document.createElement('input'); include.type = 'checkbox'; include.checked = shipped || selected.has(f.id);
    include.setAttribute('aria-label', `Include ${f.title}`);
    const details = document.createElement('div');
    const title = document.createElement('strong'); title.textContent = f.title; details.append(title);
    const meta = document.createElement('small'); meta.textContent = shipped ? 'Already in main' : f.devTag;
    if (shipped) meta.className = 'shipped'; details.append(meta);
    if (f.requires.length && !shipped) {
      const dependencies = document.createElement('small');
      dependencies.textContent = 'Requires: ' + f.requires.map(id => snapshot.catalog.features.find(dep => dep.id === id).title).join(', ');
      details.append(dependencies);
    }
    const check = document.createElement('input'); check.type = 'checkbox'; check.checked = shipped || tested.has(f.id);
    check.setAttribute('aria-label', `Tested ${f.title}`);
    for (const input of [include, check]) if (shipped) { input.disabled = true; input.dataset.shipped = 'true'; }
    include.addEventListener('change', () => { include.checked ? selected.add(f.id) : selected.delete(f.id); controls(); });
    check.addEventListener('change', () => { check.checked ? tested.add(f.id) : tested.delete(f.id); controls(); });
    row.append(include, details, check); host.append(row);
  }
  host.setAttribute('aria-busy', 'false');
}
function renderStatus() {
  el('request').hidden = !status;
  if (status) {
    const run = status.run;
    const pending = !run || run.status !== 'completed';
    el('requestStatus').classList.toggle('busy', pending);
    el('requestId').textContent = status.request;
    el('requestStatus').textContent = !run ? 'Awaiting GitHub confirmation. Do not submit again.'
      : run.status !== 'completed' ? `${status.mode === 'publish' ? 'Publication' : 'Verification'}: ${run.status.replaceAll('_', ' ')}`
      : run.conclusion === 'success' ? status.mode === 'publish' ? 'Published. Main can download the update when you choose.' : 'Both platform checks passed. Nothing published.'
      : `Request ${run.conclusion || 'failed'}. Open GitHub to confirm release status. Your running apps were not changed.`;
  }
  controls();
}
async function refresh() {
  if (busy) return;
  busy = true; controls(); error('');
  try {
    const next = await api.load();
    // Test acknowledgements never survive a change to the exact reviewed source.
    if (snapshot && snapshot.source !== next.source) { selected.clear(); tested.clear(); }
    snapshot = next; status = next.status;
    el('stable').textContent = next.stable; el('dev').textContent = next.version;
    renderFeatures(); renderStatus();
  } catch (e) { snapshot = null; error(e.message); el('features').setAttribute('aria-busy', 'false'); }
  finally { busy = false; controls(); }
}
async function submit(mode) {
  if (busy || validation()) return;
  busy = true; controls(); error('');
  try {
    const result = await api.submit({ mode, source: snapshot.source, stable: snapshot.stable,
      selected: [...selected], tested: [...tested] });
    if (!result.cancelled) { status = result; renderStatus(); }
  } catch (e) { error(e.message); try { status = await api.status(); renderStatus(); } catch {} }
  finally { busy = false; controls(); }
}
el('refresh').addEventListener('click', refresh);
el('verify').addEventListener('click', () => submit('verify'));
el('publish').addEventListener('click', () => submit('publish'));
el('github').addEventListener('click', () => api.github().catch(e => error(e.message)));
el('dismiss').addEventListener('click', async () => {
  try { if (await api.dismiss()) { status = null; renderStatus(); } } catch (e) { error(e.message); }
});
setInterval(async () => {
  if (busy || polling || !status || status.run?.status === 'completed') return;
  polling = true;
  try { status = await api.status(); renderStatus(); } catch (e) { error(e.message); }
  finally { polling = false; }
}, 10000);
refresh();
