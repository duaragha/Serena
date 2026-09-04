'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('serenaDesktop', Object.freeze({
  getVersion: () => ipcRenderer.invoke('desktop:get-version'),
  notify: (options) => ipcRenderer.invoke('desktop:notify', options),
  openExternal: (url) => ipcRenderer.invoke('desktop:open-external', url),
  pickFolder: (options) => ipcRenderer.invoke('desktop:pick-folder', options),
  // Updates, exposed so an in-page About panel can drive the same flow the
  // native menu uses. onProgress returns its own unsubscribe so a re-render
  // cannot leak listeners.
  updates: Object.freeze({
    describe: () => ipcRenderer.invoke('updates:describe'),
    check: () => ipcRenderer.invoke('updates:check'),
    download: () => ipcRenderer.invoke('updates:download'),
    install: () => ipcRenderer.invoke('updates:install'),
    onProgress: (handler) => {
      if (typeof handler !== 'function') throw new TypeError('handler must be a function');
      const listener = (_event, payload) => handler(payload);
      ipcRenderer.on('updates:progress', listener);
      return () => ipcRenderer.removeListener('updates:progress', listener);
    },
  }),
}));
