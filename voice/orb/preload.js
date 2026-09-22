// The one thing the page may ask of the app: "the conversation is over".
// window.close() from the page is not reliably honoured for a window the page
// did not open, so a summoned orb says so over IPC and the app quits.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('orb', {
  done: () => ipcRenderer.send('orb-done'),
});
