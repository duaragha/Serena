// In-app updates must behave the same on both platforms, and must never
// update themselves behind the user's back.
//
// Serena IS the terminal Raghav works in, so an update that downloads and
// installs on its own mid-turn is worse than a stale version. It must also be
// honest about the cases where it CANNOT update: a dev run, or a Linux build
// launched from an extracted copy rather than the AppImage file, both of which
// silently do nothing if you let electron-updater try.

const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const fs = require('node:fs');

const ROOT = path.resolve(__dirname, '..');

function loadUpdates({ packaged = true, platform = 'linux', appImage = '/tmp/Serena.AppImage', updater } = {}) {
  const source = fs.readFileSync(path.join(ROOT, 'updates.js'), 'utf8');
  const dialogCalls = [];
  const electron = {
    app: { isPackaged: packaged, getVersion: () => '0.1.0', getName: () => 'Serena' },
    dialog: {
      showMessageBox: async (_parent, options) => {
        dialogCalls.push(options);
        return { response: 0 };
      },
    },
  };
  const sandboxRequire = (name) => {
    if (name === 'electron') return electron;
    if (name === 'electron-updater') {
      if (updater === null) throw new Error('not installed');
      return { autoUpdater: updater };
    }
    throw new Error(`unexpected require: ${name}`);
  };
  const env = { ...process.env };
  if (appImage) env.APPIMAGE = appImage;
  else delete env.APPIMAGE;

  const module = { exports: {} };
  const context = {
    module,
    exports: module.exports,
    require: sandboxRequire,
    console: { log() {}, error() {} },
    process: { ...process, platform, env },
    setImmediate,
  };
  vm.runInNewContext(source, context, { filename: 'updates.js' });
  return { api: module.exports, dialogCalls };
}

function fakeUpdater(overrides = {}) {
  const listeners = new Map();
  return {
    autoDownload: true,
    autoInstallOnAppQuit: false,
    logger: null,
    on(event, fn) {
      listeners.set(event, fn);
      return this;
    },
    off(event) {
      listeners.delete(event);
      return this;
    },
    emit(event, payload) {
      const fn = listeners.get(event);
      if (fn) fn(payload);
    },
    checkForUpdates: async () => ({ updateInfo: { version: '0.1.0' } }),
    downloadUpdate: async () => [],
    quitAndInstall() {
      this.installed = true;
    },
    ...overrides,
  };
}

test('the updater never downloads on its own', () => {
  const updater = fakeUpdater();
  const { api } = loadUpdates({ updater });
  api.describe();
  // Touch the lazy getter through a check so the configuration is applied.
  return api.check().then(() => {
    assert.equal(updater.autoDownload, false, 'an update must never download unasked');
    assert.equal(updater.autoInstallOnAppQuit, true, 'a downloaded update installs on quit, not mid-session');
  });
});

test('a newer remote version is reported as available', async () => {
  const updater = fakeUpdater({
    checkForUpdates: async () => ({ updateInfo: { version: '0.2.0' } }),
  });
  const { api } = loadUpdates({ updater });

  const outcome = await api.check();

  assert.equal(outcome.state, 'available');
  assert.equal(outcome.remoteVersion, '0.2.0');
  assert.equal(outcome.version, '0.1.0');
});

test('a matching version reports current, not available', async () => {
  const { api } = loadUpdates({ updater: fakeUpdater() });
  const outcome = await api.check();
  assert.equal(outcome.state, 'current');
});

test('no published release reads as none-published, not as an error', async () => {
  const updater = fakeUpdater({
    checkForUpdates: async () => {
      throw new Error('HttpError: 404 Not Found');
    },
  });
  const { api } = loadUpdates({ updater });

  const outcome = await api.check();

  assert.equal(outcome.state, 'none-published');
});

test('a real failure is surfaced as an error with its reason', async () => {
  const updater = fakeUpdater({
    checkForUpdates: async () => {
      throw new Error('getaddrinfo ENOTFOUND github.com');
    },
  });
  const { api } = loadUpdates({ updater });

  const outcome = await api.check();

  assert.equal(outcome.state, 'error');
  assert.match(outcome.reason, /ENOTFOUND/);
});

test('a development build refuses to update instead of pretending', async () => {
  const { api } = loadUpdates({ packaged: false, updater: fakeUpdater() });

  const outcome = await api.check();

  assert.equal(outcome.state, 'unsupported');
  assert.match(outcome.reason, /development build/i);
});

test('an extracted AppImage refuses, because it cannot replace itself', async () => {
  const { api } = loadUpdates({ platform: 'linux', appImage: '', updater: fakeUpdater() });

  const outcome = await api.check();

  assert.equal(outcome.state, 'unsupported');
  assert.match(outcome.reason, /AppImage/);
});

test('a missing updater component degrades instead of crashing the app', async () => {
  const { api } = loadUpdates({ updater: null });

  const outcome = await api.check();

  assert.equal(outcome.state, 'error');
  assert.match(outcome.reason, /updater component/i);
});

test('installing before downloading is refused', () => {
  const { api } = loadUpdates({ updater: fakeUpdater() });
  assert.throws(() => api.install(), /No update has been downloaded/);
});

test('download progress is reported as whole percentages', async () => {
  const updater = fakeUpdater();
  updater.downloadUpdate = async function () {
    this.emit('download-progress', { percent: 42.7, transferred: 1, total: 2, bytesPerSecond: 3 });
    return [];
  };
  const { api } = loadUpdates({ updater });
  const seen = [];

  await api.download((progress) => seen.push(progress));

  assert.equal(seen.length, 1);
  assert.equal(seen[0].percent, 43);
});

test('the platform label matches the build the user is running', () => {
  assert.equal(loadUpdates({ platform: 'linux' }).api.platformLabel(), 'Linux');
  assert.equal(loadUpdates({ platform: 'win32' }).api.platformLabel(), 'Windows');
});

test('About states the version and the platform it is for', async () => {
  const { api, dialogCalls } = loadUpdates({ platform: 'win32', updater: fakeUpdater() });

  await api.showAbout();

  const box = dialogCalls.at(-1);
  assert.match(box.message, /Serena 0\.1\.0/);
  assert.match(box.detail, /Windows build/);
});
