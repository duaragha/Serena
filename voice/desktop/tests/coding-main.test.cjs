const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');
const fs = require('node:fs');
const { EventEmitter } = require('node:events');
const vm = require('node:vm');

const ITEM_ID = 'b62f3779-9bc6-4d19-b1d9-6384fc4743e9';
const SESSION_ID = '019fc3b5-2acb-7492-8d76-21f3007f8bdb';
const PROJECT_ROOT = '/home/raghav/Documents/Projects/serena';

function loadMain({ terminalInstalled = true, brokerPayload = null, brokerDelayMs = 0 } = {}) {
  const desktop = path.resolve(__dirname, '..');
  const source = fs.readFileSync(path.join(desktop, 'coding-main.js'), 'utf8');
  const handlers = new Map();
  const readyCallbacks = [];
  const configuredPreloads = [];
  const execCalls = [];
  let mainLoaded = false;
  const payload = brokerPayload || {
    ok: true,
    session_id: SESSION_ID,
    provider: 'codex',
  };

  const fakeElectron = {
    app: {
      whenReady: () => ({ then: (callback) => readyCallbacks.push(callback) }),
    },
    ipcMain: {
      handle: (channel, callback) => handlers.set(channel, callback),
    },
    session: {
      defaultSession: {
        getPreloads: () => ['/existing/preload.js'],
        setPreloads: (preloads) => configuredPreloads.push(...preloads),
      },
    },
  };
  const fakeChildProcess = {
    execFile: (binary, args, options, callback) => {
      execCalls.push({ type: 'execFile', binary, args, options });
      if (binary.endsWith('/python')) {
        callback(null, JSON.stringify([{ item_id: ITEM_ID, terminal: { can_open: true } }]), '');
        return;
      }
      callback(null, '', '');
    },
    spawn: (binary, args, options) => {
      execCalls.push({ type: 'spawn', binary, args, options });
      const child = new EventEmitter();
      child.stdout = new EventEmitter();
      child.stderr = new EventEmitter();
      child.unref = () => {};
      const emitPayload = () => {
        child.stdout.emit('data', Buffer.from(`${JSON.stringify(payload)}\n`));
        if (!payload.ok) child.emit('close', 2);
      };
      if (brokerDelayMs > 0) setTimeout(emitPayload, brokerDelayMs);
      else process.nextTick(emitPayload);
      return child;
    },
  };
  const fakeFs = {
    constants: { X_OK: 1 },
    accessSync: () => {
      if (!terminalInstalled) throw new Error('missing');
    },
  };
  const localRequire = (request) => {
    if (request === 'electron') return fakeElectron;
    if (request === 'child_process') return fakeChildProcess;
    if (request === 'fs') return fakeFs;
    if (request === 'path') return path;
    if (request === './main.js') {
      mainLoaded = true;
      return {};
    }
    throw new Error(`unexpected require: ${request}`);
  };

  vm.runInNewContext(source, {
    __dirname: desktop,
    require: localRequire,
    process,
    console,
    Error,
    JSON,
    Promise,
    String,
    clearTimeout,
    setTimeout,
  });
  return {
    handlers,
    readyCallbacks,
    configuredPreloads,
    execCalls,
    get mainLoaded() { return mainLoaded; },
  };
}

test('coding entrypoint installs bridges without automatically launching anything', async () => {
  const harness = loadMain();

  assert.equal(harness.mainLoaded, true);
  assert.equal(harness.readyCallbacks.length, 1);
  harness.readyCallbacks[0]();
  assert.equal(harness.configuredPreloads[0], '/existing/preload.js');
  assert.match(harness.configuredPreloads[1], /coding-preload\.js$/);
  assert.equal(harness.execCalls.length, 0);

  const response = await harness.handlers.get('serena-coding-jobs:list')();
  assert.deepEqual(JSON.parse(JSON.stringify(response)), {
    ok: true,
    jobs: [{ item_id: ITEM_ID, terminal: { can_open: true } }],
  });
  assert.equal(harness.execCalls.some((call) => call.binary.includes('gnome-terminal')), false);
});

