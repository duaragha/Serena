const { app, BrowserWindow, Tray, Menu, nativeImage, ipcMain, screen } = require('electron');
const { execFile } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');
const WebSocket = require('ws');

// NOTE: GNOME system tray requires the AppIndicator extension.
// Install via: sudo apt install gnome-shell-extension-appindicator
// Then enable it in GNOME Extensions or via:
//   gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com
// Without this, the tray icon will not be visible on GNOME desktops.

const WS_URL = 'ws://localhost:8765';
// One file, read by every process that speaks as her (this overlay's bridge,
// the desk conversation, the phone host), so the slider moves all of them.
const VOICE_SPEED_PATH = path.join(os.homedir(), '.config', 'serena', 'voice_speed');
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
const RECONNECT_INTERVAL_MS = 3000;
const WINDOW_WIDTH = 500;
const WINDOW_HEIGHT = 600;
const IDLE_HIDE_DELAY_MS = 1800;

let win = null;
let tray = null;
let ws = null;
let reconnectTimer = null;
let currentState = 'idle';
let focusModeEnabled = false;
let dashboardVisible = false;
let codePanelVisible = false;
let idlePinnedVisible = false;
let idleHideTimer = null;
let isQuitting = false;

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
  ipcMain.on('typed-message', (_event, text) => {
    const clean = String(text || '').trim();
    if (clean) wsSend({ type: 'typed', text: clean.slice(0, 4000) });
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
      click: (menuItem) => {
        codePanelVisible = menuItem.checked;
        sendToRenderer('toggle-code-panel', null);
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
  }
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

    case 'dashboard':
      sendToRenderer('dashboard-data', msg.data);
      break;

    case 'code_start':
      codePanelVisible = true;
      sendToRenderer('code-start', { project: msg.project, status: msg.status });
      showOverlay(false);
      updateTrayMenu();
      break;

    case 'code_hide':
      hideCodePanel();
      break;

    case 'code_event':
      sendToRenderer('code-event', msg.event);
      break;

    case 'code_done':
      sendToRenderer('code-done', { summary: msg.summary });
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
  sendToRenderer('hide-code-panel', null);
  if (currentState === 'idle') scheduleIdleHide();
  updateTrayMenu();
}

ipcMain.on('hide-code-panel', hideCodePanel);

ipcMain.on('renderer-ready', (event) => {
  event.sender.send('state-change', currentState);
  event.sender.send('voice-speed', readVoiceSpeed());
});

ipcMain.on('set-voice-speed', (_event, value) => {
  writeVoiceSpeed(value);
});

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
