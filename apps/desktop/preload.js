'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('serenaDesktop', Object.freeze({
  getVersion: () => ipcRenderer.invoke('desktop:get-version'),
  notify: (options) => ipcRenderer.invoke('desktop:notify', options),
  openExternal: (url) => ipcRenderer.invoke('desktop:open-external', url),
  pickFolder: (options) => ipcRenderer.invoke('desktop:pick-folder', options),
  // The in-app browser. The page reports where its pane is; the tabs are
  // drawn there by the main process.
  browser: Object.freeze({
    command: (name, args) => ipcRenderer.invoke('browser:command', String(name), args || {}),
    setBounds: (rect) => ipcRenderer.send('browser:bounds', rect),
    onState: (handler) => {
      if (typeof handler !== 'function') throw new TypeError('handler must be a function');
      const listener = (_event, payload) => handler(payload);
      ipcRenderer.on('browser:state', listener);
      return () => ipcRenderer.removeListener('browser:state', listener);
    },
    onReveal: (handler) => {
      if (typeof handler !== 'function') throw new TypeError('handler must be a function');
      const listener = (_event, payload) => handler(payload);
      ipcRenderer.on('browser:reveal', listener);
      return () => ipcRenderer.removeListener('browser:reveal', listener);
    },
  }),
  // Updates, exposed so an in-page About panel can drive the same flow the
  // native menu uses. onProgress returns its own unsubscribe so a re-render
  // cannot leak listeners.
  updates: Object.freeze({
    onOpen: (handler) => {
      if (typeof handler !== 'function') throw new TypeError('handler must be a function');
      const listener = (_event, payload) => handler(payload);
      ipcRenderer.on('updates:open', listener);
      return () => ipcRenderer.removeListener('updates:open', listener);
    },
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
