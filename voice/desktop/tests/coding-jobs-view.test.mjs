import assert from 'node:assert/strict';
import test from 'node:test';

import {
  codingJobLabel,
  installCodingJobsView,
  orderCodingJobs,
} from '../renderer/coding-jobs.mjs';

class FakeClassList {
  constructor(owner) {
    this.owner = owner;
    this.values = new Set();
  }

  toggle(value, enabled) {
    if (enabled) this.values.add(value);
    else this.values.delete(value);
  }
}

class FakeElement {
  constructor(tagName = 'div') {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.className = '';
    this.classList = new FakeClassList(this);
    this.textContent = '';
  }

  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = [...children]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  insertBefore(child, reference) {
    const index = this.children.indexOf(reference);
    if (index < 0) this.children.push(child);
    else this.children.splice(index, 0, child);
  }
  querySelector(selector) {
    const className = selector.startsWith('.') ? selector.slice(1) : '';
    for (const child of this.children) {
      if (String(child.className || '').split(/\s+/).includes(className)) return child;
      const nested = child.querySelector?.(selector);
      if (nested) return nested;
    }
    return null;
  }
}

test('two concurrent coding jobs remain visible in the ordered collection', () => {
  const jobs = orderCodingJobs([
    { item_id: 'weather', state: 'working', created_at: 10 },
    { item_id: 'coding-ui', state: 'working', created_at: 20 },
  ]);

  assert.deepEqual(jobs.map((job) => job.item_id), ['coding-ui', 'weather']);
});

test('active jobs stay ahead of newer completed history', () => {
  const jobs = orderCodingJobs([
    { item_id: 'done', state: 'completed', created_at: 30 },
    { item_id: 'queued', state: 'queued', created_at: 10 },
  ]);

  assert.deepEqual(jobs.map((job) => job.item_id), ['queued', 'done']);
  assert.equal(codingJobLabel({ brief: { request: 'show both jobs' } }), 'show both jobs');
});

test('the installed panel keeps and renders two concurrent jobs', async () => {
  const root = new FakeElement();
  const details = new FakeElement();
  details.className = 'code-panel__details';
  root.append(details);
  const rendered = [];
  const renderedEvents = [];
  let outputClears = 0;
  const panel = {
    el: root,
    _itemId: '',
    _snapshot: null,
    _reopenButton: new FakeElement('button'),
    renderSnapshot(snapshot) { this._itemId = snapshot.item_id; rendered.push(snapshot.item_id); },
    clear() { this._itemId = ''; },
    clearOutput() { outputClears += 1; renderedEvents.length = 0; },
    addEvent(event) { renderedEvents.push(event.item_id); },
    show() { this.visible = true; },
    hide() { this.visible = false; },
  };
  globalThis.document = { createElement: (tagName) => new FakeElement(tagName) };
  globalThis.window = {
    _serenaCodePanel: panel,
    serenaCodingJobs: {
      list: async () => ({
        ok: true,
        jobs: [
          { item_id: 'weather', state: 'working', created_at: 10, brief: { request: 'weather' } },
          { item_id: 'coding-ui', state: 'working', created_at: 20, brief: { request: 'coding ui' } },
        ],
      }),
    },
    setInterval: () => 1,
    clearInterval() {},
    addEventListener() {},
  };

  assert.equal(await installCodingJobsView(), true);
  assert.equal(window._serenaCodingJobsView.jobs.size, 2);
  assert.equal(window._serenaCodingJobsView.selectedId, 'coding-ui');
  assert.deepEqual(rendered, ['coding-ui']);
  assert.equal(root.querySelector('.code-panel__jobs-list').children.length, 2);
  assert.equal(panel.visible, true);

  panel.addEvent({ item_id: 'weather', kind: 'text', summary: 'weather work' });
  panel.addEvent({ item_id: 'coding-ui', kind: 'text', summary: 'ui work' });
  assert.deepEqual(renderedEvents, ['coding-ui']);
  window._serenaCodingJobsView.select('weather', { show: false });
  assert.deepEqual(renderedEvents, ['weather']);
  assert.equal(outputClears >= 2, true);

  delete globalThis.window;
  delete globalThis.document;
});
