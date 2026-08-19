'use strict';

const { spawn } = require('node:child_process');
const path = require('node:path');
const {
  app,
  BrowserWindow,
  ipcMain,
  Menu,
  nativeImage,
  Notification,
  shell,
  Tray,
} = require('electron');
const {
  LOOPBACK_HOST,
  backendLaunch,
  findExistingBackend,
  findFreePort,
  normalizeExternalUrl,
  terminateProcessTree,
  waitForHealth,
} = require('./runtime');

const SMOKE_TEST = process.argv.includes('--smoke-test');
const BACKEND_STABLE_MS = 30000;
// A dev run is a separate app: it serves this checkout on its own port and is
// expected to sit beside the installed build while the UI is being worked on.
// Sharing the packaged app's lock made `--dev` exit instantly with no output,
// which reads exactly like a broken launcher.
const isDevRun = process.argv.includes('--dev');
if (isDevRun) app.setPath('userData', `${app.getPath('userData')}-dev`);
const gotSingleInstanceLock = app.requestSingleInstanceLock();

let backend = null;
let backendStartedAt = 0;
let backendUrl = null;
let mainWindow = null;
let restartAttempt = 0;
let restartTimer = null;
let startingBackend = false;
let tray = null;
let quitting = false;
let quitCleanupStarted = false;

if (!gotSingleInstanceLock) {
  console.error('[desktop] another Serena instance owns this profile; exiting');
  app.quit();
}

function backendHealthUrl(url) {
  return `${url}/api/health`;
}

function writeBackendLog(stream, chunk) {
  const output = stream === 'stderr' ? process.stderr : process.stdout;
  output.write(`[backend:${stream}] ${chunk}`);
}

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

async function openExternal(value) {
  const safeUrl = normalizeExternalUrl(value);
  if (!safeUrl) throw new Error('only http and https URLs can be opened externally');
  await shell.openExternal(safeUrl);
  return true;
}

function senderIsTrusted(event) {
  if (!backendUrl) return false;
  try {
    return new URL(event.senderFrame.url).origin === new URL(backendUrl).origin;
  } catch {
    return false;
  }
}

function registerDesktopIpc() {
  ipcMain.handle('desktop:get-version', (event) => {
    if (!senderIsTrusted(event)) throw new Error('untrusted IPC sender');
    return app.getVersion();
  });
  ipcMain.handle('desktop:open-external', async (event, value) => {
    if (!senderIsTrusted(event)) throw new Error('untrusted IPC sender');
    return openExternal(value);
  });
  ipcMain.handle('desktop:notify', (event, value) => {
    if (!senderIsTrusted(event)) throw new Error('untrusted IPC sender');
    if (!value || typeof value !== 'object') throw new TypeError('notification options are required');
    const title = String(value.title || 'Serena').slice(0, 120);
    const body = String(value.body || '').slice(0, 2000);
    if (!body) throw new TypeError('notification body is required');
    if (!Notification.isSupported()) return false;
    new Notification({ title, body, silent: Boolean(value.silent) }).show();
    return true;
  });
}

