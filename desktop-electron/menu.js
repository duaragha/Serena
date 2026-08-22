'use strict';

/**
 * The application menu.
 *
 * Electron's default menu is fine but has no About and no way to update, so
 * the app looked like it had no version and no upgrade path. This keeps the
 * familiar File / Edit / View / Window layout and adds an About menu holding
 * exactly two things: "Check for Updates…" and "About Serena".
 *
 * Reload and DevTools are deliberately kept: the UI is served from a local
 * Flask app, and reloading the window is the cheapest way to pick up a change
 * without disturbing the panes, which live server-side and reattach.
 */

const { Menu, app, shell } = require('electron');

const updates = require('./updates');

function aboutSubmenu(getWindow) {
  return [
    {
      label: 'Check for Updates…',
      click: () => {
        // Fire and forget: the dialogs own the interaction from here, and an
        // unhandled rejection in a menu handler would take down the window.
        updates.checkInteractively(getWindow()).catch((error) => {
          console.error('[menu] update check failed:', error && error.message);
        });
      },
    },
    { type: 'separator' },
    {
      label: 'About Serena',
      click: () => {
        updates.showAbout(getWindow()).catch((error) => {
          console.error('[menu] about failed:', error && error.message);
        });
      },
    },
  ];
}

function template(getWindow) {
  const isMac = process.platform === 'darwin';
  const menu = [
    {
      label: 'File',
      submenu: [
        {
          label: 'New Window',
          accelerator: 'CmdOrCtrl+Shift+N',
          click: () => {
            const win = getWindow();
            if (win && !win.isDestroyed()) win.show();
          },
        },
        { type: 'separator' },
        // Hide rather than quit: closing the window leaves Serena in the tray
        // with its panes and agents still running.
        { label: 'Close Window', accelerator: 'CmdOrCtrl+W', role: 'close' },
        { label: 'Quit Serena', accelerator: isMac ? 'Cmd+Q' : 'Ctrl+Q', role: 'quit' },
      ],
    },
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' },
        { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' },
        { role: 'selectAll' },
      ],
    },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'forceReload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    {
      label: 'Window',
      submenu: [
        { role: 'minimize' },
        { role: 'zoom' },
        ...(isMac ? [{ type: 'separator' }, { role: 'front' }] : []),
      ],
    },
    {
      label: 'About',
      submenu: [
        ...aboutSubmenu(getWindow),
        { type: 'separator' },
        {
          label: 'Serena on GitHub',
          click: () => {
            shell.openExternal('https://github.com/duaragha/Serena').catch(() => {});
          },
        },
      ],
    },
  ];

  if (isMac) {
    menu.unshift({
      label: app.getName(),
      submenu: [...aboutSubmenu(getWindow), { type: 'separator' }, { role: 'quit' }],
    });
  }
  return menu;
}

function install(getWindow) {
  Menu.setApplicationMenu(Menu.buildFromTemplate(template(getWindow)));
}

module.exports = { install, template };
