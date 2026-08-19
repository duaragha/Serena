// The jobs list refreshes on a timer. Refreshing is for keeping the list
// current; it is not a reason to put the drawer back on Raghav's screen.

import assert from 'node:assert/strict';
import test from 'node:test';

import { installCodingJobsView } from '../renderer/coding-jobs.mjs';

class FakeElement {
  constructor(tagName = 'div') {
    this.tagName = tagName;
    this.children = [];
    this.className = '';
    this.dataset = {};
    this.textContent = '';
    this.attributes = {};
    this.hidden = false;
    this.disabled = false;
    this.listeners = new Map();
    this.classList = {
      _names: new Set(),
      add: (...names) => names.forEach((name) => this.classList._names.add(name)),
      remove: (...names) => names.forEach((name) => this.classList._names.delete(name)),
      toggle: (name, on) => (on
        ? this.classList._names.add(name)
        : this.classList._names.delete(name)),
      contains: (name) => this.classList._names.has(name),
    };
  }

  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); return node; }
  insertBefore(node) { this.children.unshift(node); return node; }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  async click() { return this.listeners.get('click')?.({ target: this }); }

  querySelector(selector) {
    const name = selector.replace('.', '');
    if (this.className === name) return this;
    for (const child of this.children) {
      const found = child.querySelector?.(selector);
      if (found) return found;
    }
    return null;
  }
}

function install(jobs, { serena = null, openTerminal = null } = {}) {
  const root = new FakeElement();
  const details = new FakeElement();
  details.className = 'code-panel__details';
  root.append(details);
  const panel = {
    el: root,
    _itemId: '',
    _snapshot: null,
    _available: false,
    _reopenButton: new FakeElement('button'),
    visible: false,
    renderSnapshot(snapshot) { this._itemId = snapshot.item_id; },
    clear() { this._itemId = ''; },
    show() { this.visible = true; },
    hide() { this.visible = false; },
  };
  let listed = jobs;
  const intervals = [];
  globalThis.document = { createElement: (tagName) => new FakeElement(tagName) };
  globalThis.window = {
    _serenaCodePanel: panel,
    serenaCodingJobs: {
      list: async () => ({ ok: true, jobs: listed }),
      ...(openTerminal ? { openTerminal } : {}),
    },
    setInterval: (fn) => intervals.push(fn),
    clearInterval() {},
    addEventListener() {},
  };
  if (serena) globalThis.window.serena = serena;
  return {
    panel,
    install: () => installCodingJobsView(),
    setJobs: (next) => { listed = next; },
    refresh: () => globalThis.window._serenaCodingJobsView.refresh(),
    teardown: () => {
      delete globalThis.window;
      delete globalThis.document;
    },
  };
}

const job = (state, itemId = 'job-1') => ({
  item_id: itemId,
  state,
  created_at: 10,
  brief: { request: 'a coding job' },
});

const launchableJob = () => ({
  ...job('completed', 'b62f3779-9bc6-4d19-b1d9-6384fc4743e9'),
  terminal: {
    can_open: true,
    session_id: '019fc3b5-2acb-7492-8d76-21f3007f8bdb',
    provider: 'codex',
  },
});

test('history alone never opens the drawer', async () => {
  const harness = install([job('delivered'), job('completed', 'job-2'), job('failed', 'job-3')]);
  try {
    assert.equal(await harness.install(), true);
    assert.equal(harness.panel.visible, false);
    // The list is still worth offering.
    assert.equal(harness.panel._available, true);
  } finally {
    harness.teardown();
  }
});

test('an accepted job that has not started yet does not open it either', async () => {
  const harness = install([job('queued'), job('claimed', 'job-2')]);
  try {
    await harness.install();
    assert.equal(harness.panel.visible, false);
  } finally {
    harness.teardown();
  }
});

test('a job that is running opens it once, not on every refresh', async () => {
  const harness = install([job('working')]);
  try {
    await harness.install();
    assert.equal(harness.panel.visible, true);

    // He closes it. The next few polls must leave it closed.
    harness.panel.hide();
    await harness.refresh();
    await harness.refresh();
    assert.equal(harness.panel.visible, false);
  } finally {
    harness.teardown();
  }
});

test('with a main process behind it, the window is asked first', async () => {
  // Opening the column here directly would leave a 450px drawer inside a
  // window that is still 500px wide, squeezing the app to nothing.
  const asked = [];
  const harness = install([job('working')], {
    serena: { showCodePanel: () => asked.push('showCodePanel') },
  });
  try {
    await harness.install();
    assert.deepEqual(asked, ['showCodePanel']);
    assert.equal(harness.panel.visible, false);
  } finally {
    harness.teardown();
  }
});

test('a list that merely omits the running job does not re-arm auto-open', async () => {
  const harness = install([job('working')]);
  try {
    await harness.install();
    harness.panel.hide();
    // A read that omits it entirely, then it is back and still running.
    harness.setJobs([job('completed', 'other')]);
    await harness.refresh();
    harness.setJobs([job('working')]);
    await harness.refresh();
    assert.equal(harness.panel.visible, false);
  } finally {
    harness.teardown();
  }
});

test('the drawer re-arms once the job it showed has finished', async () => {
  const harness = install([job('working')]);
  try {
    await harness.install();
    harness.panel.hide();
    harness.setJobs([job('completed')]);
    await harness.refresh();
    harness.setJobs([job('completed'), job('working', 'job-2')]);
    await harness.refresh();
    assert.equal(harness.panel.visible, true);
  } finally {
    harness.teardown();
  }
});

test('a different job starting later still opens it', async () => {
  const harness = install([job('working')]);
  try {
    await harness.install();
    harness.panel.hide();
    harness.setJobs([job('completed'), job('working', 'job-2')]);
    await harness.refresh();
    assert.equal(harness.panel.visible, true);
  } finally {
    harness.teardown();
  }
});

test('open live terminal appears only for a safely resolved selected job', async () => {
  const opened = [];
  const harness = install([launchableJob()], {
    openTerminal: async (itemId) => {
      opened.push(itemId);
      return { ok: true, session_id: '019fc3b5-2acb-7492-8d76-21f3007f8bdb' };
    },
  });
  try {
    await harness.install();
    const view = globalThis.window._serenaCodingJobsView;
    assert.equal(view.terminalAction.hidden, false);
    await view.terminalButton.click();
    assert.deepEqual(opened, ['b62f3779-9bc6-4d19-b1d9-6384fc4743e9']);
    assert.equal(view.terminalStatus.textContent, 'opened session 019fc3b5');
  } finally {
    harness.teardown();
  }
});

test('unavailable metadata is explained and click failures stay visible', async () => {
  const safe = launchableJob();
  const harness = install([{
    ...safe,
    terminal: { can_open: false, reason: 'background coding session is still active' },
  }], {
    openTerminal: async () => ({ ok: false, error: 'persisted session transcript is stale' }),
  });
  try {
    await harness.install();
    const view = globalThis.window._serenaCodingJobsView;
    assert.equal(view.terminalAction.hidden, false);
    assert.equal(view.terminalButton.disabled, true);
    assert.equal(view.terminalStatus.textContent, 'background coding session is still active');

    harness.setJobs([safe]);
    await harness.refresh();
    assert.equal(view.terminalAction.hidden, false);
    assert.equal(view.terminalButton.disabled, false);
    await view.terminalButton.click();
    assert.equal(view.terminalStatus.textContent, 'persisted session transcript is stale');
    assert.equal(
      view.terminalStatus.classList.contains('code-panel__terminal-status--error'),
      true,
    );
  } finally {
    harness.teardown();
  }
});
