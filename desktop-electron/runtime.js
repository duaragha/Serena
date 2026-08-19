'use strict';

const http = require('node:http');
const net = require('node:net');
const path = require('node:path');

const LOOPBACK_HOST = '127.0.0.1';

function findFreePort(host = LOOPBACK_HOST) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once('error', reject);
    server.listen(0, host, () => {
      const address = server.address();
      const port = address && typeof address === 'object' ? address.port : 0;
      server.close((error) => {
        if (error) reject(error);
        else if (!port) reject(new Error('operating system did not assign a port'));
        else resolve(port);
      });
    });
  });
}

function requestHealth(url, timeoutMs = 750) {
  return new Promise((resolve, reject) => {
    const request = http.get(url, { timeout: timeoutMs }, (response) => {
      const chunks = [];
      response.on('data', (chunk) => chunks.push(chunk));
      response.on('end', () => {
        if (response.statusCode !== 200) {
          reject(new Error(`health check returned HTTP ${response.statusCode}`));
          return;
        }
        try {
          const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          if (payload.ok !== true || !Number.isInteger(payload.pid) || payload.pid < 1) {
            throw new Error('health payload is missing ok=true or a valid pid');
          }
          resolve(payload);
        } catch (error) {
          reject(error);
        }
      });
    });
    request.once('timeout', () => request.destroy(new Error('health check timed out')));
    request.once('error', reject);
  });
}

function waitForHealth(child, url, options = {}) {
  const timeoutMs = options.timeoutMs ?? 30000;
  const intervalMs = options.intervalMs ?? 125;
  const startedAt = Date.now();

  return new Promise((resolve, reject) => {
    let timer = null;
    let settled = false;

    const finish = (error, payload) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      child.off('exit', onExit);
      child.off('error', onError);
      if (error) reject(error);
      else resolve(payload);
    };
    const onExit = (code, signal) => {
      finish(new Error(`backend exited before ready (code=${code}, signal=${signal})`));
    };
    const onError = (error) => finish(error);
    const poll = async () => {
      if (Date.now() - startedAt >= timeoutMs) {
        finish(new Error(`backend health check timed out after ${timeoutMs}ms`));
        return;
      }
      try {
        finish(null, await requestHealth(url));
      } catch {
        timer = setTimeout(poll, intervalMs);
      }
    };

    child.once('exit', onExit);
    child.once('error', onError);
    poll();
  });
}

function normalizeExternalUrl(value) {
  if (typeof value !== 'string') return null;
  const candidate = value.trim();
  if (!candidate || /\s/.test(candidate)) return null;
  try {
    const parsed = new URL(candidate);
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.host) return null;
    return parsed.href;
  } catch {
    return null;
  }
}

const SHARED_BACKEND_PORT = Number.parseInt(
  process.env.SERENA_DESKTOP_SHARED_PORT || '8767',
  10,
);

/**
 * Look for a backend that is already serving the UI.
 *
 * mobile_host runs the same Flask app as a persistent service so the phone
 * can reach Serena while the desktop app is closed. When it is up there is
 * no reason to pay for a second copy, so the shell attaches to it instead.
 * Returns null when nothing healthy answers, and the caller spawns its own.
 */
async function findExistingBackend(options = {}) {
  const port = options.port ?? SHARED_BACKEND_PORT;
  const host = options.host ?? LOOPBACK_HOST;
  const timeoutMs = options.timeoutMs ?? 750;
  if (!Number.isInteger(port) || port < 1 || port > 65535) return null;
  if (options.enabled === false) return null;
  const url = `http://${host}:${port}`;
  try {
    const health = await requestHealth(`${url}/api/health`, timeoutMs);
    return { url, pid: health.pid, owned: false };
  } catch {
    return null;
  }
}

function backendLaunch({ isPackaged, appDir, resourcesPath, port }) {
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new TypeError('backend port must be an integer between 1 and 65535');
  }
  if (isPackaged) {
    return {
      command: path.join(resourcesPath, 'sidecar', 'serena-web-sidecar'),
      args: ['--host', LOOPBACK_HOST, '--port', String(port)],
      cwd: resourcesPath,
    };
  }
  const repoRoot = path.resolve(appDir, '..');
  return {
    command: path.join(repoRoot, '.venv', 'bin', 'python'),
    args: [
      path.join(appDir, 'sidecar.py'),
      '--host', LOOPBACK_HOST,
      '--port', String(port),
    ],
    cwd: repoRoot,
  };
}

function childExited(child) {
  return child.exitCode !== null || child.signalCode !== null;
}

function waitForChildExit(child, timeoutMs) {
  if (!child || childExited(child)) return Promise.resolve(true);
  return new Promise((resolve) => {
    let timer;
    const finish = (exited) => {
      clearTimeout(timer);
      child.off('exit', onExit);
      resolve(exited);
    };
    const onExit = () => finish(true);
    child.once('exit', onExit);
    timer = setTimeout(() => finish(childExited(child)), timeoutMs);
  });
}

function sendSignalToTree(child, signal) {
  if (!child || !child.pid || childExited(child)) return false;
  try {
    if (process.platform === 'win32') return child.kill(signal);
    process.kill(-child.pid, signal);
    return true;
  } catch (error) {
    if (error.code === 'ESRCH') return false;
    throw error;
  }
}

async function terminateProcessTree(child, graceMs = 2500) {
  if (!child || !child.pid || childExited(child)) return;
  sendSignalToTree(child, 'SIGTERM');
  await waitForChildExit(child, graceMs);
  if (!childExited(child)) sendSignalToTree(child, 'SIGKILL');
}

module.exports = {
  LOOPBACK_HOST,
  SHARED_BACKEND_PORT,
  backendLaunch,
  findExistingBackend,
  findFreePort,
  normalizeExternalUrl,
  requestHealth,
  sendSignalToTree,
  terminateProcessTree,
  waitForChildExit,
  waitForHealth,
};
