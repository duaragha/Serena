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

const { app, dialog } = require('electron');

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

/** The whole flow behind one menu item, as a native dialog. */
async function checkInteractively(parentWindow) {
  const outcome = await check();
  const parent = parentWindow && !parentWindow.isDestroyed() ? parentWindow : undefined;

  if (outcome.state === 'available') {
    const { response } = await dialog.showMessageBox(parent, {
      type: 'info',
      buttons: ['Download', 'Not now'],
      defaultId: 0,
      cancelId: 1,
      title: 'Update available',
      message: `Serena ${outcome.remoteVersion} is available.`,
      detail: `You are on ${outcome.version} (${outcome.platform}). The download runs in the background; Serena restarts into the new version when you choose to.`,
    });
    if (response !== 0) return outcome;

    try {
      await download();
    } catch (error) {
      await dialog.showMessageBox(parent, {
        type: 'error',
        title: 'Download failed',
        message: 'The update could not be downloaded.',
        detail: String((error && error.message) || error).slice(0, 500),
      });
      return { ...outcome, state: 'error' };
    }

    const { response: restart } = await dialog.showMessageBox(parent, {
      type: 'info',
      buttons: ['Restart now', 'Later'],
      defaultId: 0,
      cancelId: 1,
      title: 'Update ready',
      message: `Serena ${outcome.remoteVersion} is ready to install.`,
      detail: 'Restarting closes every open pane. Agents keep their sessions and resume where they left off.',
    });
    if (restart === 0) install();
    return { ...outcome, state: 'downloaded' };
  }

  const copy = {
    current: {
      type: 'info',
      message: `Serena ${outcome.version} is up to date.`,
      detail: `Running the ${outcome.platform} build.`,
    },
    'none-published': {
      type: 'info',
      message: 'No releases have been published yet.',
      detail: `Serena ${outcome.version} (${outcome.platform}) is the local build. Publish a tagged release and this will start finding updates.`,
    },
    unsupported: {
      type: 'info',
      message: 'This build cannot update itself.',
      detail: outcome.reason,
    },
    error: {
      type: 'error',
      message: 'Could not check for updates.',
      detail: outcome.reason || 'Unknown error.',
    },
  }[outcome.state] || {
    type: 'info',
    message: `Serena ${outcome.version}`,
    detail: outcome.state,
  };

  await dialog.showMessageBox(parent, { title: 'Check for updates', buttons: ['OK'], ...copy });
  return outcome;
}

async function showAbout(parentWindow) {
  const facts = describe();
  const parent = parentWindow && !parentWindow.isDestroyed() ? parentWindow : undefined;
  await dialog.showMessageBox(parent, {
    type: 'info',
    title: 'About Serena',
    message: `Serena ${facts.version}`,
    detail: [
      `${facts.platform} build${facts.packaged ? '' : ' (development)'}`,
      `Electron ${facts.electron} · Chromium ${facts.chrome} · Node ${facts.node}`,
    ].join('\n'),
    buttons: ['OK'],
  });
  return facts;
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
