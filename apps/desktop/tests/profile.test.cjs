const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const yaml = require('js-yaml');
const { desktopProfile, backendEnvironment } = require('../profile');
const { configure } = require('../scripts/configure-release.cjs');

test('packaged identity cannot be switched by a development launch flag', () => {
  assert.equal(desktopProfile('0.2.85', { argv: ['--dev'] }).structured, false);
  assert.equal(desktopProfile('0.2.85-dev.1').structured, true);
  assert.equal(desktopProfile('0.2.85', { packaged: false, argv: ['--dev'] }).structured, true);
});

test('editions separate mutable UI state but share native writer locks and real home', () => {
  const stable = backendEnvironment(desktopProfile('0.2.85'), '/home/user');
  const dev = backendEnvironment(desktopProfile('0.2.85-dev.1'), '/home/user');
  for (const key of ['CHATS_DATA_DIR', 'SERENA_CONFIG_DIR', 'SERENA_CODING_MODEL_PATH']) {
    assert.notEqual(stable[key], dev[key], key);
  }
  assert.equal(stable.SERENA_RUNTIME_LEASE_DIR, dev.SERENA_RUNTIME_LEASE_DIR);
  assert.equal(stable.SERENA_STRUCTURED_WORKSPACE, '0');
  assert.equal(dev.SERENA_STRUCTURED_WORKSPACE, '1');
  for (const key of ['HOME', 'USERPROFILE', 'CLAUDE_DIR', 'CODEX_HOME', 'XDG_CONFIG_HOME']) {
    assert.equal(dev[key], undefined, `${key} must not hide native history/auth`);
  }
});

test('both platforms build distinct app IDs, names, executables, installers and feeds', () => {
  const root = path.resolve(__dirname, '..');
  const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json')));
  const windows = yaml.load(fs.readFileSync(path.join(root, 'windows/electron-builder.win.yml'), 'utf8'));
  const base = pkg.version.split('-')[0];
  const stable = configure(pkg, windows, `v${base}`);
  const dev = configure(pkg, windows, `v${base}-dev.1`);
  assert.notEqual(stable.pkg.name, dev.pkg.name);
  for (const [a, b] of [[stable.pkg.build, dev.pkg.build], [stable.windows, dev.windows]]) {
    assert.notEqual(a.appId, b.appId);
    assert.notEqual(a.productName, b.productName);
    assert.equal(a.publish[0].channel, 'latest');
    assert.equal(b.publish[0].channel, 'dev');
    assert.equal(b.publish[0].releaseType, 'prerelease');
    assert.equal(b.generateUpdatesFilesForAllChannels, false);
    assert.ok(b.files.includes('profile.js'));
  }
  assert.notEqual(stable.windows.nsis.shortcutName, dev.windows.nsis.shortcutName);
  assert.notEqual(stable.windows.artifactName, dev.windows.artifactName);
  assert.notEqual(stable.pkg.build.linux.executableName, dev.pkg.build.linux.executableName);
  assert.notEqual(stable.pkg.build.linux.desktop.entry.StartupWMClass, dev.pkg.build.linux.desktop.entry.StartupWMClass);
  assert.throws(() => configure(pkg, windows, 'v99.0.0'), /does not match/);
  assert.throws(() => configure(pkg, windows, `v${base}-beta.1`), /does not match/);
});
