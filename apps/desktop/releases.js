'use strict';

/**
 * Watch the release feed and say, natively, when each platform's build lands.
 *
 * A tagged release is not published all at once: the Linux job creates it and
 * uploads the AppImage, then the Windows job uploads the installer into the
 * same release. So "0.2.2 is out" is true twice, several minutes apart, and a
 * machine told too early offers an update whose artifact does not exist yet.
 *
 * This watches both platforms rather than only the one it runs on, because the
 * person cutting the release is waiting on both and is sitting at one screen.
 */

const { app, Notification, net, shell } = require('electron');
const fs = require('node:fs');
const path = require('node:path');

const FEED = require('./updates').FEED;
const RELEASE_API = `https://api.github.com/repos/${FEED.owner}/${FEED.repo}/releases/latest`;

// 15 minutes: four calls an hour against an anonymous limit of sixty.
const POLL_INTERVAL_MS = 15 * 60 * 1000;
const FIRST_POLL_DELAY_MS = 30 * 1000;

/**
 * A platform's build is "landed" only when BOTH its installer and its channel
 * file are on the release. The channel file is what the updater reads, so an
 * installer without one is an update nothing can find.
 */
const PLATFORMS = {
  linux: {
    label: 'Linux',
    channel: 'latest-linux.yml',
    installer: (name) => name.endsWith('.AppImage'),
  },
  win32: {
    label: 'Windows',
    channel: 'latest.yml',
    installer: (name) => /^Serena-Setup-.*\.exe$/.test(name),
  },
};

let timer = null;
let statePath = null;

function stateFile() {
  if (!statePath) statePath = path.join(app.getPath('userData'), 'announced-builds.json');
  return statePath;
}

/** Which platforms have already been announced, keyed by version. */
function readAnnounced() {
  try {
    const parsed = JSON.parse(fs.readFileSync(stateFile(), 'utf8'));
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function writeAnnounced(state) {
  try {
    fs.mkdirSync(path.dirname(stateFile()), { recursive: true });
    // Only the newest version matters; anything older is never announced again.
    const versions = Object.keys(state).sort().slice(-3);
    const trimmed = Object.fromEntries(versions.map((v) => [v, state[v]]));
    fs.writeFileSync(stateFile(), JSON.stringify(trimmed, null, 2));
  } catch (error) {
    console.error('[releases] could not record what was announced:', error.message);
  }
}

/** GET the latest release through Electron's stack so proxies apply. */
function fetchLatestRelease() {
  return new Promise((resolve, reject) => {
    const request = net.request({ method: 'GET', url: RELEASE_API });
    request.setHeader('Accept', 'application/vnd.github+json');
    request.setHeader('User-Agent', `Serena/${app.getVersion()}`);
    let body = '';
    request.on('response', (response) => {
      response.on('data', (chunk) => {
        body += chunk;
      });
      response.on('end', () => {
        if (response.statusCode !== 200) {
          reject(new Error(`release feed returned ${response.statusCode}`));
          return;
        }
        try {
          resolve(JSON.parse(body));
        } catch (error) {
          reject(new Error(`release feed was not JSON: ${error.message}`));
        }
      });
    });
    request.on('error', reject);
    request.end();
  });
}

function assetNames(release) {
  return (release && Array.isArray(release.assets) ? release.assets : [])
    .map((asset) => String(asset && asset.name ? asset.name : ''))
    .filter(Boolean);
}

/** Platforms whose installer AND channel file are both present. */
function landedPlatforms(release) {
  const names = assetNames(release);
  return Object.keys(PLATFORMS).filter((key) => {
    const platform = PLATFORMS[key];
    return names.includes(platform.channel) && names.some(platform.installer);
  });
}

function versionOf(release) {
  const tag = String((release && release.tag_name) || '');
  return tag.startsWith('v') ? tag.slice(1) : tag;
}

/** Semantic-ish compare, good enough for the x.y.z tags this project cuts. */
function isNewer(candidate, current) {
  const parse = (v) => String(v).split('.').map((part) => parseInt(part, 10) || 0);
  const [a, b] = [parse(candidate), parse(current)];
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    const diff = (a[i] || 0) - (b[i] || 0);
    if (diff !== 0) return diff > 0;
  }
  return false;
}

function announce(version, key, release) {
  const platform = PLATFORMS[key];
  const forThisMachine = key === process.platform;
  const body = forThisMachine
    ? `Serena ${version} is ready to install. Open About and check for updates.`
    : `The ${platform.label} build of Serena ${version} finished publishing.`;

  if (!Notification.isSupported()) {
    console.log(`[releases] ${platform.label} build ${version} is up (no notifications here)`);
    return;
  }
  const note = new Notification({ title: `Serena ${version} · ${platform.label} build`, body });
  note.on('click', () => {
    const url = release && release.html_url;
    if (url) shell.openExternal(url).catch(() => {});
  });
  note.show();
}

/**
 * One pass. Exposed so the tests can drive it without waiting on a timer.
 *
 * @param {{fetchRelease?: Function, version?: string}} [options]
 */
async function poll(options = {}) {
  const fetchRelease = options.fetchRelease || fetchLatestRelease;
  const current = options.version || app.getVersion();

  let release;
  try {
    release = await fetchRelease();
  } catch (error) {
    // A missed poll is not worth telling anyone about; the next one is minutes
    // away and an offline laptop would otherwise nag on every retry.
    console.error('[releases] check failed:', error.message);
    return [];
  }

  const version = versionOf(release);
  if (!version || !isNewer(version, current)) return [];

  const announced = readAnnounced();
  const already = new Set(announced[version] || []);
  const fresh = landedPlatforms(release).filter((key) => !already.has(key));
  if (!fresh.length) return [];

  for (const key of fresh) {
    announce(version, key, release);
    already.add(key);
  }
  announced[version] = [...already];
  writeAnnounced(announced);
  return fresh;
}

function start() {
  if (timer || !app.isPackaged) return false;
  setTimeout(() => {
    poll().catch(() => {});
    timer = setInterval(() => poll().catch(() => {}), POLL_INTERVAL_MS);
    if (timer.unref) timer.unref();
  }, FIRST_POLL_DELAY_MS);
  return true;
}

function stop() {
  if (timer) clearInterval(timer);
  timer = null;
}

module.exports = {
  PLATFORMS,
  POLL_INTERVAL_MS,
  RELEASE_API,
  isNewer,
  landedPlatforms,
  poll,
  start,
  stop,
  versionOf,
};
