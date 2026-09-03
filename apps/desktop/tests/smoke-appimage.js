'use strict';

const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { requestHealth, waitForChildExit } = require('../runtime');

const desktopDir = path.resolve(__dirname, '..');
const distDir = path.join(desktopDir, 'dist');
const timeoutMs = 90000;

function findAppImage() {
  const images = fs.readdirSync(distDir)
    .filter((name) => name.endsWith('.AppImage'))
    .sort();
  assert.equal(images.length, 1, `expected one AppImage in ${distDir}, found ${images.length}`);
  return path.join(distDir, images[0]);
}

async function main() {
  const appImage = findAppImage();
  const child = spawn(appImage, ['--no-sandbox', '--headless', '--smoke-test'], {
    cwd: desktopDir,
    detached: true,
    env: {
      ...process.env,
      APPIMAGE_EXTRACT_AND_RUN: '1',
      ELECTRON_DISABLE_GPU: '1',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  let readyUrl = null;
  let timer;
  const ready = new Promise((resolve, reject) => {
    const inspect = (chunk) => {
      output += chunk.toString('utf8');
      const match = output.match(/SERENA_BACKEND_READY (http:\/\/127\.0\.0\.1:\d+) pid=(\d+)/);
      if (match) {
        readyUrl = match[1];
        resolve({ url: match[1], pid: Number(match[2]) });
      }
    };
    child.stdout.on('data', inspect);
    child.stderr.on('data', inspect);
    child.once('error', reject);
    child.once('exit', (code, signal) => {
      if (!readyUrl) reject(new Error(`AppImage exited before ready: code=${code} signal=${signal}\n${output}`));
    });
    timer = setTimeout(() => reject(new Error(`AppImage smoke timed out\n${output}`)), timeoutMs);
  });

  try {
    const announced = await ready;
    const health = await requestHealth(`${announced.url}/api/health`, 2000);
    assert.equal(health.ok, true);
    assert.equal(health.pid, announced.pid);
  } finally {
    clearTimeout(timer);
    try {
      process.kill(-child.pid, 'SIGTERM');
    } catch (error) {
      if (error.code !== 'ESRCH') throw error;
    }
    const stoppedGracefully = await waitForChildExit(child, 10000);
    if (!stoppedGracefully) {
      try {
        process.kill(-child.pid, 'SIGKILL');
      } catch (error) {
        if (error.code !== 'ESRCH') throw error;
      }
      await waitForChildExit(child, 2000);
      throw new Error(`AppImage did not shut down cleanly after SIGTERM\n${output}`);
    }
  }

  let stopped = false;
  try {
    await requestHealth(`${readyUrl}/api/health`, 500);
  } catch {
    stopped = true;
  }
  assert.equal(stopped, true, 'sidecar health endpoint remained live after AppImage shutdown');
  process.stdout.write(`smoke ok: ${path.basename(appImage)} started its sidecar and shut down cleanly\n`);
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
