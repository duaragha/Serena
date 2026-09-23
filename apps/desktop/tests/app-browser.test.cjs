'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

const {
  DISCARD_AFTER_MS,
  MAX_LIVE_TABS,
  PARTITION,
  createAppBrowser,
  normalizeTarget,
} = require('../app-browser');

let nextContentsId = 1;

class FakeContents {
  constructor() {
    this.id = nextContentsId++;
    this.url = '';
    this.title = '';
    this.closed = false;
    this.inputs = [];
    this.inserted = [];
    this.scripts = [];
    this.scriptResult = null;
    this.handlers = {};
  }
  on(name, handler) { this.handlers[name] = handler; }
  setWindowOpenHandler(handler) { this.openHandler = handler; }
  async loadURL(url) { this.url = url; this.title = `title of ${url}`; }
  getURL() { return this.url; }
  getTitle() { return this.title; }
  isDestroyed() { return this.closed; }
  close() { this.closed = true; }
  async executeJavaScript(script) {
    this.scripts.push(script);
    return typeof this.scriptResult === 'function' ? this.scriptResult(script) : this.scriptResult;
  }
  sendInputEvent(event) { this.inputs.push(event); }
  insertText(text) { this.inserted.push(text); }
  async capturePage() {
    return { toPNG: () => Buffer.from('png-bytes'), getSize: () => ({ width: 1280, height: 800 }) };
  }
  reload() { this.reloaded = true; }
}

class FakeView {
  constructor(options) {
    this.options = options;
    this.webContents = new FakeContents();
    FakeView.made.push(this);
  }
  setBounds(bounds) { this.bounds = bounds; }
}
FakeView.made = [];

function harness(overrides = {}) {
  FakeView.made = [];
  const attached = new Set();
  const window = {
    isDestroyed: () => false,
    webContents: { getZoomFactor: () => 1 },
    contentView: {
      addChildView: (view) => attached.add(view),
      removeChildView: (view) => attached.delete(view),
    },
  };
  let clock = 1000;
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'serena-browser-'));
  const browser = createAppBrowser({
    WebContentsView: FakeView,
    session: { fromPartition: (name) => ({ name, webRequest: { onCompleted() {}, onErrorOccurred() {} } }) },
    getWindow: () => window,
    userDataDir,
    fs,
    path,
    http,
    crypto,
    profile: { channel: 'dev', version: '0.3.10-dev.9' },
    now: () => clock,
    setIntervalFn: () => null,
    clearIntervalFn: () => {},
    ...overrides,
  });
  return {
    browser,
    attached,
    userDataDir,
    advance: (ms) => { clock += ms; },
  };
}

test('what can be opened: dev servers, sites, files; never code-running schemes', () => {
  assert.equal(normalizeTarget('localhost:5173'), 'http://localhost:5173/');
  assert.equal(normalizeTarget('127.0.0.1:8000/api'), 'http://127.0.0.1:8000/api');
  assert.equal(normalizeTarget('github.com/duaragha'), 'https://github.com/duaragha');
  assert.equal(normalizeTarget('https://example.com/a?b=1'), 'https://example.com/a?b=1');
  assert.equal(normalizeTarget('/home/raghav/report.html'), 'file:///home/raghav/report.html');
  assert.equal(normalizeTarget('C:\\Users\\ragha\\x.pdf'), 'file:///C:/Users/ragha/x.pdf');
  assert.equal(normalizeTarget(''), 'about:blank');
  for (const bad of ['javascript:alert(1)', 'javascript:1', 'data:text/html,<b>x</b>', 'chrome://settings',
    'devtools://x', 'mailto:a@b.c', 'view-source:https://x.y']) {
    assert.throws(() => normalizeTarget(bad), /does not open/);
  }
});

test('pages live in their own session, sandboxed, and hidden tabs are throttled', async () => {
  const { browser } = harness();
  await browser.run('open', { url: 'localhost:3000' });
  const prefs = FakeView.made[0].options.webPreferences;
  assert.equal(prefs.partition, PARTITION);
  assert.equal(prefs.sandbox, true);
  assert.equal(prefs.contextIsolation, true);
  assert.equal(prefs.nodeIntegration, false);
  assert.equal(prefs.backgroundThrottling, true);
});

