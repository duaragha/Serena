'use strict';

const { spawn } = require('node:child_process');
const http = require('node:http');
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
  findExistingBackend,
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
const isDevRun = process.argv.includes('--dev');
if (isDevRun) {
  app.setPath('userData', `${app.getPath('userData')}-dev`);
} else if (SMOKE_TEST) {
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

let backendFreshness = { reachable: false, stale: false };
let restartingBackend = false;

function getJson(url) {
  return new Promise((resolve, reject) => {
    const request = http.get(url, { timeout: 4000 }, (response) => {
      const chunks = [];
      response.on('data', (chunk) => chunks.push(chunk));
      response.on('end', () => {
        if (response.statusCode !== 200) {
          reject(new Error(`HTTP ${response.statusCode}`));
          return;
        }
        try {
          resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
        } catch (error) {
          reject(error);
        }
      });
    });
    request.once('timeout', () => request.destroy(new Error('timed out')));
    request.once('error', reject);
  });
}

async function refreshBackendFreshness() {
  backendFreshness = await backendControl.freshness(backendUrl, getJson);
  updateTrayMenu();
  return backendFreshness;
}

/**
 * Restart the server this window is talking to.
 *
 * Two shapes. When the shell spawned the backend it owns the process and can
 * simply cycle it. When it attached to the long-lived systemd server it must go
 * through the helper, because a bare systemctl restart is issued from inside
 * the unit being restarted and gets killed partway through.
 */
async function restartBackend() {
  if (restartingBackend) return { ok: false, reason: 'already restarting' };
  restartingBackend = true;
  updateTrayMenu();
  const url = backendUrl;
  try {
    if (backend) {
      logging.note('restarting the backend this shell owns');
      await stopBackend();
      await startBackend();
      return { ok: true, owned: true };
    }

    const root = backendFreshness.sourceRoot || (await refreshBackendFreshness()).sourceRoot;
    const launch = backendControl.sharedRestartCommand(root);
    logging.note(`restarting ${backendControl.SHARED_UNIT} via ${launch.args[0]}`);
    await new Promise((resolve, reject) => {
      const child = spawn(launch.command, launch.args, {
        env: { ...process.env, ...launch.env },
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      });
      child.stdout.on('data', (chunk) => logging.note(`restart: ${chunk}`));
      child.stderr.on('data', (chunk) => logging.note(`restart: ${chunk}`));
      child.once('error', reject);
      child.once('exit', (code) => (code === 0 ? resolve() : reject(new Error(`helper exited ${code}`))));
    });

    const back = await backendControl.waitForBackend(url, getJson);
    if (!back.ok) throw new Error(`server did not come back: ${back.reason}`);
    logging.note(`backend restarted, now pid=${back.pid}`);
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.reload();
    return { ok: true, owned: false, pid: back.pid };
  } catch (error) {
    logging.note(`backend restart failed: ${error.message}`);
    return { ok: false, reason: error.message };
  } finally {
    restartingBackend = false;
    refreshBackendFreshness().catch(() => {});
  }
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
    {
      // Named for what it costs, because it ends open panes and the voice
      // pipeline shares the unit.
      label: restartingBackend
        ? 'Restarting Backend…'
        : (backendControl.staleLabel(backendFreshness) || 'Restart Backend'),
      enabled: !restartingBackend,
      click: () => {
        restartBackend().then((result) => {
          if (!result.ok && result.reason !== 'already restarting') {
            dialog.showMessageBox(mainWindow || undefined, {
              type: 'error',
              title: 'Restart failed',
              message: 'The backend could not be restarted.',
              detail: String(result.reason || '').slice(0, 500),
            }).catch(() => {});
          }
        });
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
    // Attach to the persistent mobile_host server when it is already up
    // rather than running a second copy of the same Flask UI.
    const shared = await findExistingBackend({
      enabled: process.env.SERENA_DESKTOP_SHARE_BACKEND !== '0',
    });
    if (shared && !quitting) {
      backend = null;
      backendUrl = shared.url;
      logging.note(`attached to a shared backend at ${shared.url} pid=${shared.pid}`);
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

    const health = await waitForHealth(child, backendHealthUrl(url), {
      timeoutMs: BACKEND_READY_TIMEOUT_MS,
    });
    if (backend !== child || quitting) return;
    backendUrl = url;
    logging.note(`backend ready at ${url} pid=${health.pid}`);
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
    // Cheap and local. The point is that the menu can say the server is behind
    // before a fix appears not to work.
    setInterval(() => refreshBackendFreshness().catch(() => {}), 60_000).unref();
    startBackend().catch((error) => {
      console.error('[desktop] initial backend start failed:', error.message);
    });
  });
}
