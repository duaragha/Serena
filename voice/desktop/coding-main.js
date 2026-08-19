const { app, ipcMain, session } = require('electron');
const { execFile, spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const CODING_JOBS_CHANNEL = 'serena-coding-jobs:list';
const CODING_MODEL_GET_CHANNEL = 'serena-coding-model:get';
const CODING_MODEL_SET_CHANNEL = 'serena-coding-model:set';
const CODING_TERMINAL_OPEN_CHANNEL = 'serena-coding-terminal:open';
const CODING_JOBS_PRELOAD = path.join(__dirname, 'coding-preload.js');
const PROJECT_ROOT = path.resolve(__dirname, '..', '..');
const PROJECT_PYTHON = path.join(PROJECT_ROOT, '.venv', 'bin', 'python');
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const terminalBrokers = new Map();

function gnomeTerminalExecutable() {
  if (process.platform !== 'linux') return '';
  for (const candidate of ['/usr/bin/gnome-terminal', '/usr/local/bin/gnome-terminal']) {
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      return candidate;
    } catch (_error) {
      // Optional integration. The drawer remains fully usable without it.
    }
  }
  return '';
}

function readCodingJobs() {
  return new Promise((resolve, reject) => {
    execFile(
      PROJECT_PYTHON,
      ['-m', 'voice.desktop.coding_jobs_query', '--limit', '20'],
      {
        cwd: PROJECT_ROOT,
        env: process.env,
        timeout: 5000,
        maxBuffer: 2 * 1024 * 1024,
        windowsHide: true,
      },
      (error, stdout, stderr) => {
        if (error) {
          reject(new Error(String(stderr || error.message || error).trim()));
          return;
        }
        try {
          const jobs = JSON.parse(stdout);
          const terminalAvailable = Boolean(gnomeTerminalExecutable());
          resolve((Array.isArray(jobs) ? jobs : []).map((job) => {
            if (!job?.terminal || terminalAvailable) return job;
            return {
              ...job,
              terminal: {
                ...job.terminal,
                can_open: false,
                reason: 'GNOME Terminal is not installed',
              },
            };
          }));
        } catch (parseError) {
          reject(new Error(`coding job list returned invalid JSON: ${parseError.message}`));
        }
      },
    );
  });
}

function launchLiveTerminal(executable, itemId) {
  return new Promise((resolve, reject) => {
    const args = [
      '-m',
      'voice.desktop.live_session_terminal',
      '--item-id',
      itemId,
      '--terminal-executable',
      executable,
    ];
    const child = spawn(
      PROJECT_PYTHON,
      args,
      {
        cwd: PROJECT_ROOT,
        env: process.env,
        detached: true,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      },
    );
    terminalBrokers.set(itemId, child);
    child.unref();

    let settled = false;
    let output = '';
    let errorOutput = '';
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      callback(value);
    };
    child.stdout.on('data', (chunk) => {
      output += String(chunk || '');
      if (output.length > 64 * 1024) {
        finish(reject, new Error('interactive terminal returned an oversized response'));
        return;
      }
      const newline = output.indexOf('\n');
      if (newline < 0) return;
      try {
        const payload = JSON.parse(output.slice(0, newline));
        if (!payload?.ok) {
          finish(reject, new Error(String(payload?.error || 'interactive terminal is unavailable')));
          return;
        }
        if (
          !UUID_RE.test(String(payload.session_id || ''))
          || !['codex', 'claude'].includes(String(payload.provider || ''))
        ) {
          finish(reject, new Error('interactive terminal returned invalid session metadata'));
          return;
        }
        finish(resolve, payload);
      } catch (parseError) {
        finish(reject, new Error(`interactive terminal returned invalid JSON: ${parseError.message}`));
      }
    });
    child.stderr.on('data', (chunk) => {
      errorOutput = `${errorOutput}${String(chunk || '')}`.slice(-4000);
    });
    child.once('error', (error) => finish(reject, error));
    child.once('close', (code) => {
      if (terminalBrokers.get(itemId) === child) terminalBrokers.delete(itemId);
      if (!settled) {
        finish(
          reject,
          new Error(errorOutput.trim() || `interactive terminal exited before attachment (${code})`),
        );
      }
    });
  });
}

function codingModelPreference(action, model = '') {
  return new Promise((resolve, reject) => {
    const args = ['-m', 'core.coding_model_preferences', action];
    if (action === 'set') args.push(String(model || ''));
    execFile(
      PROJECT_PYTHON,
      args,
      {
        cwd: PROJECT_ROOT,
        env: process.env,
        timeout: 5000,
        maxBuffer: 64 * 1024,
        windowsHide: true,
      },
      (error, stdout, stderr) => {
        if (error) {
          reject(new Error(String(stderr || error.message || error).trim()));
          return;
        }
        try {
          const payload = JSON.parse(stdout);
          if (!payload || typeof payload !== 'object') {
            throw new Error('coding model preference returned a non-object payload');
          }
          resolve({
            model: String(payload.model || 'auto'),
            options: Array.isArray(payload.options) ? payload.options : [],
          });
        } catch (parseError) {
          reject(new Error(`coding model preference returned invalid JSON: ${parseError.message}`));
        }
      },
    );
  });
}

ipcMain.handle(CODING_JOBS_CHANNEL, async () => {
  try {
    return { ok: true, jobs: await readCodingJobs() };
  } catch (error) {
    return { ok: false, jobs: [], error: String(error.message || error).slice(0, 1000) };
  }
});

ipcMain.handle(CODING_MODEL_GET_CHANNEL, async () => {
  try {
    return { ok: true, ...(await codingModelPreference('get')) };
  } catch (error) {
    return { ok: false, model: 'auto', error: String(error.message || error).slice(0, 1000) };
  }
});

ipcMain.handle(CODING_MODEL_SET_CHANNEL, async (_event, model) => {
  try {
    return { ok: true, ...(await codingModelPreference('set', model)) };
  } catch (error) {
    return { ok: false, model: 'auto', error: String(error.message || error).slice(0, 1000) };
  }
});

ipcMain.handle(CODING_TERMINAL_OPEN_CHANNEL, async (_event, requestedItemId) => {
  const itemId = String(requestedItemId || '').trim().toLowerCase();
  if (!UUID_RE.test(itemId)) {
    return { ok: false, error: 'coding job id is invalid' };
  }
  const executable = gnomeTerminalExecutable();
  if (!executable) {
    return { ok: false, error: 'GNOME Terminal is not installed' };
  }
  if (terminalBrokers.has(itemId)) {
    return { ok: false, error: 'interactive terminal is already open' };
  }
  try {
    // The broker re-resolves and claims the exact durable session at click
    // time before it launches any terminal. Renderer snapshots are display
    // data only and can never choose a session or command.
    const result = await launchLiveTerminal(executable, itemId);
    return {
      ok: true,
      session_id: String(result.session_id || ''),
      provider: String(result.provider || ''),
    };
  } catch (error) {
    return { ok: false, error: String(error.message || error).slice(0, 1000) };
  }
});

app.whenReady().then(() => {
  const preloads = session.defaultSession.getPreloads();
  if (!preloads.includes(CODING_JOBS_PRELOAD)) {
    session.defaultSession.setPreloads([...preloads, CODING_JOBS_PRELOAD]);
  }
});

require('./main.js');
