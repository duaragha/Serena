// Sorting: anything not finished belongs at the top of the list.
const ACTIVE_STATES = new Set(['queued', 'claimed', 'delivered', 'resume_queued', 'working']);
// Opening the drawer: work actually in flight, nothing else. Every job in the
// store carries 'delivered', so treating that as reason enough put the drawer
// back on his screen every fifteen seconds with nothing running.
const RUNNING_STATES = new Set(['working', 'resume_queued']);
const REFRESH_INTERVAL_MS = 15000;
const MAX_BUFFERED_EVENTS_PER_JOB = 200;
const FALLBACK_CODING_MODELS = [{ value: 'auto', label: 'auto' }];

export function orderCodingJobs(jobs) {
  return [...jobs].sort((left, right) => {
    const activeDifference = Number(!ACTIVE_STATES.has(left.state))
      - Number(!ACTIVE_STATES.has(right.state));
    if (activeDifference) return activeDifference;
    const timeDifference = Number(right.created_at || 0) - Number(left.created_at || 0);
    return timeDifference || String(left.item_id || '').localeCompare(String(right.item_id || ''));
  });
}

export function codingJobLabel(snapshot) {
  const request = String(snapshot?.brief?.request || snapshot?.brief?.trigger || '').trim();
  return request || `${snapshot?.project || 'project'} coding job`;
}

function normaliseSnapshot(snapshot, previous = {}) {
  if (!snapshot || typeof snapshot !== 'object' || !snapshot.item_id) return null;
  return {
    ...previous,
    ...snapshot,
    created_at: Number(snapshot.created_at || previous.created_at || Date.now() / 1000),
    brief: { ...(previous.brief || {}), ...(snapshot.brief || {}) },
    progress: { ...(previous.progress || {}), ...(snapshot.progress || {}) },
    controls: { ...(previous.controls || {}), ...(snapshot.controls || {}) },
  };
}

