// Wire protocol between the Capacitor app and the Serena daemon (Flask + WS).
// The daemon will be made to speak this over /ws/chat; the mock daemon
// (mock.ts) speaks it too so the whole app runs with no PC.

export type Role = 'user' | 'assistant' | 'system';
export type AgentKind = 'claude' | 'codex' | 'serena';

export interface SessionSummary {
  id: string;
  title: string;
  agent: AgentKind;
  updated: number; // epoch ms
  preview: string;
  group?: string; // linked-sibling (claude<->codex) group id, if any
  starred?: boolean;
}

export interface ChatMessage {
  id: string;
  role: Role;
  text: string;
  ts: number; // epoch ms
  streaming?: boolean; // assistant message still being emitted
}

// client -> server
export type ClientMsg =
  | { type: 'list_sessions' }
  | { type: 'open'; sessionId: string }
  | { type: 'send'; sessionId: string; text: string }
  | { type: 'new_session'; agent: AgentKind }
  | { type: 'stop'; sessionId: string };

// server -> client
export type ServerMsg =
  | { type: 'sessions'; sessions: SessionSummary[] }
  | { type: 'history'; sessionId: string; messages: ChatMessage[] }
  | { type: 'message_start'; sessionId: string; messageId: string; role: Role }
  | { type: 'chunk'; sessionId: string; messageId: string; delta: string }
  | { type: 'message_done'; sessionId: string; messageId: string }
  | { type: 'session_created'; session: SessionSummary }
  | { type: 'error'; message: string };
