'use strict';

// Run with Electron. Only an isolated fixture server/profile is restarted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { app, BrowserWindow } = require('electron');
const control = require('../backend-control');

app.setPath('userData', fs.mkdtempSync(path.join(os.tmpdir(), 'serena-restart-proof-')));
let generation = 1;
let window;
const server = http.createServer((request, response) => {
  if (request.url === '/api/health') {
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({ ok: true, pid: generation }));
  } else if (request.url === '/check') {
    response.statusCode = request.headers['x-token'] === `token-${generation}` ? 200 : 403;
    response.end();
  } else {
    response.setHeader('Content-Type', 'text/html');
    response.setHeader('Cache-Control', 'no-store');
    response.end(`<script>window.token = 'token-${generation}';</script>`);
  }
});

async function status() {
  return window.webContents.executeJavaScript(
    "fetch('/check', {headers: {'X-Token': window.token}}).then(r => r.status)"
  );
}

app.whenReady().then(async () => {
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const url = `http://127.0.0.1:${server.address().port}`;
    window = new BrowserWindow({ show: false, webPreferences: {
      sandbox: true, contextIsolation: true, nodeIntegration: false,
    } });
    await window.loadURL(url);
    await window.webContents.executeJavaScript("localStorage.setItem('draft', 'keep me')");
    assert.equal(await status(), 200);

    // Reproduce the old path: it reloads while the queued restart is pending.
    await window.loadURL(url);
    generation = 2;
    assert.equal(await status(), 403, 'old document must demonstrate stale-token failure');

    // Reload once to set up another restart, then run the corrected lifecycle.
    await window.loadURL(url);
    let oldReplies = 0;
    const timer = setTimeout(() => { generation = 3; }, 150);
    try {
      const back = await control.waitForBackend(url, async endpoint => {
        const body = await (await fetch(endpoint)).json();
        if (body.pid === 2) oldReplies += 1;
        return body;
      }, { previousPid: 2, intervalMs: 10, timeoutMs: 3000 });
      assert.equal(back.ok, true);
      assert.equal(back.pid, 3);
      assert.ok(oldReplies > 0, 'must encounter the old healthy backend');
      await control.loadBackendWindow(window, url);
      assert.equal(await status(), 200, 'new document must use the replacement token');
      assert.equal(await window.webContents.executeJavaScript("localStorage.getItem('draft')"), 'keep me');
      console.log('PASS: stale-token failure reproduced; one restart refreshed access and preserved draft storage');
    } finally {
      clearTimeout(timer);
    }
  } catch (error) {
    console.error(error);
    process.exitCode = 1;
  } finally {
    if (window) window.destroy();
    server.close();
    app.exit(process.exitCode || 0);
  }
});
