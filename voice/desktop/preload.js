const { contextBridge, ipcRenderer } = require('electron');

// Callbacks that the module script can register
const callbacks = {
  setState: null,
  setAmplitude: null,
};

// Code panel callbacks — registered by the module script
const codePanelCallbacks = {
  onStart: null,
  onEvent: null,
  onDone: null,
  onSnapshot: null,
  onControlResult: null,
  onToggle: null,
  onShow: null,
};
let latestCodePanelWidth = 450;
let codePanelWidthCallback = null;

ipcRenderer.on('code-panel-width', (_event, width) => {
  latestCodePanelWidth = Number(width) || 450;
  codePanelWidthCallback?.(latestCodePanelWidth);
});

contextBridge.exposeInMainWorld('serena', {
  // Renderer → Backend: the type bar, for when the mic is unusable
  sendTyped: (payload) => {
    ipcRenderer.send(
      'typed-message',
      typeof payload === 'object' && payload !== null
        ? payload
        : { text: String(payload || '') },
    );
  },
  onTypedInputError: (callback) => {
    ipcRenderer.on('typed-input-error', (_event, message) => callback(String(message || '')));
  },
  onTypedInputAccepted: (callback) => {
    ipcRenderer.on('typed-input-accepted', () => callback());
  },

  // How fast she talks. Persisted by the main process, read by every voice
  // surface, so it survives a restart and applies beyond this window.
  setVoiceSpeed: (value) => {
    ipcRenderer.send('set-voice-speed', Number(value));
  },
  onVoiceSpeed: (callback) => {
    ipcRenderer.on('voice-speed', (_event, value) => callback(value));
  },
  setVoiceMuted: (muted) => {
    ipcRenderer.send('set-voice-muted', Boolean(muted));
  },
  onVoiceMuted: (callback) => {
    ipcRenderer.on('voice-muted', (_event, muted) => callback(Boolean(muted)));
  },
  setMicrophoneMuted: (muted) => {
    ipcRenderer.send('set-microphone-muted', Boolean(muted));
  },
  onMicrophoneMuted: (callback) => {
    ipcRenderer.on('microphone-muted', (_event, muted) => callback(Boolean(muted)));
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
  onCodeSnapshot: (callback) => {
    ipcRenderer.on('code-snapshot', (_event, data) => callback(data));
  },
  onCodeControlResult: (callback) => {
    ipcRenderer.on('code-control-result', (_event, data) => callback(data));
  },
  onToggleCodePanel: (callback) => {
    ipcRenderer.on('toggle-code-panel', (_event) => callback());
  },
  onHideCodePanel: (callback) => {
    ipcRenderer.on('hide-code-panel', (_event) => callback());
  },
  onShowCodePanel: (callback) => {
    ipcRenderer.on('show-code-panel', (_event) => callback());
  },
  onCodePanelWidth: (callback) => {
    codePanelWidthCallback = callback;
    callback(latestCodePanelWidth);
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
  showCodePanel: () => {
    ipcRenderer.send('show-code-panel');
  },
  setCodePanelWidth: (width) => {
    ipcRenderer.send('set-code-panel-width', Number(width));
  },
  sendCodeControl: (payload) => {
    ipcRenderer.send('code-control', payload);
  },

  // Brain visualization callbacks — registered by the module script
  registerBrain: (fns) => {
    if (fns.setState) callbacks.setState = fns.setState;
    if (fns.setAmplitude) callbacks.setAmplitude = fns.setAmplitude;
    ipcRenderer.send('renderer-ready');
  },

  // Code panel callbacks — registered by the module script
  registerCodePanel: (fns) => {
    if (fns.onStart) codePanelCallbacks.onStart = fns.onStart;
    if (fns.onEvent) codePanelCallbacks.onEvent = fns.onEvent;
    if (fns.onDone) codePanelCallbacks.onDone = fns.onDone;
    if (fns.onSnapshot) codePanelCallbacks.onSnapshot = fns.onSnapshot;
    if (fns.onControlResult) codePanelCallbacks.onControlResult = fns.onControlResult;
    if (fns.onToggle) codePanelCallbacks.onToggle = fns.onToggle;
    if (fns.onShow) codePanelCallbacks.onShow = fns.onShow;
  },

  // Stable proxy functions survive contextBridge's value freezing.
  setState: (state) => callbacks.setState?.(state),
  setAmplitude: (value) => callbacks.setAmplitude?.(value),

  codePanelOnStart: (data) => codePanelCallbacks.onStart?.(data),
  codePanelOnEvent: (event) => codePanelCallbacks.onEvent?.(event),
  codePanelOnDone: (data) => codePanelCallbacks.onDone?.(data),
  codePanelOnSnapshot: (data) => codePanelCallbacks.onSnapshot?.(data),
  codePanelOnControlResult: (data) => codePanelCallbacks.onControlResult?.(data),
  codePanelOnToggle: () => codePanelCallbacks.onToggle?.(),
  codePanelOnShow: () => codePanelCallbacks.onShow?.(),
});
