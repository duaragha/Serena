'use strict';
const { contextBridge, ipcRenderer } = require('electron');
const api = {};
for (const name of ['load', 'status', 'submit', 'dismiss', 'github']) {
  api[name] = async value => {
    const result = await ipcRenderer.invoke(`promotion:${name}`, value);
    if (!result.ok) throw new Error(result.error);
    return result.value;
  };
}
contextBridge.exposeInMainWorld('serenaReleases', Object.freeze(api));
