const { app, BrowserWindow, Tray, Menu, nativeImage, ipcMain, screen } = require('electron');
const path = require('path');
const WebSocket = require('ws');

// NOTE: GNOME system tray requires the AppIndicator extension.
// Install via: sudo apt install gnome-shell-extension-appindicator
// Then enable it in GNOME Extensions or via:
//   gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com
// Without this, the tray icon will not be visible on GNOME desktops.

const WS_URL = 'ws://localhost:8765';
const RECONNECT_INTERVAL_MS = 3000;
const WINDOW_WIDTH = 500;
const WINDOW_HEIGHT = 600;

let win = null;
let tray = null;
let ws = null;
let reconnectTimer = null;
let currentState = 'idle';
let focusModeEnabled = false;
let dashboardVisible = false;
let codePanelVisible = false;

// --- Tray icon generation ---

function createTrayIcon(state) {
  // Simple colored circle: green=idle, blue=listening, orange=thinking, purple=speaking
  const colors = {
    idle: '#4ade80',
    listening: '#3b82f6',
    thinking: '#f59e0b',
    speaking: '#a855f7',
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
  const { width: screenW, height: screenH } = screen.getPrimaryDisplay().workAreaSize;

  win = new BrowserWindow({
    width: WINDOW_WIDTH,
    height: WINDOW_HEIGHT,
    x: screenW - WINDOW_WIDTH - 20,
    y: screenH - WINDOW_HEIGHT - 20,
    title: 'Serena',
    frame: true,
    resizable: true,
    minimizable: true,
    maximizable: true,
    show: false,
    backgroundColor: '#0a0a1a',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));

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
  ipcMain.on('set-ignore-mouse', (_event, _ignore) => {
    // No longer needed — proper window has real hit testing
  });

  win.on('closed', () => {
    win = null;
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
    win.hide();
  } else {
    win.show();
  }
  updateTrayMenu();
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
    // Show the window once the backend is connected
    if (win && !win.isVisible()) {
      win.show();
      updateTrayMenu();
    }
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
    // Suppress ECONNREFUSED noise — it just means the backend isn't up yet
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
      sendToRenderer('code-start', { project: msg.project });
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
  sendToRenderer('toggle-dashboard', dashboardVisible);
  updateTrayMenu();
});

// --- App lifecycle ---

app.whenReady().then(() => {
  createWindow();
  createTray();
  connectWebSocket();
});

app.on('window-all-closed', () => {
  // Don't quit on window close — keep running in tray
});

app.on('before-quit', () => {
  clearReconnectTimer();
  if (ws) ws.close();
});
