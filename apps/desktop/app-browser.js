'use strict';

/*
 * Serena's in-app browser.
 *
 * Tabs are WebContentsViews inside the main window, so a dev server, a link
 * from a terminal or a local file opens without leaving the app, and the
 * agents -- her terminal sessions and her voice -- drive the same tabs he is
 * looking at through a token-guarded control server on 127.0.0.1.
 *
 * Memory is the point of doing it here rather than in Chrome: pages run in
 * the Chromium this app already has, only the tab on screen is attached,
 * hidden tabs are throttled, and a tab left hidden for DISCARD_AFTER_MS is
 * unloaded (its URL kept) until he or an agent comes back to it. At most
 * MAX_LIVE_TABS pages are ever alive at once.
 *
 * Electron and Node are passed in so the logic runs under node:test with
 * fakes; main.js wires the real modules.
 */

const DISCARD_AFTER_MS = 5 * 60 * 1000;
const MAX_LIVE_TABS = 3;
const LOG_LIMIT = 200;
const SHOT_LIMIT = 20;
const BODY_LIMIT = 1024 * 1024;
const PARTITION = 'persist:serena-browser';
const DEFAULT_BOUNDS = Object.freeze({ x: 0, y: 0, width: 1280, height: 800 });

/*
 * What may be opened: web pages, local files and blank. A bare host or
 * host:port is what people type for dev servers ("localhost:5173"), so it is
 * taken as http. Anything that can run code in the app's own origin
 * (javascript:, data:, the app's internal schemes) is refused.
 */
function normalizeTarget(value) {
  const raw = String(value || '').trim();
  if (!raw || raw === 'about:blank') return 'about:blank';
  if (raw.startsWith('/') || /^[A-Za-z]:[\\/]/.test(raw)) {
    return `file://${raw.startsWith('/') ? '' : '/'}${raw.replace(/\\/g, '/')}`;
  }
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(raw);
  if (scheme && /^(javascript|vbscript|data|blob|about|chrome|chrome-extension|devtools|view-source)$/i
    .test(scheme[1])) {
    throw new Error(`the in-app browser does not open ${scheme[1].toLowerCase()}: URLs`);
  }
  let candidate = raw;
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(candidate)) {
    // No "scheme://": either a bare host (with a port or path) or a scheme
    // that has no business here -- "javascript:1" is not host "javascript".
    if (/^[a-z][a-z0-9+.-]*:(?!\d)/i.test(candidate)) {
      throw new Error(`the in-app browser does not open ${candidate.split(':')[0]}: URLs`);
    }
    const local = /^(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:|\/|$)/i.test(candidate);
    candidate = `${local ? 'http' : 'https'}://${candidate}`;
  }
  let parsed;
  try {
    parsed = new URL(candidate);
  } catch {
    throw new Error(`not a URL or file path: ${raw}`);
  }
  if (!['http:', 'https:', 'file:'].includes(parsed.protocol)) {
    throw new Error(`the in-app browser does not open ${parsed.protocol} URLs`);
  }
  return parsed.toString();
}

function pushCapped(list, entry) {
  list.push(entry);
  if (list.length > LOG_LIMIT) list.splice(0, list.length - LOG_LIMIT);
}

// Tags visible interactive elements with data-serena-ref and returns them,
// with the page text, so an agent can say "click e12" without selectors.
const SNAPSHOT_SCRIPT = `(() => {
  for (const old of document.querySelectorAll('[data-serena-ref]')) old.removeAttribute('data-serena-ref');
  const selector = 'a[href],button,input,textarea,select,summary,[role=button],[role=link],' +
    '[role=checkbox],[role=tab],[role=menuitem],[role=option],[role=switch],[contenteditable=""],' +
    '[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"])';
  const elements = [];
  let n = 0;
  for (const el of document.querySelectorAll(selector)) {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (!box.width || !box.height || style.visibility === 'hidden' || style.display === 'none') continue;
    const ref = 'e' + (++n);
    el.setAttribute('data-serena-ref', ref);
    const name = (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('placeholder')
      || el.getAttribute('title') || el.getAttribute('alt') || el.getAttribute('name') || '')
      .trim().replace(/\\s+/g, ' ').slice(0, 80);
    const item = { ref, tag: el.tagName.toLowerCase(), name };
    const role = el.getAttribute('role'); if (role) item.role = role;
    if (el.type) item.type = el.type;
    if (el.href) item.href = el.href;
    if (el.disabled) item.disabled = true;
    if (el.checked) item.checked = true;
    if (innerHeight && (box.bottom < 0 || box.top > innerHeight)) item.offscreen = true;
    elements.push(item);
    if (elements.length >= 300) break;
  }
  const text = document.body ? document.body.innerText.replace(/\\n{3,}/g, '\\n\\n').slice(0, 20000) : '';
  return { url: location.href, title: document.title, text, elements };
})()`;

