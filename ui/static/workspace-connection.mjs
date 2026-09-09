/** Browser transport for a persistent owner. Disposal never closes the owner. */
export class WorkspaceConnection {
  constructor({sessionId, token, receive, error, fetcher = fetch, storage = sessionStorage}) {
    this.sessionId = sessionId;
    this.token = token;
    this.receive = receive;
    this.error = error;
    this.fetcher = fetcher;
    this.storage = storage;
    this.key = `serena-workspace-pending:${sessionId}`;
    this.pending = JSON.parse(storage.getItem(this.key) || '{}');
    this.uploadKey = `serena-workspace-uploads:${sessionId}`;
    this.uploads = JSON.parse(storage.getItem(this.uploadKey) || '{}');
    this.cursor = 0;
    this.stopped = false;
    this.polling = false;
    this.timer = null;
    this.base = `/api/workspace/${encodeURIComponent(sessionId)}`;
  }

  async request(path, body) {
    if (this.stopped) throw Error('Conversation view is closed');
    const fetcher = this.fetcher;
    const multipart = body instanceof FormData;
    const response = await fetcher(this.base + path, {
      method: body === undefined ? 'GET' : 'POST',
      credentials: 'same-origin',
      headers: {'X-Serena-Workspace-Token': this.token, ...(body === undefined || multipart ? {} : {'Content-Type': 'application/json'})},
      ...(body === undefined ? {} : {body: multipart ? body : JSON.stringify(body)}),
    });
    const data = await response.json();
    if (!response.ok) throw Error(data.error || `Workspace request failed (${response.status})`);
    return data;
  }

  async connect() {
    const result = await this.request('/attach', {});
    if (!result.ok) throw Error(result.error || 'Session attachment is not confirmed');
    await this.poll({required: true});
    return result;
  }

  async poll({required = false} = {}) {
    if (this.polling || this.stopped) return;
    clearTimeout(this.timer);
    this.polling = true;
    let failed = false;
    try {
      let page;
      do {
        page = await this.request(`/events?after=${this.cursor}`);
        if (this.stopped) return;
        for (const envelope of page.events) {
          if (envelope.sequence !== this.cursor + 1) throw Error('Session event replay has a gap');
          if (this.receive(envelope) === false) throw Error('Session event was not accepted by the view');
          this.cursor = envelope.sequence;
        }
      } while (page.has_more && !this.stopped);
    } catch (error) {
      failed = true;
      if (required) throw error;
      if (!this.stopped) this.error(error);
    } finally {
      this.polling = false;
      if (!this.stopped && !(required && failed)) this.timer = setTimeout(() => this.poll(), 250);
    }
  }

  async command(action, payload) {
    const encoded = JSON.stringify({action, payload});
    const signature = action === 'answer' ? 'answer:' + [...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(encoded)))].map(byte=>byte.toString(16).padStart(2,'0')).join('') : encoded;
    const request_id = this.pending[signature] || crypto.randomUUID();
    this.pending[signature] = request_id;
    // Persist before delivery. Reloading after a lost HTTP response must use
    // the same receipt, not silently issue another coding turn.
    this.storage.setItem(this.key, JSON.stringify(this.pending));
    const result = await this.request('/commands', {request_id, action, payload});
    if (!result.ok) throw Error(result.error || 'Control delivery is unconfirmed');
    delete this.pending[signature];
    this.storage.setItem(this.key, JSON.stringify(this.pending));
    return result.result;
  }

  async sendMessage(action, {text, files = [], options = {}, expectedTurnId}) {
        if (action === 'steer' && !expectedTurnId) throw Error('Running turn identity is unavailable');
        if (files.length > 16) throw Error('Attach up to 16 files per message');
        const inputs = text || options.skills?.length ? [{type: 'text', text: text || ''}] : [];
        for (const file of files) {
          if (!file.size || file.size > 25 * 1024 * 1024) throw Error('Attach non-empty files no larger than 25 MB');
          const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
          const key = JSON.stringify([file.name, ...new Uint8Array(digest)]);
          if (!this.uploads[key]) {
            const form = new FormData(); form.append('file', file, file.name);
            const result = await this.request('/uploads', form);
            if (!result.ok || !result.upload?.token) throw Error(result.error || 'Upload failed');
            this.uploads[key] = result.upload.token;
            this.storage.setItem(this.uploadKey, JSON.stringify(this.uploads));
          }
          inputs.push({type: 'upload', token: this.uploads[key]});
        }
        return this.command(action, {inputs, ...(action === 'steer' ? {expectedTurnId, ...(options.skills?.length ? {skills:options.skills} : {})} : {}), ...(action === 'submit' && Object.keys(options).length ? {options} : {})});
  }

  controls() {
    return {
      events: after => {
        if (!Number.isSafeInteger(after) || after < 0) throw Error('Invalid event cursor');
        return this.request(`/events?after=${after}`);
      },
      image: async token => {
        if (this.stopped || !/^[a-f0-9]{32}$/.test(token)) throw Error('Image preview unavailable');
        const fetcher = this.fetcher;
        const response = await fetcher(`${this.base}/attachments/${token}`, {credentials:'same-origin',headers:{'X-Serena-Workspace-Token':this.token}});
        if (!response.ok) throw Error('Image preview unavailable in this session');
        return response.blob();
      },
      models: () => this.command('models', {}),
      commands: () => this.command('commands', {}),
      contextUsage: () => this.command('context_usage', {}),
      permissions: () => this.command('permissions', {}),
      setPermissions: (mode, confirmed) => this.command('set_permissions', {mode, confirmed}),
      mcpServers: () => this.command('mcp_servers', {}),
      mcpServerControl: (name, action) => this.command('mcp_server_control', {name, action}),
      cancelQueuedBridge: request_id => this.command('cancel_queued_bridge', {request_id}),
      editQueuedBridge: (request_id, prompt, expected_prompt) => this.command('edit_queued_bridge', {request_id, prompt, expected_prompt}),
      backgroundTasks: () => this.command('background_tasks', {}),
      terminateBackgroundTask: processId => this.command('terminate_background_task', {processId}),
      review: target => this.command('review', {target}),
      compact: () => this.command('compact', {}),
      submit: message => this.sendMessage('submit', message),
      steer: message => this.sendMessage('steer', message),
      interrupt: expectedTurnId => this.command('interrupt', expectedTurnId === undefined ? {} : {expectedTurnId}),
      answer: (request_id, answer) => this.command('answer', {request_id, answer}),
    };
  }

  dispose() {
    this.stopped = true;
    clearTimeout(this.timer);
    // No interrupt, close, shutdown, or provider request here.
  }
}