export async function installCodingJobsView() {
  const panel = window._serenaCodePanel;
  const api = window.serenaCodingJobs;
  if (!panel || panel.__codingJobsInstalled || !api?.list) return false;
  panel.__codingJobsInstalled = true;

  const jobs = new Map();
  const eventBuffers = new Map();
  let selectedId = String(panel._itemId || '');
  let shownRunningId = '';

  const section = document.createElement('section');
  section.className = 'code-panel__jobs';
  section.setAttribute('aria-label', 'Coding jobs');

  const heading = document.createElement('div');
  heading.className = 'code-panel__jobs-heading';
  const title = document.createElement('span');
  title.textContent = 'coding jobs';
  const count = document.createElement('span');
  count.className = 'code-panel__jobs-count';
  heading.append(title, count);

  const modelControl = document.createElement('label');
  modelControl.className = 'code-panel__model-picker';
  const modelLabel = document.createElement('span');
  modelLabel.textContent = 'future jobs';
  const modelPicker = document.createElement('select');
  modelPicker.className = 'code-panel__model-select';
  modelPicker.setAttribute('aria-label', 'Model for future coding jobs');
  function installModelOptions(options) {
    const valid = Array.isArray(options)
      ? options.filter((item) => item && typeof item.value === 'string' && typeof item.label === 'string')
      : [];
    const selected = modelPicker.value;
    modelPicker.replaceChildren();
    for (const { value, label } of valid.length ? valid : FALLBACK_CODING_MODELS) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      modelPicker.appendChild(option);
    }
    modelPicker.value = valid.some((item) => item.value === selected) ? selected : 'auto';
  }
  for (const { value, label } of FALLBACK_CODING_MODELS) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    modelPicker.appendChild(option);
  }
  modelPicker.value = 'auto';
  modelControl.append(modelLabel, modelPicker);

  const list = document.createElement('div');
  list.className = 'code-panel__jobs-list';
  const terminalAction = document.createElement('div');
  terminalAction.className = 'code-panel__terminal-action';
  terminalAction.hidden = true;
  const terminalButton = document.createElement('button');
  terminalButton.type = 'button';
  terminalButton.className = 'code-panel__terminal-button';
  terminalButton.textContent = 'open live terminal';
  const terminalStatus = document.createElement('span');
  terminalStatus.className = 'code-panel__terminal-status';
  terminalStatus.setAttribute('role', 'status');
  terminalStatus.setAttribute('aria-live', 'polite');
  terminalAction.append(terminalButton, terminalStatus);
  section.append(heading, modelControl, list, terminalAction);
  const details = panel.el.querySelector('.code-panel__details');
  panel.el.insertBefore(section, details || panel.el.firstChild);

  const originalRenderSnapshot = panel.renderSnapshot.bind(panel);
  const originalClear = panel.clear.bind(panel);
  const originalAddEvent = typeof panel.addEvent === 'function'
    ? panel.addEvent.bind(panel)
    : null;

  function renderEvents(itemId) {
    if (!originalAddEvent || typeof panel.clearOutput !== 'function') return;
    panel.clearOutput();
    for (const event of eventBuffers.get(String(itemId)) || []) {
      originalAddEvent(event);
    }
  }

  function selectJob(itemId, { show = true } = {}) {
    const snapshot = jobs.get(String(itemId));
    if (!snapshot) return;
    selectedId = snapshot.item_id;
    originalRenderSnapshot(snapshot);
    renderEvents(selectedId);
    renderTerminalAction(snapshot);
    renderList();
    if (show) panel.show();
  }

  function renderTerminalAction(snapshot) {
    const supported = Boolean(snapshot?.terminal && api.openTerminal);
    const available = Boolean(supported && snapshot.terminal.can_open);
    terminalAction.hidden = !supported;
    terminalButton.dataset.itemId = available ? String(snapshot.item_id) : '';
    terminalButton.disabled = !available;
    terminalStatus.textContent = available
      ? ''
      : String(snapshot?.terminal?.reason || 'interactive terminal is unavailable');
    terminalStatus.classList.remove('code-panel__terminal-status--error');
  }

  terminalButton.addEventListener('click', async () => {
    const itemId = String(terminalButton.dataset.itemId || '');
    if (!itemId || !api.openTerminal) return;
    terminalButton.disabled = true;
    terminalStatus.textContent = 'attaching exact session...';
    terminalStatus.classList.remove('code-panel__terminal-status--error');
    let opened = false;
    try {
      const result = await api.openTerminal(itemId);
      if (!result?.ok) throw new Error(result?.error || 'live terminal could not be opened');
      opened = true;
      terminalButton.dataset.itemId = '';
      terminalStatus.textContent = `opened session ${String(result.session_id || '').slice(0, 8)}`;
    } catch (error) {
      terminalStatus.textContent = String(error?.message || error || 'live terminal could not be opened');
      terminalStatus.classList.add('code-panel__terminal-status--error');
    } finally {
      terminalButton.disabled = opened;
    }
  });

  function renderList() {
    const ordered = orderCodingJobs(jobs.values());
    count.textContent = `${ordered.length} job${ordered.length === 1 ? '' : 's'}`;
    list.replaceChildren();
    for (const snapshot of ordered) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'code-panel__job';
      button.classList.toggle('code-panel__job--selected', snapshot.item_id === selectedId);
      button.setAttribute('aria-pressed', String(snapshot.item_id === selectedId));
      button.dataset.itemId = snapshot.item_id;

      const state = document.createElement('span');
      state.className = `code-panel__job-state code-panel__job-state--${snapshot.state || 'queued'}`;
      state.textContent = String(snapshot.state || 'queued').replace('_', ' ');
      const label = document.createElement('span');
      label.className = 'code-panel__job-label';
      label.textContent = codingJobLabel(snapshot);
      button.append(state, label);
      button.addEventListener('click', () => selectJob(snapshot.item_id));
      list.appendChild(button);
    }
  }

  function upsert(snapshot) {
    const itemId = String(snapshot?.item_id || '');
    if (!itemId) return null;
    const merged = normaliseSnapshot(snapshot, jobs.get(itemId));
    jobs.set(itemId, merged);
    return merged;
  }

  panel.renderSnapshot = (snapshot) => {
    const merged = upsert(snapshot);
    if (!merged) return;
    if (!selectedId || selectedId === merged.item_id) {
      selectedId = merged.item_id;
      originalRenderSnapshot(merged);
      renderTerminalAction(merged);
    }
    renderList();
  };

  panel.clear = () => {
    originalClear();
    selectedId = '';
    renderTerminalAction(null);
    renderList();
  };

  if (originalAddEvent) {
    panel.addEvent = (event) => {
      const itemId = String(event?.item_id || selectedId || panel._itemId || '');
      if (!itemId) return;
      const buffered = eventBuffers.get(itemId) || [];
      buffered.push(event);
      if (buffered.length > MAX_BUFFERED_EVENTS_PER_JOB) {
        buffered.splice(0, buffered.length - MAX_BUFFERED_EVENTS_PER_JOB);
      }
      eventBuffers.set(itemId, buffered);
      if (itemId === selectedId) originalAddEvent(event);
    };
  }

  panel._reopenButton?.addEventListener('click', () => panel.show(), { capture: true });

  async function loadModelPreference() {
    if (!api.getModel) return;
    try {
      const result = await api.getModel();
      if (result?.ok) {
        installModelOptions(result.options);
      }
      if (result?.ok && [...modelPicker.children].some((option) => option.value === result.model)) {
        modelPicker.value = result.model;
      }
    } catch (error) {
      console.error('[coding-jobs] model preference read failed:', error);
    }
  }

  modelPicker.addEventListener('change', async () => {
    if (!api.setModel) return;
    const requested = modelPicker.value;
    modelPicker.disabled = true;
    try {
      const result = await api.setModel(requested);
      if (!result?.ok) throw new Error(result?.error || 'model preference was not saved');
      if (result.options) installModelOptions(result.options);
      modelPicker.value = result.model;
    } catch (error) {
      console.error('[coding-jobs] model preference write failed:', error);
      await loadModelPreference();
    } finally {
      modelPicker.disabled = false;
    }
  });

  async function refresh() {
    let result;
    try {
      result = await api.list();
    } catch (error) {
      console.error('[coding-jobs] list failed:', error);
      return;
    }
    if (!result?.ok || !Array.isArray(result.jobs)) {
      console.error('[coding-jobs] list failed:', result?.error || 'invalid response');
      return;
    }
    for (const snapshot of result.jobs) upsert(snapshot);
    const ordered = orderCodingJobs(jobs.values());
    if (!selectedId && ordered.length) selectedId = ordered[0].item_id;
    if (selectedId && jobs.has(selectedId)) selectJob(selectedId, { show: false });
    renderList();
    // A job that has just started running may open the drawer. Polling is for
    // keeping the list fresh, not for reopening a drawer he closed, so a job
    // already shown never opens it a second time.
    const running = ordered.find((job) => RUNNING_STATES.has(job.state));
    if (running && running.item_id !== shownRunningId) {
      shownRunningId = running.item_id;
      // The main process owns the window and has to widen it by the drawer's
      // width first, so ask it rather than opening a column inside a window
      // that is still the narrow size. Opening here directly is the fallback
      // for a renderer with no main process behind it.
      if (window.serena?.showCodePanel) window.serena.showCodePanel();
      else panel.show();
    } else if (!running && shownRunningId) {
      // Only forget the job once it is genuinely finished. Clearing on any
      // list that happens to omit it would re-arm auto-open and reopen a
      // drawer he closed.
      const shown = jobs.get(shownRunningId);
      if (shown && !RUNNING_STATES.has(shown.state)) shownRunningId = '';
    }
    if (!running && ordered.length) panel._available = true;
  }

  if (panel._snapshot) upsert(panel._snapshot);
  await loadModelPreference();
  await refresh();
  const refreshTimer = window.setInterval(refresh, REFRESH_INTERVAL_MS);
  window.addEventListener('beforeunload', () => window.clearInterval(refreshTimer), { once: true });

  window._serenaCodingJobsView = {
    jobs,
    eventBuffers,
    refresh,
    select: selectJob,
    modelPicker,
    terminalAction,
    terminalButton,
    terminalStatus,
    get selectedId() { return selectedId; },
  };
  return true;
}

if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  installCodingJobsView();
}
