'use strict';

/**
 * Keep the backend's output somewhere it can actually be read.
 *
 * A packaged Windows app has no console: process.stdout goes nowhere, so every
 * line the Python sidecar printed was discarded. When the backend died, the
 * window was left on a dead port and the only symptom anyone ever saw was
 * "Failed to fetch" from a renderer whose server had quietly gone. There was no
 * record of the exit code, let alone the traceback above it.
 *
 * So everything the backend says, and every decision the shell makes about the
 * backend's life, lands in one file next to the app's data.
 */

const fs = require('node:fs');
const path = require('node:path');

const MAX_BYTES = 2 * 1024 * 1024;
const KEEP = 2;

let resolveDir = null;
let handle = null;
let written = 0;

/** Called once at startup; kept injectable so the tests do not need Electron. */
function configure(directoryProvider) {
  resolveDir = directoryProvider;
  close();
}

function logPath() {
  if (!resolveDir) return null;
  return path.join(resolveDir(), 'logs', 'backend.log');
}

function rotate(file) {
  for (let index = KEEP - 1; index >= 1; index -= 1) {
    const older = `${file}.${index}`;
    const newer = index === 1 ? file : `${file}.${index - 1}`;
    try {
      fs.rmSync(older, { force: true });
      fs.renameSync(newer, older);
    } catch {
      // A rotation that cannot happen must not stop the logging.
    }
  }
}

function open() {
  if (handle !== null) return handle;
  const file = logPath();
  if (!file) return null;
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    let size = 0;
    try {
      size = fs.statSync(file).size;
    } catch {
      size = 0;
    }
    if (size >= MAX_BYTES) {
      rotate(file);
      size = 0;
    }
    handle = fs.openSync(file, 'a');
    written = size;
  } catch {
    handle = false; // Do not retry on every line.
  }
  return handle;
}

function close() {
  if (typeof handle === 'number') {
    try {
      fs.closeSync(handle);
    } catch {
      // Nothing useful to do while shutting down.
    }
  }
  handle = null;
  written = 0;
}

function stamp() {
  return new Date().toISOString();
}

/** One line, with its origin, so backend noise and shell decisions interleave. */
function write(tag, text) {
  const body = String(text).replace(/\s+$/, '');
  if (!body) return;
  const line = `${stamp()} [${tag}] ${body}\n`;

  // A dev run still has a console, and that is where it is wanted.
  if (process.stdout && process.stdout.isTTY) process.stdout.write(line);

  const fd = open();
  if (typeof fd !== 'number') return;
  try {
    fs.writeSync(fd, line);
    written += Buffer.byteLength(line);
    if (written >= MAX_BYTES) close();
  } catch {
    close();
  }
}

/** The sidecar's own stdout/stderr. */
function backend(stream, chunk) {
  for (const line of String(chunk).split(/\r?\n/)) write(`backend:${stream}`, line);
}

/** The shell's decisions about the backend: started, died, restarting. */
function note(message) {
  write('desktop', message);
}

module.exports = { MAX_BYTES, backend, close, configure, logPath, note };
