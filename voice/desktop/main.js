const { app, BrowserWindow, Tray, Menu, nativeImage, ipcMain, screen } = require('electron');
const { execFile } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');
const WebSocket = require('ws');
const { normaliseTypedPayload } = require('./typed-images.js');

// NOTE: GNOME system tray requires the AppIndicator extension.
// Install via: sudo apt install gnome-shell-extension-appindicator
// Then enable it in GNOME Extensions or via:
//   gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com
// Without this, the tray icon will not be visible on GNOME desktops.

const WS_URL = 'ws://localhost:8765';
// One file, read by every process that speaks as her (this overlay's bridge,
// the desk conversation, the phone host), so the slider moves all of them.
const VOICE_SPEED_PATH = path.join(os.homedir(), '.config', 'serena', 'voice_speed');
const VOICE_MUTED_PATH = path.join(os.homedir(), '.config', 'serena', 'voice_muted');
const MICROPHONE_MUTED_PATH = path.join(
  os.homedir(), '.config', 'serena', 'microphone_muted',
);
const CODE_PANEL_WIDTH_PATH = path.join(
  os.homedir(), '.config', 'serena', 'coding_pane_width',
);
const MIN_VOICE_SPEED = 0.5;
const MAX_VOICE_SPEED = 2.0;

function clampSpeed(value) {
  const speed = Number(value);
  if (!Number.isFinite(speed)) return 1.0;
  return Math.min(Math.max(speed, MIN_VOICE_SPEED), MAX_VOICE_SPEED);
}

function readVoiceSpeed() {
  try {
    return clampSpeed(fs.readFileSync(VOICE_SPEED_PATH, 'utf8').trim());
  } catch {
    return 1.0;  // unset or unreadable is simply her normal rate
  }
}

function writeVoiceSpeed(value) {
  const speed = clampSpeed(value);
  const temporary = `${VOICE_SPEED_PATH}.tmp`;
  try {
    fs.mkdirSync(path.dirname(VOICE_SPEED_PATH), { recursive: true });
    // Written then renamed: a reader mid-sentence never sees half a number.
    fs.writeFileSync(temporary, `${speed.toFixed(2)}\n`, 'utf8');
    fs.renameSync(temporary, VOICE_SPEED_PATH);
  } catch (error) {
    console.error('[serena] could not save voice speed:', error.message);
  }
  return speed;
}

function readVoiceMuted() {
  try {
    return ['1', 'true', 'yes', 'on', 'muted'].includes(
      fs.readFileSync(VOICE_MUTED_PATH, 'utf8').trim().toLowerCase(),
    );
  } catch {
    return false;
  }
}

function writeVoiceMuted(value) {
  const muted = Boolean(value);
  const temporary = `${VOICE_MUTED_PATH}.tmp`;
  try {
    fs.mkdirSync(path.dirname(VOICE_MUTED_PATH), { recursive: true });
    fs.writeFileSync(temporary, muted ? '1\n' : '0\n', 'utf8');
    fs.renameSync(temporary, VOICE_MUTED_PATH);
  } catch (error) {
    console.error('[serena] could not save voice mute:', error.message);
  }
  return muted;
}

function readMicrophoneMuted() {
  try {
    return ['1', 'true', 'yes', 'on', 'muted'].includes(
      fs.readFileSync(MICROPHONE_MUTED_PATH, 'utf8').trim().toLowerCase(),
    );
  } catch {
    return false;
  }
}

