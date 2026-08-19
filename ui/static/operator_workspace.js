(() => {
  'use strict';

  const state = { commands: [], filtered: [], selected: 0, lastView: '' };

  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }

  function currentSession() {
    const sid = typeof currentSessionId === 'string' ? currentSessionId : '';
    if (!sid) return null;
    if (typeof _findClientSession === 'function') return _findClientSession(sid);
    return Array.isArray(window.sessions)
      ? window.sessions.find((item) => item && item.session_id === sid) || null
      : null;
  }

  function api(url, options) {
    return fetch(url, options).then(async (response) => {
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    });
  }

  function buildShell() {
    if (document.getElementById('operatorPalette')) return;
    const launch = node('button', 'operator-launch', '⌘');
    launch.id = 'operatorLaunch';
    launch.type = 'button';
    launch.title = 'Serena command palette (Ctrl/Cmd+K)';
    launch.setAttribute('aria-label', launch.title);
    launch.addEventListener('click', openPalette);

    const backdrop = node('div', 'operator-backdrop');
    backdrop.id = 'operatorPalette';
    backdrop.setAttribute('aria-hidden', 'true');
    backdrop.addEventListener('mousedown', (event) => {
      if (event.target === backdrop) closePalette();
    });

    const palette = node('section', 'operator-palette');
    palette.setAttribute('role', 'dialog');
    palette.setAttribute('aria-modal', 'true');
    palette.setAttribute('aria-label', 'Serena command palette');
    const input = node('input', 'operator-search');
    input.id = 'operatorSearch';
    input.type = 'search';
    input.placeholder = 'inspect, correct, queue, artifacts…';
    input.autocomplete = 'off';
    input.addEventListener('input', renderCommands);
    input.addEventListener('keydown', onPaletteKey);
    const results = node('div', 'operator-results');
    results.id = 'operatorResults';
    const view = node('div', 'operator-view');
    view.id = 'operatorView';
    palette.append(input, results, view);
    backdrop.appendChild(palette);
    document.body.append(launch, backdrop);
  }

  function commands() {
    return [
      { label: 'inspect current chat', hint: 'context, diff, focus, usage', run: inspectCurrent },
      { label: 'correct active Codex turn', hint: 'native safe steering only', run: correctCurrent },
      { label: 'queue next prompt', hint: 'durable, editable before dispatch', run: queueCurrent },
      { label: 'manage queued prompts', hint: 'edit, pause, resume, stash', run: showPrompts },
      { label: 'search artifact gallery', hint: 'chat and Fleet provenance', run: showArtifacts },
      { label: 'open Fleet', hint: 'durable run navigation', run: openFleet },
      { label: 'toggle focus mode', hint: 'renderer layout remains authoritative', run: toggleFocus },
    ];
  }

  function openPalette() {
    const backdrop = document.getElementById('operatorPalette');
    if (!backdrop) return;
    state.commands = commands();
    state.selected = 0;
    backdrop.classList.add('visible');
    backdrop.setAttribute('aria-hidden', 'false');
    const input = document.getElementById('operatorSearch');
    input.value = '';
    renderCommands();
    setTimeout(() => input.focus(), 0);
  }

  function closePalette() {
    const backdrop = document.getElementById('operatorPalette');
    if (!backdrop) return;
    backdrop.classList.remove('visible');
    backdrop.setAttribute('aria-hidden', 'true');
  }

  function onPaletteKey(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      closePalette();
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      state.selected = Math.max(0, Math.min(state.filtered.length - 1, state.selected + delta));
      renderCommands();
      return;
    }
    if (event.key === 'Enter' && state.filtered[state.selected]) {
      event.preventDefault();
      state.filtered[state.selected].run();
    }
  }

  function renderCommands() {
    const input = document.getElementById('operatorSearch');
    const results = document.getElementById('operatorResults');
    if (!input || !results) return;
    const query = input.value.trim().toLowerCase();
    state.filtered = state.commands.filter((command) =>
      `${command.label} ${command.hint}`.toLowerCase().includes(query));
    state.selected = Math.min(state.selected, Math.max(0, state.filtered.length - 1));
    results.replaceChildren();
    state.filtered.forEach((command, index) => {
      const row = node('button', `operator-command${index === state.selected ? ' selected' : ''}`);
      row.type = 'button';
      row.append(node('span', 'operator-command-label', command.label));
      row.append(node('span', 'operator-command-hint', command.hint));
      row.addEventListener('mouseenter', () => { state.selected = index; renderCommands(); });
      row.addEventListener('click', command.run);
      results.appendChild(row);
    });
  }

  function viewTitle(title, subtitle = '') {
    const view = document.getElementById('operatorView');
    view.replaceChildren();
    const head = node('div', 'operator-view-head');
    head.append(node('strong', '', title));
    if (subtitle) head.append(node('span', '', subtitle));
    const back = node('button', 'operator-back', 'commands');
    back.type = 'button';
    back.addEventListener('click', () => {
      view.replaceChildren();
      document.getElementById('operatorResults').hidden = false;
      document.getElementById('operatorSearch').focus();
    });
    head.appendChild(back);
    view.appendChild(head);
    document.getElementById('operatorResults').hidden = true;
    return view;
  }

  function inspectCurrent() {
    const session = currentSession();
    if (!session) return showError('open a chat first');
    const view = viewTitle('chat inspection', session.display_title || session.title || session.session_id);
    view.appendChild(node('div', 'operator-loading', 'reading native state…'));
    api(`/api/operator/sessions/${encodeURIComponent(session.session_id)}/inspect`)
      .then(({ inspection }) => renderInspection(view, inspection))
      .catch((error) => showError(error.message, view));
  }

  function renderInspection(view, inspection) {
    view.querySelector('.operator-loading')?.remove();
    const focus = inspection.focus || {};
    const usage = inspection.context_usage || {};
    const capabilities = inspection.capabilities || {};
    const rows = [
      ['focus', focus.focused ? 'focused here' : `focused: ${focus.focused_session_id || 'none'}`],
      ['split', (focus.split_pair || []).join(' + ') || 'single pane'],
      ['runtime', inspection.runtime ? `${inspection.runtime.agent} / ${inspection.runtime.state}` : 'sleeping or closed'],
      ['observed context', `${usage.observed_tokens || 0} tokens`],
      ['input', usage.input_tokens || 0],
      ['output', usage.output_tokens || 0],
      ['cache read', usage.cache_read_tokens || 0],
      ['cache create', usage.cache_create_tokens || 0],
      ['next turn', capabilities.next_turn?.reason || 'unavailable'],
      ['correction', capabilities.correction?.reason || 'unavailable'],
    ];
    const grid = node('div', 'operator-inspection-grid');
    rows.forEach(([label, value]) => {
      grid.append(node('div', 'operator-inspection-label', label));
      grid.append(node('div', 'operator-inspection-value', value));
    });
    view.appendChild(grid);
    if (usage.note) view.appendChild(node('p', 'operator-note', usage.note));
    const diffs = Array.isArray(inspection.diffs) ? inspection.diffs : [];
    const diffHead = node('h3', '', `diff evidence · ${diffs.length}`);
    view.appendChild(diffHead);
    if (!diffs.length) view.appendChild(node('p', 'operator-note', 'no durable Fleet integration diff for this chat'));
    diffs.forEach((diff) => {
      const card = node('article', 'operator-card');
      card.append(node('strong', '', diff.ok ? 'accepted integration' : 'refused integration'));
      card.append(node('p', '', diff.reason || 'no reason recorded'));
      (diff.changed_paths || []).forEach((path) => card.append(node('code', '', path)));
      view.appendChild(card);
    });
  }

  function queueCurrent() { queueForCurrent('next_turn'); }
  function correctCurrent() { queueForCurrent('correction', true); }

  function queueForCurrent(mode, dispatch = false) {
    const session = currentSession();
    if (!session) return showError('open a chat first');
    const text = window.prompt(mode === 'correction' ? 'correction for the active turn' : 'queue the next prompt');
    if (!text || !text.trim()) return;
    const provider = String(session.agent || '').toLowerCase();
    api('/api/operator/prompts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: session.session_id, provider, text, mode }),
    }).then(({ prompt }) => dispatch ? dispatchPrompt(prompt.prompt_id) : showPrompts())
      .catch((error) => showError(error.message));
  }

  function showPrompts() {
    const session = currentSession();
    const view = viewTitle('queued prompts', session ? session.session_id : 'all chats');
    const url = '/api/operator/prompts' + (session ? `?session_id=${encodeURIComponent(session.session_id)}` : '');
    view.appendChild(node('div', 'operator-loading', 'reading prompt journal…'));
    api(url).then(({ prompts }) => renderPrompts(view, prompts)).catch((error) => showError(error.message, view));
  }

  function renderPrompts(view, prompts) {
    view.querySelector('.operator-loading')?.remove();
    if (!prompts.length) return view.appendChild(node('p', 'operator-note', 'nothing queued for this chat'));
    prompts.forEach((prompt) => {
      const card = node('article', 'operator-card operator-prompt');
      card.append(node('strong', '', `${prompt.mode.replace('_', ' ')} · ${prompt.state}`));
      card.append(node('p', '', prompt.text));
      const controls = node('div', 'operator-card-actions');
      const actions = prompt.state === 'queued'
        ? ['dispatch', 'edit', 'pause', 'stash']
        : prompt.state === 'paused' || prompt.state === 'stashed'
          ? ['edit', 'resume', 'cancel'] : ['stash', 'cancel'];
      actions.forEach((action) => {
        const button = node('button', '', action);
        button.type = 'button';
        button.addEventListener('click', () => promptAction(prompt, action));
        controls.appendChild(button);
      });
      card.appendChild(controls);
      view.appendChild(card);
    });
  }

  function promptAction(prompt, action) {
    if (action === 'dispatch') return dispatchPrompt(prompt.prompt_id);
    if (action === 'edit') {
      const text = window.prompt('edit queued prompt', prompt.text);
      if (!text || text === prompt.text) return;
      return api(`/api/operator/prompts/${encodeURIComponent(prompt.prompt_id)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }),
      }).then(showPrompts).catch((error) => showError(error.message));
    }
    api(`/api/operator/prompts/${encodeURIComponent(prompt.prompt_id)}/${action}`, { method: 'POST' })
      .then(showPrompts).catch((error) => showError(error.message));
  }

  function dispatchPrompt(promptId) {
    api(`/api/operator/prompts/${encodeURIComponent(promptId)}/dispatch`, { method: 'POST' })
      .then(() => { closePalette(); if (typeof showToast === 'function') showToast('prompt dispatched'); })
      .catch((error) => showError(error.message));
  }

  function showArtifacts() {
    const query = window.prompt('search artifacts', '') || '';
    const view = viewTitle('artifact gallery', query || 'recent');
    const params = new URLSearchParams({ q: query });
    view.appendChild(node('div', 'operator-loading', 'searching verified artifacts…'));
    api(`/api/operator/artifacts?${params}`).then(({ artifacts }) => renderArtifacts(view, artifacts))
      .catch((error) => showError(error.message, view));
  }

  function renderArtifacts(view, artifacts) {
    view.querySelector('.operator-loading')?.remove();
    if (!artifacts.length) return view.appendChild(node('p', 'operator-note', 'no matching live artifacts'));
    artifacts.forEach((artifact) => {
      const card = node('article', 'operator-card');
      card.append(node('strong', '', artifact.name));
      card.append(node('p', '', `${artifact.content_type} · ${artifact.size} bytes`));
      const origin = artifact.origin_session_id
        || [artifact.fleet_run_id, artifact.fleet_worker_key].filter(Boolean).join(' / ')
        || `job ${artifact.job_id}`;
      card.append(node('small', '', origin));
      const controls = node('div', 'operator-card-actions');
      const open = node('button', '', 'open artifact');
      open.addEventListener('click', () => window.open(artifact.url, '_blank', 'noopener'));
      controls.appendChild(open);
      if (artifact.origin_session_id) {
        const chat = node('button', '', 'open chat');
        chat.addEventListener('click', () => {
          closePalette();
          if (typeof openConv === 'function') openConv(artifact.origin_session_id);
        });
        controls.appendChild(chat);
      }
      if (artifact.fleet_run_id) {
        const fleet = node('button', '', 'open Fleet run');
        fleet.addEventListener('click', () => openFleetRun(artifact.fleet_run_id));
        controls.appendChild(fleet);
      }
      card.appendChild(controls);
      view.appendChild(card);
    });
  }

  function openFleetRun(runId) {
    closePalette();
    if (typeof switchTab === 'function') switchTab('fleet');
    const frame = document.getElementById('fleetFrame');
    if (!frame) return;
    const selectRun = () => {
      const fleetWindow = frame.contentWindow;
      if (!fleetWindow || typeof fleetWindow.selectRun !== 'function') return false;
      fleetWindow.selectRun(runId);
      return true;
    };
    if (!selectRun()) frame.addEventListener('load', selectRun, { once: true });
  }

  function openFleet() {
    closePalette();
    if (typeof switchTab === 'function') switchTab('fleet');
  }

  function toggleFocus() {
    closePalette();
    if (typeof toggleFocusMode === 'function') toggleFocusMode();
  }

  function showError(message, existingView) {
    const view = existingView || viewTitle('operator request refused');
    view.querySelector('.operator-loading')?.remove();
    const error = node('p', 'operator-error', message);
    view.appendChild(error);
  }

  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      event.stopPropagation();
      openPalette();
    }
  }, true);

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', buildShell);
  else buildShell();
  window.SerenaOperator = { open: openPalette, inspect: inspectCurrent };
})();
