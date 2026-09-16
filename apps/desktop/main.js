'use strict';

const { spawn } = require('node:child_process');
const path = require('node:path');
const {
  app,
  BrowserWindow,
  dialog,
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
  findFreePort,
  normalizeExternalUrl,
  terminateProcessTree,
  waitForHealth,
} = require('./runtime');
const appMenu = require('./menu');
const updates = require('./updates');
const releases = require('./releases');
const logging = require('./logging');
const backendControl = require('./backend-control');
const { desktopProfile, backendEnvironment } = require('./profile');
const folderPicker = require('./folder-picker');

const SMOKE_TEST = process.argv.includes('--smoke-test');
const BACKEND_STABLE_MS = 30000;
// A freshly installed frozen sidecar can spend well over 30 seconds in the
// first Windows Defender scan. Killing it at the generic timeout only repeats
// that cold start and delays the window further; once warmed, startup remains
// fast. Linux/dev launches keep the tighter failure signal.
const BACKEND_READY_TIMEOUT_MS = process.platform === 'win32' && app.isPackaged
  ? 90000
  : 30000;
// A dev run is a separate app: it serves this checkout on its own port and is
// expected to sit beside the installed build while the UI is being worked on.
// Sharing the packaged app's lock made `--dev` exit instantly with no output,
// which reads exactly like a broken launcher.
const profile = desktopProfile(app.getVersion(), { packaged: app.isPackaged, argv: process.argv });
app.setName(profile.name);
// Stable's new profile can coexist with an active pre-split window during the
// first upgrade. Neither edition borrows the old window's instance lock.
app.setPath('userData', path.join(app.getPath('appData'), `serena-desktop-${profile.channel}`));
if (SMOKE_TEST) {
  // Release verification must be able to run beside the installed app without
  // stealing its single-instance lock or touching its real profile.
  app.setPath('userData', `${app.getPath('userData')}-smoke-${process.pid}`);
}
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
  logging.backend(stream, chunk);
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

function registerUpdateIpc() {
  // The renderer gets the same three operations the menu uses, so an in-page
  // About panel and the native menu can never disagree about state.
  ipcMain.handle('updates:describe', (event) => {
    if (!senderIsTrusted(event)) throw new Error('untrusted IPC sender');
    return updates.describe();
  });
  ipcMain.handle('updates:check', async (event) => {
    if (!senderIsTrusted(event)) throw new Error('untrusted IPC sender');
    return updates.check();
  });
  ipcMain.handle('updates:download', async (event) => {
    if (!senderIsTrusted(event)) throw new Error('untrusted IPC sender');
    return updates.download((progress) => {
      if (!event.sender.isDestroyed()) event.sender.send('updates:progress', progress);
    });
  });
  ipcMain.handle('updates:install', (event) => {
    if (!senderIsTrusted(event)) throw new Error('untrusted IPC sender');
    return updates.install();
  });
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
  ipcMain.handle('desktop:pick-folder', async (event, value) => {
    if (!senderIsTrusted(event)) throw new Error('untrusted IPC sender');
    const owner = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
    return folderPicker.chooseFolder(dialog, owner, value);
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
    title: profile.name,
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
  return mainWindow.loadURL(url);
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
      label: `${mainWindow && mainWindow.isVisible() ? 'Hide' : 'Show'} ${profile.name}`,
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
  tray.setToolTip(profile.name);
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
  logging.note(`backend unavailable (${reason}); restart #${restartAttempt} in ${delay}ms`);
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
  logging.note(`backend exited after ${Date.now() - backendStartedAt}ms: code=${code} signal=${signal}`);
  scheduleBackendRestart(`exit code=${code}, signal=${signal}`);
}

async function startBackend() {
  if (quitting || startingBackend || backend) return;
  startingBackend = true;
  let child = null;
  try {
    // Every desktop edition owns its bundled backend. Never attach to or
    // replace the phone host, another release, or the other edition.
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
        ...launch.env,
        ...backendEnvironment(profile, app.getPath('home')),
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

    const health = await waitForHealth(child, backendHealthUrl(url), {
      timeoutMs: BACKEND_READY_TIMEOUT_MS,
    });
    if (health.desktop?.channel !== profile.channel
      || health.desktop?.version !== profile.version
      || Boolean(health.capabilities?.structuredWorkspace) !== profile.structured) {
      throw new Error('Bundled backend identity does not match this desktop edition');
    }
    if (backend !== child || quitting) return;
    backendUrl = url;
    logging.note(`backend ready at ${url} pid=${health.pid}`);
    console.log(`SERENA_BACKEND_READY ${url} pid=${health.pid}`);
    if (!SMOKE_TEST) {
      if (!mainWindow) await createWindow(url);
      else await backendControl.loadBackendWindow(mainWindow, url);
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
  // Only this edition's child can be stopped here.
  const child = backend;
  backend = null;
  backendUrl = null;
  if (child) await terminateProcessTree(child, 25000);
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
    // Before anything else: a crash with no log is what made the last one take
    // an afternoon to find.
    logging.configure(() => app.getPath('userData'));
    logging.note(`Serena ${app.getVersion()} starting on ${process.platform}`);
    registerDesktopIpc();
    registerUpdateIpc();
    // The menu needs a live window reference, not the one that existed at
    // startup: the window is recreated when reopened from the tray.
    appMenu.install(() => mainWindow);
    // Say when each platform's build lands. A tagged release publishes Linux
    // first and Windows minutes later, so both are worth hearing about.
    releases.start();
    startBackend().catch((error) => {
      console.error('[desktop] initial backend start failed:', error.message);
    });
  });
}
