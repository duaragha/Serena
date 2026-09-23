// Serena's coding sessions, visible in the window he is looking at.
//
// She opens her own terminals to write code for him, and he had no way to
// see them: the pane ran inside this app's backend while nothing on screen
// said so, so "it's underway" was all the proof he got. This polls her
// sessions, announces each one as it starts and finishes with a Watch
// button, and keeps a chip in the corner while any of them is working.
(function installSerenaWork(root) {
  'use strict';

  const POLL_MS = 4000;
  const HIDDEN_POLL_MS = 20000;
  // Sessions opened this recently are announced even if the window opened
  // after she started them; older ones are listed but not announced.
  const FRESH_MINUTES = 10;
  const LIVE_STATES = new Set(['starting', 'working', 'waiting']);
  const ENDED_STATES = new Set(['done', 'blocked']);

  function live(sessions) {
    return (sessions || []).filter(s => LIVE_STATES.has(s.state));
  }

  function chipLabel(sessions) {
    const count = live(sessions).length;
    return count ? 'serena coding · ' + count : '';
  }

  // What changed since the last poll: sessions that started (and are fresh)
  // and sessions that ended. `known` maps id -> last state seen.
  function changes(sessions, known) {
    const started = [];
    const ended = [];
    for (const s of sessions || []) {
      const before = known.get(s.id);
      if (before === undefined) {
        if (LIVE_STATES.has(s.state) && Number(s.opened_minutes_ago || 0) <= FRESH_MINUTES) {
          started.push(s);
        }
      } else if (LIVE_STATES.has(before) && ENDED_STATES.has(s.state)) {
        ended.push(s);
      }
    }
    return { started, ended };
  }

  // Dev opens chats in structured panes, which cannot attach to her
  // terminal; the transcript is what it can show. Main shows the terminal.
  function watchMode(structured) {
    return structured ? 'read' : 'live';
  }

  const api = Object.freeze({
    POLL_MS, FRESH_MINUTES, live, chipLabel, changes, watchMode,
  });
  root.SerenaWork = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof document === 'undefined') return;

  // The page keeps its state in top-level `let`s, which are shared with this
  // script by name but are not properties of window.
  function page(name) {
    try {
      switch (name) {
        case 'currentSessionId': return typeof currentSessionId === 'undefined' ? null : currentSessionId;
        case 'convMode': return typeof convMode === 'undefined' ? null : convMode;
        case 'currentTab': return typeof currentTab === 'undefined' ? null : currentTab;
        default: return null;
      }
    } catch (error) {
      return null;
    }
  }

  const known = new Map();
  let sessions = [];
  let chip = null;
  let menu = null;
  let followTimer = null;

  function style() {
    const css = document.createElement('style');
    css.textContent = [
      // Clear of the status bar and the corner button that already lives there.
      '#serenaWorkChip{position:fixed;right:60px;bottom:30px;z-index:9998;display:none;',
      'align-items:center;gap:7px;padding:5px 11px;border-radius:999px;cursor:pointer;',
      'font:11px var(--mono,monospace);letter-spacing:.3px;color:var(--text-bright);',
      'background:var(--surface2);border:1px solid var(--accent);',
      'box-shadow:0 4px 14px rgba(0,0,0,.4)}',
      '#serenaWorkChip.visible{display:flex}',
      '#serenaWorkChip .dot{width:7px;height:7px;border-radius:50%;background:var(--accent);',
      'animation:serenaWorkPulse 1.4s ease-in-out infinite}',
      '@keyframes serenaWorkPulse{0%,100%{opacity:.35}50%{opacity:1}}',
      '#serenaWorkMenu{position:fixed;right:60px;bottom:62px;z-index:9999;min-width:280px;',
      'max-width:380px;padding:6px;border-radius:8px;background:var(--surface2);',
      'border:1px solid var(--border-bright);box-shadow:0 8px 24px rgba(0,0,0,.45)}',
      '#serenaWorkMenu .row{display:flex;flex-direction:column;gap:2px;padding:7px 9px;',
      'border-radius:6px;cursor:pointer}',
      '#serenaWorkMenu .row:hover{background:var(--accent-dim)}',
      '#serenaWorkMenu .title{font-size:12px;color:var(--text-bright)}',
      '#serenaWorkMenu .meta{font:10px var(--mono,monospace);color:var(--text-dim)}',
      '.toast .serena-work-watch{margin-left:auto;padding:3px 10px;border-radius:4px;',
      'border:1px solid var(--accent);background:transparent;color:var(--accent);cursor:pointer;',
      'font:11px var(--mono,monospace)}',
      '.toast .serena-work-close{padding:0 4px;border:0;background:transparent;',
      'color:var(--text-dim);cursor:pointer;font-size:14px}',
    ].join('');
    document.head.appendChild(css);
  }

  function ensureChip() {
    if (chip) return chip;
    chip = document.createElement('div');
    chip.id = 'serenaWorkChip';
    chip.title = 'Serena is writing code. Click to watch.';
    chip.innerHTML = '<span class="dot"></span><span class="label"></span>';
    chip.addEventListener('click', event => {
      event.stopPropagation();
      toggleMenu();
    });
    document.body.appendChild(chip);
    document.addEventListener('click', () => closeMenu());
    return chip;
  }

  function closeMenu() {
    if (menu) menu.remove();
    menu = null;
  }

  function toggleMenu() {
    if (menu) return closeMenu();
    const listed = live(sessions).concat(sessions.filter(s => !LIVE_STATES.has(s.state))).slice(0, 6);
    if (!listed.length) return;
    menu = document.createElement('div');
    menu.id = 'serenaWorkMenu';
    for (const s of listed) {
      const row = document.createElement('div');
      row.className = 'row';
      const title = document.createElement('div');
      title.className = 'title';
      title.textContent = s.title;
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = s.project + ' · ' + s.state
        + (s.outcome ? ' · ' + s.outcome.slice(0, 60) : '');
      row.append(title, meta);
      row.addEventListener('click', event => {
        event.stopPropagation();
        closeMenu();
        watch(s);
      });
      menu.appendChild(row);
    }
    document.body.appendChild(menu);
  }

  function render() {
    const el = ensureChip();
    const label = chipLabel(sessions);
    el.querySelector('.label').textContent = label;
    el.classList.toggle('visible', Boolean(label));
  }

  function followRead(sid) {
    // Keep the transcript moving while she works, even where the app does
    // not know her pane is running.
    if (followTimer) clearInterval(followTimer);
    followTimer = setInterval(() => {
      if (page('currentSessionId') !== sid || page('convMode') !== 'read') {
        clearInterval(followTimer);
        followTimer = null;
        return;
      }
      if (typeof root.loadReadTranscript === 'function') root.loadReadTranscript(sid, true);
    }, 3000);
  }

  async function watch(s, attempt) {
    const tries = attempt || 0;
    if (!s.session_id || !s.indexed) {
      if (tries === 0 && typeof root.showToast === 'function') {
        root.showToast('her session is still starting, opening it in a moment');
      }
      if (tries < 8) setTimeout(() => refresh().then(() => {
        const fresh = sessions.find(item => item.id === s.id) || s;
        watch(fresh, tries + 1);
      }), 2500);
      return;
    }
    if (typeof root.switchTab === 'function' && page('currentTab') !== 'chats') root.switchTab('chats');
    const structured = Boolean(root.SERENA && root.SERENA.structuredWorkspace);
    if (typeof root.openConv !== 'function') return;
    if (watchMode(structured) === 'read') {
      await root.openConv(s.session_id, { mode: 'read' });
      followRead(s.session_id);
    } else {
      await root.openConv(s.session_id);
    }
  }

  function announce(s, message) {
    if (typeof root.showToast !== 'function') return;
    const toast = root.showToast(message, { sticky: true });
    const button = document.createElement('button');
    button.className = 'serena-work-watch';
    button.textContent = 'Watch';
    button.addEventListener('click', () => {
      toast.dismiss();
      watch(s);
    });
    const close = document.createElement('button');
    close.className = 'serena-work-close';
    close.textContent = '×';
    close.title = 'Dismiss';
    close.addEventListener('click', () => toast.dismiss());
    toast.el.append(button, close);
  }

  async function refresh() {
    try {
      const response = await fetch('/api/serena-coding', { cache: 'no-store' });
      if (!response.ok) return;
      const data = await response.json();
      sessions = data.sessions || [];
    } catch (error) {
      return;
    }
    const { started, ended } = changes(sessions, known);
    for (const s of sessions) known.set(s.id, s.state);
    for (const s of started) announce(s, 'serena started coding: ' + s.title);
    for (const s of ended) {
      announce(s, (s.state === 'done' ? 'serena finished: ' : 'serena is stuck: ') + s.title);
    }
    render();
  }

  function loop() {
    refresh().finally(() => {
      setTimeout(loop, document.visibilityState === 'hidden' ? HIDDEN_POLL_MS : POLL_MS);
    });
  }

  function start() {
    style();
    ensureChip();
    loop();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})(typeof window !== 'undefined' ? window : globalThis);
