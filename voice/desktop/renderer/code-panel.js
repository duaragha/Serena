// Code Output Panel — shows Claude Code's live output in the Serena overlay

// The same bounds main.js clamps to. The drawer is a column of the window, so
// its width is a window measurement, and both ends have to agree on it.
export const DEFAULT_CODE_PANEL_WIDTH = 450;
export const MIN_CODE_PANEL_WIDTH = 300;
export const MAX_CODE_PANEL_WIDTH = 720;
const KEYBOARD_STEP = 24;
const KEYBOARD_PAGE = 96;

export class CodePanel {
  constructor(container) {
    this._visible = false;
    this._container = container;
    this._width = DEFAULT_CODE_PANEL_WIDTH;
    // The job this pane is showing. Events for any other job belong to another
    // pane and must not be painted into the one he is reading.
    this._itemId = null;
    this._build();
  }

  _build() {
    // Root panel element
    this.el = document.createElement('div');
    this.el.className = 'code-panel';

    // Status bar
    this._statusBar = document.createElement('div');
    this._statusBar.className = 'code-panel__status';

    this._statusDot = document.createElement('div');
    this._statusDot.className = 'code-panel__status-dot';

    this._statusText = document.createElement('span');
    this._statusText.className = 'code-panel__status-text';
    this._statusText.textContent = 'idle';

    this._statusBar.appendChild(this._statusDot);
    this._statusBar.appendChild(this._statusText);

    // Clear button
    const clearBtn = document.createElement('button');
    clearBtn.className = 'code-panel__clear';
    clearBtn.textContent = '\u00d7';
    clearBtn.title = 'Clear output';
    clearBtn.addEventListener('click', () => this.clear());
    this._statusBar.appendChild(clearBtn);

    // Scrollable output area
    this._output = document.createElement('div');
    this._output.className = 'code-panel__output';

    this._resizeHandle = this._buildResizeHandle();

    this.el.appendChild(this._statusBar);
    this.el.appendChild(this._output);
    this.el.appendChild(this._resizeHandle);
    this._container.appendChild(this.el);
    this.setWidth(this._width);

    // Mouse passthrough handling
    this.el.addEventListener('mouseenter', () => {
      window.serena.setIgnoreMouse(false);
    });
    this.el.addEventListener('mouseleave', () => {
      window.serena.setIgnoreMouse(true);
    });
  }

