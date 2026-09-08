'use strict';

/**
 * In-app updates for both platforms.
 *
 * Deliberately manual: Serena IS the terminal Raghav works in, so an update
 * that lands by itself mid-turn is a worse failure than a stale version. The
 * user asks, we check, and the swap happens on quit.
 *
 * The security model is electron-updater's: the feed is served over HTTPS from
 * GitHub Releases and every artifact is checked against the SHA512 recorded in
 * the channel file. On Windows a signed build additionally proves the update
 * carries the same publisher as the installed app. There is no paid
 * certificate here on purpose; a self-signed key gives the same
 * same-key-as-install guarantee for a two-machine personal tool, and the only
 * thing forfeited is the SmartScreen prompt on a manual install.
 */

const { app } = require('electron');
const fs = require('node:fs');
const path = require('node:path');

const FEED_HOST = 'github.com';

/**
 * The release feed.
 *
 * duaragha/Serena is public, which is the whole reason this file carries no
 * credential. A private feed would have to be read with a GitHub token, and a
 * token has to live either inside the artifact, where it cannot be rotated, or
 * in a file on every machine. Public releases are fetched anonymously.
 */
const FEED = Object.freeze({
  provider: 'github',
  owner: 'duaragha',
  repo: 'Serena',
});

let updater = null;
let inFlight = null;
let downloaded = null;

/** electron-updater is optional at runtime so a broken install still starts. */
function getUpdater() {
  if (updater !== null) return updater;
  try {
    ({ autoUpdater: updater } = require('electron-updater'));
  } catch (error) {
    console.error('[updates] electron-updater is unavailable:', error.message);
    updater = false;
    return false;
  }
  updater.autoDownload = false;
  updater.autoInstallOnAppQuit = true;
  updater.logger = console;
  updater.on('error', (error) => {
    console.error('[updates] updater error:', error && error.message);
  });
  updater.on('update-downloaded', (info) => {
    downloaded = info;
  });
  return updater;
}

function platformLabel() {
  if (process.platform === 'win32') return 'Windows';
  if (process.platform === 'darwin') return 'macOS';
  return 'Linux';
}

/**
 * Why an update cannot be applied here, or null when it can.
 *
 * A dev run has no packaged artifact to replace, and a Linux AppImage that was
 * extracted rather than launched as a file has nothing to swap: electron-updater
 * rewrites the AppImage in place, so it needs the real path.
 */
function updateBlocker() {
  if (!app.isPackaged) {
    return 'This is a development build, so there is nothing to update. Updates apply to the installed app.';
  }
  if (process.platform === 'linux' && !process.env.APPIMAGE) {
    return 'This copy was not launched from the AppImage file, so it cannot replace itself. Run the AppImage directly to update in place.';
  }
  return null;
}

function describe() {
  return {
    version: app.getVersion(),
    platform: platformLabel(),
    packaged: app.isPackaged,
    electron: process.versions.electron,
    node: process.versions.node,
    chrome: process.versions.chrome,
    channelHost: FEED_HOST,
    feed: `${FEED.owner}/${FEED.repo}`,
    blocker: updateBlocker(),
    downloadedVersion: downloaded && downloaded.version,
  };
}

/**
 * Ask the feed whether a newer version exists.
 *
 * Returns a plain object rather than throwing so both the menu and the
 * renderer can render the same three outcomes: available, current, failed.
 */
async function check({ silent = false } = {}) {
  const blocker = updateBlocker();
  if (blocker) return { state: 'unsupported', reason: blocker, ...describe() };

  const auto = getUpdater();
  if (!auto) {
    return { state: 'error', reason: 'The updater component is missing from this build.', ...describe() };
  }
  if (inFlight) return inFlight;

  inFlight = (async () => {
    try {
      const result = await auto.checkForUpdates();
      const remote = result && result.updateInfo && result.updateInfo.version;
      if (remote && remote !== app.getVersion()) {
        return { state: 'available', remoteVersion: remote, ...describe() };
      }
      return { state: 'current', ...describe() };
    } catch (error) {
      // A missing feed is the normal state before the first release, and it
      // should read as "nothing published yet" rather than as a fault.
      const message = String((error && error.message) || error);
      const missing = /404|ERR_UPDATER_CHANNEL_FILE_NOT_FOUND|No published versions/i.test(message);
      return {
        state: missing ? 'none-published' : 'error',
        reason: message.slice(0, 500),
        ...describe(),
      };
    } finally {
      inFlight = null;
    }
  })();

  const outcome = await inFlight;
  if (!silent) console.log(`[updates] check -> ${outcome.state}`);
  return outcome;
}

async function download(onProgress) {
  const auto = getUpdater();
  if (!auto) throw new Error('The updater component is missing from this build.');
  const listener = (progress) => {
    if (typeof onProgress === 'function') {
      onProgress({
        percent: Math.round(progress.percent || 0),
        transferred: progress.transferred,
        total: progress.total,
        bytesPerSecond: progress.bytesPerSecond,
      });
    }
  };
  auto.on('download-progress', listener);
  try {
    await auto.downloadUpdate();
    return { state: 'downloaded', version: downloaded && downloaded.version };
  } finally {
    auto.off('download-progress', listener);
  }
}

/**
 * Restart into the new version.
 *
 * isSilent=false on Windows so the installer is visible: an unattended
 * reinstall that fails silently is how you end up with no working app and no
 * idea why.
 */
function install() {
  const auto = getUpdater();
  if (!auto) throw new Error('The updater component is missing from this build.');
  if (!downloaded) throw new Error('No update has been downloaded yet.');
  setImmediate(() => auto.quitAndInstall(false, true));
  return true;
}

/** Keep update interaction inside the themed renderer, never an OS dialog. */
async function openPanel(parentWindow, action) {
  if (!parentWindow || parentWindow.isDestroyed()) throw new Error('Serena window is unavailable.');
  const contents = parentWindow.webContents;
  if (!contents || contents.isDestroyed()) throw new Error('Serena page is unavailable.');
  if (contents.isLoadingMainFrame()) {
    await new Promise(resolve => contents.once('did-stop-loading', resolve));
  }
  if (contents.isDestroyed()) throw new Error('Serena page is unavailable.');
  // Ship with the shell: a shared backend may still serve an older page.
  await contents.executeJavaScript(fs.readFileSync(path.join(__dirname, 'about-panel.js'), 'utf8'));
  if (!contents.isDestroyed()) contents.send('updates:open', { action });
  return describe();
}

async function checkInteractively(parentWindow) {
  return openPanel(parentWindow, 'check');
}

async function showAbout(parentWindow) {
  return openPanel(parentWindow, 'about');
}

module.exports = {
  FEED,
  check,
  checkInteractively,
  describe,
  download,
  install,
  platformLabel,
  showAbout,
  updateBlocker,
};
