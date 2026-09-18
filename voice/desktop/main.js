const { app, BrowserWindow, Tray, Menu, nativeImage, ipcMain, screen } = require('electron');
const fs = require('fs');
const os = require('os');
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

// The drawer is a column of the window, not a sheet over it: opening it widens
// the window by its own width and closing it gives that room straight back, so
// the dot field he was looking at never gets covered. These bounds are shared
// with renderer/styles.css, which sizes the column from the same numbers.
const DEFAULT_CODE_PANEL_WIDTH = 450;
const MIN_CODE_PANEL_WIDTH = 300;
const MAX_CODE_PANEL_WIDTH = 720;
const CODING_PANE_WIDTH_PATH = path.join(
  os.homedir(), '.config', 'serena', 'coding_pane_width',
);
// The only job states that are still going. Everything else is history, and
// history has no business taking the screen.
const RUNNING_JOB_STATES = new Set(['working', 'resume_queued']);

let win = null;
let tray = null;
let ws = null;
let reconnectTimer = null;
let currentState = 'idle';
let focusModeEnabled = false;
let dashboardVisible = false;
let codePanelVisible = false;
let codePanelWidth = DEFAULT_CODE_PANEL_WIDTH;
// The job whose snapshot the drawer is currently showing, and the dismissal he
// made against it. `itemId: null` is a blind dismissal -- he closed a drawer
// that was not showing any particular job -- and that one has to hold against
// every snapshot, because there is no job for it to key on.
let currentCodeItemId = null;
let codePanelDismissal = null;
let lastCodeSnapshot = null;

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
  // workArea, not workAreaSize: on a second monitor the usable rectangle has an
  // origin too, and a size-only read parks the window on the wrong screen.
  const { width: screenW, height: screenH } = screen.getPrimaryDisplay().workArea;

  codePanelWidth = readCodingPaneWidth();

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

// --- The coding drawer ---

function clampCodePanelWidth(value) {
  const width = Math.round(Number(value));
  if (!Number.isFinite(width)) return DEFAULT_CODE_PANEL_WIDTH;
  return Math.min(MAX_CODE_PANEL_WIDTH, Math.max(MIN_CODE_PANEL_WIDTH, width));
}

function readCodingPaneWidth() {
  try {
    return clampCodePanelWidth(fs.readFileSync(CODING_PANE_WIDTH_PATH, 'utf8').trim());
  } catch (_error) {
    // He has never dragged the separator. The default is not a failure.
    return DEFAULT_CODE_PANEL_WIDTH;
  }
}

function persistCodingPaneWidth(width) {
  const temporary = `${CODING_PANE_WIDTH_PATH}.tmp`;
  try {
    fs.mkdirSync(path.dirname(CODING_PANE_WIDTH_PATH), { recursive: true });
    fs.writeFileSync(temporary, String(width), 'utf8');
    fs.renameSync(temporary, CODING_PANE_WIDTH_PATH);
  } catch (error) {
    console.error('[drawer] could not save the coding pane width:', error.message);
  }
}

// Grow leftward so the window keeps whichever corner he parked it in.
function widenWindowBy(delta) {
  if (!win || win.isDestroyed() || !delta) return;
  const bounds = win.getBounds();
  win.setBounds({
    x: bounds.x - delta,
    y: bounds.y,
    width: bounds.width + delta,
    height: bounds.height,
  });
}

function openCodePanel() {
  codePanelDismissal = null;
  if (codePanelVisible) return;
  codePanelVisible = true;
  widenWindowBy(codePanelWidth);
  sendToRenderer('show-code-panel', null);
  updateTrayMenu();
}

function closeCodePanel({ dismissed = false } = {}) {
  if (dismissed) codePanelDismissal = { itemId: currentCodeItemId };
  if (!codePanelVisible) {
    updateTrayMenu();
    return;
  }
  codePanelVisible = false;
  widenWindowBy(-codePanelWidth);
  updateTrayMenu();
}

// A dismissal keyed on a job dies with that job. A blind one has nothing to
// die with, so it holds until the next job actually starts.
function dismissalBlocks(itemId) {
  if (!codePanelDismissal) return false;
  if (codePanelDismissal.itemId === null) return true;
  return codePanelDismissal.itemId === itemId;
}

function surfaceWindow() {
  if (!win || win.isDestroyed()) return;
  if (win.isMinimized()) win.restore();
  win.showInactive();
}

function handleCodeSnapshot(snapshot) {
  lastCodeSnapshot = snapshot || null;
  sendToRenderer('code-snapshot', lastCodeSnapshot);
  const itemId = (snapshot && snapshot.item_id) || null;
  if (!snapshot || !RUNNING_JOB_STATES.has(snapshot.state)) return;
  if (dismissalBlocks(itemId)) return;
  currentCodeItemId = itemId;
  openCodePanel();
}

function handleCodeStart(msg) {
  currentCodeItemId = msg.item_id || null;
  if (msg.snapshot) lastCodeSnapshot = msg.snapshot;
  sendToRenderer('code-start', {
    item_id: msg.item_id || null,
    project: msg.project,
    status: msg.status,
    snapshot: msg.snapshot || null,
  });
  // A job he just asked for is the one thing allowed to override a dismissal:
  // he started it, so he is expecting to see it.
  surfaceWindow();
  openCodePanel();
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
        if (menuItem.checked) openCodePanel();
        else closeCodePanel({ dismissed: true });
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
      handleCodeStart(msg);
      break;

    case 'code_snapshot':
      handleCodeSnapshot(msg.snapshot);
      break;

    case 'code_event':
      sendToRenderer('code-event', msg.event);
      break;

    case 'code_done':
      sendToRenderer('code-done', { summary: msg.summary });
      break;

    case 'toggle_code_panel':
      if (codePanelVisible) closeCodePanel({ dismissed: true });
      else openCodePanel();
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

// He closed the drawer. That sticks: the window gives the room back and no
// further snapshot of this job puts it in front of him again.
ipcMain.on('hide-code-panel', () => {
  closeCodePanel({ dismissed: true });
});

ipcMain.on('show-code-panel', () => {
  openCodePanel();
});

// He dragged the separator. Keep the rest of the window exactly where it is by
// absorbing the difference into the same edge the drawer grew from.
ipcMain.on('set-code-panel-width', (_event, requested) => {
  const width = clampCodePanelWidth(requested);
  if (codePanelVisible) widenWindowBy(width - codePanelWidth);
  codePanelWidth = width;
  persistCodingPaneWidth(width);
  sendToRenderer('code-panel-width', width);
});

// The renderer reloaded. Put its contents back, and the drawer only if it was
// open -- a reload is not a new job and must not undo a dismissal.
ipcMain.on('renderer-ready', (event) => {
  const sender = event && event.sender;
  if (!sender) return;
  sender.send('code-panel-width', codePanelWidth);
  if (lastCodeSnapshot) sender.send('code-snapshot', lastCodeSnapshot);
  if (codePanelVisible) sender.send('show-code-panel', null);
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
