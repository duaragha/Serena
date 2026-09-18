'use strict';

// Read-only GitHub proof using the real sandboxed preload and IPC, not a browser mock.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const electron = require('electron');
const { createPromotionWindow } = require('../promotion-window.cjs');
const { app, BrowserWindow } = electron;
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'serena-release-proof-'));
app.setPath('userData', directory);
app.disableHardwareAcceleration();
const output = process.env.SERENA_EVIDENCE_DIR;
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

app.whenReady().then(async () => {
  try {
    const open = createPromotionWindow({ ...electron, profile: { channel: 'dev', version: '0.3.9-dev.2' } });
    await open();
    const window = BrowserWindow.getAllWindows()[0];
    const deadline = Date.now() + 90000;
    let state;
    do {
      state = await window.webContents.executeJavaScript(`({
        rows: document.querySelectorAll('.feature').length,
        error: document.querySelector('#error').textContent,
        disabled: document.querySelector('#publish').disabled,
        stable: document.querySelector('#stable').textContent
      })`);
      if (state.rows || state.error) break;
      await wait(200);
    } while (Date.now() < deadline);
    assert.equal(state.error, '');
    assert.ok(state.rows >= 5, JSON.stringify(state));
    assert.equal(state.disabled, true);
    assert.match(state.stable, /^v\d+\.\d+\.\d+$/);
    const denied = new BrowserWindow({ show: false, webPreferences: {
      preload: path.resolve(__dirname, '../promotion-preload.cjs'),
      contextIsolation: true, sandbox: true, nodeIntegration: false,
    } });
    await denied.loadFile(path.resolve(__dirname, '../promotion.html'));
    const denial = await denied.webContents.executeJavaScript('serenaReleases.load().then(()=>"unexpected", e=>e.message)');
    assert.match(denial, /Untrusted release manager sender/);
    denied.destroy();
    if (output) {
      fs.mkdirSync(output, { recursive: true });
      fs.writeFileSync(path.join(output, 'electron-release-manager.png'), (await window.webContents.capturePage()).toPNG());
      fs.writeFileSync(path.join(output, 'electron-proof.json'), JSON.stringify({ ...state, deniedOtherWindow: true }, null, 2));
    }
    console.log(JSON.stringify({ ...state, deniedOtherWindow: true }));
    window.destroy();
    app.exit(0);
  } catch (error) { console.error(error); app.exit(1); }
}).catch(error => { console.error(error); app.exit(1); });
app.on('quit', () => fs.rmSync(directory, { recursive: true, force: true }));
