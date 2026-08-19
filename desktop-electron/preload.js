'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('serenaDesktop', Object.freeze({
  getVersion: () => ipcRenderer.invoke('desktop:get-version'),
  notify: (options) => ipcRenderer.invoke('desktop:notify', options),
  openExternal: (url) => ipcRenderer.invoke('desktop:open-external', url),
}));
