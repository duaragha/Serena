'use strict';

const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { PromotionService, WORKFLOW_URL } = require('./promotion-service.cjs');

function createPromotionWindow({ app, BrowserWindow, ipcMain, dialog, shell, profile, log = () => {} }) {
  if (profile.channel !== 'dev') throw new Error('Release management is Dev-only');
  let window = null;
  const file = path.join(__dirname, 'promotion.html');
  const trustedUrl = pathToFileURL(file).href;
  const service = new PromotionService({ directory: app.getPath('userData'), version: profile.version,
    confirm: async ({ mode, stable, titles }) => {
      if (!window || window.isDestroyed()) return false;
      const dismiss = mode === 'dismiss';
      const result = await dialog.showMessageBox(window, { type: 'question', defaultId: 1, cancelId: 1,
        title: 'Serena releases', buttons: [dismiss ? 'Dismiss tracking' : mode === 'publish' ? 'Build and publish' : 'Check selection', 'Cancel'],
        message: dismiss ? 'Stop tracking this request?' : mode === 'publish' ? 'Publish these tested features to main after checks pass?' : 'Build and check this selection without publishing?',
        detail: dismiss ? 'This does not cancel GitHub builds. Check or cancel the existing run on GitHub before submitting another request.'
          : `Main baseline: ${stable}\n\n${titles.join('\n')}\n\nYour running apps will not be installed, stopped, or restarted.` });
      return result.response === 0;
    } });
  const handlers = {
    load: () => service.load(), status: () => service.status(), submit: value => service.submit(value),
    dismiss: () => service.dismiss(), github: () => shell.openExternal(WORKFLOW_URL),
  };
  for (const [name, handler] of Object.entries(handlers)) {
    ipcMain.handle(`promotion:${name}`, async (event, value) => {
      if (!window || window.isDestroyed() || event.sender !== window.webContents
          || event.senderFrame !== window.webContents.mainFrame || event.senderFrame.url !== trustedUrl)
        throw new Error('Untrusted release manager sender');
      try { return { ok: true, value: await handler(value) }; }
      catch (error) {
        // Persist it: a failure seen only in the window leaves nothing to diagnose later.
        log(`releases ${name} failed: ${error.message}`);
        return { ok: false, error: error.message };
      }
    });
  }
  return async () => {
    if (window && !window.isDestroyed()) { window.show(); window.focus(); return; }
    window = new BrowserWindow({ width: 800, height: 730, minWidth: 360, minHeight: 420,
      title: 'Serena Dev - Releases', backgroundColor: '#100d11', show: false,
      webPreferences: { preload: path.join(__dirname, 'promotion-preload.cjs'), sandbox: true,
        contextIsolation: true, nodeIntegration: false } });
    window.setMenu(null);
    window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    window.webContents.on('will-navigate', event => event.preventDefault());
    window.once('ready-to-show', () => window?.show());
    window.on('closed', () => { window = null; });
    await window.loadFile(file);
  };
}
module.exports = { createPromotionWindow };