function locateScript(target) {
  const find = target.ref
    ? `document.querySelector('[data-serena-ref="' + ${JSON.stringify(String(target.ref))} + '"]')`
    : `document.querySelector(${JSON.stringify(String(target.selector))})`;
  return `(() => {
    const el = ${find};
    if (!el) return null;
    el.scrollIntoView({ block: 'center', inline: 'center' });
    const box = el.getBoundingClientRect();
    return { x: Math.round(box.left + box.width / 2), y: Math.round(box.top + box.height / 2),
             tag: el.tagName.toLowerCase() };
  })()`;
}

function createAppBrowser(deps) {
  const {
    WebContentsView,
    session,
    getWindow,
    userDataDir,
    fs,
    path,
    http,
    crypto,
    profile = { channel: 'stable', version: '' },
    now = () => Date.now(),
    setIntervalFn = setInterval,
    clearIntervalFn = clearInterval,
    log = () => {},
  } = deps;

  const tabs = new Map();
  let activeId = null;
  let seq = 0;
  let placeholder = null; // renderer-reported rect of the browser pane
  let shown = false;
  let rendererSend = null;
  let sweep = null;
  let server = null;
  let token = null;
  let hooked = false;

  const shotsDir = path.join(userDataDir, 'app-browser', 'shots');
  const controlFile = path.join(userDataDir, 'app-browser.json');
  const sessionFile = path.join(userDataDir, 'app-browser', 'tabs.json');
  let saveTimer = null;

  // Tabs survive a restart the way a browser's do: back unloaded, loaded
  // when he or an agent goes to one.
  function saveTabs() {
    if (saveTimer) return;
    saveTimer = setTimeout(() => {
      saveTimer = null;
      try {
        fs.mkdirSync(path.dirname(sessionFile), { recursive: true, mode: 0o700 });
        const saved = [...tabs.values()].filter((tab) => tab.url && tab.url !== 'about:blank')
          .map((tab) => ({ url: tab.url, title: tab.title, active: tab.id === activeId }));
        fs.writeFileSync(sessionFile, JSON.stringify(saved), { mode: 0o600 });
      } catch (error) {
        log(`[browser] could not save tabs: ${error.message}`);
      }
    }, 500);
    if (saveTimer.unref) saveTimer.unref();
  }

  function restoreTabs() {
    let saved = [];
    try {
      saved = JSON.parse(fs.readFileSync(sessionFile, 'utf8'));
    } catch {
      return;
    }
    for (const item of Array.isArray(saved) ? saved.slice(0, 20) : []) {
      let url;
      try {
        url = normalizeTarget(item.url);
      } catch {
        continue;
      }
      seq += 1;
      const tab = { id: `t${seq}`, url, title: String(item.title || ''), view: null, attached: false,
        lastShown: 0, console: [], network: [], loading: false };
      tabs.set(tab.id, tab);
      if (item.active) activeId = tab.id;
    }
    if (!activeId && tabs.size) activeId = [...tabs.keys()].pop();
  }

  function partition() {
    const browserSession = session.fromPartition(PARTITION);
    if (!hooked && browserSession.webRequest) {
      hooked = true;
      const record = (details, entry) => {
        const tab = [...tabs.values()].find((item) => item.view
          && item.view.webContents.id === details.webContentsId);
        if (tab) pushCapped(tab.network, { at: now(), url: details.url, method: details.method, ...entry });
      };
      browserSession.webRequest.onCompleted((details) => {
        if (details.statusCode >= 400) record(details, { status: details.statusCode });
      });
      browserSession.webRequest.onErrorOccurred((details) => record(details, { error: details.error }));
    }
    return PARTITION;
  }

  function publicTab(tab) {
    return {
      id: tab.id,
      url: tab.url,
      title: tab.title,
      loading: Boolean(tab.loading),
      active: tab.id === activeId,
      unloaded: !tab.view,
    };
  }

  function state() {
    return { tabs: [...tabs.values()].map(publicTab), activeId };
  }

  function emit() {
    saveTabs();
    if (rendererSend) rendererSend('browser:state', state());
  }

  function windowOrNull() {
    const win = getWindow();
    return win && !win.isDestroyed() ? win : null;
  }

  function scaledBounds() {
    const win = windowOrNull();
    const zoom = win ? (win.webContents.getZoomFactor ? win.webContents.getZoomFactor() : 1) : 1;
    const rect = placeholder || DEFAULT_BOUNDS;
    return {
      x: Math.round(rect.x * zoom),
      y: Math.round(rect.y * zoom),
      width: Math.max(1, Math.round(rect.width * zoom)),
      height: Math.max(1, Math.round(rect.height * zoom)),
    };
  }

  function wire(tab, view) {
    const contents = view.webContents;
    contents.setWindowOpenHandler(({ url }) => {
      open({ url, newTab: true }).catch((error) => log(`[browser] popup refused: ${error.message}`));
      return { action: 'deny' };
    });
    const retitle = () => {
      tab.url = contents.getURL() || tab.url;
      tab.title = contents.getTitle() || tab.title;
      emit();
    };
    contents.on('page-title-updated', retitle);
    contents.on('did-navigate', retitle);
    contents.on('did-navigate-in-page', retitle);
    contents.on('did-start-loading', () => { tab.loading = true; emit(); });
    contents.on('did-stop-loading', () => { tab.loading = false; retitle(); });
    contents.on('did-fail-load', (_event, code, description, url, isMainFrame) => {
      if (code === -3) return; // aborted by a newer navigation
      pushCapped(tab.network, { at: now(), url, error: description, code, mainFrame: Boolean(isMainFrame) });
    });
    contents.on('console-message', (...args) => {
      // Electron 35+ passes one event object; older builds passed positionals.
      const first = args[0] || {};
      const entry = first.message !== undefined
        ? { level: first.level, message: first.message, line: first.lineNumber, source: first.sourceId }
        : { level: args[1], message: args[2], line: args[3], source: args[4] };
      pushCapped(tab.console, { at: now(), ...entry });
    });
    contents.on('render-process-gone', (_event, details) => {
      pushCapped(tab.console, { at: now(), level: 'error', message: `page crashed: ${details.reason}` });
      discard(tab);
      emit();
    });
  }

  function makeView(tab) {
    const view = new WebContentsView({
      webPreferences: {
        partition: partition(),
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
        backgroundThrottling: true,
      },
    });
    tab.view = view;
    wire(tab, view);
    return view;
  }

  function detach(tab) {
    const win = windowOrNull();
    if (win && tab.view && tab.attached) {
      try { win.contentView.removeChildView(tab.view); } catch { /* already gone */ }
    }
    tab.attached = false;
  }

  function discard(tab) {
    if (!tab.view) return;
    detach(tab);
    const contents = tab.view.webContents;
    tab.view = null;
    try {
      if (!contents.isDestroyed()) contents.close();
    } catch { /* already closed */ }
  }

  // Only the tab on screen is attached; the rest stay throttled or unloaded.
  function layout() {
    const win = windowOrNull();
    for (const tab of tabs.values()) {
      if (tab.id !== activeId || !shown) detach(tab);
    }
    const tab = tabs.get(activeId);
    if (!tab || !shown || !win) return;
    if (!tab.view) {
      makeView(tab);
      tab.view.webContents.loadURL(tab.url).catch(() => {});
    }
    tab.view.setBounds(scaledBounds());
    if (!tab.attached) {
      win.contentView.addChildView(tab.view);
      tab.attached = true;
    }
    tab.lastShown = now();
    enforceLiveCap();
  }

  function enforceLiveCap() {
    const live = [...tabs.values()].filter((tab) => tab.view && tab.id !== activeId)
      .sort((a, b) => a.lastShown - b.lastShown);
    let count = live.length + (tabs.get(activeId)?.view ? 1 : 0);
    for (const tab of live) {
      if (count <= MAX_LIVE_TABS) break;
      discard(tab);
      count -= 1;
    }
  }

  function sweepIdle() {
    const moment = now();
    for (const tab of tabs.values()) {
      if (tab.view && !(tab.id === activeId && shown) && moment - tab.lastShown > DISCARD_AFTER_MS) {
        discard(tab);
      }
    }
    emit();
  }

  function resolveTab(id) {
    const tab = tabs.get(id || activeId);
    if (!tab) throw new Error(id ? `no browser tab ${id}` : 'no browser tab is open');
    return tab;
  }

  // An agent's command needs a live page. Unloaded tabs come back first.
  async function ensureLive(tab) {
    if (!tab.view) {
      makeView(tab);
      tab.lastShown = now();
      await tab.view.webContents.loadURL(tab.url).catch(() => {});
      layout();
      enforceLiveCap();
    }
    return tab.view.webContents;
  }

  function reveal() {
    if (rendererSend) rendererSend('browser:reveal', { activeId });
  }

  async function open({ url, newTab = false, tab: id } = {}) {
    const target = normalizeTarget(url);
    let tab = !newTab && (id || activeId) ? tabs.get(id || activeId) : null;
    if (!tab) {
      seq += 1;
      tab = { id: `t${seq}`, url: target, title: '', view: null, attached: false, lastShown: now(),
        console: [], network: [], loading: false };
      tabs.set(tab.id, tab);
    }
    activeId = tab.id;
    tab.url = target;
    tab.lastShown = now();
    if (!tab.view) makeView(tab);
    layout();
    reveal();
    emit();
    await tab.view.webContents.loadURL(target).catch((error) => {
      // A failed load is reported through did-fail-load; the tab stays.
      pushCapped(tab.network, { at: now(), url: target, error: error.message, mainFrame: true });
    });
    tab.url = tab.view ? tab.view.webContents.getURL() || target : target;
    tab.title = tab.view ? tab.view.webContents.getTitle() : tab.title;
    emit();
    return publicTab(tab);
  }

  async function navigate({ tab: id, action }) {
    const tab = resolveTab(id);
    const contents = await ensureLive(tab);
    const history = contents.navigationHistory || contents;
    if (action === 'back' && (history.canGoBack ? history.canGoBack() : true)) history.goBack();
    else if (action === 'forward' && (history.canGoForward ? history.canGoForward() : true)) history.goForward();
    else if (action === 'reload') contents.reload();
    else if (action === 'stop') contents.stop();
    else if (!['back', 'forward'].includes(action)) throw new Error(`unknown navigation: ${action}`);
    return publicTab(tab);
  }

  function select({ tab: id }) {
    const tab = resolveTab(id);
    activeId = tab.id;
    layout();
    emit();
    return publicTab(tab);
  }

  function close({ tab: id }) {
    const tab = resolveTab(id);
    discard(tab);
    tabs.delete(tab.id);
    if (activeId === tab.id) {
      const rest = [...tabs.values()].sort((a, b) => b.lastShown - a.lastShown);
      activeId = rest.length ? rest[0].id : null;
    }
    layout();
    emit();
    return { closed: tab.id };
  }

  async function snapshot({ tab: id }) {
    const tab = resolveTab(id);
    const contents = await ensureLive(tab);
    const page = await contents.executeJavaScript(SNAPSHOT_SCRIPT, true);
    return { tab: tab.id, ...page };
  }

  async function point(tab, target) {
    const contents = await ensureLive(tab);
    if (target.x !== undefined && target.y !== undefined) return { contents, x: Number(target.x), y: Number(target.y) };
    if (!target.ref && !target.selector) throw new Error('say which element: ref (from look), selector, or x/y');
    const found = await contents.executeJavaScript(locateScript(target), true);
    if (!found) throw new Error(`no element ${target.ref || target.selector}; look again, the page may have changed`);
    return { contents, ...found };
  }

  async function click({ tab: id, ...target }) {
    const tab = resolveTab(id);
    const { contents, x, y } = await point(tab, target);
    const button = target.button || 'left';
    if (!tab.attached && (target.ref || target.selector)) {
      // Mouse events need a painted view; off screen, click the element itself.
      const clicked = await contents.executeJavaScript(`(() => { const el = ${target.ref
        ? `document.querySelector('[data-serena-ref="' + ${JSON.stringify(String(target.ref))} + '"]')`
        : `document.querySelector(${JSON.stringify(String(target.selector))})`}; if (!el) return false;
        el.click(); return true; })()`, true);
      if (!clicked) throw new Error('that element is gone; look again');
      return { tab: tab.id, clicked: target.ref || target.selector, x, y, offscreen: true };
    }
    // Real input events, so framework handlers see a genuine click.
    contents.sendInputEvent({ type: 'mouseMove', x, y });
    contents.sendInputEvent({ type: 'mouseDown', x, y, button, clickCount: 1 });
    contents.sendInputEvent({ type: 'mouseUp', x, y, button, clickCount: 1 });
    return { tab: tab.id, clicked: target.ref || target.selector || `${x},${y}`, x, y };
  }

  async function type({ tab: id, text = '', clear = true, submit = false, ...target }) {
    const tab = resolveTab(id);
    const contents = await ensureLive(tab);
    if (target.ref || target.selector) {
      await point(tab, target);
      const focus = `(() => { const el = ${target.ref
        ? `document.querySelector('[data-serena-ref="' + ${JSON.stringify(String(target.ref))} + '"]')`
        : `document.querySelector(${JSON.stringify(String(target.selector))})`};
        if (!el) return false; el.focus();
        if (${clear ? 'true' : 'false'} && 'value' in el) { el.select && el.select(); }
        return true; })()`;
      if (!await contents.executeJavaScript(focus, true)) throw new Error('that element is gone; look again');
    }
    contents.insertText(String(text));
    if (submit) await press({ tab: tab.id, key: 'Enter' });
    return { tab: tab.id, typed: String(text).length };
  }

  async function press({ tab: id, key }) {
    const tab = resolveTab(id);
    const contents = await ensureLive(tab);
    const keyCode = String(key || '');
    if (!keyCode) throw new Error('say which key');
    contents.sendInputEvent({ type: 'keyDown', keyCode });
    if (keyCode.length === 1) contents.sendInputEvent({ type: 'char', keyCode });
    contents.sendInputEvent({ type: 'keyUp', keyCode });
    return { tab: tab.id, pressed: keyCode };
  }

  async function evaluate({ tab: id, js }) {
    const tab = resolveTab(id);
    const contents = await ensureLive(tab);
    const result = await contents.executeJavaScript(String(js || ''), true);
    let text;
    try {
      text = JSON.stringify(result);
    } catch {
      text = String(result);
    }
    return { tab: tab.id, result: text === undefined ? 'undefined' : text.slice(0, 20000) };
  }

  async function screenshot({ tab: id }) {
    const tab = resolveTab(id);
    const contents = await ensureLive(tab);
    const image = await contents.capturePage();
    const png = image.toPNG();
    if (!png.length) throw new Error('the page painted nothing; bring the browser tab on screen and try again');
    fs.mkdirSync(shotsDir, { recursive: true, mode: 0o700 });
    const file = path.join(shotsDir, `${tab.id}-${now()}.png`);
    fs.writeFileSync(file, png, { mode: 0o600 });
    const shots = fs.readdirSync(shotsDir).filter((name) => name.endsWith('.png')).sort();
    for (const old of shots.slice(0, Math.max(0, shots.length - SHOT_LIMIT))) {
      try { fs.unlinkSync(path.join(shotsDir, old)); } catch { /* raced */ }
    }
    const size = image.getSize ? image.getSize() : {};
    return { tab: tab.id, path: file, width: size.width, height: size.height };
  }

  function logs({ tab: id, since = 0 }) {
    const tab = resolveTab(id);
    const after = Number(since) || 0;
    return {
      tab: tab.id,
      console: tab.console.filter((entry) => entry.at > after),
      network: tab.network.filter((entry) => entry.at > after),
    };
  }

  async function waitFor({ tab: id, text, selector, timeoutMs = 10000 }) {
    const tab = resolveTab(id);
    const contents = await ensureLive(tab);
    const probe = selector
      ? `Boolean(document.querySelector(${JSON.stringify(String(selector))}))`
      : `Boolean(document.body && document.body.innerText.includes(${JSON.stringify(String(text || ''))}))`;
    const deadline = now() + Math.min(Number(timeoutMs) || 10000, 60000);
    for (;;) {
      if (await contents.executeJavaScript(probe, true).catch(() => false)) return { tab: tab.id, found: true };
      if (now() >= deadline) return { tab: tab.id, found: false };
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }

  const commands = {
    tabs: async () => state(),
    open,
    navigate,
    back: (args) => navigate({ ...args, action: 'back' }),
    forward: (args) => navigate({ ...args, action: 'forward' }),
    reload: (args) => navigate({ ...args, action: 'reload' }),
    select: async (args) => select(args),
    close: async (args) => close(args),
    look: snapshot,
    snapshot,
    click,
    type,
    press,
    eval: evaluate,
    screenshot,
    logs: async (args) => logs(args),
    wait: waitFor,
  };

  async function run(name, args) {
    const command = Object.prototype.hasOwnProperty.call(commands, name) ? commands[name] : null;
    if (!command) throw new Error(`unknown browser command: ${name}`);
    return command(args && typeof args === 'object' ? args : {});
  }

  // -- the renderer's pane ---------------------------------------------------

  function setPlaceholder(rect) {
    if (!rect || typeof rect !== 'object') return;
    shown = Boolean(rect.visible) && rect.width > 0 && rect.height > 0;
    if (shown) {
      placeholder = {
        x: Number(rect.x) || 0,
        y: Number(rect.y) || 0,
        width: Number(rect.width) || 0,
        height: Number(rect.height) || 0,
      };
    }
    layout();
  }

  function registerIpc(ipcMain, senderIsTrusted) {
    ipcMain.handle('browser:command', (event, name, args) => {
      if (!senderIsTrusted(event)) throw new Error('untrusted IPC sender');
      rendererSend = (channel, payload) => {
        if (!event.sender.isDestroyed()) event.sender.send(channel, payload);
      };
      return run(String(name), args);
    });
    ipcMain.on('browser:bounds', (event, rect) => {
      if (!senderIsTrusted(event)) return;
      rendererSend = (channel, payload) => {
        if (!event.sender.isDestroyed()) event.sender.send(channel, payload);
      };
      setPlaceholder(rect);
    });
  }

  // -- the agents' control server --------------------------------------------

  function writeControlFile(port) {
    fs.mkdirSync(path.dirname(controlFile), { recursive: true, mode: 0o700 });
    const temporary = `${controlFile}.${process.pid}.tmp`;
    fs.writeFileSync(temporary, JSON.stringify({
      port,
      token,
      pid: process.pid,
      channel: profile.channel,
      version: profile.version,
    }), { mode: 0o600 });
    fs.renameSync(temporary, controlFile);
  }

  function authorized(request) {
    const header = String(request.headers.authorization || '');
    const offered = Buffer.from(header.replace(/^Bearer\s+/i, ''));
    const expected = Buffer.from(token || '');
    return offered.length === expected.length && offered.length > 0
      && crypto.timingSafeEqual(offered, expected);
  }

  function respond(response, status, payload) {
    const body = JSON.stringify(payload);
    response.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' });
    response.end(body);
  }

  async function handle(request, response) {
    // A web page can reach 127.0.0.1 too; pages always send Origin, agents do not.
    if (request.headers.origin) return respond(response, 403, { ok: false, error: 'browsers may not drive this' });
    if (request.method !== 'POST') return respond(response, 405, { ok: false, error: 'POST only' });
    if (!authorized(request)) return respond(response, 401, { ok: false, error: 'bad token' });
    const match = /^\/browser\/([a-z]+)$/.exec(request.url || '');
    if (!match) return respond(response, 404, { ok: false, error: 'unknown path' });
    let raw = '';
    for await (const chunk of request) {
      raw += chunk;
      if (raw.length > BODY_LIMIT) return respond(response, 413, { ok: false, error: 'request too large' });
    }
    let args = {};
    try {
      args = raw ? JSON.parse(raw) : {};
    } catch {
      return respond(response, 400, { ok: false, error: 'body is not JSON' });
    }
    try {
      return respond(response, 200, { ok: true, ...(await run(match[1], args)) });
    } catch (error) {
      return respond(response, 200, { ok: false, error: error.message });
    }
  }

  function start() {
    restoreTabs();
    sweep = setIntervalFn(sweepIdle, 30 * 1000);
    if (sweep && sweep.unref) sweep.unref();
    token = crypto.randomBytes(24).toString('hex');
    server = http.createServer((request, response) => {
      handle(request, response).catch((error) => respond(response, 500, { ok: false, error: error.message }));
    });
    return new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(0, '127.0.0.1', () => {
        writeControlFile(server.address().port);
        resolve(server.address().port);
      });
    });
  }

  function stop() {
    if (sweep) clearIntervalFn(sweep);
    sweep = null;
    for (const tab of tabs.values()) discard(tab);
    if (server) server.close();
    server = null;
    try { fs.unlinkSync(controlFile); } catch { /* not written */ }
  }

  return {
    start,
    stop,
    run,
    registerIpc,
    setPlaceholder,
    sweepIdle,
    state,
    controlFile,
    _tabs: tabs,
  };
}

module.exports = {
  DISCARD_AFTER_MS,
  MAX_LIVE_TABS,
  PARTITION,
  createAppBrowser,
  normalizeTarget,
};
