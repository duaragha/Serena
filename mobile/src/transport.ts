import type { ChatMessage, ClientMsg, ServerMsg, SessionSummary } from './types';

export type ConnStatus = 'connecting' | 'open' | 'closed' | 'error';
export type ConnectionSource = 'laptop' | 'locket' | 'mock' | 'offline';

export interface Transport {
  connect(): void;
  send(msg: ClientMsg): void;
  onMessage(cb: (m: ServerMsg) => void): void;
  onStatus(cb: (s: ConnStatus) => void): void;
  onSource(cb: (s: ConnectionSource) => void): void;
  close(): void;
}

export class WebSocketTransport implements Transport {
  private ws: WebSocket | null = null;
  private msgCb: ((m: ServerMsg) => void) | null = null;
  private statusCb: ((s: ConnStatus) => void) | null = null;
  private sourceCb: ((s: ConnectionSource) => void) | null = null;
  private queue: ClientMsg[] = [];
  private reconnectTimer: number | null = null;
  private closedByUser = false;
  private url: string;
  private token: string;

  constructor(url: string, token: string) {
    this.url = url;
    this.token = token;
  }

  connect() {
    this.closedByUser = false;
    this.sourceCb?.('laptop');
    this.open();
  }

  private open() {
    let raw = this.url.trim();
    if (!/^(wss?|https?):\/\//i.test(raw)) raw = 'ws://' + raw;
    const base = raw.replace(/^http/i, 'ws');
    const sep = base.includes('?') ? '&' : '?';
    const wsUrl = this.token
      ? `${base}${sep}token=${encodeURIComponent(this.token)}`
      : base;

    this.statusCb?.('connecting');
    try {
      this.ws = new WebSocket(wsUrl);
    } catch {
      this.statusCb?.('error');
      this.scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      this.statusCb?.('open');
      for (const m of this.queue) this.ws!.send(JSON.stringify(m));
      this.queue = [];
    };
    this.ws.onmessage = (e) => {
      try {
        this.msgCb?.(JSON.parse(e.data) as ServerMsg);
      } catch {
        /* ignore malformed frames */
      }
    };
    this.ws.onerror = () => this.statusCb?.('error');
    this.ws.onclose = () => {
      this.statusCb?.('closed');
      if (!this.closedByUser) this.scheduleReconnect();
    };
  }

  private scheduleReconnect() {
    if (this.reconnectTimer != null) return;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.open();
    }, 1500);
  }

  send(msg: ClientMsg) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    } else {
      this.queue.push(msg);
    }
  }

  onMessage(cb: (m: ServerMsg) => void) {
    this.msgCb = cb;
  }
  onStatus(cb: (s: ConnStatus) => void) {
    this.statusCb = cb;
  }
  onSource(cb: (s: ConnectionSource) => void) {
    this.sourceCb = cb;
  }

  close() {
    this.closedByUser = true;
    if (this.reconnectTimer != null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
  }
}

interface ApiEnvelope<T> {
  success?: boolean;
  data?: T;
  error?: string | { message?: string; code?: string };
}

interface SessionDetail {
  session: SessionSummary;
  messages: ChatMessage[];
}

interface SendResult {
  session: SessionSummary;
  assistantMessage: ChatMessage;
}

