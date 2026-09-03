'use strict';

/**
 * Restarting the server the window is talking to, on purpose.
 *
 * Serena runs from a checkout that changes under her, and the desktop app
 * usually attaches to a long-lived systemd server rather than owning one. So a
 * fix can land on disk and every request keeps being served by the process that
 * started hours earlier. Nothing said so. An afternoon went into a bug that was
 * already fixed, because the only symptom was that the fix appeared not to work.
 *
 * Two halves: notice the staleness, and offer the restart in the app rather
 * than as a command to remember. The restart stays an explicit choice, because
 * it ends open panes and the voice pipeline lives in the same unit.
 */

const SHARED_UNIT = 'serena-mobile-host.service';
const HELPER = 'scripts/serena-host-restart.sh';

/**
 * Ask the running server whether it predates the code on disk.
 *
 * @param {string} baseUrl
 * @param {(url: string) => Promise<any>} getJson
 */
async function freshness(baseUrl, getJson) {
  if (!baseUrl) return { reachable: false, stale: false, reason: 'no backend' };
  try {
    const body = await getJson(`${baseUrl}/api/backend-freshness`);
    return {
      reachable: true,
      stale: Boolean(body && body.stale),
      staleBySeconds: Number((body && body.stale_by_seconds) || 0),
      sourceRoot: (body && body.source_root) || '',
      frozen: Boolean(body && body.frozen),
      pid: body && body.pid,
    };
  } catch (error) {
    // An older server has no such endpoint. That is itself staleness, but it
    // is not worth a scary label; treat it as simply unknown.
    return { reachable: false, stale: false, reason: error.message };
  }
}

/** How long the server has been behind, in words a menu can show. */
function staleLabel(state) {
  if (!state || !state.stale) return null;
  const seconds = Math.max(0, Math.round(state.staleBySeconds || 0));
  if (seconds < 90) return 'Restart Backend (code on disk is newer)';
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `Restart Backend (${minutes} min behind the code)`;
  const hours = Math.round(minutes / 60);
  return `Restart Backend (${hours} h behind the code)`;
}

/**
 * The command that restarts the shared server.
 *
 * Deliberately the helper rather than systemctl: the caller is a child of the
 * unit being restarted, so a bare restart kills the shell partway through and
 * sometimes leaves it half applied. The helper runs in its own transient scope
 * and records the outcome.
 */
function sharedRestartCommand(sourceRoot) {
  if (!sourceRoot) throw new Error('the server did not report where it runs from');
  return {
    command: 'bash',
    args: [`${sourceRoot}/${HELPER}`, SHARED_UNIT],
    env: { SERENA_ALLOW_HOST_RESTART: '1' },
  };
}

/**
 * Wait for the server to answer again after a restart.
 *
 * @param {string} baseUrl
 * @param {(url: string) => Promise<any>} getJson
 * @param {{timeoutMs?: number, intervalMs?: number, sleep?: Function}} [options]
 */
async function waitForBackend(baseUrl, getJson, options = {}) {
  const timeoutMs = options.timeoutMs ?? 60000;
  const intervalMs = options.intervalMs ?? 500;
  const sleep = options.sleep || ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  const now = options.now || (() => Date.now());

  const deadline = now() + timeoutMs;
  let lastError = 'never answered';
  while (now() < deadline) {
    try {
      const body = await getJson(`${baseUrl}/api/health`);
      if (body && body.ok) return { ok: true, pid: body.pid };
    } catch (error) {
      lastError = error.message;
    }
    await sleep(intervalMs);
  }
  return { ok: false, reason: lastError };
}

module.exports = {
  HELPER,
  SHARED_UNIT,
  freshness,
  sharedRestartCommand,
  staleLabel,
  waitForBackend,
};
