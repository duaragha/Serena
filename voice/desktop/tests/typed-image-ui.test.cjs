const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const imageHelpers = require('../typed-images.js');

const PNG_DATA =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=';
const PNG_URL = `data:image/png;base64,${PNG_DATA}`;

class ClassList {
  constructor(...names) {
    this.names = new Set(names);
  }

  add(...names) {
    names.forEach((name) => this.names.add(name));
  }

  remove(...names) {
    names.forEach((name) => this.names.delete(name));
  }

  contains(name) {
    return this.names.has(name);
  }

  toggle(name, force) {
    const enabled = force === undefined ? !this.contains(name) : force;
    if (enabled) this.add(name);
    else this.remove(name);
    return enabled;
  }
}

class Element {
  constructor(id = '') {
    this.id = id;
    this.classList = new ClassList();
    this.style = {};
    this.dataset = {};
    this.textContent = '';
    this.value = id === 'speed-range' ? '1' : '';
    this.placeholder = '';
    this.src = '';
    this.listeners = new Map();
    this.children = [];
    this.scrollHeight = 0;
    this.scrollTop = 0;
  }

  addEventListener(kind, callback) {
    const listeners = this.listeners.get(kind) || [];
    listeners.push(callback);
    this.listeners.set(kind, listeners);
  }

  dispatch(kind, event = {}) {
    for (const callback of this.listeners.get(kind) || []) callback(event);
  }

  append(...children) {
    this.children.push(...children);
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  replaceChildren(...children) {
    this.children = children;
  }

  focus() {}
}

class ImmediateFileReader {
  readAsDataURL(file) {
    this.result = file.dataUrl;
    this.onload?.();
  }
}

function loadRenderer({ helpers = imageHelpers } = {}) {
  const callbacks = new Map();
  const elements = new Map();
  const sent = [];
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, new Element(id));
    return elements.get(id);
  };
  for (const id of ['transcription', 'response', 'typed-attachment', 'type-error']) {
    element(id).classList.add('hidden');
  }
  const on = (kind) => (callback) => {
    const listeners = callbacks.get(kind) || [];
    listeners.push(callback);
    callbacks.set(kind, listeners);
  };
  const serena = {
    onStateChange: on('state'),
    onAmplitude: on('amplitude'),
    onTranscription: on('transcription'),
    onResponse: on('response'),
    onDashboardData: on('dashboard'),
    onFocusMode: on('focus'),
    onToggleDashboard: on('toggle-dashboard'),
    onCodeStart: on('code-start'),
    onCodeEvent: on('code-event'),
    onCodeDone: on('code-done'),
    onCodeSnapshot: on('code-snapshot'),
    onCodeControlResult: on('code-control-result'),
    onToggleCodePanel: on('toggle-code-panel'),
    onShowCodePanel: on('show-code-panel'),
    onTypedInputAccepted: on('typed-accepted'),
    onTypedInputError: on('typed-error'),
    sendTyped(payload) {
      sent.push(payload);
    },
    setIgnoreMouse() {},
    setAmplitude() {},
    setState() {},
  };
  const document = {
    activeElement: null,
    addEventListener() {},
    createElement: () => new Element(),
    getElementById: element,
  };
  const window = {
    matchMedia: () => ({ matches: false }),
    serena,
  };
  if (helpers) window.SerenaTypedImages = helpers;
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8'),
    { clearTimeout, console, document, FileReader: ImmediateFileReader, setTimeout, window },
    { filename: 'app.js' },
  );
  return {
    element,
    sent,
    emit(kind, value) {
      for (const callback of callbacks.get(kind) || []) callback(value);
    },
  };
}

function pasteEvent(items) {
  const event = {
    clipboardData: { items },
    prevented: false,
    stopped: false,
    preventDefault() { this.prevented = true; },
    stopPropagation() { this.stopped = true; },
  };
  return event;
}

