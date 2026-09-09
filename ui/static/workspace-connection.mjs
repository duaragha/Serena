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
    this.cursor = 0;
    this.stopped = false;
    this.polling = false;
    this.timer = null;
    this.base = `/api/workspace/${encodeURIComponent(sessionId)}`;
  }

  async request(path, body) {
    if (this.stopped) throw Error('Conversation view is closed');
    const fetcher = this.fetcher;
    const response = await fetcher(this.base + path, {
      method: body === undefined ? 'GET' : 'POST',
      credentials: 'same-origin',
      headers: {'X-Serena-Workspace-Token': this.token, ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}),
    });
    const data = await response.json();
    if (!response.ok) throw Error(data.error || `Workspace request failed (${response.status})`);
    return data;
  }

  async connect() {
    const result = await this.request('/attach', {});
    if (!result.ok) throw Error(result.error || 'Session attachment is not confirmed');
    await this.poll();
    return result;
  }

  async poll() {
    if (this.polling || this.stopped) return;
    clearTimeout(this.timer);
    this.polling = true;
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
      if (!this.stopped) this.error(error);
    } finally {
      this.polling = false;
      if (!this.stopped) this.timer = setTimeout(() => this.poll(), 250);
    }
  }

  async command(action, payload) {
    const signature = JSON.stringify({action, payload});
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

  controls() {
    return {
      submit: ({text, files}) => {
        if (files?.length) throw Error('Owner-bound uploads are not connected yet');
        return this.command('submit', {inputs: [{type: 'text', text}]});
      },
      interrupt: () => this.command('interrupt', {}),
      answer: (request_id, answer) => this.command('answer', {request_id, answer}),
    };
  }

  dispose() {
    this.stopped = true;
    clearTimeout(this.timer);
    // No interrupt, close, shutdown, or provider request here.
  }
}
