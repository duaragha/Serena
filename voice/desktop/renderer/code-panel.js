// Code Output Panel. Shows live coding output in the Serena overlay.

export const DEFAULT_CODE_PANEL_WIDTH = 450;
export const MIN_CODE_PANEL_WIDTH = 300;
export const MAX_CODE_PANEL_WIDTH = 720;
const CODE_PANEL_WIDTH_STEP = 24;

export class CodePanel {
  constructor(container) {
    this._visible = false;
    this._available = false;
    this._itemId = '';
    this._snapshot = null;
    this._width = DEFAULT_CODE_PANEL_WIDTH;
    this._container = container;
    this._build();
  }

  _build() {
    // Root panel element
    this.el = document.createElement('div');
    this.el.className = 'code-panel';
    this.el.id = 'serena-code-panel';

    this._resizeHandle = document.createElement('div');
    this._resizeHandle.className = 'code-panel__resize';
    this._resizeHandle.tabIndex = 0;
    this._resizeHandle.setAttribute('role', 'separator');
    this._resizeHandle.setAttribute('aria-label', 'Resize coding pane');
    this._resizeHandle.setAttribute('aria-controls', this.el.id);
    this._resizeHandle.setAttribute('aria-orientation', 'vertical');
    this._resizeHandle.setAttribute('aria-valuemin', String(MIN_CODE_PANEL_WIDTH));
    this._resizeHandle.setAttribute('aria-valuemax', String(MAX_CODE_PANEL_WIDTH));
    this._resizeHandle.title = 'Drag to resize coding pane. Arrow keys resize it too.';
    this._installResizeInteraction();

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

    // Hide button
    const clearBtn = document.createElement('button');
    clearBtn.className = 'code-panel__clear';
    clearBtn.textContent = '\u00d7';
    clearBtn.title = 'Hide coding work';
    clearBtn.addEventListener('click', () => window.serena.hideCodePanel());
    this._statusBar.appendChild(clearBtn);

    this._details = document.createElement('div');
    this._details.className = 'code-panel__details';

    this._controls = document.createElement('div');
    this._controls.className = 'code-panel__controls';
    this._steerInput = document.createElement('textarea');
    this._steerInput.className = 'code-panel__steer-input';
    this._steerInput.placeholder = 'correct or steer this job';
    this._steerInput.maxLength = 4000;
    this._steerButton = this._controlButton('steer', () => {
      const text = this._steerInput.value.trim();
      if (!text || !this._itemId) return;
      this._sendControl('steer', { text });
    });
    this._statusButton = this._controlButton('status', () => this._sendControl('status'));
    this._cancelButton = this._controlButton('cancel', () => this._sendControl('cancel'));
    this._resumeButton = this._controlButton('resume', () => this._sendControl('resume'));
    this._controls.append(
      this._steerInput,
      this._steerButton,
      this._statusButton,
      this._cancelButton,
      this._resumeButton,
    );

    // Scrollable output area
    this._output = document.createElement('div');
    this._output.className = 'code-panel__output';

    this.el.appendChild(this._resizeHandle);
    this.el.appendChild(this._statusBar);
    this.el.appendChild(this._details);
    this.el.appendChild(this._controls);
    this.el.appendChild(this._output);
    this._container.appendChild(this.el);

    this._reopenButton = document.createElement('button');
    this._reopenButton.type = 'button';
    this._reopenButton.className = 'code-panel__reopen';
    this._reopenButton.textContent = 'coding';
    this._reopenButton.title = 'Reopen coding work';
    this._reopenButton.setAttribute('aria-label', 'Reopen coding work');
    this._reopenButton.addEventListener('click', () => window.serena.showCodePanel());
    this._container.appendChild(this._reopenButton);

    // Mouse passthrough handling
    this.el.addEventListener('mouseenter', () => {
      window.serena.setIgnoreMouse(false);
    });
    this.el.addEventListener('mouseleave', () => {
      window.serena.setIgnoreMouse(true);
    });
    this._reopenButton.addEventListener('mouseenter', () => {
      window.serena.setIgnoreMouse(false);
    });
    this._reopenButton.addEventListener('mouseleave', () => {
      window.serena.setIgnoreMouse(true);
    });
  }

