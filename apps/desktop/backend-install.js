'use strict';

/**
 * Make an update carry the server too.
 *
 * The release already contains a complete frozen backend, but the desktop
 * attaches to a long-lived systemd unit that ran the same Flask app out of the
 * git checkout. So installing a new build replaced the window and left the
 * server on whatever code it started with, and the only way to land a fix was
 * to remember to restart it. No other app asks that.
 *
 * The unit cannot point into the AppImage: it mounts at a fresh throwaway path
 * on every launch. So the shipped backend is copied once per version into a
 * stable directory the unit can name, and the unit is restarted only when the
 * bytes it runs actually changed.
 *
 * Everything here is idempotent. It runs on every launch, does nothing on all
 * the launches where the version has not moved, and never leaves a half-copied
 * directory behind for systemd to execute.
 */

const fs = require('node:fs');
const fsp = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');

const run = promisify(execFile);

const UNIT = 'serena-mobile-host.service';
const BINARY = 'serena-web-sidecar';
const STAMP = '.installed-version';

/** Where the unit's ExecStart points, independent of any one build. */
function installRoot(home = os.homedir()) {
  return path.join(home, '.local', 'share', 'serena', 'backend');
}

/** The backend inside the running build, or null when running from a checkout. */
function packagedBackend({ isPackaged, resourcesPath }) {
  if (!isPackaged || !resourcesPath) return null;
  const dir = path.join(resourcesPath, 'sidecar');
  return fs.existsSync(path.join(dir, BINARY)) ? dir : null;
}

function installedVersion(root) {
  try {
    return fs.readFileSync(path.join(root, STAMP), 'utf8').trim();
  } catch {
    return '';
  }
}

/**
 * Put this build's backend where the unit can run it.
 *
 * Copied to a temporary sibling and renamed into place, so a crash or a power
 * cut cannot leave the unit pointing at a directory that is half this version
 * and half the last one.
 */
async function installBackend({ isPackaged, resourcesPath, version, home = os.homedir() }) {
  const source = packagedBackend({ isPackaged, resourcesPath });
  if (!source) return { installed: false, reason: 'not a packaged build' };
  if (!version) return { installed: false, reason: 'no version to stamp' };

  const root = installRoot(home);
  if (installedVersion(root) === version && fs.existsSync(path.join(root, BINARY))) {
    return { installed: false, reason: 'already current', root, version };
  }

  const staging = `${root}.incoming`;
  const retired = `${root}.previous`;
  await fsp.rm(staging, { recursive: true, force: true });
  await fsp.mkdir(path.dirname(root), { recursive: true });
  await fsp.cp(source, staging, { recursive: true, dereference: true });
  // Stamped last, inside the staging copy: a directory carrying this version
  // is therefore a directory that finished copying.
  await fsp.writeFile(path.join(staging, STAMP), `${version}\n`, { mode: 0o600 });
  await fsp.chmod(path.join(staging, BINARY), 0o755);

  await fsp.rm(retired, { recursive: true, force: true });
  if (fs.existsSync(root)) await fsp.rename(root, retired);
  await fsp.rename(staging, root);
  await fsp.rm(retired, { recursive: true, force: true });
  return { installed: true, root, version, executable: path.join(root, BINARY) };
}

/** The unit text that runs the installed backend rather than a checkout. */
function unitFile(executable) {
  return `[Unit]
Description=Serena same-origin mobile chat and call host
After=default.target

[Service]
Type=simple
ExecStart=${executable} --host 127.0.0.1 --port 8767
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
`;
}

/**
 * Point the unit at the installed backend, reporting whether it moved.
 *
 * The file is only rewritten when its ExecStart no longer matches, so a user's
 * drop-ins and an unchanged unit are both left alone.
 */
async function ensureUnit({ executable, home = os.homedir(), runner = run }) {
  const unitPath = path.join(home, '.config', 'systemd', 'user', UNIT);
  const wanted = unitFile(executable);
  let current = '';
  try {
    current = await fsp.readFile(unitPath, 'utf8');
  } catch {
    current = '';
  }
  if (current.includes(`ExecStart=${executable} `)) return { changed: false, unitPath };
  await fsp.mkdir(path.dirname(unitPath), { recursive: true });
  await fsp.writeFile(unitPath, wanted, { mode: 0o644 });
  await runner('systemctl', ['--user', 'daemon-reload']);
  return { changed: true, unitPath };
}

async function restartUnit({ runner = run } = {}) {
  await runner('systemctl', ['--user', 'restart', UNIT]);
}

/**
 * The whole job, for one launch.
 *
 * Returns what it did so the caller can log it. It never throws for an
 * ordinary failure: a backend that could not be swapped is a backend that
 * keeps serving the previous version, which is worse than an update but far
 * better than a window with nothing behind it.
 */
async function syncBackend({ isPackaged, resourcesPath, version, home = os.homedir(), runner = run,
                             log = () => {}, platform = process.platform }) {
  // Only Linux keeps a shared systemd server for the phone and the voice desk
  // to reach. Elsewhere the app already owns the backend it ships with, so an
  // update carries it with no help from here.
  if (platform !== 'linux') return { installed: false, restarted: false, reason: 'no shared unit on this platform' };
  try {
    const installed = await installBackend({ isPackaged, resourcesPath, version, home });
    if (!installed.installed) {
      log(`backend already carries ${version || 'this build'} (${installed.reason})`);
      return { ...installed, restarted: false };
    }
    const unit = await ensureUnit({ executable: installed.executable, home, runner });
    await restartUnit({ runner });
    log(`backend updated to ${version} and restarted${unit.changed ? ' (unit repointed)' : ''}`);
    return { ...installed, unitChanged: unit.changed, restarted: true };
  } catch (error) {
    log(`backend update failed, keeping the running server: ${error.message}`);
    return { installed: false, restarted: false, error: error.message };
  }
}

module.exports = {
  UNIT, BINARY, STAMP,
  installRoot, packagedBackend, installedVersion,
  installBackend, unitFile, ensureUnit, restartUnit, syncBackend,
};