function writeMicrophoneMuted(value) {
  const muted = Boolean(value);
  const temporary = `${MICROPHONE_MUTED_PATH}.tmp`;
  try {
    fs.mkdirSync(path.dirname(MICROPHONE_MUTED_PATH), { recursive: true });
    fs.writeFileSync(temporary, muted ? '1\n' : '0\n', { encoding: 'utf8', mode: 0o600 });
    fs.renameSync(temporary, MICROPHONE_MUTED_PATH);
    fs.chmodSync(MICROPHONE_MUTED_PATH, 0o600);
  } catch (error) {
    console.error('[serena] could not save microphone mute:', error.message);
  }
  return muted;
}
const RECONNECT_INTERVAL_MS = 3000;
const WINDOW_WIDTH = 500;
const WINDOW_HEIGHT = 600;
const MIN_WINDOW_HEIGHT = 360;
const IDLE_HIDE_DELAY_MS = 1800;
// Keep these aligned with code-panel.js. The main process owns the actual
// window bounds while the renderer owns the accessible resize interaction.
const DEFAULT_CODE_PANEL_WIDTH = 450;
const MIN_CODE_PANEL_WIDTH = 300;
const MAX_CODE_PANEL_WIDTH = 720;

function clampCodePanelWidth(value) {
  const width = Number(value);
  if (!Number.isFinite(width)) return DEFAULT_CODE_PANEL_WIDTH;
  return Math.round(Math.min(Math.max(width, MIN_CODE_PANEL_WIDTH), MAX_CODE_PANEL_WIDTH));
}

function readCodePanelWidth() {
  try {
    return clampCodePanelWidth(fs.readFileSync(CODE_PANEL_WIDTH_PATH, 'utf8').trim());
  } catch {
    return DEFAULT_CODE_PANEL_WIDTH;
  }
}

function writeCodePanelWidth(value) {
  const width = clampCodePanelWidth(value);
  const temporary = `${CODE_PANEL_WIDTH_PATH}.tmp`;
  try {
    fs.mkdirSync(path.dirname(CODE_PANEL_WIDTH_PATH), { recursive: true });
    fs.writeFileSync(temporary, `${width}\n`, 'utf8');
    fs.renameSync(temporary, CODE_PANEL_WIDTH_PATH);
  } catch (error) {
    console.error('[serena] could not save coding pane width:', error.message);
  }
  return width;
}

let win = null;
let tray = null;
let ws = null;
let reconnectTimer = null;
let currentState = 'idle';
let focusModeEnabled = false;
let voiceMuted = readVoiceMuted();
let microphoneMuted = readMicrophoneMuted();
let dashboardVisible = false;
let codePanelVisible = false;
let codePanelAvailable = false;
let codePanelDismissed = false;
// The job whose drawer he closed, or null when he closed it with nothing on it.
let codePanelDismissedJob = null;
let codePanelWidth = readCodePanelWidth();
let codePanelWidthApplied = 0;
let currentCodeSnapshot = null;
let idlePinnedVisible = false;
let idleHideTimer = null;
let isQuitting = false;

// The coding drawer is optional viewing history, so it only takes the screen
// for a job that is genuinely mid-flight. A queued, finished, cancelled, or
// long-dead job updates in place; snapshots also arrive on every bridge
// reconnect and after every control press, and those must not reopen a drawer
// Raghav closed.
const RUNNING_JOB_STATES = new Set(['working', 'resume_queued']);

function isRunningJob(snapshot) {
  if (!snapshot || typeof snapshot !== 'object') return false;
  return RUNNING_JOB_STATES.has(String(snapshot.state || ''));
}

function shouldOpenForSnapshot(snapshot) {
  if (!isRunningJob(snapshot)) return false;
  if (codePanelVisible) return false;
  if (!codePanelDismissed) return true;
  // Closed with nothing on it: no snapshot may reopen it, only a job start.
  if (codePanelDismissedJob === null) return false;
  // He closed this job's drawer. Only a different job may open it again.
  return String(snapshot.item_id || '') !== codePanelDismissedJob;
}