  get isVisible() {
    return this._visible;
  }

  setWidth(value) {
    const numeric = Number(value);
    const width = Math.round(Math.min(
      Math.max(Number.isFinite(numeric) ? numeric : DEFAULT_CODE_PANEL_WIDTH, MIN_CODE_PANEL_WIDTH),
      MAX_CODE_PANEL_WIDTH,
    ));
    this._width = width;
    this.el.style.setProperty('--code-panel-width', `${width}px`);
    this._resizeHandle.setAttribute('aria-valuenow', String(width));
    this._resizeHandle.setAttribute('aria-valuetext', `${width} pixels`);
    return width;
  }

  _requestWidth(value) {
    const width = this.setWidth(value);
    window.serena.setCodePanelWidth(width);
  }

  _installResizeInteraction() {
    let startX = 0;
    let startWidth = this._width;

    this._resizeHandle.addEventListener('pointerdown', (event) => {
      if (event.button !== undefined && event.button !== 0) return;
      startX = Number(event.clientX || 0);
      startWidth = this._width;
      this._resizeHandle.setPointerCapture?.(event.pointerId);
      this._resizeHandle.classList.add('code-panel__resize--active');
      event.preventDefault?.();
    });
    this._resizeHandle.addEventListener('pointermove', (event) => {
      if (!this._resizeHandle.hasPointerCapture?.(event.pointerId)) return;
      this._requestWidth(startWidth + Number(event.clientX || 0) - startX);
    });
    const finishPointerResize = (event) => {
      if (this._resizeHandle.hasPointerCapture?.(event.pointerId)) {
        this._resizeHandle.releasePointerCapture?.(event.pointerId);
      }
      this._resizeHandle.classList.remove('code-panel__resize--active');
    };
    this._resizeHandle.addEventListener('pointerup', finishPointerResize);
    this._resizeHandle.addEventListener('pointercancel', finishPointerResize);
    this._resizeHandle.addEventListener('dblclick', () => {
      this._requestWidth(DEFAULT_CODE_PANEL_WIDTH);
    });
    this._resizeHandle.addEventListener('keydown', (event) => {
      const step = event.shiftKey ? CODE_PANEL_WIDTH_STEP * 2 : CODE_PANEL_WIDTH_STEP;
      let next = null;
      if (event.key === 'ArrowLeft') next = this._width - step;
      if (event.key === 'ArrowRight') next = this._width + step;
      if (event.key === 'Home') next = MIN_CODE_PANEL_WIDTH;
      if (event.key === 'End') next = MAX_CODE_PANEL_WIDTH;
      if (next === null) return;
      event.preventDefault();
      this._requestWidth(next);
    });
    this.setWidth(this._width);
  }

  show() {
    this._available = true;
    this._visible = true;
    this.el.classList.add('code-panel--visible');
    this._reopenButton.classList.remove('code-panel__reopen--visible');
  }

  hide() {
    this._visible = false;
    this.el.classList.remove('code-panel--visible');
    this._reopenButton.classList.toggle('code-panel__reopen--visible', this._available);
  }

  toggle() {
    if (this._visible) {
      this.hide();
    } else {
      this.show();
    }
  }

  clear() {
    this.clearOutput();
    this._details.innerHTML = '';
    this._steerInput.value = '';
    this._itemId = '';
    this._snapshot = null;
    this._available = false;
    this._reopenButton.classList.remove('code-panel__reopen--visible');
  }

  clearOutput() {
    this._output.innerHTML = '';
  }

