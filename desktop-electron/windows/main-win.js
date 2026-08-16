'use strict';

const { app } = require('electron');
const { autoUpdater } = require('electron-updater');

require('../main');

async function checkForUpdates() {
  try {
    await autoUpdater.checkForUpdatesAndNotify();
  } catch (error) {
    console.error('[updater] update check failed', error);
  }
}

if (app.isPackaged) {
  autoUpdater.logger = console;
  app.whenReady().then(checkForUpdates).catch((error) => {
    console.error('[updater] failed to schedule update check', error);
  });
}
