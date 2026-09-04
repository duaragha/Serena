'use strict';

const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');
const test = require('node:test');
const {
  backendLaunch,
  findFreePort,
  normalizeExternalUrl,
  waitForChildExit,
  waitForHealth,
} = require('../runtime');

const desktopDir = path.resolve(__dirname, '..');

test('findFreePort returns a reusable loopback port', async (t) => {
  let port;
  try {
    port = await findFreePort();
  } catch (error) {
    if (error.code === 'EPERM') {
      t.skip('sandbox forbids opening loopback listeners');
      return;
    }
    throw error;
  }
  assert.ok(Number.isInteger(port) && port > 0);
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', resolve);
  });
  await new Promise((resolve) => server.close(resolve));
});

test('waitForHealth accepts only a valid sidecar health payload', async (t) => {
  const server = http.createServer((_request, response) => {
    response.setHeader('content-type', 'application/json');
    response.end(JSON.stringify({ ok: true, pid: 1234 }));
  });
  try {
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(0, '127.0.0.1', resolve);
    });
  } catch (error) {
    if (error.code === 'EPERM') {
      t.skip('sandbox forbids opening loopback listeners');
      return;
    }
    throw error;
  }
  const port = server.address().port;
  const child = new EventEmitter();
  const health = await waitForHealth(child, `http://127.0.0.1:${port}/api/health`, {
    timeoutMs: 1000,
    intervalMs: 10,
  });
  assert.deepEqual(health, { ok: true, pid: 1234 });
  await new Promise((resolve) => server.close(resolve));
});

test('external URL normalization rejects privileged protocols', () => {
  assert.equal(normalizeExternalUrl('https://example.com/docs'), 'https://example.com/docs');
  assert.equal(normalizeExternalUrl('http://127.0.0.1:1234/a'), 'http://127.0.0.1:1234/a');
  for (const candidate of ['javascript:alert(1)', 'file:///tmp/secret', 'data:text/html,x', 'https://bad host']) {
    assert.equal(normalizeExternalUrl(candidate), null);
  }
});

test('waitForChildExit distinguishes graceful exit from timeout', async () => {
  const graceful = new EventEmitter();
  graceful.exitCode = null;
  graceful.signalCode = null;
  setImmediate(() => {
    graceful.exitCode = 0;
    graceful.emit('exit', 0, null);
  });
  assert.equal(await waitForChildExit(graceful, 100), true);

  const stuck = new EventEmitter();
  stuck.exitCode = null;
  stuck.signalCode = null;
  assert.equal(await waitForChildExit(stuck, 5), false);
  assert.equal(stuck.listenerCount('exit'), 0);
});

test('backend launch uses the repo venv in dev and bundled sidecar in production', () => {
  const appDir = path.join(path.parse(desktopDir).root, 'repo', 'apps', 'desktop');
  const resourcesPath = path.join(path.parse(desktopDir).root, 'app', 'resources');
  const repoRoot = path.resolve(appDir, '..', '..');
  const linuxDev = backendLaunch({
    isPackaged: false,
    appDir,
    resourcesPath,
    port: 43210,
    platform: 'linux',
  });
  assert.equal(linuxDev.command, path.join(repoRoot, '.venv', 'bin', 'python'));
  assert.equal(linuxDev.args[0], path.join(appDir, 'sidecar.py'));
  assert.equal(linuxDev.cwd, repoRoot);

  const windowsDev = backendLaunch({
    isPackaged: false,
    appDir,
    resourcesPath,
    port: 43210,
    platform: 'win32',
  });
  assert.equal(windowsDev.command, path.join(repoRoot, '.venv', 'Scripts', 'python.exe'));
  assert.equal(windowsDev.args[0], path.join(appDir, 'sidecar.py'));
  assert.equal(windowsDev.cwd, repoRoot);

  const packaged = backendLaunch({
    isPackaged: true,
    appDir: path.join(resourcesPath, 'app.asar'),
    resourcesPath,
    port: 43210,
  });
  assert.equal(packaged.command, path.join(resourcesPath, 'sidecar', 'serena-web-sidecar'));
  assert.equal(packaged.cwd, resourcesPath);
});

test('main and preload retain the required Electron security contract', () => {
  const main = fs.readFileSync(path.join(desktopDir, 'main.js'), 'utf8');
  const preload = fs.readFileSync(path.join(desktopDir, 'preload.js'), 'utf8');
  const packageJson = JSON.parse(fs.readFileSync(path.join(desktopDir, 'package.json'), 'utf8'));

  assert.match(main, /requestSingleInstanceLock\(\)/);
  assert.match(main, /SMOKE_TEST[\s\S]*setPath\('userData',[\s\S]*-smoke-/);
  assert.match(main, /contextIsolation:\s*true/);
  assert.match(main, /nodeIntegration:\s*false/);
  assert.match(main, /sandbox:\s*true/);
  assert.match(main, /setWindowOpenHandler/);
  assert.match(main, /new Tray\(/);
  assert.match(preload, /contextBridge\.exposeInMainWorld\('serenaDesktop'/);
  assert.match(preload, /getVersion/);
  assert.match(preload, /notify/);
  assert.match(preload, /openExternal/);
  assert.match(preload, /pickFolder/);
  assert.match(main, /desktop:pick-folder/);
  assert.ok(packageJson.build.files.includes('folder-picker.js'));
  assert.deepEqual(packageJson.build.linux.target, ['AppImage', 'deb']);
});

test('linux sidecar packaging resolves the repository above apps/desktop', () => {
  const script = fs.readFileSync(path.join(desktopDir, 'scripts', 'build-sidecar.sh'), 'utf8');
  assert.match(script, /repo_root="\$\(cd "\$desktop_dir\/\.\.\/\.\." && pwd\)"/);
});

test('the AppImage smoke run is isolated from the installed app', () => {
  const smoke = fs.readFileSync(path.join(desktopDir, 'tests', 'smoke-appimage.js'), 'utf8');
  assert.match(smoke, /SERENA_DESKTOP_SHARE_BACKEND:\s*'0'/);
  assert.match(smoke, /--smoke-test/);
});
