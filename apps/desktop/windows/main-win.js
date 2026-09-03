'use strict';

/**
 * Windows entry point.
 *
 * This used to own its own electron-updater instance and call
 * checkForUpdatesAndNotify() at launch, which meant Windows and Linux had
 * different update behaviour: one checked by itself, the other could not check
 * at all. Both now go through the shared updates module, so the platforms
 * behave identically and the settings that matter (never auto-download, install
 * on quit) are declared in exactly one place.
 */

const { app } = require('electron');

require('../main');
const updates = require('../updates');

// A quiet check at startup. It cannot download and cannot interrupt: the only
// effect is that the About menu already knows the answer when it is opened.
const STARTUP_CHECK_DELAY_MS = 20_000;

if (app.isPackaged) {
  app.whenReady()
    .then(() => {
      setTimeout(() => {
        updates.check({ silent: true }).catch((error) => {
          console.error('[updater] startup check failed', error && error.message);
        });
      }, STARTUP_CHECK_DELAY_MS);
    })
    .catch((error) => {
      console.error('[updater] failed to schedule the startup check', error);
    });
}
