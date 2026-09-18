const assert = require('assert/strict');
const path = require('path');
const { app, BrowserWindow, ipcMain } = require('electron');

app.commandLine.appendSwitch('no-sandbox');

ipcMain.handle('serena-coding-jobs:list', async () => ({
  ok: true,
  jobs: [
    {
      item_id: 'weather-job',
      state: 'working',
      created_at: 10,
      project: 'serena',
      brief: { request: 'add current weather' },
      progress: { attempt: 3 },
      controls: { can_cancel: true, can_steer: true, can_resume: false },
    },
    {
      item_id: 'coding-ui-job',
      state: 'working',
      created_at: 20,
      project: 'serena',
      brief: { request: 'show both coding jobs' },
      progress: { attempt: 1 },
      controls: { can_cancel: true, can_steer: true, can_resume: false },
    },
  ],
}));

app.whenReady().then(async () => {
  const desktop = path.resolve(__dirname, '..');
  const window = new BrowserWindow({
    width: 500,
    height: 600,
    show: false,
    webPreferences: {
      preload: path.join(desktop, 'coding-preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  await window.loadFile(path.join(desktop, 'renderer', 'index.html'));
  await new Promise((resolve) => setTimeout(resolve, 500));

  const proof = await window.webContents.executeJavaScript(`(() => {
    window._serenaCodePanel.hide();
    const state = document.getElementById('state-label').getBoundingClientRect();
    const button = document.querySelector('.code-panel__reopen').getBoundingClientRect();
    const resize = document.querySelector('.code-panel__resize');
    resize.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }));
    const overlap = !(button.right <= state.left || button.left >= state.right
      || button.bottom <= state.top || button.top >= state.bottom);
    return {
      jobs: [...document.querySelectorAll('.code-panel__job')].map((job) => job.dataset.itemId),
      selected: window._serenaCodingJobsView.selectedId,
      buttonRight: Math.round(window.innerWidth - button.right),
      overlap,
      resizeRole: resize.getAttribute('role'),
      resizeWidth: resize.getAttribute('aria-valuenow'),
      panelWidth: window._serenaCodePanel.el.style.getPropertyValue('--code-panel-width'),
    };
  })()`);

  assert.deepEqual(proof.jobs, ['coding-ui-job', 'weather-job']);
  assert.equal(proof.selected, 'coding-ui-job');
  assert.equal(proof.buttonRight, 14);
  assert.equal(proof.overlap, false);
  assert.equal(proof.resizeRole, 'separator');
  assert.equal(proof.resizeWidth, '720');
  assert.equal(proof.panelWidth, '720px');
  await window.close();
  app.quit();
});

app.on('window-all-closed', () => app.quit());
