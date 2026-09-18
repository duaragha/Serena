// The coding drawer is optional viewing history. It may take the screen when a
// job is actually running and at no other time: not on a plain voice turn, not
// on a typed message, not on the snapshot the bridge replays at every
// reconnect, and never again after Raghav closes it on a job.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const DESKTOP = path.resolve(__dirname, '..');

function loadOverlay({ codingPaneWidth = '' } = {}) {
  const source = fs.readFileSync(path.join(DESKTOP, 'main.js'), 'utf8');
  const sends = [];
  const shown = [];
  const ipcHandlers = new Map();
  // The window is parked against the right edge of a 1920-wide work area.
  let bounds = { x: 1400, y: 440, width: 500, height: 600 };
  let savedCodingPaneWidth = String(codingPaneWidth);

  const webContents = {
    on() {},
    send: (channel, payload) => sends.push({ channel, payload }),
  };
  const win = {
    webContents,
    loadFile() {},
    setVisibleOnAllWorkspaces() {},
    setAlwaysOnTop() {},
    on() {},
    isVisible: () => true,
    isDestroyed: () => false,
    isMinimized: () => false,
    restore() {},
    show: () => shown.push('show'),
    showInactive: () => shown.push('showInactive'),
    moveTop() {},
    hide: () => shown.push('hide'),
    getBounds: () => ({ ...bounds }),
    setBounds: (next) => {
      bounds = { ...bounds, ...next };
    },
    setMinimumSize() {},
  };

  const fakeElectron = {
    app: {
      whenReady: () => ({ then() {} }),
      on() {},
      quit() {},
    },
    BrowserWindow: function BrowserWindow() {
      return win;
    },
    Tray: function Tray() {
      return { setToolTip() {}, setContextMenu() {}, setImage() {} };
    },
    Menu: { buildFromTemplate: (template) => template },
    nativeImage: { createFromDataURL: () => ({}) },
    ipcMain: { on: (channel, handler) => ipcHandlers.set(channel, handler) },
    screen: {
      getPrimaryDisplay: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }),
      getDisplayMatching: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }),
    },
  };

  const localRequire = (request) => {
    if (request === 'electron') return fakeElectron;
    if (request === 'child_process') return { execFile() {} };
    if (request === 'ws') return function WebSocket() {};
    if (request === 'fs') {
      return {
        ...fs,
        readFileSync(file, encoding) {
          if (String(file).endsWith('coding_pane_width')) {
            if (!savedCodingPaneWidth) throw new Error('ENOENT');
            return savedCodingPaneWidth;
          }
          return fs.readFileSync(file, encoding);
        },
        mkdirSync(directory, options) {
          if (String(directory).endsWith(path.join('.config', 'serena'))) return;
          return fs.mkdirSync(directory, options);
        },
        writeFileSync(file, value, encoding) {
          if (String(file).endsWith('coding_pane_width.tmp')) {
            savedCodingPaneWidth = String(value).trim();
            return;
          }
          return fs.writeFileSync(file, value, encoding);
        },
        renameSync(source, destination) {
          if (String(destination).endsWith('coding_pane_width')) return;
          return fs.renameSync(source, destination);
        },
      };
    }
    return require(request);
  };

  const context = {
    require: localRequire,
    __dirname: DESKTOP,
    process,
    console,
    Buffer,
    // Real timers would keep the idle-hide alive past the test.
    setTimeout: () => 0,
    clearTimeout: () => {},
  };
  vm.runInNewContext(source, context);
  context.createWindow();
  context.createTray();
  return {
    context,
    sends,
    shown,
    ipcHandlers,
    bounds: () => ({ ...bounds }),
    // Him dragging the window edge.
    resize: (next) => {
      bounds = { ...bounds, ...next };
    },
    savedCodingPaneWidth: () => savedCodingPaneWidth,
  };
}

const channels = (sends) => sends.map((entry) => entry.channel);

function snapshot(state, itemId = 'job-1') {
  return { item_id: itemId, state, project: 'serena' };
}

test('a snapshot for a job that is not running never opens the drawer', () => {
  for (const state of ['queued', 'completed', 'failed', 'cancelled']) {
    const { context, sends, shown } = loadOverlay();
    context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot(state) });
    assert.ok(channels(sends).includes('code-snapshot'), state);
    assert.ok(!channels(sends).includes('show-code-panel'), state);
    assert.deepEqual(shown, [], state);
  }
});

test('a real coding job starting does open the drawer', () => {
  const { context, sends, shown } = loadOverlay();
  context.handleBackendMessage({
    type: 'code_start',
    item_id: 'job-1',
    project: 'serena',
    status: 'working',
    snapshot: snapshot('working'),
  });
  assert.ok(channels(sends).includes('code-start'));
  assert.deepEqual(shown, ['showInactive']);
});

test('a running job keeps the drawer open through its snapshots', () => {
  const { context, sends } = loadOverlay();
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('working') });
  assert.ok(channels(sends).includes('show-code-panel'));
});

test('closing the drawer keeps it closed for the rest of that job', () => {
  const { context, sends, ipcHandlers } = loadOverlay();
  context.handleBackendMessage({
    type: 'code_start',
    item_id: 'job-1',
    project: 'serena',
    status: 'working',
    snapshot: snapshot('working'),
  });
  ipcHandlers.get('hide-code-panel')();
  sends.length = 0;
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('working') });
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('working') });
  assert.ok(channels(sends).includes('code-snapshot'));
  assert.ok(!channels(sends).includes('show-code-panel'));
});

