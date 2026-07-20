/**
 * NotificationManager — toast-style notification popups.
 * Stacks from bottom-right, max 3 visible, auto-dismiss with configurable duration.
 */

let nextId = 1;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

const DURATION_BY_PRIORITY = {
  high: 10000,
  medium: 5000,
  low: 5000,
};

const MAX_VISIBLE = 3;

export class NotificationManager {
  /** @param {HTMLElement} container */
  constructor(container) {
    this._container = el('div', 'notification-container');
    container.appendChild(this._container);

    /** @type {Map<number, {element: HTMLElement, timer: number|null, onClick: Function|null}>} */
    this._active = new Map();

    // Queue for when we already have MAX_VISIBLE showing
    /** @type {Array<{text: string, options: object}>} */
    this._queue = [];

    // Enable mouse interaction on notification area
    this._container.addEventListener('mouseenter', () => {
      if (window.serena) window.serena.setIgnoreMouse(false);
    });
    this._container.addEventListener('mouseleave', () => {
      if (window.serena) window.serena.setIgnoreMouse(true);
    });
  }

  /**
   * Show a notification toast.
   * @param {string} text
   * @param {{priority?: 'high'|'medium'|'low', duration?: number, onClick?: Function}} [options]
   * @returns {number} notification id
   */
  show(text, options = {}) {
    const priority = options.priority || 'medium';
    const duration = options.duration ?? DURATION_BY_PRIORITY[priority] ?? 5000;

    // If at capacity, queue it
    if (this._active.size >= MAX_VISIBLE) {
      this._queue.push({ text, options: { ...options, priority, duration } });
      return -1; // queued, no id yet
    }

    return this._create(text, priority, duration, options.onClick || null);
  }

  /**
   * Dismiss a specific notification by id.
   * @param {number} id
   */
  dismiss(id) {
    const entry = this._active.get(id);
    if (!entry) return;

    if (entry.timer) clearTimeout(entry.timer);

    entry.element.classList.remove('notification-toast--visible');
    entry.element.classList.add('notification-toast--dismissing');

    entry.element.addEventListener('transitionend', () => {
      entry.element.remove();
      this._active.delete(id);
      this._showQueued();
    }, { once: true });

    // Fallback removal if transition doesn't fire
    setTimeout(() => {
      if (this._active.has(id)) {
        entry.element.remove();
        this._active.delete(id);
        this._showQueued();
      }
    }, 400);
  }

  /** Dismiss all notifications and clear the queue. */
  dismissAll() {
    this._queue = [];
    for (const id of [...this._active.keys()]) {
      this.dismiss(id);
    }
  }

  // ---- Internal ----

  _create(text, priority, duration, onClick) {
    const id = nextId++;

    const toast = el('div', `notification-toast notification-toast--${priority}`);
    toast.setAttribute('data-notification-id', id);

    toast.appendChild(el('div', 'notification-toast__text text-shadow', text));

    const now = new Date();
    const timeStr = now.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
    toast.appendChild(el('div', 'notification-toast__time', timeStr));

    // Click to dismiss (or custom handler)
    toast.addEventListener('click', () => {
      if (onClick) onClick(id);
      this.dismiss(id);
    });

    // Pause auto-dismiss on hover
    let timer = null;
    let remaining = duration;
    let startTime = 0;

    const startTimer = () => {
      startTime = Date.now();
      timer = setTimeout(() => this.dismiss(id), remaining);
    };

    toast.addEventListener('mouseenter', () => {
      if (timer) {
        clearTimeout(timer);
        remaining -= Date.now() - startTime;
        if (remaining < 0) remaining = 0;
        timer = null;
      }
    });

    toast.addEventListener('mouseleave', () => {
      startTimer();
    });

    this._container.appendChild(toast);

    // Trigger entrance animation on next frame
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        toast.classList.add('notification-toast--visible');
      });
    });

    // Start auto-dismiss timer
    startTimer();

    this._active.set(id, { element: toast, timer, onClick });

    return id;
  }

  _showQueued() {
    while (this._queue.length > 0 && this._active.size < MAX_VISIBLE) {
      const { text, options } = this._queue.shift();
      this._create(text, options.priority, options.duration, options.onClick || null);
    }
  }
}
