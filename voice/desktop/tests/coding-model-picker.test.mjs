import assert from 'node:assert/strict';
import test from 'node:test';

import { installCodingJobsView } from '../renderer/coding-jobs.mjs';

class FakeElement {
  constructor(tagName = 'div') {
    this.tagName = tagName;
    this.children = [];
    this.className = '';
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.value = '';
    this.classList = { toggle() {}, add() {}, remove() {} };
  }

  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = [...children]; }
  insertBefore(child, reference) {
    const index = this.children.indexOf(reference);
    if (index < 0) this.children.push(child);
    else this.children.splice(index, 0, child);
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelector(selector) {
    const className = selector.startsWith('.') ? selector.slice(1) : '';
    if (String(this.className).split(/\s+/).includes(className)) return this;
    for (const child of this.children) {
      const match = child.querySelector?.(selector);
      if (match) return match;
    }
    return null;
  }
}

test('future-job picker loads and persists the selected model', async () => {
  const root = new FakeElement();
  const details = new FakeElement();
  details.className = 'code-panel__details';
  root.append(details);
  const saved = [];
  globalThis.document = { createElement: (tagName) => new FakeElement(tagName) };
  globalThis.window = {
    _serenaCodePanel: {
      el: root,
      _itemId: '',
      _snapshot: null,
      _reopenButton: new FakeElement('button'),
      renderSnapshot() {},
      clear() {},
      show() {},
    },
    serenaCodingJobs: {
      list: async () => ({ ok: true, jobs: [] }),
      getModel: async () => ({
        ok: true,
        model: 'claude-opus-5',
        options: [
          { value: 'auto', label: 'auto' },
          { value: 'gpt-5.6-terra', label: 'terra 5.6' },
          { value: 'gpt-5.6-sol', label: 'sol 5.6' },
          { value: 'claude-sonnet-5', label: 'sonnet 5' },
          { value: 'claude-opus-5', label: 'opus 5' },
        ],
      }),
      setModel: async (model) => { saved.push(model); return { ok: true, model }; },
    },
    setInterval: () => 1,
    clearInterval() {},
    addEventListener() {},
  };

  try {
    assert.equal(await installCodingJobsView(), true);
    const picker = window._serenaCodingJobsView.modelPicker;
    assert.equal(picker.attributes['aria-label'], 'Model for future coding jobs');
    assert.equal(picker.value, 'claude-opus-5');
    assert.equal(picker.children.length, 5);
    picker.value = 'gpt-5.6-sol';
    await picker.listeners.change();
    assert.deepEqual(saved, ['gpt-5.6-sol']);
    assert.equal(picker.value, 'gpt-5.6-sol');
  } finally {
    delete globalThis.window;
    delete globalThis.document;
  }
});