  _buildResizeHandle() {
    // A separator, announced as one: he can drag it, and he can also reach it
    // with the keyboard, which a bare draggable div gives nobody.
    const handle = document.createElement('div');
    handle.className = 'code-panel__resize';
    handle.setAttribute('role', 'separator');
    handle.setAttribute('aria-label', 'Resize coding pane');
    handle.setAttribute('aria-orientation', 'vertical');
    handle.setAttribute('tabindex', '0');
    handle.setAttribute('aria-valuemin', MIN_CODE_PANEL_WIDTH);
    handle.setAttribute('aria-valuemax', MAX_CODE_PANEL_WIDTH);

    let dragFrom = null;

    handle.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      dragFrom = { x: event.clientX, width: this._width };
      handle.setPointerCapture(event.pointerId);
    });

    handle.addEventListener('pointermove', (event) => {
      if (!dragFrom || !handle.hasPointerCapture(event.pointerId)) return;
      // The drawer grows rightward from its own left edge, so a rightward drag
      // widens it.
      this.setWidth(dragFrom.width + (event.clientX - dragFrom.x));
    });

    const endDrag = (event) => {
      if (!dragFrom) return;
      dragFrom = null;
      handle.releasePointerCapture(event.pointerId);
      // One request, on release: every intermediate width would move the OS
      // window under his cursor mid-drag.
      this._requestWidth(this._width);
    };

    handle.addEventListener('pointerup', endDrag);
    handle.addEventListener('pointercancel', endDrag);

    handle.addEventListener('keydown', (event) => {
      const step = event.shiftKey ? KEYBOARD_PAGE : KEYBOARD_STEP;
      let next = null;
      if (event.key === 'ArrowRight') next = this._width + step;
      else if (event.key === 'ArrowLeft') next = this._width - step;
      else if (event.key === 'Home') next = MIN_CODE_PANEL_WIDTH;
      else if (event.key === 'End') next = MAX_CODE_PANEL_WIDTH;
      if (next === null) return;
      event.preventDefault();
      this.setWidth(next);
      this._requestWidth(this._width);
    });

    return handle;
  }

  get isVisible() {
    return this._visible;
  }

  get width() {
    return this._width;
  }

  /** Draw at this width. Local only: the window is resized by _requestWidth. */
  setWidth(value) {
    const width = Math.round(Number(value));
    this._width = Math.min(
      MAX_CODE_PANEL_WIDTH,
      Math.max(MIN_CODE_PANEL_WIDTH, Number.isFinite(width) ? width : this._width),
    );
    this.el.style.setProperty('--code-panel-width', `${this._width}px`);
    this._resizeHandle.setAttribute('aria-valuenow', this._width);
    return this._width;
  }

  _requestWidth(width) {
    window.serena.setCodePanelWidth(width);
  }

  /** Show this job's events, and only this job's, in this pane. */
  setItemId(itemId) {
    this._itemId = itemId || null;
  }

  show() {
    this._visible = true;
    this.el.classList.add('code-panel--visible');
  }

  hide() {
    this._visible = false;
    this.el.classList.remove('code-panel--visible');
  }

  toggle() {
    if (this._visible) {
      this.hide();
    } else {
      this.show();
    }
  }

  clear() {
    this._output.innerHTML = '';
  }

  setStatus(status) {
    this._statusText.textContent = status;

    if (status === 'done') {
      this._statusDot.classList.remove('code-panel__status-dot--active');
      this._statusDot.classList.add('code-panel__status-dot--done');
    } else if (status && status !== 'idle') {
      this._statusDot.classList.remove('code-panel__status-dot--done');
      this._statusDot.classList.add('code-panel__status-dot--active');
    } else {
      this._statusDot.classList.remove('code-panel__status-dot--active');
      this._statusDot.classList.remove('code-panel__status-dot--done');
    }
  }

  addEvent(event) {
    // A second job starting does not repaint the pane he is reading.
    if (this._itemId && event.item_id && event.item_id !== this._itemId) return false;

    const block = document.createElement('div');
    block.className = 'code-event';

    switch (event.kind) {
      case 'file_edit':
        block.classList.add('code-event--file');
        block.innerHTML = this._renderFileEdit(event);
        break;
      case 'bash':
        block.classList.add('code-event--bash');
        block.innerHTML = this._renderBash(event);
        break;
      case 'text':
        block.classList.add('code-event--text');
        block.innerHTML = this._renderText(event);
        break;
      case 'tool_call':
        block.classList.add('code-event--tool');
        block.innerHTML = this._renderToolCall(event);
        break;
      default:
        block.classList.add('code-event--text');
        block.textContent = event.summary || event.detail || JSON.stringify(event);
    }

    this._output.appendChild(block);
    this._scrollToBottom();
    return true;
  }

  _renderFileEdit(event) {
    const filename = this._esc(event.filename || event.summary || 'unknown file');
    let html = `<div class="code-event__filename">${filename}</div>`;

    if (event.detail) {
      const lines = event.detail.split('\n');
      const diffLines = lines.map((line) => {
        const escaped = this._esc(line);
        if (line.startsWith('+') && !line.startsWith('+++')) {
          return `<div class="code-diff__add">${escaped}</div>`;
        } else if (line.startsWith('-') && !line.startsWith('---')) {
          return `<div class="code-diff__remove">${escaped}</div>`;
        } else if (line.startsWith('@@')) {
          return `<div class="code-diff__hunk">${escaped}</div>`;
        }
        return `<div class="code-diff__context">${escaped}</div>`;
      });
      html += `<div class="code-diff">${diffLines.join('')}</div>`;
    } else if (event.summary) {
      html += `<div class="code-event__summary">${this._esc(event.summary)}</div>`;
    }

    return html;
  }

  _renderBash(event) {
    const cmd = this._esc(event.summary || event.command || '');
    let html = `<div class="code-bash__cmd"><span class="code-bash__prompt">$</span> ${cmd}</div>`;

    if (event.detail) {
      html += `<div class="code-bash__output">${this._esc(event.detail)}</div>`;
    }

    return html;
  }

  _renderText(event) {
    const text = this._esc(event.summary || event.detail || '');
    return `<div class="code-event__text">${text}</div>`;
  }

  _renderToolCall(event) {
    const name = this._esc(event.summary || event.tool_name || 'tool');
    let html = `<div class="code-tool__name">${name}</div>`;

    if (event.detail) {
      html += `<div class="code-tool__args">${this._esc(event.detail)}</div>`;
    }

    return html;
  }

  _scrollToBottom() {
    requestAnimationFrame(() => {
      this._output.scrollTop = this._output.scrollHeight;
    });
  }

  _esc(str) {
    const el = document.createElement('span');
    el.textContent = str;
    return el.innerHTML;
  }
}