// Give the drawer its own room and take it back afterwards. The window grows
// leftward so it keeps the corner it was parked in, and a width he chose by
// hand survives, because the change is a delta rather than a fixed size.
function applyCodePanelLayout() {
  if (!win || win.isDestroyed()) return;
  const bounds = win.getBounds();
  const area = screen.getDisplayMatching(bounds).workArea;
  const baseWidth = Math.max(WINDOW_WIDTH, bounds.width - codePanelWidthApplied);
  const availableForPanel = Math.max(
    MIN_CODE_PANEL_WIDTH,
    area.width - baseWidth,
  );
  const desiredWidth = codePanelVisible
    ? Math.min(codePanelWidth, availableForPanel)
    : 0;
  if (desiredWidth === codePanelWidthApplied) return;
  const delta = desiredWidth - codePanelWidthApplied;
  const width = Math.min(area.width, Math.max(WINDOW_WIDTH, bounds.width + delta));
  const x = Math.max(area.x, bounds.x + bounds.width - width);
  if (!codePanelVisible) win.setMinimumSize?.(WINDOW_WIDTH, MIN_WINDOW_HEIGHT);
  win.setBounds({ x, y: bounds.y, width, height: bounds.height });
  codePanelWidthApplied = desiredWidth;
  if (codePanelVisible) {
    win.setMinimumSize?.(WINDOW_WIDTH + desiredWidth, MIN_WINDOW_HEIGHT);
  }
}

function setCodePanelWidth(value) {
  codePanelWidth = writeCodePanelWidth(value);
  applyCodePanelLayout();
  sendToRenderer('code-panel-width', codePanelWidthApplied || codePanelWidth);
}

function requestActiveSessionClose() {
  if (process.platform !== 'linux' || currentState === 'idle') return;
  execFile(
    'systemctl',
    ['--user', 'kill', '--signal=SIGUSR1', 'serena-dot-overlay.service'],
    { timeout: 2000 },
    (error) => {
      if (error) console.error('[voice] Failed to close active desk session:', error.message);
    },
  );
}

// --- Tray icon generation ---

