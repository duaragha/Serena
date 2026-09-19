// Serena's orb as its own window.
//
// The window owns nothing but the picture. Everything real -- the microphone,
// Scribe, the brain, her voice -- lives in bridge.py, which this process
// starts on a private port and stops on the way out. That split is deliberate
// and was learned the hard way: the old dot overlay spawned the voice client
// as its own child, so when Electron died on a missing X server it took her
// ears down with it and the laptop was deaf for five days.
//
// Nothing here is wanted by anything else, so the worst this window can do by
// crashing is stop drawing.

const { app, BrowserWindow, Tray, Menu, shell } = require('electron');
const { spawn } = require('node:child_process');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');
const fs = require('node:fs');

const HERE = __dirname;
const REPO = path.resolve(HERE, '..', '..');

let win = null;
let tray = null;
let bridge = null;
let bridgePort = 0;
let quitting = false;

/** The interpreter that actually has the voice stack installed. */
function pythonBin() {
  const candidates = process.platform === 'win32'
    ? [path.join(REPO, '.venv', 'Scripts', 'python.exe')]
    : [path.join(REPO, '.venv', 'bin', 'python')];
  for (const bin of candidates) if (fs.existsSync(bin)) return bin;
  // Falling back to a bare python3 usually means no numpy, which is the exact
  // failure that made the packaged sidecar mute. Say so rather than guess.
  console.warn('[orb] repo venv not found; falling back to python3');
  return process.platform === 'win32' ? 'python' : 'python3';
}

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.unref();
    probe.on('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

function reachable(port) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, path: '/', timeout: 1000 }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

async function waitFor(port, seconds = 40) {
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    if (await reachable(port)) return true;
    await new Promise((r) => setTimeout(r, 250));
  }
  return false;
}

async function startBridge() {
  bridgePort = await freePort();
  bridge = spawn(pythonBin(), [path.join(HERE, 'bridge.py'), String(bridgePort)], {
    cwd: REPO,
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  bridge.stdout.on('data', (b) => process.stdout.write(`[bridge] ${b}`));
  bridge.stderr.on('data', (b) => process.stderr.write(`[bridge] ${b}`));
  bridge.on('exit', (code) => {
    bridge = null;
    if (!quitting) console.error(`[orb] bridge exited with ${code}`);
  });
  if (!await waitFor(bridgePort)) {
    throw new Error('bridge did not come up; is the repo venv built?');
  }
  return bridgePort;
}

function stopBridge() {
  if (!bridge) return;
  const child = bridge;
  bridge = null;
  try { child.kill('SIGTERM'); } catch (_) { /* already gone */ }
  setTimeout(() => { try { child.kill('SIGKILL'); } catch (_) {} }, 3000).unref?.();
}

function createWindow(port) {
  win = new BrowserWindow({
    width: 620,
    height: 760,
    minWidth: 360,
    minHeight: 460,
    backgroundColor: '#05040a',
    title: 'Serena',
    autoHideMenuBar: true,
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      // The page is served from 127.0.0.1, so it is already a secure context;
      // the microphone needs nothing beyond the permission grant below.
    },
  });

  // Electron denies getUserMedia unless asked. Only media, only our own page.
  const session = win.webContents.session;
  session.setPermissionRequestHandler((contents, permission, callback) => {
    const url = contents.getURL();
    const ours = url.startsWith(`http://127.0.0.1:${port}`);
    callback(ours && (permission === 'media' || permission === 'audioCapture'));
  });

  win.once('ready-to-show', () => win.show());
  win.on('closed', () => { win = null; });

  // Links open in the real browser rather than replacing the orb.
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  win.loadURL(`http://127.0.0.1:${port}/`);
}

function createTray() {
  const icon = path.join(HERE, 'assets', 'tray.png');
  if (!fs.existsSync(icon)) return;
  tray = new Tray(icon);
  tray.setToolTip('Serena');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Show', click: () => { if (win) { win.show(); win.focus(); } } },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() },
  ]));
  tray.on('click', () => { if (win) { win.isVisible() ? win.hide() : win.show(); } });
}

// One orb. A second instance would fight the first for the microphone.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (win) { if (win.isMinimized()) win.restore(); win.show(); win.focus(); }
  });

  app.whenReady().then(async () => {
    try {
      const port = await startBridge();
      createWindow(port);
      createTray();
    } catch (err) {
      console.error(`[orb] ${err.message}`);
      app.quit();
    }
  });

  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', () => { quitting = true; stopBridge(); });
  // A crash or a kill must not leave the bridge holding the microphone.
  process.on('exit', stopBridge);
  for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => app.quit());
}
