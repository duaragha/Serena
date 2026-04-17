// Code Output Panel — shows Claude Code's live output in the Serena overlay

export class CodePanel {
  constructor(container) {
    this._visible = false;
    this._container = container;
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

    this.el.appendChild(this._statusBar);
    this.el.appendChild(this._output);
    this._container.appendChild(this.el);

    // Mouse passthrough handling
    this.el.addEventListener('mouseenter', () => {
      window.serena.setIgnoreMouse(false);
    });
    this.el.addEventListener('mouseleave', () => {
      window.serena.setIgnoreMouse(true);
    });
  }

  get isVisible() {
    return this._visible;
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
