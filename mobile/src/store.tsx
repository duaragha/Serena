import {
  createContext,
  useContext,
  useEffect,
  useReducer,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import type { AgentKind, ChatMessage, ServerMsg, SessionSummary } from './types';
import { MockTransport } from './mock';
import {
  AutoTransport,
  LocketRestTransport,
  WebSocketTransport,
  type ConnectionSource,
  type ConnStatus,
  type Transport,
} from './transport';
import { loadSettings, saveSettings, type Settings } from './settings';

interface State {
  status: ConnStatus;
  source: ConnectionSource;
  sessions: SessionSummary[];
  activeId: string | null;
  messages: Record<string, ChatMessage[]>;
  lastError: string | null;
}

type Action =
  | { type: 'status'; status: ConnStatus }
  | { type: 'source'; source: ConnectionSource }
  | { type: 'set_active'; sessionId: string | null }
  | { type: 'user_send'; sessionId: string; messageId: string; text: string }
  | { type: 'server'; msg: ServerMsg };

const initial: State = {
  status: 'closed',
  source: 'offline',
  sessions: [],
  activeId: null,
  messages: {},
  lastError: null,
};

function upsertMessage(list: ChatMessage[], msg: ChatMessage): ChatMessage[] {
  const i = list.findIndex((m) => m.id === msg.id);
  if (i === -1) return [...list, msg];
  const copy = list.slice();
  copy[i] = msg;
  return copy;
}

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case 'status':
      return {
        ...state,
        status: action.status,
        lastError:
          action.status === 'open'
            ? null
            : action.status === 'error'
              ? state.source === 'locket'
                ? "can't reach Locket — check the base URL and API key"
                : state.source === 'mock'
                  ? 'mock transport failed'
                  : "can't reach the daemon — check the server URL + that the PC container is up"
              : state.lastError,
      };
    case 'source':
      return { ...state, source: action.source };
    case 'set_active':
      return { ...state, activeId: action.sessionId };
    case 'user_send': {
      const list = state.messages[action.sessionId] ?? [];
      const msg: ChatMessage = { id: action.messageId, role: 'user', text: action.text, ts: Date.now() };
      return { ...state, messages: { ...state.messages, [action.sessionId]: [...list, msg] } };
    }
    case 'server':
      return applyServer(state, action.msg);
  }
}

function applyServer(state: State, msg: ServerMsg): State {
  switch (msg.type) {
    case 'sessions':
      return { ...state, sessions: msg.sessions, lastError: null };
    case 'history':
      return { ...state, messages: { ...state.messages, [msg.sessionId]: msg.messages } };
    case 'message_start': {
      const list = state.messages[msg.sessionId] ?? [];
      const placeholder: ChatMessage = { id: msg.messageId, role: msg.role, text: '', ts: Date.now(), streaming: true };
      return { ...state, messages: { ...state.messages, [msg.sessionId]: upsertMessage(list, placeholder) } };
    }
    case 'chunk': {
      const list = state.messages[msg.sessionId] ?? [];
      const existing = list.find((m) => m.id === msg.messageId);
      const merged: ChatMessage = existing
        ? { ...existing, text: existing.text + msg.delta, streaming: true }
        : { id: msg.messageId, role: 'assistant', text: msg.delta, ts: Date.now(), streaming: true };
      return { ...state, messages: { ...state.messages, [msg.sessionId]: upsertMessage(list, merged) } };
    }
    case 'message_done': {
      const list = state.messages[msg.sessionId] ?? [];
      const existing = list.find((m) => m.id === msg.messageId);
      if (!existing) return state;
      const done: ChatMessage = { ...existing, streaming: false };
      return { ...state, messages: { ...state.messages, [msg.sessionId]: upsertMessage(list, done) } };
    }
    case 'session_created':
      return { ...state, sessions: [msg.session, ...state.sessions], activeId: msg.session.id };
    case 'error':
      console.warn('[serena] daemon error:', msg.message);
      return { ...state, lastError: msg.message };
  }
}

interface SerenaCtx {
  state: State;
  settings: Settings;
  openSession(id: string): void;
  sendMessage(text: string): void;
  newSession(agent: AgentKind): void;
  refresh(): void;
  updateSettings(s: Settings): void;
}

const Ctx = createContext<SerenaCtx | null>(null);

export function SerenaProvider({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<Settings>(loadSettings);
  const [state, dispatch] = useReducer(reducer, initial);
  const transportRef = useRef<Transport | null>(null);
  const activeIdRef = useRef<string | null>(null);

  useEffect(() => {
    activeIdRef.current = state.activeId;
  }, [state.activeId]);

  // (Re)build the transport whenever the connection settings change.
  useEffect(() => {
    const tr: Transport =
      settings.connectionMode === 'mock'
        ? new MockTransport()
        : settings.connectionMode === 'locket'
          ? new LocketRestTransport(settings.locketBaseUrl, settings.locketApiKey)
          : settings.connectionMode === 'laptop'
            ? new WebSocketTransport(settings.serverUrl, settings.token)
            : new AutoTransport(
                settings.serverUrl,
                settings.token,
                settings.locketBaseUrl,
                settings.locketApiKey,
              );
    transportRef.current = tr;
    tr.onStatus((s) => dispatch({ type: 'status', status: s }));
    tr.onSource((source) => dispatch({ type: 'source', source }));
    tr.onMessage((m) => dispatch({ type: 'server', msg: m }));
    tr.connect();
    tr.send({ type: 'list_sessions' }); // queued until the socket opens
    return () => tr.close();
  }, [
    settings.connectionMode,
    settings.serverUrl,
    settings.token,
    settings.locketBaseUrl,
    settings.locketApiKey,
  ]);

  const value: SerenaCtx = {
    state,
    settings,
    openSession(id) {
      activeIdRef.current = id;
      dispatch({ type: 'set_active', sessionId: id });
      transportRef.current?.send({ type: 'open', sessionId: id });
    },
    sendMessage(text) {
      const id = activeIdRef.current;
      if (!id || !text.trim()) return;
      dispatch({ type: 'user_send', sessionId: id, messageId: 'u' + Date.now(), text });
      transportRef.current?.send({ type: 'send', sessionId: id, text });
    },
    newSession(agent) {
      transportRef.current?.send({ type: 'new_session', agent });
    },
    refresh() {
      transportRef.current?.send({ type: 'list_sessions' });
    },
    updateSettings(s) {
      setSettings(s);
      saveSettings(s);
    },
  };

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useSerena(): SerenaCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useSerena must be used inside <SerenaProvider>');
  return ctx;
}