test('closing it before any job has been shown keeps it closed', () => {
  // He asked for the panel out loud, so it opened with nothing in it, and then
  // he closed it. That dismissal has no job to key on, so it has to hold
  // against every snapshot rather than only the one job it was showing.
  const { context, sends, ipcHandlers, bounds } = loadOverlay();
  context.handleBackendMessage({ type: 'code_start', project: 'coding', status: 'ready' });
  ipcHandlers.get('hide-code-panel')();
  const closed = bounds();
  sends.length = 0;
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('working', 'job-9') });
  assert.ok(!channels(sends).includes('show-code-panel'));
  assert.deepEqual(bounds(), closed);
});

test('a job start still reaches him after a blind dismissal', () => {
  const { context, shown, ipcHandlers } = loadOverlay();
  ipcHandlers.get('hide-code-panel')();
  shown.length = 0;
  context.handleBackendMessage({
    type: 'code_start',
    item_id: 'job-9',
    project: 'serena',
    status: 'working',
    snapshot: snapshot('working', 'job-9'),
  });
  assert.deepEqual(shown, ['showInactive']);
});

test('an open drawer is not re-shown on every snapshot tick', () => {
  const { context, sends } = loadOverlay();
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('working') });
  sends.length = 0;
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('working') });
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('working') });
  assert.ok(!channels(sends).includes('show-code-panel'));
});

test('a different job starting after a dismissal still opens the drawer', () => {
  const { context, sends, ipcHandlers } = loadOverlay();
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('working') });
  ipcHandlers.get('hide-code-panel')();
  sends.length = 0;
  context.handleBackendMessage({
    type: 'code_snapshot',
    snapshot: snapshot('working', 'job-2'),
  });
  assert.ok(channels(sends).includes('show-code-panel'));
});

test('a renderer reload restores contents without reopening a closed drawer', () => {
  const { context, sends, ipcHandlers } = loadOverlay();
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('completed') });
  const replayed = [];
  const event = { sender: { send: (channel, payload) => replayed.push({ channel, payload }) } };
  ipcHandlers.get('renderer-ready')(event);
  assert.ok(channels(replayed).includes('code-snapshot'));
  assert.ok(!channels(replayed).includes('show-code-panel'));
  assert.ok(!channels(sends).includes('show-code-panel'));
});

test('opening the drawer widens the window instead of covering the app', () => {
  const { context, bounds } = loadOverlay();
  const before = bounds();
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('working') });
  const open = bounds();
  assert.equal(open.width, before.width + 450);
  // Grown leftward, so the window keeps the corner it was parked in.
  assert.equal(open.x + open.width, before.x + before.width);
  assert.equal(open.y, before.y);
  assert.equal(open.height, before.height);
});

test('closing the drawer gives the room back', () => {
  const { context, ipcHandlers, bounds } = loadOverlay();
  const before = bounds();
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('working') });
  ipcHandlers.get('hide-code-panel')();
  assert.deepEqual(bounds(), before);
});

test('the window is widened once, not once per snapshot', () => {
  const { context, bounds } = loadOverlay();
  const before = bounds();
  for (let i = 0; i < 4; i += 1) {
    context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('working') });
  }
  assert.equal(bounds().width, before.width + 450);
});

test('a width he chose by hand survives the drawer opening and closing', () => {
  const { context, ipcHandlers, bounds, resize } = loadOverlay();
  resize({ x: 1180, width: 720 });
  const chosen = bounds();
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('working') });
  assert.equal(bounds().width, chosen.width + 450);
  ipcHandlers.get('hide-code-panel')();
  assert.deepEqual(bounds(), chosen);
});

test('the persisted coding pane width is restored when the drawer opens', () => {
  const { context, bounds } = loadOverlay({ codingPaneWidth: '610' });
  const before = bounds();
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('working') });
  assert.equal(bounds().width, before.width + 610);
});

test('resizing the coding pane preserves the stage and persists the chosen width', () => {
  const { context, ipcHandlers, bounds, savedCodingPaneWidth } = loadOverlay();
  const before = bounds();
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('working') });
  ipcHandlers.get('set-code-panel-width')({}, 620);
  assert.equal(bounds().width, before.width + 620);
  assert.equal(bounds().x + bounds().width, before.x + before.width);
  assert.equal(savedCodingPaneWidth(), '620');
  ipcHandlers.get('hide-code-panel')();
  assert.deepEqual(bounds(), before);
});

test('coding pane resize requests are clamped to sensible bounds', () => {
  const { context, ipcHandlers, bounds, savedCodingPaneWidth } = loadOverlay();
  const before = bounds();
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('working') });
  ipcHandlers.get('set-code-panel-width')({}, 100);
  assert.equal(bounds().width, before.width + 300);
  assert.equal(savedCodingPaneWidth(), '300');
  ipcHandlers.get('set-code-panel-width')({}, 900);
  assert.equal(bounds().width, before.width + 720);
  assert.equal(savedCodingPaneWidth(), '720');
});

test('a job that is not running never moves the window at all', () => {
  const { context, bounds } = loadOverlay();
  const before = bounds();
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('completed') });
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('queued') });
  assert.deepEqual(bounds(), before);
});

test('a reload while a job is on screen puts the drawer back', () => {
  const { context, ipcHandlers } = loadOverlay();
  context.handleBackendMessage({ type: 'code_snapshot', snapshot: snapshot('working') });
  const replayed = [];
  const event = { sender: { send: (channel, payload) => replayed.push({ channel, payload }) } };
  ipcHandlers.get('renderer-ready')(event);
  assert.ok(channels(replayed).includes('show-code-panel'));
});
