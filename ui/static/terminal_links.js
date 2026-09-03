(function installSerenaTerminalLinks(root) {
  'use strict';

  function normalizeExternalUri(value) {
    if (typeof value !== 'string') return null;
    const uri = value.trim();
    if (!uri || /\s/.test(uri)) return null;
    try {
      const parsed = new URL(uri);
      if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
      if (!parsed.host) return null;
      return uri;
    } catch (_) {
      return null;
    }
  }

  function openExternalUri(value, options) {
    const uri = normalizeExternalUri(value);
    if (!uri) return false;
    const bridge = options && options.gtkSend
      ? options.gtkSend
      : root.gtkSend;
    if (typeof bridge === 'function') {
      bridge({ type: 'open-external-uri', uri });
      return true;
    }
    const opener = options && options.openWindow
      ? options.openWindow
      : (typeof root.open === 'function' ? root.open.bind(root) : null);
    if (!opener) return false;
    const opened = opener(uri, '_blank', 'noopener,noreferrer');
    if (opened) {
      try { opened.opener = null; } catch (_) {}
      return true;
    }
    return false;
  }

  const api = Object.freeze({ normalizeExternalUri, openExternalUri });
  root.SerenaTerminalLinks = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
