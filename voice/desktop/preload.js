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
  onSnapshot: null,
  onControlResult: null,
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
  // Whether the drawer is on screen is the main process's decision: it owns the
  // window width the drawer is a column of. The renderer is told, it does not
  // decide.
  onShowCodePanel: (callback) => {
    ipcRenderer.on('show-code-panel', (_event) => callback());
  },
  onHideCodePanel: (callback) => {
    ipcRenderer.on('hide-code-panel', (_event) => callback());
  },
  onCodeSnapshot: (callback) => {
    ipcRenderer.on('code-snapshot', (_event, snapshot) => callback(snapshot));
  },
  onCodePanelWidth: (callback) => {
    ipcRenderer.on('code-panel-width', (_event, width) => callback(width));
  },
  onCodeControlResult: (callback) => {
    ipcRenderer.on('code-control-result', (_event, result) => callback(result));
  },

  // Renderer → Main process
  setIgnoreMouse: (ignore) => {
    ipcRenderer.send('set-ignore-mouse', ignore);
  },
  toggleDashboard: () => {
    ipcRenderer.send('toggle-dashboard');
  },
  showCodePanel: () => {
    ipcRenderer.send('show-code-panel');
  },
  hideCodePanel: () => {
    ipcRenderer.send('hide-code-panel');
  },
  setCodePanelWidth: (width) => {
    ipcRenderer.send('set-code-panel-width', width);
  },
  sendCodeControl: (control) => {
    ipcRenderer.send('code-control', control);
  },
  // Say so after a reload, so the main process can put back what was on screen.
  rendererReady: () => {
    ipcRenderer.send('renderer-ready');
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
    if (fns.onSnapshot) codePanelCallbacks.onSnapshot = fns.onSnapshot;
    if (fns.onControlResult) codePanelCallbacks.onControlResult = fns.onControlResult;
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
  get codePanelOnSnapshot() { return codePanelCallbacks.onSnapshot; },
  get codePanelOnControlResult() { return codePanelCallbacks.onControlResult; },
});
