// Isolated Electron proof. No updater download, application restart, or terminal.
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs');
const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const updates = require('../updates');

const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'serena-about-proof-'));
app.setPath('userData', profile);
app.disableHardwareAcceleration();
dialog.showMessageBox = () => { throw new Error('Native dialog must not open'); };
const timeout = setTimeout(() => { console.error('About proof timed out'); app.exit(1); }, 20000);
app.whenReady().then(async () => {
  console.log('Electron ready');
  const win = new BrowserWindow({ show: false, webPreferences: {
    preload: path.resolve(__dirname, '../preload.js'), contextIsolation: true, sandbox: true, nodeIntegration: false,
    backgroundThrottling: false,
  } });
  win.webContents.on('preload-error', (_event, _path, error) => console.error(error));
  ipcMain.handle('updates:describe', () => updates.describe());
  ipcMain.handle('updates:check', () => updates.check());
  ipcMain.handle('updates:download', () => { throw new Error('No downloads in proof'); });
  ipcMain.handle('updates:install', () => { throw new Error('No installs in proof'); });
  await win.loadURL('data:text/html,<html><head></head><body></body></html>');
  console.log('Page loaded', win.webContents.isLoadingMainFrame(), win.webContents.isLoading());
  await updates.showAbout(win);
  console.log('Panel injected');
  const probe = () => win.webContents.executeJavaScript(`({
    open:document.querySelector('#desktopAbout')?.open,
    count:document.querySelectorAll('#desktopAbout').length,
    text:document.querySelector('#desktopAbout')?.textContent,
    disabled:document.querySelector('[data-primary]')?.disabled,
  })`);
  let result;
  for (let i=0; i<100; i++) {
    result = await probe();
    if (result.open && result.text.includes('development build')) break;
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  assert.equal(result.open, true);
  assert.equal(result.disabled, true);
  assert.ok(result.text.includes('development build'));
  await updates.checkInteractively(win);
  assert.equal((await probe()).count, 1, 'reopening must reuse the panel');
  console.log('PASS: real Electron preload, menu dispatch, themed dialog, unsupported state, singleton; no native dialogs or downloads');
  clearTimeout(timeout);
  win.destroy();
  app.quit();
}).catch(error => { console.error(error); clearTimeout(timeout); app.exit(1); });
