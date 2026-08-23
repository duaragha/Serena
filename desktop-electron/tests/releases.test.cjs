// A release publishes in two halves, minutes apart, and both are worth saying.
//
// The Linux job creates the GitHub release and uploads the AppImage; the
// Windows job then uploads the installer into the same release. Announcing on
// the tag alone would fire once, too early, and point a Windows machine at an
// artifact that does not exist yet.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..');

// Values built inside the vm context carry that realm's prototypes, so
// deepStrictEqual on them fails even when the contents match.
function here(value) {
  return Array.from(value);
}

function load({ platform = 'linux', version = '0.2.1' } = {}) {
  const source = fs.readFileSync(path.join(ROOT, 'releases.js'), 'utf8');
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'serena-releases-'));
  const shown = [];
  const opened = [];

  const electron = {
    app: { getVersion: () => version, getPath: () => userData, isPackaged: true },
    net: { request: () => { throw new Error('the tests must inject a fetcher'); } },
    shell: { openExternal: async (url) => { opened.push(url); } },
    Notification: class {
      constructor(options) {
        this.options = options;
        shown.push(options);
      }
      on() { return this; }
      show() { return this; }
      static isSupported() { return true; }
    },
  };

  const module = { exports: {} };
  vm.runInNewContext(source, {
    module,
    exports: module.exports,
    require: (name) => {
      if (name === 'electron') return electron;
      if (name === './updates') return { FEED: { owner: 'duaragha', repo: 'Serena' } };
      if (name === 'node:fs') return fs;
      if (name === 'node:path') return path;
      throw new Error(`unexpected require: ${name}`);
    },
    console: { log() {}, error() {} },
    process: { ...process, platform },
    setTimeout,
    setInterval,
    clearInterval,
    Promise,
    JSON,
    Object,
    Math,
    Set,
    Array,
    String,
    Number,
    parseInt,
  }, { filename: 'releases.js' });

  return { api: module.exports, shown, opened, userData };
}

function release(tag, assets) {
  return { tag_name: tag, html_url: `https://github.com/duaragha/Serena/releases/tag/${tag}`, assets: assets.map((name) => ({ name })) };
}

const LINUX_ONLY = ['Serena-0.2.2-x86_64.AppImage', 'latest-linux.yml'];
const BOTH = [...LINUX_ONLY, 'Serena-Setup-0.2.2-x64.exe', 'Serena-Setup-0.2.2-x64.exe.blockmap', 'latest.yml'];

test('a build counts as landed only when its channel file is there too', () => {
  const { api } = load();
  assert.deepEqual(here(api.landedPlatforms(release('v0.2.2', LINUX_ONLY))), ['linux']);
  assert.deepEqual(here(api.landedPlatforms(release('v0.2.2', BOTH))).sort(), ['linux', 'win32']);
  // An installer with no channel file is an update nothing can find.
  assert.deepEqual(here(api.landedPlatforms(release('v0.2.2', ['Serena-Setup-0.2.2-x64.exe']))), []);
});

test('each platform is announced once, as it lands', async () => {
  const { api, shown } = load();
  let current = release('v0.2.2', LINUX_ONLY);
  const fetchRelease = async () => current;

  assert.deepEqual(here(await api.poll({ fetchRelease })), ['linux']);
  assert.equal(shown.length, 1);
  assert.match(shown[0].title, /Linux build/);

  // Polling again before Windows finishes must stay quiet.
  assert.deepEqual(here(await api.poll({ fetchRelease })), []);
  assert.equal(shown.length, 1, 'the same build must not be announced twice');

  current = release('v0.2.2', BOTH);
  assert.deepEqual(here(await api.poll({ fetchRelease })), ['win32']);
  assert.equal(shown.length, 2);
  assert.match(shown[1].title, /Windows build/);

  assert.deepEqual(here(await api.poll({ fetchRelease })), [], 'nothing left to say');
});

test('the machine running the app is told what to do about it', async () => {
  const linux = load({ platform: 'linux' });
  await linux.api.poll({ fetchRelease: async () => release('v0.2.2', BOTH) });

  const own = linux.shown.find((n) => /Linux/.test(n.title));
  const other = linux.shown.find((n) => /Windows/.test(n.title));

  assert.match(own.body, /check for updates/i, 'the local build should be actionable');
  assert.match(other.body, /finished publishing/i, 'the other machine is just news');
});

test('the version you are already running is not news', async () => {
  const { api, shown } = load({ version: '0.2.2' });
  assert.deepEqual(here(await api.poll({ fetchRelease: async () => release('v0.2.2', BOTH) })), []);
  assert.equal(shown.length, 0);
});

test('an older release is never announced', async () => {
  const { api, shown } = load({ version: '0.3.0' });
  await api.poll({ fetchRelease: async () => release('v0.2.2', BOTH) });
  assert.equal(shown.length, 0);
});

test('a failed check is silent, not a notification every fifteen minutes', async () => {
  const { api, shown } = load();
  const outcome = await api.poll({ fetchRelease: async () => { throw new Error('ENOTFOUND'); } });
  assert.deepEqual(here(outcome), []);
  assert.equal(shown.length, 0);
});

test('what was announced survives a restart', async () => {
  const first = load();
  await first.api.poll({ fetchRelease: async () => release('v0.2.2', BOTH) });
  assert.equal(first.shown.length, 2);

  // A second run against the same profile directory.
  const restarted = load();
  fs.copyFileSync(
    path.join(first.userData, 'announced-builds.json'),
    path.join(restarted.userData, 'announced-builds.json'),
  );
  await restarted.api.poll({ fetchRelease: async () => release('v0.2.2', BOTH) });
  assert.equal(restarted.shown.length, 0, 'a restart must not replay old announcements');
});

test('version ordering handles the tags this project actually cuts', () => {
  const { api } = load();
  assert.ok(api.isNewer('0.2.2', '0.2.1'));
  assert.ok(api.isNewer('0.3.0', '0.2.9'));
  assert.ok(api.isNewer('1.0.0', '0.9.9'));
  assert.ok(!api.isNewer('0.2.1', '0.2.1'));
  assert.ok(!api.isNewer('0.2.0', '0.2.1'));
  assert.equal(api.versionOf({ tag_name: 'v0.2.2' }), '0.2.2');
});