test('explicit click starts the guarded broker for only the selected job', async () => {
  const harness = loadMain();
  const open = harness.handlers.get('serena-coding-terminal:open');

  const invalid = await open(null, '../../not-a-job');
  assert.equal(invalid.ok, false);
  assert.equal(harness.execCalls.length, 0);

  const result = await open(null, ITEM_ID);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    ok: true,
    session_id: SESSION_ID,
    provider: 'codex',
  });
  const launch = harness.execCalls.find((call) => call.type === 'spawn');
  assert.ok(launch);
  assert.match(launch.binary, /\.venv\/bin\/python$/);
  assert.equal(launch.options.cwd, PROJECT_ROOT);
  assert.equal(launch.options.detached, true);
  assert.equal(launch.args.includes('voice.desktop.live_session_terminal'), true);
  assert.equal(launch.args.includes(ITEM_ID), true);
  assert.equal(launch.args.includes('/usr/bin/gnome-terminal'), true);
  assert.equal(launch.args.includes(SESSION_ID), false);
  assert.equal(launch.args.includes('voice.desktop.live_session_view'), false);

  const duplicate = await open(null, ITEM_ID);
  assert.deepEqual(JSON.parse(JSON.stringify(duplicate)), {
    ok: false,
    error: 'interactive terminal is already open',
  });
  assert.equal(harness.execCalls.filter((call) => call.type === 'spawn').length, 1);
});

test('stale target metadata fails visibly and never launches a terminal', async () => {
  const harness = loadMain({
    brokerPayload: { ok: false, error: 'persisted session transcript is stale' },
  });
  const result = await harness.handlers.get('serena-coding-terminal:open')(null, ITEM_ID);

  assert.equal(result.ok, false);
  assert.match(result.error, /stale/);
  assert.equal(harness.execCalls.some((call) => call.binary.includes('gnome-terminal')), false);
});

test('a delayed broker result is not falsely reported as a failed launch', async () => {
  const harness = loadMain({ brokerDelayMs: 20 });
  const result = await harness.handlers.get('serena-coding-terminal:open')(null, ITEM_ID);

  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    ok: true,
    session_id: SESSION_ID,
    provider: 'codex',
  });
});

test('GNOME Terminal stays optional and its absence removes launch capability', async () => {
  const harness = loadMain({ terminalInstalled: false });
  const listed = await harness.handlers.get('serena-coding-jobs:list')();
  const opened = await harness.handlers.get('serena-coding-terminal:open')(null, ITEM_ID);

  assert.equal(listed.jobs[0].terminal.can_open, false);
  assert.equal(listed.jobs[0].terminal.reason, 'GNOME Terminal is not installed');
  assert.deepEqual(JSON.parse(JSON.stringify(opened)), {
    ok: false,
    error: 'GNOME Terminal is not installed',
  });
  assert.equal(harness.execCalls.some((call) => call.binary.includes('gnome-terminal')), false);
});

test('preload exposes only the selected job id through guarded IPC', async () => {
  const desktop = path.resolve(__dirname, '..');
  const source = fs.readFileSync(path.join(desktop, 'coding-preload.js'), 'utf8');
  const calls = [];
  let exposed = null;
  vm.runInNewContext(source, {
    require: (request) => {
      assert.equal(request, 'electron');
      return {
        contextBridge: {
          exposeInMainWorld: (name, api) => {
            assert.equal(name, 'serenaCodingJobs');
            exposed = api;
          },
        },
        ipcRenderer: {
          invoke: (...args) => {
            calls.push(args);
            return Promise.resolve({ ok: true });
          },
        },
      };
    },
    document: {
      readyState: 'loading',
      querySelector: () => null,
      createElement: () => ({ dataset: {} }),
      head: { appendChild() {} },
    },
    window: { addEventListener() {} },
  });

  await exposed.openTerminal(ITEM_ID);
  assert.deepEqual(calls, [['serena-coding-terminal:open', ITEM_ID]]);
});
