/* The desktop shell owns updates; this surface only presents its results. */
(() => {
  'use strict';
  if (document.getElementById('desktopAbout')) return;
  const desktop = window.serenaDesktop;
  if (!desktop?.updates?.onOpen) return;
  const api = desktop.updates;
  const style = document.createElement('style');
  style.textContent = `
    #desktopAbout { color:#e6edf3; background:#151015; border:1px solid #684054;
      border-radius:8px; width:min(600px,calc(100vw - 32px)); max-height:calc(100dvh - 32px);
      margin:auto; padding:0; overflow:auto; box-shadow:0 24px 90px #0009;
      font:13px/1.6 var(--mono,ui-monospace,monospace); letter-spacing:0; }
    #desktopAbout::backdrop { background:#0009; }
    #desktopAbout * { box-sizing:border-box; }
    #desktopAbout header { display:flex; align-items:center; justify-content:space-between;
      padding:14px 22px; border-bottom:1px solid #393140; color:#d18cb0; }
    #desktopAbout button { font:inherit; color:#d7c7d0; background:transparent;
      border:1px solid #4b3946; border-radius:4px; padding:9px 13px; cursor:pointer; }
    #desktopAbout button:hover { background:#e07ba812; border-color:#e07ba8; }
    #desktopAbout button:focus-visible, #desktopAbout a:focus-visible { outline:2px solid #e07ba8; outline-offset:3px; }
    #desktopAbout button:disabled { opacity:.5; cursor:default; }
    #desktopAbout .about-close { width:32px; height:32px; padding:0; border:0; font-size:23px; }
    #desktopAbout .about-brand { display:flex; align-items:center; gap:18px; padding:26px 26px 22px; }
    #desktopAbout .about-brand img { width:60px; height:60px; object-fit:contain; }
    #desktopAbout h1 { font-size:27px; line-height:1.3; font-weight:600; margin:0 0 4px; color:#f0bed7; }
    #desktopAbout p { margin:0; overflow-wrap:anywhere; }
    #desktopAbout .about-muted { color:#b29faa; }
    #desktopAbout .about-update { border-top:1px solid #393140; border-bottom:1px solid #393140;
      padding:22px 26px; background:#100c10; }
    #desktopAbout .about-status { display:flex; align-items:center; gap:10px; margin-bottom:8px; }
    #desktopAbout .about-dot { width:8px; height:8px; border-radius:50%; background:#e07ba8; flex:none; }
    #desktopAbout[data-state=current] .about-dot, #desktopAbout[data-state=downloaded] .about-dot { background:#78cf98; }
    #desktopAbout[data-state=error] .about-dot { background:#ff7b88; }
    #desktopAbout h2 { font-size:15px; font-weight:600; margin:0; }
    #desktopAbout progress { width:100%; height:7px; display:block; margin-top:18px;
      border:0; border-radius:3px; overflow:hidden; accent-color:#e07ba8; }
    #desktopAbout progress::-webkit-progress-bar { background:#393140; }
    #desktopAbout progress::-webkit-progress-value { background:#e07ba8; }
    #desktopAbout [hidden] { display:none !important; }
    #desktopAbout .about-actions { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:20px; }
    #desktopAbout .about-primary { color:#ffd4e8; border-color:#b9658e; background:#e07ba815; margin-left:auto; }
    #desktopAbout .about-runtime { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px;
      margin:0; padding:22px 26px; color:#b29faa; }
    #desktopAbout dt { font-size:11px; margin-bottom:3px; }
    #desktopAbout dd { margin:0; color:#d7c7d0; font-size:12px; overflow-wrap:anywhere; }
    #desktopAbout footer { padding:0 26px 22px; font-size:11px; color:#b29faa; }
    @media(max-width:420px) { #desktopAbout .about-runtime { grid-template-columns:1fr; gap:8px; }
      #desktopAbout .about-actions button { width:100%; margin:0; } }
  `;
  document.head.appendChild(style);
  const dialog = document.createElement('dialog');
  dialog.id = 'desktopAbout';
  dialog.setAttribute('aria-labelledby', 'desktopAboutTitle');
  dialog.innerHTML = `
    <header><span>ABOUT / DESKTOP</span><button class="about-close" aria-label="Close About" title="Close">&times;</button></header>
    <section class="about-brand"><img src="/static/icons/serena-icon.png" alt="" width="60" height="60">
      <div><h1 id="desktopAboutTitle">Serena</h1><p class="about-muted" data-version>Loading version...</p></div></section>
    <section class="about-update">
      <div class="about-status"><span class="about-dot" aria-hidden="true"></span><h2 data-title>Updates</h2></div>
      <p class="about-muted" data-detail role="status" aria-live="polite"></p>
      <progress max="100" value="0" aria-label="Update download" hidden></progress>
      <div class="about-actions"><button data-notes>Release notes &#8599;</button>
        <button data-cancel hidden>Not now</button><button class="about-primary" data-primary disabled>Check for updates</button></div>
    </section>
    <dl class="about-runtime"><div><dt>ELECTRON</dt><dd data-electron>-</dd></div>
      <div><dt>CHROMIUM</dt><dd data-chrome>-</dd></div><div><dt>NODE</dt><dd data-node>-</dd></div></dl>
    <footer>Updates download only when requested. A downloaded update installs on the next app exit.</footer>`;
  document.body.appendChild(dialog);
  const el = name => dialog.querySelector(`[data-${name}]`);
  const progress = dialog.querySelector('progress');
  let facts = {}, state = 'idle', reason = '', remote = '', percent = 0, busy = false;
  function render() {
    dialog.dataset.state = state;
    el('version').textContent = `${facts.version || '-'} / ${facts.platform || 'Desktop'}${facts.packaged === false ? ' / development' : ''}`;
    for (const key of ['electron', 'chrome', 'node']) el(key).textContent = facts[key] || '-';
    const copy = {
      idle: ['Desktop updates', 'Check for the latest Serena release.', 'Check for updates'],
      checking: ['Checking for updates', 'Contacting the release feed...', 'Checking...'],
      available: ['Update available', `Serena ${remote} is available. You are running ${facts.version}.`, 'Download update'],
      downloading: ['Downloading update', `${percent}% downloaded.`, 'Downloading...'],
      downloaded: ['Ready to install', `Serena ${remote || facts.downloadedVersion || ''} is downloaded.`, 'Install update'],
      confirm: ['Restart Serena?', 'Restarting closes this window and its open panes. Save any unfinished input first.', 'Restart and install'],
      installing: ['Restarting Serena', 'Installing the downloaded update...', 'Restarting...'],
      current: ['You are up to date', `Serena ${facts.version} is the latest available release.`, 'Check again'],
      'none-published': ['No release available', 'No published update was found for this build.', 'Check again'],
      unsupported: ['Updates unavailable', reason || facts.blocker || '', 'Unavailable'],
      error: ['Update failed', reason || 'The update could not be completed.', 'Try again'],
    }[state];
    el('title').textContent = copy[0]; el('detail').textContent = copy[1];
    el('primary').textContent = copy[2];
    el('primary').disabled = busy || state === 'unsupported' || state === 'installing';
    el('cancel').hidden = state !== 'confirm';
    progress.hidden = state !== 'downloading'; progress.value = percent;
  }
  async function run(operation) {
    if (busy) return;
    busy = true; reason = '';
    state = { check:'checking', download:'downloading', install:'installing' }[operation];
    if (operation === 'download') percent = 0;
    render();
    try {
      const result = await api[operation]();
      if (operation === 'check') {
        facts = { ...facts, ...result }; remote = result.remoteVersion || remote;
        state = result.state; reason = result.reason || '';
      } else if (operation === 'download') {
        remote = result.version || remote; state = 'downloaded';
      }
    } catch (error) { state = 'error'; reason = String(error.message || error); }
    finally { busy = false; render(); }
  }
  el('primary').addEventListener('click', () => {
    if (state === 'available') run('download');
    else if (state === 'downloaded') { state = 'confirm'; render(); }
    else if (state === 'confirm') run('install');
    else run('check');
  });
  el('cancel').addEventListener('click', () => { state = 'downloaded'; render(); });
  el('notes').addEventListener('click', () => {
    desktop.openExternal('https://github.com/duaragha/Serena/releases').catch(error => {
      reason = String(error.message || error); state = 'error'; render();
    });
  });
  dialog.querySelector('.about-close').addEventListener('click', () => dialog.close());
  // Keep app-wide chat shortcuts out of the modal, while retaining native
  // dialog focus trapping, Escape, and button activation.
  dialog.addEventListener('keydown', event => event.stopPropagation());
  api.onProgress(value => {
    if (state !== 'downloading') return;
    percent = Math.min(100, Math.max(0, Number(value.percent) || 0)); render();
  });
  api.onOpen(async ({ action } = {}) => {
    if (!dialog.open) dialog.showModal();
    try {
      facts = await api.describe();
      if (!busy && state !== 'installing') {
        if (facts.downloadedVersion) { remote = facts.downloadedVersion; state = 'downloaded'; }
        else if (facts.blocker) { state = 'unsupported'; reason = facts.blocker; }
      }
      render();
      if (action === 'check' && !busy && state !== 'downloaded' && state !== 'unsupported') run('check');
    } catch (error) { state = 'error'; reason = String(error.message || error); render(); }
  });
})();