test('only the tab on screen is attached, where the pane says it is', async () => {
  const { browser, attached } = harness();
  await browser.run('open', { url: 'localhost:3000' });
  assert.equal(attached.size, 0, 'nothing is drawn until the pane is on screen');
  browser.setPlaceholder({ visible: true, x: 300, y: 80, width: 900, height: 700 });
  assert.equal(attached.size, 1);
  assert.deepEqual(FakeView.made[0].bounds, { x: 300, y: 80, width: 900, height: 700 });
  await browser.run('open', { url: 'localhost:4000', newTab: true });
  assert.equal(attached.size, 1, 'switching tabs swaps the view, never stacks them');
  assert.ok(attached.has(FakeView.made[1]));
  browser.setPlaceholder({ visible: false });
  assert.equal(attached.size, 0);
});

test('hidden tabs are unloaded after a while and come back on demand', async () => {
  const { browser, advance } = harness();
  browser.setPlaceholder({ visible: true, x: 0, y: 0, width: 800, height: 600 });
  await browser.run('open', { url: 'localhost:3000' });
  await browser.run('open', { url: 'localhost:4000', newTab: true });
  advance(DISCARD_AFTER_MS + 1);
  browser.sweepIdle();
  const [first, second] = browser.state().tabs;
  assert.equal(first.unloaded, true, 'the hidden tab gave its memory back');
  assert.equal(first.url, 'http://localhost:3000/', 'but kept where it was');
  assert.equal(second.unloaded, false, 'the tab on screen stays');
  assert.equal(FakeView.made[0].webContents.closed, true);
  await browser.run('select', { tab: first.id });
  assert.equal(browser.state().tabs[0].unloaded, false);
});

test('never more than a few pages alive at once', async () => {
  const { browser, advance } = harness();
  browser.setPlaceholder({ visible: true, x: 0, y: 0, width: 800, height: 600 });
  for (let i = 0; i < MAX_LIVE_TABS + 3; i += 1) {
    advance(10);
    await browser.run('open', { url: `localhost:${3000 + i}`, newTab: true });
  }
  const live = browser.state().tabs.filter((tab) => !tab.unloaded);
  assert.equal(live.length, MAX_LIVE_TABS);
  assert.ok(live.some((tab) => tab.active));
});

test('look tags elements; click sends real input on screen and a DOM click off screen', async () => {
  const { browser } = harness();
  await browser.run('open', { url: 'localhost:3000' });
  const contents = FakeView.made[0].webContents;
  contents.scriptResult = (script) => (script.includes('data-serena-ref]')
    && script.includes('elements') ? { url: 'http://localhost:3000/', title: 'App', text: 'Hi', elements: [{ ref: 'e1' }] }
    : script.includes('el.click()') ? true : { x: 40, y: 20, tag: 'button' });
  const page = await browser.run('look', {});
  assert.deepEqual(page.elements, [{ ref: 'e1' }]);
  const offscreen = await browser.run('click', { ref: 'e1' });
  assert.equal(offscreen.offscreen, true);
  assert.equal(contents.inputs.length, 0);
  browser.setPlaceholder({ visible: true, x: 0, y: 0, width: 800, height: 600 });
  await browser.run('click', { ref: 'e1' });
  assert.deepEqual(contents.inputs.map((event) => event.type), ['mouseMove', 'mouseDown', 'mouseUp']);
  assert.equal(contents.inputs[1].x, 40);
  await browser.run('type', { text: 'hello', submit: true });
  assert.deepEqual(contents.inserted, ['hello']);
  assert.equal(contents.inputs.at(-1).keyCode, 'Enter');
});