  _controlButton(label, handler) {
    const button = document.createElement('button');
    button.className = `code-panel__control code-panel__control--${label}`;
    button.textContent = label;
    button.addEventListener('click', handler);
    return button;
  }

  _sendControl(action, extra = {}) {
    if (!this._itemId) return;
    window.serena.sendCodeControl({ item_id: this._itemId, action, ...extra });
    this.setStatus(`${action} requested`);
  }

  renderSnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== 'object') return;
    // There is a job worth looking at, so offer the way back in without
    // opening over whatever he is doing.
    this._available = true;
    if (!this._visible) {
      this._reopenButton.classList.add('code-panel__reopen--visible');
    }
    this._snapshot = snapshot;
    this._itemId = String(snapshot.item_id || this._itemId || '');
    this.setStatus(String(snapshot.state || 'working'));
    const brief = snapshot.brief || {};
    const model = snapshot.model || {};
    const progress = snapshot.progress || {};
    const evidence = snapshot.evidence || {};
    const review = snapshot.review || {};
    const changes = Array.isArray(snapshot.changes) ? snapshot.changes : [];
    const tests = Array.isArray(snapshot.tests) ? snapshot.tests : [];
    const proof = Array.isArray(snapshot.live_proof) ? snapshot.live_proof : [];
    const rows = [
      this._section('project', snapshot.project_root || snapshot.project || ''),
      this._section('brief', brief.request || brief.trigger || ''),
      this._section(
        'model',
        `${model.reported || model.requested || 'pending'} / ${model.reported_effort || model.effort || 'pending'}`,
      ),
      this._section(
        'progress',
        `attempt ${progress.attempt || 0}${progress.session_id ? `, session ${progress.session_id}` : ''}`
          + (progress.last_error ? `\n${progress.last_error}` : ''),
      ),
      this._section(
        'changes',
        (changes.length ? changes.join('\n') : 'none recorded yet')
          + (snapshot.changes_truncated ? `\n… ${snapshot.changes_truncated} more` : ''),
      ),
      this._section('tests', this._commandSummary(tests, evidence.complete ? 'complete' : 'pending')),
      this._section('live proof', this._commandSummary(proof, proof.length ? '' : 'not recorded')),
      this._section(
        'evidence',
        evidence.complete
          ? `complete, ${evidence.unrelated_dirty_count || 0} unrelated dirty paths preserved`
          : (evidence.errors || []).join('\n') || 'pending',
      ),
      this._section('review', `${review.state || 'pending'}${review.reason ? `: ${review.reason}` : ''}`),
    ];
    this._details.innerHTML = rows.join('');
    const controls = snapshot.controls || {};
    this._cancelButton.disabled = !controls.can_cancel;
    this._steerButton.disabled = !controls.can_steer;
    this._steerInput.disabled = !controls.can_steer;
    this._resumeButton.disabled = !controls.can_resume;
  }

  controlResult(result) {
    if (!result || result.item_id !== this._itemId) return;
    if (result.ok) {
      if (result.action === 'steer') this._steerInput.value = '';
      this.setStatus(`${result.action} accepted`);
    } else {
      this.setStatus(`control failed: ${result.error || 'unknown error'}`);
    }
  }

  _section(label, value) {
    return `<section class="code-panel__section"><h3>${this._esc(label)}</h3>`
      + `<div>${this._esc(String(value || ''))}</div></section>`;
  }

  _commandSummary(entries, fallback) {
    if (!entries.length) return fallback;
    return entries.map((entry) => {
      const code = entry.exit_code === null || entry.exit_code === undefined
        ? '?' : entry.exit_code;
      return `[${code}] ${entry.command || ''}`;
    }).join('\n');
  }

  setStatus(status) {
    this._statusText.textContent = status;

    if (['done', 'completed', 'failed', 'cancelled'].includes(status)) {
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
    const eventItemId = String(event?.item_id || '');
    if (eventItemId && this._itemId && eventItemId !== this._itemId) return false;
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