function normalizeHttpBase(url: string): string {
  let raw = url.trim().replace(/\/+$/, '');
  if (!raw) return '';
  if (!/^https?:\/\//i.test(raw)) raw = 'https://' + raw;
  return raw;
}

function apiErrorMessage(error: ApiEnvelope<unknown>['error'], fallback: string): string {
  if (typeof error === 'string') return error;
  return error?.message || fallback;
}

export class LocketRestTransport implements Transport {
  private msgCb: ((m: ServerMsg) => void) | null = null;
  private statusCb: ((s: ConnStatus) => void) | null = null;
  private sourceCb: ((s: ConnectionSource) => void) | null = null;
  private baseUrl: string;
  private apiKey: string;
  private closed = false;

  constructor(baseUrl: string, apiKey: string) {
    this.baseUrl = normalizeHttpBase(baseUrl);
    this.apiKey = apiKey.trim();
  }

  connect() {
    this.closed = false;
    this.sourceCb?.(this.baseUrl && this.apiKey ? 'locket' : 'offline');
    if (!this.baseUrl || !this.apiKey) {
      this.statusCb?.('error');
      this.emit({ type: 'error', message: 'Locket URL and API key are required' });
      return;
    }
    this.statusCb?.('open');
  }

  send(msg: ClientMsg) {
    if (this.closed) return;
    if (!this.baseUrl || !this.apiKey) {
      this.statusCb?.('error');
      this.sourceCb?.('offline');
      this.emit({ type: 'error', message: 'Locket URL and API key are required' });
      return;
    }
    switch (msg.type) {
      case 'list_sessions':
        void this.listSessions();
        break;
      case 'open':
        void this.openSession(msg.sessionId);
        break;
      case 'send':
        void this.sendMessage(msg.sessionId, msg.text);
        break;
      case 'new_session':
        void this.createSession();
        break;
      case 'stop':
        break;
    }
  }

  onMessage(cb: (m: ServerMsg) => void) {
    this.msgCb = cb;
  }
  onStatus(cb: (s: ConnStatus) => void) {
    this.statusCb = cb;
  }
  onSource(cb: (s: ConnectionSource) => void) {
    this.sourceCb = cb;
  }

  close() {
    this.closed = true;
    this.statusCb?.('closed');
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    this.statusCb?.('connecting');
    try {
      const headers = new Headers(init.headers);
      headers.set('Authorization', `Bearer ${this.apiKey}`);
      headers.set('Content-Type', 'application/json');
      const res = await fetch(`${this.baseUrl}${path}`, {
        ...init,
        headers,
      });
      const payload = (await res.json().catch(() => ({}))) as ApiEnvelope<T>;
      if (!res.ok || payload.success === false) {
        throw new Error(apiErrorMessage(payload.error, `Locket returned ${res.status}`));
      }
      this.statusCb?.('open');
      this.sourceCb?.('locket');
      return payload.data as T;
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Could not reach Locket';
      this.statusCb?.('error');
      this.emit({ type: 'error', message });
      throw error;
    }
  }

  private async listSessions() {
    try {
      const sessions = await this.request<SessionSummary[]>('/api/v1/serena/mobile/sessions');
      this.emit({ type: 'sessions', sessions });
    } catch {
      /* error emitted by request */
    }
  }

  private async openSession(sessionId: string) {
    try {
      const detail = await this.request<SessionDetail>(
        `/api/v1/serena/mobile/sessions/${encodeURIComponent(sessionId)}`,
      );
      this.emit({ type: 'history', sessionId, messages: detail.messages });
    } catch {
      /* error emitted by request */
    }
  }

  private async createSession() {
    try {
      const session = await this.request<SessionSummary>('/api/v1/serena/mobile/sessions', {
        method: 'POST',
        body: JSON.stringify({ title: 'serena' }),
      });
      this.emit({ type: 'session_created', session });
      this.emit({ type: 'history', sessionId: session.id, messages: [] });
    } catch {
      /* error emitted by request */
    }
  }

  private async sendMessage(sessionId: string, text: string) {
    try {
      const result = await this.request<SendResult>(
        `/api/v1/serena/mobile/sessions/${encodeURIComponent(sessionId)}/messages`,
        {
          method: 'POST',
          body: JSON.stringify({ text }),
        },
      );
      const msg = result.assistantMessage;
      this.emit({ type: 'message_start', sessionId, messageId: msg.id, role: 'assistant' });
      this.emit({ type: 'chunk', sessionId, messageId: msg.id, delta: msg.text });
      this.emit({ type: 'message_done', sessionId, messageId: msg.id });
      await this.listSessions();
    } catch {
      /* error emitted by request */
    }
  }

  private emit(m: ServerMsg) {
    this.msgCb?.(m);
  }
}

export class AutoTransport implements Transport {
  private msgCb: ((m: ServerMsg) => void) | null = null;
  private statusCb: ((s: ConnStatus) => void) | null = null;
  private sourceCb: ((s: ConnectionSource) => void) | null = null;
  private active: Transport | null = null;
  private laptop: WebSocketTransport | null = null;
  private queue: ClientMsg[] = [];
  private fallbackTimer: number | null = null;
  private laptopOpen = false;
  private fallbackStarted = false;
  private closed = false;
  private laptopUrl: string;
  private laptopToken: string;
  private locketUrl: string;
  private locketKey: string;
  private timeoutMs: number;

  constructor(
    laptopUrl: string,
    laptopToken: string,
    locketUrl: string,
    locketKey: string,
    timeoutMs = 1200,
  ) {
    this.laptopUrl = laptopUrl;
    this.laptopToken = laptopToken;
    this.locketUrl = locketUrl;
    this.locketKey = locketKey;
    this.timeoutMs = timeoutMs;
  }

  connect() {
    this.closed = false;
    this.fallbackStarted = false;
    this.laptopOpen = false;
    this.statusCb?.('connecting');
    this.sourceCb?.('offline');
    if (this.laptopUrl.trim()) {
      this.tryLaptop();
    } else {
      this.fallbackToLocket();
    }
  }

  send(msg: ClientMsg) {
    if (this.active) {
      this.active.send(msg);
    } else {
      this.queue.push(msg);
    }
  }

  onMessage(cb: (m: ServerMsg) => void) {
    this.msgCb = cb;
  }
  onStatus(cb: (s: ConnStatus) => void) {
    this.statusCb = cb;
  }
  onSource(cb: (s: ConnectionSource) => void) {
    this.sourceCb = cb;
  }

  close() {
    this.closed = true;
    if (this.fallbackTimer != null) {
      clearTimeout(this.fallbackTimer);
      this.fallbackTimer = null;
    }
    this.active?.close();
    this.laptop?.close();
    this.statusCb?.('closed');
  }

  private tryLaptop() {
    this.laptop = new WebSocketTransport(this.laptopUrl, this.laptopToken);
    this.laptop.onMessage((m) => this.msgCb?.(m));
    this.laptop.onSource((s) => this.sourceCb?.(s));
    this.laptop.onStatus((s) => {
      if (this.closed) return;
      if (s === 'open') {
        this.laptopOpen = true;
        this.active = this.laptop;
        this.clearFallbackTimer();
        this.sourceCb?.('laptop');
        this.statusCb?.('open');
        this.flush();
        return;
      }
      if ((s === 'error' || s === 'closed') && !this.laptopOpen) {
        if (this.fallbackStarted) return;
        this.fallbackToLocket();
        return;
      }
      if ((s === 'error' || s === 'closed') && this.active === this.laptop) {
        if (this.fallbackStarted) return;
        this.fallbackToLocket();
        return;
      }
      this.statusCb?.(s);
    });
    this.laptop.connect();
    this.fallbackTimer = window.setTimeout(() => {
      if (!this.laptopOpen) this.fallbackToLocket();
    }, this.timeoutMs);
  }

  private fallbackToLocket() {
    if (this.closed) return;
    if (this.fallbackStarted && this.active instanceof LocketRestTransport) return;
    this.fallbackStarted = true;
    this.clearFallbackTimer();
    this.laptop?.close();
    const locket = new LocketRestTransport(this.locketUrl, this.locketKey);
    this.active = locket;
    locket.onMessage((m) => this.msgCb?.(m));
    locket.onStatus((s) => this.statusCb?.(s));
    locket.onSource((s) => this.sourceCb?.(s));
    locket.connect();
    this.flush();
  }

  private flush() {
    if (!this.active) return;
    const queued = this.queue;
    this.queue = [];
    for (const msg of queued) this.active.send(msg);
  }

  private clearFallbackTimer() {
    if (this.fallbackTimer == null) return;
    clearTimeout(this.fallbackTimer);
    this.fallbackTimer = null;
  }
}
