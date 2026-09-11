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
  assert.equal(packaged.env.SERENA_WORKSPACE_RUNTIME_ROOT, path.join(resourcesPath, 'runtimes', 'claude-sdk'));
  assert.equal(packaged.env.SERENA_WORKSPACE_NODE, process.execPath);
  assert.equal(packaged.env.SERENA_WORKSPACE_NODE_MODE, 'electron');
  assert.equal(packaged.env.ELECTRON_RUN_AS_NODE, undefined);
  assert.equal(linuxDev.env.SERENA_WORKSPACE_RUNTIME_ROOT, path.join(repoRoot, 'runtimes', 'claude-sdk'));
  assert.equal(windowsDev.env.SERENA_WORKSPACE_NODE_MODE, 'electron');
});

test('main and preload retain the required Electron security contract', () => {
  const main = fs.readFileSync(path.join(desktopDir, 'main.js'), 'utf8');
  const preload = fs.readFileSync(path.join(desktopDir, 'preload.js'), 'utf8');
  const packageJson = JSON.parse(fs.readFileSync(path.join(desktopDir, 'package.json'), 'utf8'));

  assert.match(main, /requestSingleInstanceLock\(\)/);
  assert.match(main, /\.\.\.launch\.env/);
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

test('both desktop builds provision SDK resources and native worker modules', () => {
  const packageJson=JSON.parse(fs.readFileSync(path.join(desktopDir,'package.json'),'utf8'));
  const sdk=packageJson.build.extraResources.find(resource=>resource.to==='runtimes/claude-sdk');
  assert.equal(sdk.from,'../../runtimes/claude-sdk');
  assert.deepEqual(sdk.filter,['package.json']);
  const modules=packageJson.build.extraResources.find(resource=>resource.to==='runtimes/claude-sdk/node_modules');
  assert.equal(modules.from,'../../runtimes/claude-sdk/node_modules');
  assert.ok(modules.filter.includes('**/*'));
  assert.equal(packageJson.build.afterPack,'./scripts/verify-packed-workspace.cjs');
  const linux=fs.readFileSync(path.join(desktopDir,'scripts/build-sidecar.sh'),'utf8');
  const windows=fs.readFileSync(path.join(desktopDir,'windows/build-win.ps1'),'utf8');
  const spec=fs.readFileSync(path.join(desktopDir,'windows/sidecar-win.spec'),'utf8');
  const builder=fs.readFileSync(path.join(desktopDir,'windows/electron-builder.win.yml'),'utf8');
  assert.match(linux,/runtimes\/claude-sdk.*ci --ignore-scripts --omit=optional/);
  assert.match(windows,/runtimes\\claude-sdk.*ci --ignore-scripts --omit=optional/);
  assert.match(builder,/to: runtimes\/claude-sdk/);
  for(const filename of ['workspace_claude_worker.mjs','workspace_claude_channel.mjs','workspace_claude_sdk.mjs']) {
    assert.ok(linux.includes(filename));
    assert.ok(spec.includes(filename));
    assert.ok(fs.existsSync(path.resolve(desktopDir,'../../core',filename)));
  }
});

test('linux sidecar packaging resolves the repository above apps/desktop', () => {
  const script = fs.readFileSync(path.join(desktopDir, 'scripts', 'build-sidecar.sh'), 'utf8');
  assert.match(script, /repo_root="\$\(cd "\$desktop_dir\/\.\.\/\.\." && pwd\)"/);
});

test('linux sidecar bundles the opt-in Gemini adapter and checks its dependency', () => {
  const script = fs.readFileSync(path.join(desktopDir, 'scripts', 'build-sidecar.sh'), 'utf8');
  assert.match(script, /--hidden-import core\.workspace_gemini/);
  assert.match(script, /"\$python_bin" -c 'import hjson'/);
  assert.ok(script.indexOf("import hjson") < script.indexOf('rm -rf'));
});

test('the AppImage smoke run is isolated from the installed app', () => {
  const smoke = fs.readFileSync(path.join(desktopDir, 'tests', 'smoke-appimage.js'), 'utf8');
  assert.match(smoke, /SERENA_DESKTOP_SHARE_BACKEND:\s*'0'/);
  assert.match(smoke, /--smoke-test/);
});