test('console and failed requests are kept per tab for the agents to read', async () => {
  const { browser, advance } = harness();
  await browser.run('open', { url: 'localhost:3000' });
  const contents = FakeView.made[0].webContents;
  advance(5);
  contents.handlers['console-message']({ level: 'error', message: 'boom', lineNumber: 3, sourceId: 'app.js' });
  contents.handlers['did-fail-load']({}, -105, 'ERR_NAME_NOT_RESOLVED', 'http://nope/', true);
  const logs = await browser.run('logs', {});
  assert.equal(logs.console[0].message, 'boom');
  assert.equal(logs.network[0].error, 'ERR_NAME_NOT_RESOLVED');
});

test('a popup opens as another in-app tab instead of a window', async () => {
  const { browser } = harness();
  await browser.run('open', { url: 'localhost:3000' });
  const decision = FakeView.made[0].webContents.openHandler({ url: 'https://example.com/' });
  assert.deepEqual(decision, { action: 'deny' });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(browser.state().tabs.length, 2);
});

test('screenshots are written privately and only the last few are kept', async () => {
  const { browser, userDataDir, advance } = harness();
  await browser.run('open', { url: 'localhost:3000' });
  let shot;
  for (let i = 0; i < 25; i += 1) {
    advance(1);
    shot = await browser.run('screenshot', {});
  }
  assert.ok(fs.existsSync(shot.path));
  assert.equal(fs.statSync(shot.path).mode & 0o777, 0o600);
  assert.equal(fs.readdirSync(path.join(userDataDir, 'app-browser', 'shots')).length, 20);
});

function post(port, route, { token, origin, body = {} } = {}) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const headers = { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data) };
    if (token) headers.authorization = `Bearer ${token}`;
    if (origin) headers.origin = origin;
    const request = http.request({ host: '127.0.0.1', port, path: route, method: 'POST', headers }, (response) => {
      let text = '';
      response.on('data', (chunk) => { text += chunk; });
      response.on('end', () => resolve({ status: response.statusCode, body: JSON.parse(text) }));
    });
    request.on('error', reject);
    request.end(data);
  });
}

test('the control server answers only its token holder, never a web page', async () => {
  const { browser } = harness();
  const port = await browser.start();
  try {
    const control = JSON.parse(fs.readFileSync(browser.controlFile, 'utf8'));
    assert.equal(control.port, port);
    assert.equal(control.channel, 'dev');
    assert.equal(fs.statSync(browser.controlFile).mode & 0o777, 0o600);
    assert.equal((await post(port, '/browser/tabs')).status, 401);
    assert.equal((await post(port, '/browser/tabs', { token: 'wrong' })).status, 401);
    const page = await post(port, '/browser/tabs', { token: control.token, origin: 'http://evil.test' });
    assert.equal(page.status, 403);
    const opened = await post(port, '/browser/open', { token: control.token, body: { url: 'localhost:5173' } });
    assert.equal(opened.body.ok, true);
    assert.equal(opened.body.url, 'http://localhost:5173/');
    const bad = await post(port, '/browser/open', { token: control.token, body: { url: 'javascript:1' } });
    assert.equal(bad.body.ok, false);
    assert.match(bad.body.error, /does not open/);
  } finally {
    browser.stop();
  }
  assert.equal(fs.existsSync(browser.controlFile), false, 'the control file goes with the app');
});

test('tabs come back after a restart, unloaded until used', async () => {
  const first = harness();
  await first.browser.run('open', { url: 'localhost:3000' });
  await first.browser.run('open', { url: 'localhost:4000', newTab: true });
  await new Promise((resolve) => setTimeout(resolve, 600));
  const saved = path.join(first.userDataDir, 'app-browser', 'tabs.json');
  assert.equal(fs.statSync(saved).mode & 0o777, 0o600);
  const second = harness({ userDataDir: first.userDataDir });
  const port = await second.browser.start();
  try {
    assert.ok(port > 0);
    const restored = second.browser.state();
    assert.deepEqual(restored.tabs.map((tab) => tab.url), ['http://localhost:3000/', 'http://localhost:4000/']);
    assert.ok(restored.tabs.every((tab) => tab.unloaded), 'nothing loads until someone looks');
    assert.equal(restored.tabs.find((tab) => tab.active).url, 'http://localhost:4000/');
  } finally {
    second.browser.stop();
  }
});
