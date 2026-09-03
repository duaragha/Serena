const { contextBridge, ipcRenderer } = require('electron');

// Callbacks that the module script can register
const callbacks = {
  setState: null,
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
  // Backend → Renderer event listeners
  onStateChange: (callback) => {
    ipcRenderer.on('state-change', (_event, state) => callback(state));
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

  // Renderer → Main process
  setIgnoreMouse: (ignore) => {
    ipcRenderer.send('set-ignore-mouse', ignore);
  },
  toggleDashboard: () => {
    ipcRenderer.send('toggle-dashboard');
  },

  // Brain visualization callbacks — registered by the module script
  registerBrain: (fns) => {
    if (fns.setState) callbacks.setState = fns.setState;
    if (fns.showUserText) callbacks.showUserText = fns.showUserText;
    if (fns.showResponseText) callbacks.showResponseText = fns.showResponseText;
    if (fns.clearText) callbacks.clearText = fns.clearText;
  },

  // Code panel callbacks — registered by the module script
  registerCodePanel: (fns) => {
    if (fns.onStart) codePanelCallbacks.onStart = fns.onStart;
    if (fns.onEvent) codePanelCallbacks.onEvent = fns.onEvent;
    if (fns.onDone) codePanelCallbacks.onDone = fns.onDone;
    if (fns.onToggle) codePanelCallbacks.onToggle = fns.onToggle;
  },

  // Accessors for app.js to call brain functions
  get setState() { return callbacks.setState; },
  get showUserText() { return callbacks.showUserText; },
  get showResponseText() { return callbacks.showResponseText; },
  get clearText() { return callbacks.clearText; },

  // Accessors for app.js to call code panel functions
  get codePanelOnStart() { return codePanelCallbacks.onStart; },
  get codePanelOnEvent() { return codePanelCallbacks.onEvent; },
  get codePanelOnDone() { return codePanelCallbacks.onDone; },
  get codePanelOnToggle() { return codePanelCallbacks.onToggle; },
});
