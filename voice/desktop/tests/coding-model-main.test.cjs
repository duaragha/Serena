const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

test('coding model ipc carries get and set through the durable preference helper', async () => {
  const desktop = path.resolve(__dirname, '..');
  const source = fs.readFileSync(path.join(desktop, 'coding-main.js'), 'utf8');
  const handlers = new Map();
  const calls = [];
  const fakeElectron = {
    app: { whenReady: () => ({ then() {} }) },
    ipcMain: { handle: (channel, callback) => handlers.set(channel, callback) },
    session: { defaultSession: { getPreloads: () => [], setPreloads() {} } },
  };
  const fakeChildProcess = {
    execFile: (_binary, args, _options, callback) => {
      calls.push(args);
      callback(null, JSON.stringify({
        model: args.at(-1) === 'get' ? 'auto' : args.at(-1),
        options: [{ value: 'auto', label: 'auto' }],
      }), '');
    },
  };
  const localRequire = (request) => {
    if (request === 'electron') return fakeElectron;
    if (request === 'child_process') return fakeChildProcess;
    if (request === 'path') return path;
    if (request === './main.js') return {};
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
  });

  assert.deepEqual(
    JSON.parse(JSON.stringify(await handlers.get('serena-coding-model:get')())),
    { ok: true, model: 'auto', options: [{ value: 'auto', label: 'auto' }] },
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(await handlers.get('serena-coding-model:set')({}, 'claude-opus-5'))),
    {
      ok: true,
      model: 'claude-opus-5',
      options: [{ value: 'auto', label: 'auto' }],
    },
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(calls[0].slice(-3))),
    ['-m', 'core.coding_model_preferences', 'get'],
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(calls[1].slice(-4))),
    ['-m', 'core.coding_model_preferences', 'set', 'claude-opus-5'],
  );
});
