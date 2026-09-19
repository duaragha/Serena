// Serena Ambient Tabs (MV3): report tab activation, tab updates, and window
// focus to the local native host. Incognito tabs are never reported — the
// extension must also stay disallowed in incognito (the default) — and only
// the tab's URL and title cross the pipe, never page contents.
const HOST_NAME = "com.serena.ambient";
const DEBOUNCE_MS = 2000;
let port = null;
let lastReport = { key: "", at: 0 };
// tab.active is per window, so it cannot tell the foreground tab from the
// active tab of a minimized window. Track the focused window instead; null
// (before the first lookup) fails open rather than dropping real activity.
let focusedWindowId = null;

function noteFocusedWindow() {
  chrome.windows.getLastFocused((window) => {
    if (!chrome.runtime.lastError && window) {
      focusedWindowId = window.id;
    }
  });
}

function isForeground(tab) {
  return !!tab.active && (focusedWindowId === null || tab.windowId === focusedWindowId);
}

function connect() {
  try {
    port = chrome.runtime.connectNative(HOST_NAME);
  } catch (error) {
    port = null;
    setTimeout(connect, 5000);
    return;
  }
  port.onDisconnect.addListener(() => {
    port = null;
    setTimeout(connect, 5000);
  });
}

function report(tab) {
  if (!tab || tab.incognito || !tab.url || tab.url === "about:blank") {
    return;
  }
  const key = `${tab.url} ${tab.title || ""}`;
  const now = Date.now();
  if (key === lastReport.key && now - lastReport.at < DEBOUNCE_MS) {
    return;
  }
  lastReport = { key, at: now };
  if (!port) {
    connect();
    if (!port) {
      return;
    }
  }
  try {
    port.postMessage({
      extensionId: chrome.runtime.id,
      type: "tab",
      url: tab.url,
      title: tab.title || "",
      incognito: !!tab.incognito,
      active: !!tab.active,
    });
  } catch (error) {
    port = null;
  }
}

function reportActiveTab() {
  chrome.tabs.query({ active: true, lastFocusedWindow: true }, (tabs) => {
    if (chrome.runtime.lastError || !tabs || !tabs.length) {
      return;
    }
    report(tabs[0]);
  });
}

chrome.tabs.onActivated.addListener((info) => {
  chrome.tabs.get(info.tabId, (tab) => {
    if (chrome.runtime.lastError || !tab || !isForeground(tab)) {
      return;
    }
    report(tab);
  });
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  // Foreground only, like the X11 sensor: a background tab finishing a load
  // must not flood the store or read as what he is doing.
  if (!isForeground(tab)) {
    return;
  }
  if (changeInfo.url || changeInfo.title || changeInfo.status === "complete") {
    report(tab);
  }
});

chrome.windows.onFocusChanged.addListener((windowId) => {
  if (windowId === chrome.windows.WINDOW_ID_NONE) {
    return;
  }
  focusedWindowId = windowId;
  reportActiveTab();
});

noteFocusedWindow();
connect();
