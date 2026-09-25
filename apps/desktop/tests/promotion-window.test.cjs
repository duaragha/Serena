'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { createPromotionWindow } = require('../promotion-window.cjs');

test('stable cannot register the release window or privileged IPC', () => {
  assert.throws(() => createPromotionWindow({ profile: { channel: 'stable' } }), /Dev-only/);
});
test('only the isolated release window main frame can invoke its bounded IPC', async () => {
  const handlers = new Map();
  let instance;
  let external;
  class Window {
    constructor(options) {
      this.options = options; instance = this;
      this.webContents = { mainFrame: {}, setWindowOpenHandler: f => { this.open = f; }, on: () => {} };
    }
    setMenu() {} once() {} on() {} isDestroyed() { return false; } async loadFile() {} show() {} focus() {}
  }
  const open = createPromotionWindow({ app: { getPath: () => '/tmp/test-unused' }, profile: { channel: 'dev' },
    BrowserWindow: Window, ipcMain: { handle: (name, fn) => handlers.set(name, fn) },
    shell: { openExternal: async url => { external = url; } }, dialog: {} });
  await open();
  assert.equal(instance.options.webPreferences.sandbox, true);
  assert.equal(instance.options.webPreferences.contextIsolation, true);
  assert.equal(instance.options.webPreferences.nodeIntegration, false);
  assert.deepEqual(instance.open(), { action: 'deny' });
  const fn = handlers.get('promotion:github');
  const trusted = pathToFileURL(path.resolve(__dirname, '../promotion.html')).href;
  const frame = instance.webContents.mainFrame;
  frame.url = trusted;
  await assert.rejects(fn({ sender: {}, senderFrame: frame }), /Untrusted/);
  await assert.rejects(fn({ sender: instance.webContents, senderFrame: { url: trusted } }), /Untrusted/);
  frame.url = 'http://127.0.0.1:1234';
  await assert.rejects(fn({ sender: instance.webContents, senderFrame: frame }), /Untrusted/);
  frame.url = trusted;
  assert.equal((await fn({ sender: instance.webContents, senderFrame: frame })).ok, true);
  assert.equal(external, 'https://github.com/duaragha/Serena/actions/workflows/selective-promotion.yml');
});

test('a failed release action is written to the desktop log, not only shown in the window', async () => {
  const handlers = new Map();
  const logged = [];
  let instance;
  class Window {
    constructor() { instance = this; this.webContents = { mainFrame: {}, setWindowOpenHandler() {}, on() {} }; }
    setMenu() {} once() {} on() {} isDestroyed() { return false; } async loadFile() {} show() {} focus() {}
  }
  const open = createPromotionWindow({ app: { getPath: () => '/tmp/test-unused' }, profile: { channel: 'dev' },
    BrowserWindow: Window, ipcMain: { handle: (name, fn) => handlers.set(name, fn) },
    shell: { openExternal: async () => { throw new Error('No browser available'); } }, dialog: {},
    log: message => logged.push(message) });
  await open();
  const frame = instance.webContents.mainFrame;
  frame.url = pathToFileURL(path.resolve(__dirname, '../promotion.html')).href;
  const result = await handlers.get('promotion:github')({ sender: instance.webContents, senderFrame: frame });
  assert.deepEqual(result, { ok: false, error: 'No browser available' });
  assert.deepEqual(logged, ['releases github failed: No browser available']);
});