function createTrayIcon(state) {
  // Match the violet, pink, and amber state language used by the dot field.
  const colors = {
    idle: '#a78bfa',
    listening: '#f472b6',
    thinking: '#f6ad55',
    working: '#c084fc',
    speaking: '#fb7185',
  };
  const color = colors[state] || colors.idle;

  // Draw a 22x22 icon with a colored circle (nativeImage from data URL)
  const size = 22;
  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}">
      <circle cx="${size / 2}" cy="${size / 2}" r="${size / 2 - 2}" fill="${color}" />
    </svg>
  `;
  const encoded = Buffer.from(svg).toString('base64');
  return nativeImage.createFromDataURL(`data:image/svg+xml;base64,${encoded}`);
}

// --- Window creation ---

function createWindow() {
  const { x, y, width: screenW, height: screenH } = screen.getPrimaryDisplay().workArea;

  win = new BrowserWindow({
    width: WINDOW_WIDTH,
    height: WINDOW_HEIGHT,
    minWidth: WINDOW_WIDTH,
    minHeight: MIN_WINDOW_HEIGHT,
    x: x + screenW - WINDOW_WIDTH - 20,
    y: y + screenH - WINDOW_HEIGHT - 20,
    title: 'Serena',
    frame: true,
    resizable: true,
    minimizable: true,
    maximizable: true,
    show: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    backgroundColor: '#0a0a1a',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  // Forward renderer console messages to main process stdout for debugging
  win.webContents.on('console-message', function() {
    // Try Electron 35 signature: (event) where event has .message
    // Fall back to old signature: (event, level, message, line, sourceId)
    const args = Array.from(arguments);
    let msg;
    if (args.length === 1 && args[0]?.message) {
      msg = args[0].message;
    } else {
      msg = args[2] || args[0];
    }
    process.stdout.write(`[renderer] ${msg}\n`);
  });

  // Renderer can still send set-ignore-mouse events, but no-op now
  // The overlay's type bar. Same turn a spoken one makes; the bridge runs it.
  ipcMain.on('typed-message', (event, payload) => {
    const checked = normaliseTypedPayload(payload);
    if (!checked.ok) {
      event.sender.send('typed-input-error', checked.error);
      return;
    }
    if (!wsSend({ type: 'typed', ...checked.payload })) {
      event.sender.send(
        'typed-input-error',
        'serena is reconnecting. your message is still here.',
      );
      return;
    }
  });

  ipcMain.on('set-ignore-mouse', (_event, _ignore) => {
    // No longer needed. The proper window has real hit testing.
  });

  win.on('close', (event) => {
    if (!isQuitting) {
      isQuitting = true;
      clearIdleHideTimer();
      clearReconnectTimer();
      if (ws) ws.close();
    }
  });
}

// --- System tray ---

function createTray() {
  const icon = createTrayIcon('idle');
  tray = new Tray(icon);
  tray.setToolTip('Serena');
  updateTrayMenu();
}

function updateTrayMenu() {
  const menu = Menu.buildFromTemplate([
    {
      label: win && win.isVisible() ? 'Hide Overlay' : 'Show Overlay',
      click: () => toggleOverlay(),
    },
    {
      label: 'Mute Voice',
      type: 'checkbox',
      checked: voiceMuted,
      click: (menuItem) => setVoiceMuted(menuItem.checked),
    },
    {
      label: 'Mute Microphone',
      type: 'checkbox',
      checked: microphoneMuted,
      click: (menuItem) => setMicrophoneMuted(menuItem.checked),
    },
    {
      label: 'Focus Mode',
      type: 'checkbox',
      checked: focusModeEnabled,
      click: (menuItem) => {
        focusModeEnabled = menuItem.checked;
        sendToRenderer('focus-mode', focusModeEnabled);
        // Notify Python backend
        wsSend({ type: 'focus_mode', enabled: focusModeEnabled });
      },
    },
    {
      label: 'Dashboard',
      type: 'checkbox',
      checked: dashboardVisible,
      click: (menuItem) => {
        dashboardVisible = menuItem.checked;
        sendToRenderer('toggle-dashboard', dashboardVisible);
      },
    },
    {
      label: 'Code Output',
      type: 'checkbox',
      checked: codePanelVisible,
      enabled: codePanelAvailable,
      click: (menuItem) => {
        // Same two doors the drawer's own buttons use, so unchecking it here
        // counts as him closing it and stays closed.
        if (menuItem.checked) showCodePanel();
        else hideCodePanel();
      },
    },
    { type: 'separator' },
    {
      label: 'Quit',
      click: () => {
        if (ws) ws.close();
        app.quit();
      },
    },
  ]);
  tray.setContextMenu(menu);
}

function toggleOverlay() {
  if (!win) return;
  if (win.isVisible()) {
    requestActiveSessionClose();
    idlePinnedVisible = false;
    win.hide();
  } else {
    idlePinnedVisible = currentState === 'idle';
    showOverlay(true);
  }
  updateTrayMenu();
}

function clearIdleHideTimer() {
  if (idleHideTimer) {
    clearTimeout(idleHideTimer);
    idleHideTimer = null;
  }
}

function showOverlay(interactive = false) {
  if (!win || win.isDestroyed()) return;
  clearIdleHideTimer();
  if (win.isMinimized()) win.restore();
  win.setAlwaysOnTop(true, 'floating');
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  if (interactive) {
    win.show();
  } else {
    // Voice activity should be visible without stealing focus from Raghav's work.
    win.showInactive();
  }
  win.moveTop();
  updateTrayMenu();
}

function scheduleIdleHide() {
  clearIdleHideTimer();
  // A drawer he can reopen is not a reason to pin the overlay open forever;
  // only one that is actually on screen is.
  if (idlePinnedVisible || dashboardVisible || codePanelVisible) return;
  idleHideTimer = setTimeout(() => {
    idleHideTimer = null;
    if (win && !win.isDestroyed() && currentState === 'idle' && !idlePinnedVisible) {
      win.hide();
      updateTrayMenu();
    }
  }, IDLE_HIDE_DELAY_MS);
}

function presentVoiceState(state) {
  if (state === 'idle') {
    scheduleIdleHide();
    return;
  }
  showOverlay(false);
}

// --- WebSocket connection to Python backend ---

function connectWebSocket() {
  if (ws) {
    ws.removeAllListeners();
    ws.close();
    ws = null;
  }

  ws = new WebSocket(WS_URL);

  ws.on('open', () => {
    console.log('[IPC] Connected to Python backend');
    clearReconnectTimer();
  });

  ws.on('message', (data) => {
    try {
      const msg = JSON.parse(data.toString());
      handleBackendMessage(msg);
    } catch (err) {
      console.error('[IPC] Failed to parse message:', err.message);
    }
  });

  ws.on('close', () => {
    console.log('[IPC] Disconnected from Python backend');
    ws = null;
    scheduleReconnect();
  });

  ws.on('error', (err) => {
    // Suppress ECONNREFUSED noise. It just means the backend is not up yet.
    if (err.code !== 'ECONNREFUSED') {
      console.error('[IPC] WebSocket error:', err.message);
    }
    ws = null;
    scheduleReconnect();
  });
}

function scheduleReconnect() {
  clearReconnectTimer();
  reconnectTimer = setTimeout(() => {
    console.log('[IPC] Attempting reconnect...');
    connectWebSocket();
  }, RECONNECT_INTERVAL_MS);
}

function clearReconnectTimer() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
}

function wsSend(message) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(message));
    return true;
  }
  return false;
}

// --- Message handling ---

function handleBackendMessage(msg) {
  switch (msg.type) {
    case 'state_change':
      currentState = msg.state;
      if (tray) tray.setImage(createTrayIcon(currentState));
      sendToRenderer('state-change', msg.state);
      presentVoiceState(currentState);
      break;

    case 'amplitude':
      if (typeof msg.value === 'number' && Number.isFinite(msg.value)) {
        sendToRenderer('voice-amplitude', Math.max(0, Math.min(1, msg.value)));
      }
      break;

    case 'transcription':
      sendToRenderer('transcription', msg.text);
      break;

    case 'response':
      sendToRenderer('response', msg.text);
      break;

    case 'typed_input_accepted':
      sendToRenderer('typed-input-accepted', null);
      break;

    case 'typed_input_error':
      sendToRenderer(
        'typed-input-error',
        String(msg.error || 'that message could not be accepted.'),
      );
      break;

    case 'dashboard':
      sendToRenderer('dashboard-data', msg.data);
      break;

    case 'code_start':
      // A real job starting is the one event allowed to take the screen, so it
      // also clears an earlier dismissal.
      codePanelVisible = true;
      codePanelAvailable = true;
      codePanelDismissed = false;
      codePanelDismissedJob = null;
      if (msg.snapshot) currentCodeSnapshot = msg.snapshot;
      sendToRenderer('code-start', {
        item_id: msg.item_id,
        project: msg.project,
        status: msg.status,
        snapshot: msg.snapshot,
      });
      applyCodePanelLayout();
      showOverlay(false);
      updateTrayMenu();
      break;

    case 'code_snapshot':
      codePanelAvailable = true;
      currentCodeSnapshot = msg.snapshot;
      sendToRenderer('code-snapshot', msg.snapshot);
      if (shouldOpenForSnapshot(msg.snapshot)) {
        codePanelVisible = true;
        sendToRenderer('show-code-panel', null);
        applyCodePanelLayout();
        showOverlay(false);
      }
      updateTrayMenu();
      break;

    case 'code_hide':
      hideCodePanel();
      break;

    case 'code_event':
      sendToRenderer('code-event', {
        ...(msg.event || {}),
        ...(msg.item_id ? { item_id: String(msg.item_id) } : {}),
      });
      break;

    case 'code_done':
      codePanelAvailable = true;
      if (msg.snapshot) currentCodeSnapshot = msg.snapshot;
      sendToRenderer('code-done', { summary: msg.summary, snapshot: msg.snapshot });
      break;

    case 'code_control_result':
      sendToRenderer('code-control-result', msg);
      break;

    case 'toggle_code_panel':
      sendToRenderer('toggle-code-panel', null);
      break;

    default:
      console.log('[IPC] Unknown message type:', msg.type);
  }
}

function sendToRenderer(channel, data) {
  if (win && !win.isDestroyed()) {
    win.webContents.send(channel, data);
  }
}

// --- IPC from renderer ---

ipcMain.on('toggle-dashboard', () => {
  dashboardVisible = !dashboardVisible;
  if (dashboardVisible) showOverlay(true);
  sendToRenderer('toggle-dashboard', dashboardVisible);
  updateTrayMenu();
});

function hideCodePanel() {
  codePanelVisible = false;
  codePanelDismissed = true;
  codePanelDismissedJob = currentCodeSnapshot
    ? String(currentCodeSnapshot.item_id || '')
    : null;
  sendToRenderer('hide-code-panel', null);
  applyCodePanelLayout();
  if (currentState === 'idle') scheduleIdleHide();
  updateTrayMenu();
}

ipcMain.on('hide-code-panel', hideCodePanel);

function showCodePanel() {
  if (!codePanelAvailable) return;
  codePanelVisible = true;
  codePanelDismissed = false;
  codePanelDismissedJob = null;
  sendToRenderer('show-code-panel', null);
  applyCodePanelLayout();
  sendToRenderer('code-panel-width', codePanelWidthApplied || codePanelWidth);
  showOverlay(false);
  updateTrayMenu();
}

ipcMain.on('show-code-panel', showCodePanel);
ipcMain.on('set-code-panel-width', (_event, width) => setCodePanelWidth(width));

ipcMain.on('code-control', (_event, payload) => {
  if (!payload || typeof payload !== 'object') return;
  const action = String(payload.action || '').toLowerCase();
  const itemId = String(payload.item_id || '').slice(0, 100);
  if (!itemId || !['status', 'cancel', 'steer', 'resume'].includes(action)) return;
  const message = { type: 'code_control', item_id: itemId, action };
  if (action === 'steer') {
    const text = String(payload.text || '').trim().slice(0, 4000);
    if (!text) return;
    message.text = text;
  }
  wsSend(message);
});

ipcMain.on('renderer-ready', (event) => {
  event.sender.send('state-change', currentState);
  event.sender.send('voice-speed', readVoiceSpeed());
  voiceMuted = readVoiceMuted();
  event.sender.send('voice-muted', voiceMuted);
  microphoneMuted = readMicrophoneMuted();
  event.sender.send('microphone-muted', microphoneMuted);
  event.sender.send('code-panel-width', codePanelWidthApplied || codePanelWidth);
  // A reload restores the drawer's contents, but only reopens it if it was
  // open when the renderer went away.
  if (currentCodeSnapshot) event.sender.send('code-snapshot', currentCodeSnapshot);
  if (codePanelVisible) event.sender.send('show-code-panel', null);
});

ipcMain.on('set-voice-speed', (_event, value) => {
  writeVoiceSpeed(value);
});

function setVoiceMuted(value) {
  voiceMuted = writeVoiceMuted(value);
  sendToRenderer('voice-muted', voiceMuted);
  updateTrayMenu();
}

ipcMain.on('set-voice-muted', (_event, value) => setVoiceMuted(value));

function setMicrophoneMuted(value) {
  microphoneMuted = writeMicrophoneMuted(value);
  sendToRenderer('microphone-muted', microphoneMuted);
  updateTrayMenu();
}

ipcMain.on('set-microphone-muted', (_event, value) => setMicrophoneMuted(value));

// --- App lifecycle ---

app.whenReady().then(() => {
  idlePinnedVisible = true;
  createWindow();
  createTray();
  connectWebSocket();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  isQuitting = true;
  clearIdleHideTimer();
  clearReconnectTimer();
  if (ws) ws.close();
});
