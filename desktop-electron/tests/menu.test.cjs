// The menu is the only way into About and updates, so its shape is a contract.
//
// The app previously shipped Electron's default menu: no version anywhere, and
// no way to update. Raghav asked for the familiar File / Edit / View / Window
// bar with an About menu holding "Check for Updates" and the version info.

const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const fs = require('node:fs');

const ROOT = path.resolve(__dirname, '..');

function loadMenu(platform = 'linux') {
  const source = fs.readFileSync(path.join(ROOT, 'menu.js'), 'utf8');
  const built = [];
  const electron = {
    shell: { openExternal: async () => {}, showItemInFolder: () => { calls.revealed += 1; } },
    Menu: {
      buildFromTemplate: (tpl) => {
        built.push(tpl);
        return { tpl };
      },
      setApplicationMenu: () => {},
    },
    app: { getName: () => 'Serena' },
  };
  const calls = { about: 0, check: 0, revealed: 0 };
  const updates = {
    checkInteractively: async () => {
      calls.check += 1;
    },
    showAbout: async () => {
      calls.about += 1;
    },
  };
  const module = { exports: {} };
  vm.runInNewContext(source, {
    module,
    exports: module.exports,
    require: (name) => {
      if (name === 'electron') return electron;
      if (name === './updates') return updates;
      if (name === './logging') return { logPath: () => '/tmp/serena/logs/backend.log' };
      throw new Error(`unexpected require: ${name}`);
    },
    console: { error() {} },
    process: { ...process, platform },
  }, { filename: 'menu.js' });
  return { api: module.exports, built, calls };
}

function labels(template) {
  // Array.from rebuilds the list in THIS realm. Each menu is built inside its
  // own vm context, so mapping there returns an array whose prototype belongs
  // to the sandbox, and deepStrictEqual compares prototypes.
  return Array.from(template, (entry) => entry.label);
}

function find(template, label) {
  return template.find((entry) => entry.label === label);
}

test('the standard menus are present in the expected order', () => {
  const { api } = loadMenu();
  const tpl = api.template(() => null);

  const names = labels(tpl);
  for (const expected of ['File', 'Edit', 'View', 'Window', 'About']) {
    assert.ok(names.includes(expected), `${expected} menu is missing`);
  }
  assert.ok(
    names.indexOf('File') < names.indexOf('Edit'),
    'File must come before Edit',
  );
});

test('About holds check-for-updates and the version info', () => {
  const { api } = loadMenu();
  const about = find(api.template(() => null), 'About');

  const items = about.submenu.map((entry) => entry.label).filter(Boolean);

  assert.ok(items.some((l) => /check for updates/i.test(l)), 'no update entry');
  assert.ok(items.some((l) => /about serena/i.test(l)), 'no about entry');
});

test('both About items are wired to the shared updates module', async () => {
  const { api, calls } = loadMenu();
  const about = find(api.template(() => null), 'About');

  for (const entry of about.submenu) {
    if (/check for updates/i.test(entry.label || '')) entry.click();
    if (/about serena/i.test(entry.label || '')) entry.click();
  }
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(calls.check, 1, 'check for updates is not wired');
  assert.equal(calls.about, 1, 'about is not wired');
});

test('the menu is identical on Windows and Linux', () => {
  const linux = labels(loadMenu('linux').api.template(() => null));
  const windows = labels(loadMenu('win32').api.template(() => null));

  assert.deepEqual(windows, linux, 'the two platforms must present the same menu');
});

test('macOS additionally gets the app menu convention', () => {
  const mac = labels(loadMenu('darwin').api.template(() => null));
  assert.equal(mac[0], 'Serena', 'macOS puts the app menu first');
});

test('installing the menu builds it once from the template', () => {
  const { api, built } = loadMenu();
  api.install(() => null);
  assert.equal(built.length, 1);
  assert.ok(labels(built[0]).includes('About'));
});

test('About offers a way to reach the log file', () => {
  // On Windows this file is the only record a backend crash leaves, and it
  // lives somewhere nobody would find by hand.
  const { api, calls } = loadMenu();
  const about = find(api.template(() => null), 'About');

  const item = about.submenu.find((entry) => /log/i.test(entry.label || ''));
  assert.ok(item, 'no way to open the logs');

  item.click();
  assert.equal(calls.revealed, 1, 'the log entry is not wired to the file');
});
