import type { ClientMsg, ServerMsg, SessionSummary, ChatMessage } from './types';
import type { Transport, ConnStatus, ConnectionSource } from './transport';

// In-memory daemon stand-in. Lets the entire app — list, threads, streaming
// replies, new sessions — run in a browser/emulator with no PC. Swap it for
// WebSocketTransport (Settings -> turn off "Use mock daemon") once the real
// server is deployed.

const t = (minAgo: number) => Date.now() - minAgo * 60_000;

const SESSIONS: SessionSummary[] = [
  { id: '709a4856', title: 'Questions', agent: 'claude', updated: t(3), preview: 'what would you suggest', group: 'g_72b6' },
  { id: '019dd017', title: 'Questions (codex)', agent: 'codex', updated: t(4), preview: 'looking at paseo.sh…', group: 'g_72b6' },
  { id: 'a1b2c3d4', title: 'serena mobile app', agent: 'claude', updated: t(31), preview: "let's build everything app-side first" },
  { id: 'e5f6a7b8', title: 'split-kill fix', agent: 'codex', updated: t(60 * 18), preview: 'codex crash handling' },
];

const HISTORY: Record<string, ChatMessage[]> = {
  '709a4856': [
    { id: 'm1', role: 'user', text: 'https://paseo.sh/ look at this and tell me what they do better', ts: t(8) },
    { id: 'm2', role: 'assistant', text: "paseo is the productized version of the orchestration half of what we built — daemon + WS + Expo clients. we win on memory, persona, and recall; they win on mobile + surface polish.", ts: t(7) },
    { id: 'm3', role: 'user', text: 'what would you suggest', ts: t(3) },
  ],
  'a1b2c3d4': [
    { id: 'n1', role: 'user', text: "let's build everything app-side first, deploy server later", ts: t(31) },
    { id: 'n2', role: 'assistant', text: 'smart order — the client is the bulk of the work and none of it needs the PC. building the Capacitor app now.', ts: t(30) },
  ],
};

export class MockTransport implements Transport {
  private msgCb: ((m: ServerMsg) => void) | null = null;
  private statusCb: ((s: ConnStatus) => void) | null = null;
  private sourceCb: ((s: ConnectionSource) => void) | null = null;
  private timers = new Set<number>();

  connect() {
    this.sourceCb?.('mock');
    this.statusCb?.('connecting');
    this.after(180, () => this.statusCb?.('open'));
  }

  send(msg: ClientMsg) {
    switch (msg.type) {
      case 'list_sessions':
        this.emit({ type: 'sessions', sessions: [...SESSIONS].sort((a, b) => b.updated - a.updated) });
        break;
      case 'open':
        this.emit({ type: 'history', sessionId: msg.sessionId, messages: HISTORY[msg.sessionId] ?? [] });
        break;
      case 'send':
        this.handleSend(msg.sessionId, msg.text);
        break;
      case 'new_session': {
        const session: SessionSummary = {
          id: 'new' + Math.random().toString(36).slice(2, 8),
          title: 'new chat',
          agent: msg.agent,
          updated: Date.now(),
          preview: '',
        };
        SESSIONS.push(session);
        HISTORY[session.id] = [];
        this.emit({ type: 'session_created', session });
        break;
      }
      case 'stop':
        this.timers.forEach((id) => clearTimeout(id));
        this.timers.clear();
        break;
    }
  }

  private handleSend(sessionId: string, text: string) {
    const hist = (HISTORY[sessionId] ??= []);
    hist.push({ id: 'u' + Date.now(), role: 'user', text, ts: Date.now() });

    const messageId = 'a' + Date.now();
    this.after(250, () => this.emit({ type: 'message_start', sessionId, messageId, role: 'assistant' }));

    const reply = `[mock] got it — "${text.slice(0, 60)}". this is the mock daemon streaming a reply so you can feel the UI. point Settings at the real server and i'll actually run.`;
    const words = reply.split(' ');
    let acc = '';
    words.forEach((w, i) => {
      this.after(450 + i * 55, () => {
        const delta = (i ? ' ' : '') + w;
        acc += delta;
        this.emit({ type: 'chunk', sessionId, messageId, delta });
      });
    });
    this.after(450 + words.length * 55 + 60, () => {
      hist.push({ id: messageId, role: 'assistant', text: acc, ts: Date.now() });
      this.emit({ type: 'message_done', sessionId, messageId });
    });
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
    this.timers.forEach((id) => clearTimeout(id));
    this.timers.clear();
    this.statusCb?.('closed');
  }

  private emit(m: ServerMsg) {
    this.msgCb?.(m);
  }
  private after(ms: number, fn: () => void) {
    const id = window.setTimeout(() => {
      this.timers.delete(id);
      fn();
    }, ms);
    this.timers.add(id);
  }
}
