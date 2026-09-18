import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(
  new URL('../renderer/code-panel.js', import.meta.url),
  'utf8',
);
const {
  CodePanel,
  MAX_CODE_PANEL_WIDTH,
  MIN_CODE_PANEL_WIDTH,
} = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
  toggle(value, enabled) {
    if (enabled) this.add(value);
    else this.remove(value);
  }
}

class FakeElement {
  constructor() {
    this.attributes = {};
    this.children = [];
    this.classList = new FakeClassList();
    this.listeners = {};
    this.style = { values: {}, setProperty: (name, value) => { this.style.values[name] = value; } };
    this.capturedPointer = null;
  }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  setPointerCapture(pointerId) { this.capturedPointer = pointerId; }
  hasPointerCapture(pointerId) { return this.capturedPointer === pointerId; }
  releasePointerCapture(pointerId) {
    if (this.capturedPointer === pointerId) this.capturedPointer = null;
  }
}

function installPanel() {
  const requested = [];
  globalThis.document = { createElement: () => new FakeElement() };
  globalThis.window = {
    serena: {
      hideCodePanel() {},
      showCodePanel() {},
      setIgnoreMouse() {},
      sendCodeControl() {},
      setCodePanelWidth: (width) => requested.push(width),
    },
  };
  const panel = new CodePanel(new FakeElement());
  return { panel, requested };
}

test('coding pane exposes an accessible bounded keyboard resize separator', () => {
  const { panel, requested } = installPanel();
  const handle = panel._resizeHandle;
  const key = (value) => handle.listeners.keydown({
    key: value,
    shiftKey: false,
    preventDefault() {},
  });

  assert.equal(handle.attributes.role, 'separator');
  assert.equal(handle.attributes['aria-label'], 'Resize coding pane');
  assert.equal(handle.attributes['aria-orientation'], 'vertical');
  assert.equal(handle.attributes['aria-valuemin'], String(MIN_CODE_PANEL_WIDTH));
  assert.equal(handle.attributes['aria-valuemax'], String(MAX_CODE_PANEL_WIDTH));

  key('End');
  key('Home');
  key('ArrowRight');
  assert.deepEqual(requested, [720, 300, 324]);
  assert.equal(handle.attributes['aria-valuenow'], '324');
  assert.equal(panel.el.style.values['--code-panel-width'], '324px');

  delete globalThis.window;
  delete globalThis.document;
});

test('dragging the coding pane separator resizes from its current width', () => {
  const { panel, requested } = installPanel();
  const handle = panel._resizeHandle;
  panel.setWidth(420);
  handle.listeners.pointerdown({
    button: 0,
    clientX: 100,
    pointerId: 7,
    preventDefault() {},
  });
  handle.listeners.pointermove({ clientX: 180, pointerId: 7 });
  handle.listeners.pointerup({ pointerId: 7 });

  assert.deepEqual(requested, [500]);
  assert.equal(handle.capturedPointer, null);
  assert.equal(handle.attributes['aria-valuenow'], '500');

  delete globalThis.window;
  delete globalThis.document;
});

test('the plain overlay never renders another jobs event in the selected pane', () => {
  const { panel } = installPanel();
  globalThis.requestAnimationFrame = (callback) => callback();
  panel._itemId = 'selected-job';

  assert.equal(panel.addEvent({ item_id: 'other-job', kind: 'text', summary: 'wrong' }), false);
  assert.equal(panel._output.children.length, 0);
  assert.equal(panel.addEvent({ item_id: 'selected-job', kind: 'text', summary: 'right' }), true);
  assert.equal(panel._output.children.length, 1);

  delete globalThis.requestAnimationFrame;
  delete globalThis.window;
  delete globalThis.document;
});
