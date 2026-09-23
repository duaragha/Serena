// The in-app browser's pane: a tab in the Code pane with a toolbar, and a
// slot the desktop app draws the page into.
//
// The page itself is a native view in the main process (apps/desktop/
// app-browser.js), drawn over this document. It cannot know when the slot is
// covered by another view, so this reports the slot's rectangle -- and
// whether it is on screen at all -- whenever that changes. Only in the
// desktop app; a plain browser gets no Browser tab.
(function installSerenaAppBrowser(root) {
  'use strict';

  const POLL_MS = 250;

  // Pure helpers, exported for tests.
  function slotReport(rect, displayed, pageVisible) {
    const visible = Boolean(displayed && pageVisible && rect && rect.width > 8 && rect.height > 8);
    if (!visible) return { visible: false };
    return {
      visible: true,
      x: Math.round(rect.left),
      y: Math.round(rect.top),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
    };
  }

  function sameReport(a, b) {
    if (!a || !b) return false;
    return a.visible === b.visible && a.x === b.x && a.y === b.y
      && a.width === b.width && a.height === b.height;
  }

  function tabLabel(tab) {
    const title = String((tab && tab.title) || '').trim();
    if (title) return title.length > 28 ? title.slice(0, 27) + '…' : title;
    try {
      const url = new URL(tab.url);
      return url.host || url.pathname.split('/').pop() || tab.url;
    } catch (_) {
      return String((tab && tab.url) || 'New tab');
    }
  }

  const pure = { slotReport, sameReport, tabLabel };
  if (typeof module !== 'undefined' && module.exports) module.exports = pure;
  if (typeof document === 'undefined') return;

  const desktop = root.serenaDesktop && root.serenaDesktop.browser;
  if (!desktop) return;

  let state = { tabs: [], activeId: null };
  let pane = null;
  let slot = null;
  let urlInput = null;
  let chips = null;
  let active = false;   // the Browser code tab is the selected one
  let listed = false;   // the Browser code tab is in the tab strip
  let standalone = null; // the chat that was open when it took the center column
  let typing = false;    // he is editing the address; leave it alone until he is done
  let shownTab = null;   // which tab the address bar last showed
  let last = null;

  function css() {
    const style = document.createElement('style');
    style.textContent = [
      '.browser-pane{display:flex;flex-direction:column;background:var(--bg)}',
      '.browser-pane.hidden{display:none}',
      '.bp-bar{display:flex;align-items:center;gap:6px;padding:6px 8px;border-bottom:1px solid var(--border)}',
      '.bp-btn{border:1px solid var(--border);background:transparent;color:var(--text);border-radius:4px;',
      'padding:2px 8px;cursor:pointer;font:12px var(--mono,monospace)}',
      '.bp-btn:hover{border-color:var(--accent);color:var(--accent)}',
      '.bp-url{flex:1;min-width:120px;background:var(--surface);color:var(--text-bright);',
      'border:1px solid var(--border);border-radius:4px;padding:4px 8px;font:12px var(--mono,monospace)}',
      '.bp-url:focus{outline:none;border-color:var(--accent)}',
      '.bp-tabs{display:flex;gap:4px;padding:4px 8px 0;overflow-x:auto}',
      '.bp-chip{display:flex;align-items:center;gap:6px;padding:3px 8px;border-radius:5px 5px 0 0;',
      'border:1px solid var(--border);border-bottom:none;cursor:pointer;font-size:11px;color:var(--text);',
      'white-space:nowrap;max-width:220px}',
      '.bp-chip.active{background:var(--surface);color:var(--text-bright);border-color:var(--accent)}',
      '.bp-chip.unloaded{opacity:.55}',
      '.bp-chip .x{color:var(--text-dim)}',
      '.bp-slot{flex:1;position:relative;background:#fff}',
      '.browser-pane.bp-standalone{position:absolute;inset:0;z-index:40;display:flex}',
      '.browser-pane.bp-standalone.hidden{display:none}',
      '.bp-dismiss{display:none}',
      '.bp-standalone .bp-dismiss{display:inline-block}',
      '.bp-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;',
      'color:var(--text-dim);background:var(--bg);font-size:12px}',
    ].join('');
    document.head.appendChild(style);
  }

  function command(name, args) {
    return desktop.command(name, args).catch((error) => {
      if (typeof root.showToast === 'function') root.showToast('browser: ' + error.message, { variant: 'error' });
      return null;
    });
  }

  // A chat's Code pane when one is open; otherwise the whole center column,
  // which is where an agent's page has to show up when he is on the front door.
  function dock(where) {
    build();
    if (!pane) return;
    const target = where === 'standalone' ? document.getElementById('convPanel')
      : document.getElementById('codePaneWrap');
    if (!target) return;
    if (where === 'standalone' && getComputedStyle(target).position === 'static') target.style.position = 'relative';
    if (pane.parentElement !== target) target.appendChild(pane);
    pane.classList.toggle('bp-standalone', where === 'standalone');
  }

  function chatCodeViewOpen() {
    const content = document.getElementById('convContent');
    return Boolean(page('currentSessionId') && content && !content.classList.contains('hidden'));
  }

  function page(name) {
    try {
      switch (name) {
        case 'currentSessionId': return typeof currentSessionId === 'undefined' ? null : currentSessionId; // eslint-disable-line no-undef
        case 'convMode': return typeof convMode === 'undefined' ? 'live' : convMode; // eslint-disable-line no-undef
        default: return null;
      }
    } catch (_) {
      return null;
    }
  }

  function build() {
    if (pane) return pane;
    const wrap = document.getElementById('codePaneWrap');
    if (!wrap) return null;
    pane = document.createElement('div');
    pane.className = 'code-pane browser-pane hidden';
    pane.innerHTML = [
      '<div class="bp-tabs"></div>',
      '<div class="bp-bar">',
      '<button class="bp-btn" data-act="back" title="Back">←</button>',
      '<button class="bp-btn" data-act="forward" title="Forward">→</button>',
      '<button class="bp-btn" data-act="reload" title="Reload">⟳</button>',
      '<input class="bp-url" spellcheck="false" placeholder="localhost:5173, a site, or a file path">',
      '<button class="bp-btn" data-act="new" title="New tab">+</button>',
      '<button class="bp-btn" data-act="external" title="Open in your browser">↗</button>',
      '<button class="bp-btn bp-dismiss" data-act="dismiss" title="Close the browser (tabs stay)">✕</button>',
      '</div>',
      '<div class="bp-slot"><div class="bp-empty">Nothing open. Type an address above.</div></div>',
    ].join('');
    wrap.appendChild(pane);
    slot = pane.querySelector('.bp-slot');
    urlInput = pane.querySelector('.bp-url');
    chips = pane.querySelector('.bp-tabs');
    urlInput.addEventListener('input', () => { typing = true; });
    urlInput.addEventListener('blur', () => { typing = false; });
    urlInput.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { typing = false; renderState(); return; }
      if (event.key !== 'Enter') return;
      typing = false;
      const value = urlInput.value.trim();
      if (value) command('open', { url: value });
    });
    pane.querySelector('.bp-bar').addEventListener('click', (event) => {
      const button = event.target.closest('[data-act]');
      if (!button) return;
      const act = button.dataset.act;
      if (act === 'dismiss') {
        hide();
      } else if (act === 'new') {
        urlInput.value = '';
        urlInput.focus();
        command('open', { url: 'about:blank', newTab: true });
      } else if (act === 'external') {
        const tab = state.tabs.find((item) => item.active);
        if (tab && /^https?:/.test(tab.url) && root.serenaDesktop.openExternal) root.serenaDesktop.openExternal(tab.url);
      } else if (state.activeId) {
        command(act, {});
      }
    });
    chips.addEventListener('click', (event) => {
      const close = event.target.closest('[data-close]');
      if (close) { command('close', { tab: close.dataset.close }); return; }
      const chip = event.target.closest('[data-tab]');
      if (chip) command('select', { tab: chip.dataset.tab });
    });
    return pane;
  }

  function renderState() {
    if (!pane) return;
    const escape = (value) => String(value).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    chips.innerHTML = state.tabs.map((tab) => '<div class="bp-chip' + (tab.active ? ' active' : '')
      + (tab.unloaded ? ' unloaded' : '') + '" data-tab="' + escape(tab.id) + '" title="' + escape(tab.url) + '">'
      + (tab.loading ? '… ' : '') + escape(tabLabel(tab))
      + '<span class="x" data-close="' + escape(tab.id) + '">✕</span></div>').join('');
    const current = state.tabs.find((tab) => tab.active);
    if (current && current.id !== shownTab) typing = false; // another tab: its address wins
    if (current && !typing) urlInput.value = current.url === 'about:blank' ? '' : current.url;
    shownTab = current ? current.id : null;
    pane.querySelector('.bp-empty').style.display = current ? 'none' : 'flex';
  }

  function report() {
    // He opened a chat while the browser had the center column: step aside.
    if (standalone !== null && page('currentSessionId') !== standalone.sid) closeStandalone();
    let next = { visible: false };
    if (pane && (active || standalone !== null) && slot && state.tabs.length) {
      const displayed = pane.offsetParent !== null && !pane.classList.contains('hidden');
      next = slotReport(slot.getBoundingClientRect(), displayed, document.visibilityState === 'visible');
    }
    if (!sameReport(next, last)) {
      last = next;
      desktop.setBounds(next);
    }
  }

  // The Code pane asks these.
  function tabHtml(isActive) {
    if (!listed && !isActive) {
      return '<div class="code-tab" data-tabid="__browser__" title="Open the in-app browser">'
        + '<span>\u25ce</span></div>';
    }
    return '<div class="code-tab' + (isActive ? ' active' : '') + '" data-tabid="__browser__" title="In-app browser">'
      + '<span>◎ Browser</span><span class="ct-close" data-close="__browser__" title="Hide">✕</span></div>';
  }

  function setActive(on) {
    active = Boolean(on);
    if (active && !listed) {
      listed = true;
      if (typeof root.renderCodeTabs === 'function') root.renderCodeTabs();
    }
    if (active) {
      standalone = null;
      dock('docked');
    }
    if (pane && standalone === null) pane.classList.toggle('hidden', !active);
    report();
  }

  function closeStandalone() {
    standalone = null;
    if (pane) {
      pane.classList.add('hidden');
      pane.classList.remove('bp-standalone');
    }
  }

  function show() {
    listed = true;
    if (chatCodeViewOpen()) {
      closeStandalone();
      if (page('convMode') !== 'live' && typeof root.setConvMode === 'function') root.setConvMode('live');
      if (typeof root.renderCodeTabs === 'function') root.renderCodeTabs();
      if (typeof root.switchCodeTab === 'function') root.switchCodeTab('__browser__');
      else setActive(true);
      return;
    }
    standalone = { sid: page('currentSessionId') };
    dock('standalone');
    if (pane) pane.classList.remove('hidden');
    report();
  }

  function hide() {
    if (standalone !== null) {
      closeStandalone();
      report();
      return;
    }
    listed = false;
    setActive(false);
    if (typeof root.switchCodeTab === 'function') root.switchCodeTab('__term__');
  }

  async function open(url) {
    show();
    await command('open', { url, newTab: state.tabs.length > 0 });
  }

  desktop.onState((next) => {
    state = next || { tabs: [], activeId: null };
    if (state.tabs.length && !listed) {
      listed = true;
      if (typeof root.renderCodeTabs === 'function') root.renderCodeTabs();
    }
    renderState();
    report();
  });
  // An agent opened something: put it on screen so he sees what it sees.
  desktop.onReveal(() => show());

  css();
  setInterval(report, POLL_MS);
  root.addEventListener('resize', report);
  document.addEventListener('visibilitychange', report);
  command('tabs', {}).then((initial) => {
    if (!initial) return;
    state = initial;
    listed = state.tabs.length > 0;
    if (listed && typeof root.renderCodeTabs === 'function') root.renderCodeTabs();
    renderState();
  });

  root.SerenaAppBrowser = Object.freeze({ tabHtml, setActive, show, hide, open, ...pure });
})(typeof window !== 'undefined' ? window : globalThis);
