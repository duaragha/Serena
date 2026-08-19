const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { app, BrowserWindow, ipcMain } = require('electron');

const STATES = ['idle', 'listening', 'thinking', 'working', 'speaking', 'offline'];
const rendererPath = path.join(__dirname, '..', 'renderer', 'index.html');

async function waitForRenderer(window) {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const ready = await window.webContents.executeJavaScript(
      "Boolean(window._serenaBrain && document.querySelector('[data-renderer=\"dot-field\"]'))",
    );
    if (ready) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error('dot-field renderer did not become ready');
}

app.whenReady().then(async () => {
  const errors = [];
  const window = new BrowserWindow({
    width: 500,
    height: 600,
    show: false,
    backgroundColor: '#080612',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      offscreen: true,
      preload: path.join(__dirname, '..', 'preload.js'),
    },
  });

  window.webContents.on('console-message', (event) => {
    if (event.level === 'error') {
      errors.push(event.message);
    }
  });
  window.webContents.on('render-process-gone', (_event, details) => {
    errors.push(`renderer process exited: ${details.reason}`);
  });
  ipcMain.on('show-code-panel', () => {
    window.webContents.send('show-code-panel', null);
  });

  try {
    await window.loadURL(pathToFileURL(rendererPath).href);
    await waitForRenderer(window);

    const rendererKind = await window.webContents.executeJavaScript(
      "document.querySelector('canvas').dataset.renderer",
    );
    assert.equal(rendererKind, 'dot-field');

    const hasThree = await window.webContents.executeJavaScript("typeof window.THREE !== 'undefined'");
    assert.equal(hasThree, false);

    for (const state of STATES) {
      window.webContents.send('state-change', state);
      let renderedState = '';
      for (let attempt = 0; attempt < 20; attempt += 1) {
        renderedState = await window.webContents.executeJavaScript(
          "document.querySelector('[data-renderer=\"dot-field\"]').dataset.state",
        );
        if (renderedState === state) {
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 10));
      }
      assert.equal(renderedState, state);
    }

    window.webContents.send('state-change', 'speaking');
    window.webContents.send('voice-amplitude', 0.73);
    await new Promise((resolve) => setTimeout(resolve, 25));
    const amplitude = await window.webContents.executeJavaScript(
      'window._serenaBrain._amplitudeTarget',
    );
    assert.equal(amplitude, 0.73);

    const reducedMotionState = await window.webContents.executeJavaScript(`
      window._serenaBrain._reducedMotion = true;
      window._serenaBrain.setState('thinking');
      window._serenaBrain._render(performance.now(), 16);
      window._serenaBrain._state;
    `);
    assert.equal(reducedMotionState, 'thinking');

    const reducedText = await window.webContents.executeJavaScript(`
      window._serenaTranscription._reducedMotion = { matches: true };
      window._serenaTranscription.showResponseText('instant response');
      window._serenaTranscription._responseText.textContent;
    `);
    assert.equal(reducedText, 'instant response');

    await window.webContents.executeJavaScript(`
      window._serenaTranscription.clear();
      window._serenaTranscription.showResponseText('new response');
    `);
    await new Promise((resolve) => setTimeout(resolve, 500));
    const textAfterOldClear = await window.webContents.executeJavaScript(
      'window._serenaTranscription._responseText.textContent',
    );
    assert.equal(textAfterOldClear, 'new response');

    for (let index = 0; index < 13; index += 1) {
      window.webContents.send('transcription', `question ${index}`);
      window.webContents.send('response', `answer ${index}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 25));
    const history = await window.webContents.executeJavaScript(`({
      open: document.getElementById('conversation-history').open,
      count: window._serenaConversationHistory.entries.length,
      first: window._serenaConversationHistory.entries[0],
      last: window._serenaConversationHistory.entries.at(-1),
      rows: document.querySelectorAll('.conversation-turn').length,
    })`);
    assert.equal(history.open, false);
    assert.equal(history.count, 12);
    assert.equal(history.rows, 12);
    assert.deepEqual(history.first, { user: 'question 1', assistant: 'answer 1' });
    assert.deepEqual(history.last, { user: 'question 12', assistant: 'answer 12' });

    window.webContents.send('code-start', { project: 'serena' });
    window.webContents.send('code-event', {
      kind: 'bash',
      summary: 'pytest -q',
      detail: '36 passed',
    });
    window.webContents.send('code-done', { summary: 'finished' });
    await new Promise((resolve) => setTimeout(resolve, 50));
    const codePanel = await window.webContents.executeJavaScript(`({
      visible: window._serenaCodePanel.isVisible,
      status: document.querySelector('.code-panel__status-text').textContent,
      output: document.querySelector('.code-panel__output').textContent,
    })`);
    assert.equal(codePanel.visible, true);
    assert.equal(codePanel.status, 'done');
    assert.match(codePanel.output, /pytest -q/);
    assert.match(codePanel.output, /36 passed/);

    window.webContents.send('code-start', { project: 'coding', status: 'ready' });
    await new Promise((resolve) => setTimeout(resolve, 25));
    const readyStatus = await window.webContents.executeJavaScript(
      "document.querySelector('.code-panel__status-text').textContent",
    );
    assert.equal(readyStatus, 'ready');
    window.webContents.send('hide-code-panel', null);
    await new Promise((resolve) => setTimeout(resolve, 25));
    const collapsedPanel = await window.webContents.executeJavaScript(`({
      hidden: !window._serenaCodePanel.isVisible,
      reopenVisible: document.querySelector('.code-panel__reopen')
        .classList.contains('code-panel__reopen--visible'),
    })`);
    assert.deepEqual(collapsedPanel, { hidden: true, reopenVisible: true });
    await window.webContents.executeJavaScript(
      "document.querySelector('.code-panel__reopen').click()",
    );
    await new Promise((resolve) => setTimeout(resolve, 25));
    const reopenedPanel = await window.webContents.executeJavaScript(`({
      visible: window._serenaCodePanel.isVisible,
      reopenHidden: !document.querySelector('.code-panel__reopen')
        .classList.contains('code-panel__reopen--visible'),
    })`);
    assert.deepEqual(reopenedPanel, { visible: true, reopenHidden: true });
    assert.deepEqual(errors, []);
    console.log('dot-field smoke passed');
  } finally {
    ipcMain.removeAllListeners('renderer-ready');
    ipcMain.removeAllListeners('show-code-panel');
    window.destroy();
    app.quit();
  }
}).catch((error) => {
  console.error(error);
  app.exit(1);
});