function pngClipboardItem() {
  const file = {
    type: 'image/png',
    size: Buffer.from(PNG_DATA, 'base64').length,
    dataUrl: PNG_URL,
  };
  return { kind: 'file', type: file.type, getAsFile: () => file };
}

test('text paste falls through while image paste previews and can be removed', () => {
  const renderer = loadRenderer();
  const input = renderer.element('type-input');
  const attachment = renderer.element('typed-attachment');
  const preview = renderer.element('typed-attachment-preview');

  const textPaste = pasteEvent([{ kind: 'string', type: 'text/plain' }]);
  input.dispatch('paste', textPaste);
  assert.equal(textPaste.prevented, false);
  assert.equal(textPaste.stopped, false);

  const imagePaste = pasteEvent([pngClipboardItem()]);
  input.dispatch('paste', imagePaste);
  assert.equal(imagePaste.prevented, true);
  assert.equal(imagePaste.stopped, true);
  assert.equal(attachment.classList.contains('hidden'), false);
  assert.equal(preview.src, PNG_URL);

  renderer.element('typed-attachment-remove').dispatch('click');
  assert.equal(attachment.classList.contains('hidden'), true);
  assert.equal(preview.src, '');
});

test('unsupported image paste shows a readable error without losing the draft', () => {
  const renderer = loadRenderer();
  const input = renderer.element('type-input');
  input.value = 'keep this text';
  const file = { type: 'image/bmp', size: 20, dataUrl: 'data:image/bmp;base64,AAAA' };
  const event = pasteEvent([
    { kind: 'file', type: file.type, getAsFile: () => file },
  ]);

  input.dispatch('paste', event);

  assert.equal(event.prevented, true);
  assert.equal(input.value, 'keep this text');
  assert.equal(renderer.element('typed-attachment').classList.contains('hidden'), true);
  assert.match(renderer.element('type-error').textContent, /not supported/);
  assert.equal(renderer.element('type-error').classList.contains('hidden'), false);
});

test('a rejected send keeps both text and the pasted image for retry', () => {
  const renderer = loadRenderer();
  const input = renderer.element('type-input');
  const attachment = renderer.element('typed-attachment');
  input.dispatch('paste', pasteEvent([pngClipboardItem()]));
  input.value = 'what is this?';

  input.dispatch('keydown', {
    key: 'Enter',
    shiftKey: false,
    stopPropagation() {},
  });
  assert.equal(renderer.sent.length, 1);
  assert.equal(input.value, 'what is this?');
  assert.equal(attachment.classList.contains('hidden'), false);

  renderer.emit('typed-error', 'serena is reconnecting. your message is still here.');
  assert.equal(input.value, 'what is this?');
  assert.equal(attachment.classList.contains('hidden'), false);
  assert.equal(renderer.element('type-bar').classList.contains('busy'), false);
  assert.match(renderer.element('type-error').textContent, /still here/);
});

test('an accepted send clears only the submitted draft', () => {
  const renderer = loadRenderer();
  const input = renderer.element('type-input');
  input.dispatch('paste', pasteEvent([pngClipboardItem()]));
  input.value = 'describe it';
  input.dispatch('keydown', {
    key: 'Enter',
    shiftKey: false,
    stopPropagation() {},
  });

  renderer.emit('typed-accepted');
  assert.equal(input.value, '');
  assert.equal(renderer.element('typed-attachment').classList.contains('hidden'), true);
  assert.equal(renderer.element('typed-attachment-preview').src, '');
});

test('missing image helpers degrades to a working text-only type bar', () => {
  const renderer = loadRenderer({ helpers: null });
  const input = renderer.element('type-input');
  input.value = 'plain text still works';
  input.dispatch('keydown', {
    key: 'Enter',
    shiftKey: false,
    stopPropagation() {},
  });

  assert.equal(renderer.sent.length, 1);
  assert.equal(renderer.sent[0].text, 'plain text still works');
  assert.equal(renderer.sent[0].image, null);
  renderer.emit('typed-accepted');
  assert.equal(input.value, '');
});
