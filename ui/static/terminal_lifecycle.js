(function installSerenaTerminalLifecycle(root) {
  'use strict';

  const MIN_WIDTH = 160;
  const MIN_HEIGHT = 120;
  const TAIL_FOLLOW_MS = 6000;

  function isRenderable(state) {
    const value = state || {};
    const rect = value.rect || {};
    return value.tab === 'chats'
      && value.mode === 'live'
      && !value.hidden
      && Number(rect.width || 0) >= MIN_WIDTH
      && Number(rect.height || 0) >= MIN_HEIGHT;
  }

  function afterReveal(callback, options) {
    const raf = options && options.requestAnimationFrame
      ? options.requestAnimationFrame
      : root.requestAnimationFrame.bind(root);
    raf(() => raf(() => callback()));
  }

  function tailDeadline(now) {
    const value = Number(now);
    return (Number.isFinite(value) ? value : 0) + TAIL_FOLLOW_MS;
  }

  function shouldFollowTail(deadline, now) {
    const end = Number(deadline);
    const current = Number(now);
    return Number.isFinite(end) && end > 0
      && Number.isFinite(current) && current <= end;
  }

  const api = Object.freeze({
    MIN_WIDTH,
    MIN_HEIGHT,
    TAIL_FOLLOW_MS,
    isRenderable,
    afterReveal,
    tailDeadline,
    shouldFollowTail,
  });

  root.SerenaTerminalLifecycle = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