function createWindow(url) {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    show: false,
    title: 'Serena',
    // Without this the running window carries Electron's default icon on
    // Linux: the desktop entry's icon only applies once the window manager
    // matches StartupWMClass, and it never matched this app's.
    ...(brandIcon() ? { icon: brandIcon() } : {}),
    backgroundColor: '#0d1117',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  mainWindow.webContents.setWindowOpenHandler(({ url: requestedUrl }) => {
    openExternal(requestedUrl).catch((error) => {
      console.error('[desktop] refused external URL:', error.message);
    });
    return { action: 'deny' };
  });
  mainWindow.webContents.on('will-navigate', (event, destination) => {
    try {
      if (backendUrl && new URL(destination).origin === new URL(backendUrl).origin) return;
    } catch {
      // Invalid destinations are denied below.
    }
    event.preventDefault();
  });
  mainWindow.once('ready-to-show', () => mainWindow && mainWindow.show());
  mainWindow.on('close', (event) => {
    if (!quitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
  mainWindow.loadURL(url);
}

/*
 * The one logo, loaded once. It ships inside the bundle (see `files` in
 * package.json) so this resolves identically from source and from the
 * packaged AppImage.
 */
let brandIconCache = null;
function brandIcon() {
  if (brandIconCache) return brandIconCache;
  const image = nativeImage.createFromPath(path.join(__dirname, 'build', 'icon.png'));
  brandIconCache = image.isEmpty() ? null : image;
  return brandIconCache;
}

function trayIcon() {
  const icon = brandIcon();
  if (icon) return icon.resize({ width: 22, height: 22 });
  // Only if the asset is somehow missing — a shape, never a different brand.
  const svg = [
    '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32">',
    '<circle cx="16" cy="16" r="14" fill="#f2809f"/>',
    '<circle cx="16" cy="16" r="6" fill="#0d1117"/>',
    '</svg>',
  ].join('');
  return nativeImage.createFromDataURL(
    `data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}`,
  ).resize({ width: 22, height: 22 });
}

function updateTrayMenu() {
  if (!tray) return;
  tray.setContextMenu(Menu.buildFromTemplate([
    {
      label: mainWindow && mainWindow.isVisible() ? 'Hide Serena' : 'Show Serena',
      click: () => {
        if (!mainWindow) return;
        if (mainWindow.isVisible()) mainWindow.hide();
        else showMainWindow();
        updateTrayMenu();
      },
    },
    { type: 'separator' },
    {
      label: 'Quit',
      click: () => app.quit(),
    },
  ]));
}

function createTray() {
  tray = new Tray(trayIcon());
  tray.setToolTip('Serena');
  tray.on('click', () => {
    if (mainWindow && mainWindow.isVisible()) mainWindow.hide();
    else showMainWindow();
    updateTrayMenu();
  });
  updateTrayMenu();
}

function restartDelayMs() {
  return Math.min(10000, 250 * (2 ** Math.min(restartAttempt, 6)));
}

function scheduleBackendRestart(reason) {
  if (quitting || restartTimer) return;
  const delay = restartDelayMs();
  restartAttempt += 1;
  console.error(`[desktop] backend unavailable (${reason}); restarting in ${delay}ms`);
  restartTimer = setTimeout(() => {
    restartTimer = null;
    startBackend().catch((error) => scheduleBackendRestart(error.message));
  }, delay);
}

function handleBackendExit(child, code, signal) {
  if (backend !== child) return;
  backend = null;
  backendUrl = null;
  if (quitting) return;
  if (Date.now() - backendStartedAt >= BACKEND_STABLE_MS) restartAttempt = 0;
  scheduleBackendRestart(`exit code=${code}, signal=${signal}`);
}

async function startBackend() {
  if (quitting || startingBackend || backend) return;
  startingBackend = true;
  let child = null;
  try {
    // Attach to the persistent mobile_host server when it is already up
    // rather than running a second copy of the same Flask UI.
    const shared = await findExistingBackend({
      enabled: process.env.SERENA_DESKTOP_SHARE_BACKEND !== '0',
    });
    if (shared && !quitting) {
      backend = null;
      backendUrl = shared.url;
      console.log(`SERENA_BACKEND_SHARED ${shared.url} pid=${shared.pid}`);
      if (!SMOKE_TEST) {
        if (!mainWindow) createWindow(shared.url);
        else mainWindow.loadURL(shared.url);
        if (!tray) createTray();
      }
      return;
    }
    const port = await findFreePort();
    const url = `http://${LOOPBACK_HOST}:${port}`;
    const launch = backendLaunch({
      isPackaged: app.isPackaged,
      appDir: __dirname,
      resourcesPath: process.resourcesPath,
      port,
    });
    child = spawn(launch.command, launch.args, {
      cwd: launch.cwd,
      detached: process.platform !== 'win32',
      env: {
        ...process.env,
        PYTHONUNBUFFERED: '1',
        SERENA_CALL_RUNTIME: 'lazy',
      },
      shell: false,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
    backend = child;
    backendStartedAt = Date.now();
    child.stdout.on('data', (chunk) => writeBackendLog('stdout', chunk));
    child.stderr.on('data', (chunk) => writeBackendLog('stderr', chunk));
    child.once('exit', (code, signal) => handleBackendExit(child, code, signal));

    const health = await waitForHealth(child, backendHealthUrl(url));
    if (backend !== child || quitting) return;
    backendUrl = url;
    console.log(`SERENA_BACKEND_READY ${url} pid=${health.pid}`);
    if (!SMOKE_TEST) {
      if (!mainWindow) createWindow(url);
      else mainWindow.loadURL(url);
      if (!tray) createTray();
    }
  } catch (error) {
    if (child && backend === child) {
      backend = null;
      await terminateProcessTree(child);
    }
    if (!quitting) scheduleBackendRestart(error.message);
    throw error;
  } finally {
    startingBackend = false;
  }
}

async function stopBackend() {
  if (restartTimer) {
    clearTimeout(restartTimer);
    restartTimer = null;
  }
  // `backend` is null when we attached to mobile_host: that server belongs
  // to systemd and the phone, so quitting the app must leave it running.
  const child = backend;
  backend = null;
  backendUrl = null;
  if (child) await terminateProcessTree(child);
}

app.on('second-instance', () => showMainWindow());
app.on('window-all-closed', () => {
  // Closing the only window leaves Serena available from the tray.
});
app.on('before-quit', (event) => {
  quitting = true;
  if (quitCleanupStarted) return;
  event.preventDefault();
  quitCleanupStarted = true;
  stopBackend()
    .catch((error) => console.error('[desktop] backend shutdown failed:', error))
    .finally(() => app.quit());
});

process.on('SIGTERM', () => app.quit());
process.on('SIGINT', () => app.quit());

if (gotSingleInstanceLock) {
  app.whenReady().then(() => {
    registerDesktopIpc();
    startBackend().catch((error) => {
      console.error('[desktop] initial backend start failed:', error.message);
    });
  });
}
