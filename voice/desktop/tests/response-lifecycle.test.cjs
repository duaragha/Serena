const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

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
    this.value = '1';
    this.title = '';
    this.attributes = new Map();
    this.listeners = new Map();
    this.children = [];
    this.scrollHeight = 0;
    this.scrollTop = 0;
  }

  addEventListener(kind, callback) {
    this.listeners.set(kind, callback);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
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

function makeClock() {
  let now = 0;
  let nextId = 1;
  const timers = new Map();

  return {
    setTimeout(callback, delay = 0) {
      const id = nextId++;
      timers.set(id, { callback, due: now + Number(delay) });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    advance(milliseconds) {
      const target = now + milliseconds;
      while (true) {
        const ready = [...timers.entries()]
          .filter(([, timer]) => timer.due <= target)
          .sort((left, right) => left[1].due - right[1].due)[0];
        if (!ready) break;
        const [id, timer] = ready;
        timers.delete(id);
        now = timer.due;
        timer.callback();
      }
      now = target;
    },
    pending() {
      return timers.size;
    },
  };
}

function loadRenderer() {
  const callbacks = new Map();
  const elements = new Map();
  const clock = makeClock();
  const voiceMuteChanges = [];
  const microphoneMuteChanges = [];
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, new Element(id));
    return elements.get(id);
  };
  element('transcription').classList.add('hidden');
  element('response').classList.add('hidden');

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
    onVoiceMuted: on('voice-muted'),
    onMicrophoneMuted: on('microphone-muted'),
    onCodeStart: on('code-start'),
    onCodeEvent: on('code-event'),
    onCodeDone: on('code-done'),
    onCodeSnapshot: on('code-snapshot'),
    onCodeControlResult: on('code-control-result'),
    onToggleCodePanel: on('toggle-code-panel'),
    onShowCodePanel: on('show-code-panel'),
    setIgnoreMouse() {},
    setAmplitude() {},
    setState() {},
    setVoiceMuted(value) { voiceMuteChanges.push(Boolean(value)); },
    setMicrophoneMuted(value) { microphoneMuteChanges.push(Boolean(value)); },
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
  const context = {
    clearTimeout: clock.clearTimeout,
    console,
    document,
    setTimeout: clock.setTimeout,
    window,
  };
  const source = fs.readFileSync(
    path.join(__dirname, '..', 'renderer', 'app.js'),
    'utf8',
  );
  vm.runInNewContext(source, context, { filename: 'app.js' });

  return {
    clock,
    element,
    voiceMuteChanges,
    microphoneMuteChanges,
    emit(kind, value) {
      for (const callback of callbacks.get(kind) || []) callback(value);
    },
  };
}

test('the mute button reflects saved state and toggles voice output', () => {
  const renderer = loadRenderer();
  const button = renderer.element('voice-mute');
  const label = renderer.element('voice-mute-label');

  assert.equal(button.getAttribute('aria-pressed'), 'false');
  assert.equal(label.textContent, 'mute');

  renderer.emit('voice-muted', true);
  assert.equal(button.getAttribute('aria-pressed'), 'true');
  assert.equal(button.getAttribute('aria-label'), 'Unmute Serena voice');
  assert.equal(label.textContent, 'muted');

  button.listeners.get('click')();
  assert.deepEqual(renderer.voiceMuteChanges, [false]);

  renderer.emit('voice-muted', false);
  button.listeners.get('click')();
  assert.deepEqual(renderer.voiceMuteChanges, [false, true]);
});

test('the microphone button reflects saved state and toggles Serena input', () => {
  const renderer = loadRenderer();
  const button = renderer.element('microphone-mute');
  const label = renderer.element('microphone-mute-label');

  assert.equal(button.getAttribute('aria-pressed'), 'false');
  assert.equal(label.textContent, 'mic');

  renderer.emit('microphone-muted', true);
  assert.equal(button.getAttribute('aria-pressed'), 'true');
  assert.equal(button.getAttribute('aria-label'), 'Unmute Serena microphone');
  assert.equal(label.textContent, 'mic off');

  button.listeners.get('click')();
  assert.deepEqual(renderer.microphoneMuteChanges, [false]);
});

test('late speech completion keeps the complete response visible', () => {
  const renderer = loadRenderer();
  const response = renderer.element('response');
  const reply = 'one '.repeat(150).trim();

  renderer.emit('state', 'thinking');
  renderer.emit('transcription', 'give me the long version');
  renderer.emit('response', reply);

  assert.equal(response.textContent, reply);
  renderer.clock.advance(8_500);
  assert.equal(response.classList.contains('hidden'), false);
  assert.equal(response.classList.contains('fade-out'), false);

  renderer.emit('state', 'speaking');
  renderer.clock.advance(30_000);
  assert.equal(response.textContent, reply);
  assert.equal(response.classList.contains('hidden'), false);

  renderer.emit('state', 'listening');
  assert.equal(response.classList.contains('fade-out'), true);
  renderer.clock.advance(499);
  assert.equal(response.classList.contains('hidden'), false);
  renderer.clock.advance(1);
  assert.equal(response.classList.contains('hidden'), true);
  assert.equal(response.textContent, '');
});

test('a late speaking state cancels the text-only fallback race', () => {
  const renderer = loadRenderer();
  const response = renderer.element('response');

  renderer.emit('state', 'idle');
  renderer.emit('response', 'the response socket arrived first');
  assert.equal(renderer.clock.pending(), 1);

  renderer.emit('state', 'speaking');
  assert.equal(renderer.clock.pending(), 0);
  renderer.clock.advance(20_000);
  assert.equal(response.textContent, 'the response socket arrived first');
  assert.equal(response.classList.contains('hidden'), false);

  renderer.emit('state', 'idle');
  renderer.clock.advance(500);
  assert.equal(response.classList.contains('hidden'), true);
});

test('an interruption clears the old response before showing new text', () => {
  const renderer = loadRenderer();
  const response = renderer.element('response');
  const transcription = renderer.element('transcription');

  renderer.emit('state', 'speaking');
  renderer.emit('response', 'the old answer');
  renderer.emit('state', 'listening');
  renderer.emit('transcription', 'stop, answer this instead');

  assert.equal(response.textContent, '');
  assert.equal(response.classList.contains('hidden'), true);
  assert.equal(response.classList.contains('fade-out'), false);
  assert.equal(transcription.textContent, 'stop, answer this instead');
  assert.equal(transcription.classList.contains('hidden'), false);
  renderer.clock.advance(10_000);
  assert.equal(transcription.classList.contains('hidden'), false);
});

test('text-only replies keep the bounded fallback and have no reveal delay', () => {
  const renderer = loadRenderer();
  const response = renderer.element('response');
  const reply = 'x'.repeat(500);

  renderer.emit('state', 'idle');
  renderer.emit('response', reply);
  assert.equal(response.textContent.length, 500);
  assert.equal(renderer.clock.pending(), 1);

  renderer.clock.advance(7_999);
  assert.equal(response.classList.contains('hidden'), false);
  renderer.clock.advance(1);
  assert.equal(response.classList.contains('fade-out'), true);
  renderer.clock.advance(500);
  assert.equal(response.classList.contains('hidden'), true);
});
