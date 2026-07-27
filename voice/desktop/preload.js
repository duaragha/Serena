const { contextBridge, ipcRenderer } = require('electron');

// Callbacks that the module script can register
const callbacks = {
  setState: null,
  setAmplitude: null,
  showUserText: null,
  showResponseText: null,
  clearText: null,
};

// Code panel callbacks — registered by the module script
const codePanelCallbacks = {
  onStart: null,
  onEvent: null,
  onDone: null,
  onToggle: null,
};

contextBridge.exposeInMainWorld('serena', {
  // Renderer → Backend: the type bar, for when the mic is unusable
  sendTyped: (text) => {
    ipcRenderer.send('typed-message', String(text || ''));
  },

  // Backend → Renderer event listeners
  onStateChange: (callback) => {
    ipcRenderer.on('state-change', (_event, state) => callback(state));
  },
  onAmplitude: (callback) => {
    ipcRenderer.on('voice-amplitude', (_event, value) => callback(value));
  },
  onTranscription: (callback) => {
    ipcRenderer.on('transcription', (_event, text) => callback(text));
  },
  onResponse: (callback) => {
    ipcRenderer.on('response', (_event, text) => callback(text));
  },
  onDashboardData: (callback) => {
    ipcRenderer.on('dashboard-data', (_event, data) => callback(data));
  },
  onFocusMode: (callback) => {
    ipcRenderer.on('focus-mode', (_event, enabled) => callback(enabled));
  },
  onToggleDashboard: (callback) => {
    ipcRenderer.on('toggle-dashboard', (_event, visible) => callback(visible));
  },

  // Code panel events
  onCodeStart: (callback) => {
    ipcRenderer.on('code-start', (_event, data) => callback(data));
  },
  onCodeEvent: (callback) => {
    ipcRenderer.on('code-event', (_event, event) => callback(event));
  },
  onCodeDone: (callback) => {
    ipcRenderer.on('code-done', (_event, data) => callback(data));
  },
  onToggleCodePanel: (callback) => {
    ipcRenderer.on('toggle-code-panel', (_event) => callback());
  },
  onHideCodePanel: (callback) => {
    ipcRenderer.on('hide-code-panel', (_event) => callback());
  },

  // Renderer → Main process
  setIgnoreMouse: (ignore) => {
    ipcRenderer.send('set-ignore-mouse', ignore);
  },
  toggleDashboard: () => {
    ipcRenderer.send('toggle-dashboard');
  },
  hideCodePanel: () => {
    ipcRenderer.send('hide-code-panel');
  },

  // Brain visualization callbacks — registered by the module script
  registerBrain: (fns) => {
    if (fns.setState) callbacks.setState = fns.setState;
    if (fns.setAmplitude) callbacks.setAmplitude = fns.setAmplitude;
    if (fns.showUserText) callbacks.showUserText = fns.showUserText;
    if (fns.showResponseText) callbacks.showResponseText = fns.showResponseText;
    if (fns.clearText) callbacks.clearText = fns.clearText;
    ipcRenderer.send('renderer-ready');
  },

  // Code panel callbacks — registered by the module script
  registerCodePanel: (fns) => {
    if (fns.onStart) codePanelCallbacks.onStart = fns.onStart;
    if (fns.onEvent) codePanelCallbacks.onEvent = fns.onEvent;
    if (fns.onDone) codePanelCallbacks.onDone = fns.onDone;
    if (fns.onToggle) codePanelCallbacks.onToggle = fns.onToggle;
  },

  // Stable proxy functions survive contextBridge's value freezing.
  setState: (state) => callbacks.setState?.(state),
  setAmplitude: (value) => callbacks.setAmplitude?.(value),
  showUserText: (text) => callbacks.showUserText?.(text),
  showResponseText: (text) => callbacks.showResponseText?.(text),
  clearText: () => callbacks.clearText?.(),

  codePanelOnStart: (data) => codePanelCallbacks.onStart?.(data),
  codePanelOnEvent: (event) => codePanelCallbacks.onEvent?.(event),
  codePanelOnDone: (data) => codePanelCallbacks.onDone?.(data),
  codePanelOnToggle: () => codePanelCallbacks.onToggle?.(),
});
