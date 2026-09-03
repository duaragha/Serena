const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('serenaCodingJobs', {
  list: () => ipcRenderer.invoke('serena-coding-jobs:list'),
  getModel: () => ipcRenderer.invoke('serena-coding-model:get'),
  setModel: (model) => ipcRenderer.invoke('serena-coding-model:set', model),
  openTerminal: (itemId) => ipcRenderer.invoke('serena-coding-terminal:open', itemId),
});

function installCodingJobsView() {
  if (document.querySelector('script[data-serena-coding-jobs]')) return;

  const stylesheet = document.createElement('link');
  stylesheet.rel = 'stylesheet';
  stylesheet.href = 'coding-jobs.css';
  stylesheet.dataset.serenaCodingJobs = 'styles';
  document.head.appendChild(stylesheet);

  const script = document.createElement('script');
  script.type = 'module';
  script.src = 'coding-jobs.mjs';
  script.dataset.serenaCodingJobs = 'view';
  document.head.appendChild(script);
}

if (document.readyState === 'loading') {
  window.addEventListener('DOMContentLoaded', installCodingJobsView, { once: true });
} else {
  installCodingJobsView();
}
