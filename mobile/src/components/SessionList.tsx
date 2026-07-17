import { useState } from 'react';
import { useSerena } from '../store';
import type { SessionSummary } from '../types';

function relTime(ms: number): string {
  const s = Math.floor((Date.now() - ms) / 1000);
  if (s < 60) return 'now';
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

export function SessionList({ onOpen }: { onOpen: () => void }) {
  const { state, openSession, newSession } = useSerena();
  const [starOpen, setStarOpen] = useState(true);

  const open = (s: SessionSummary) => {
    openSession(s.id);
    onOpen();
  };

  // Collapse each linked pair to one row (claude main); codex sibling lives
  // behind the in-chat toggle. (Mobile-only.)
  const mainByGroup = new Map<string, string>();
  for (const s of state.sessions) {
    if (!s.group) continue;
    if (!mainByGroup.has(s.group) || s.agent === 'claude') mainByGroup.set(s.group, s.id);
  }
  const visible = state.sessions.filter((s) => !s.group || mainByGroup.get(s.group) === s.id);
  const starred = visible.filter((s) => s.starred);
  const rest = visible.filter((s) => !s.starred);

  const row = (s: SessionSummary) => (
    <button key={s.id} className={`list-row ${s.group ? 'linked' : ''}`} onClick={() => open(s)}>
      <div className="list-row-top">
        <span className="list-title">
          {s.starred && <span className="star-mark" title="starred">★</span>}
          {s.group && <span className="link-mark" title="linked chat">⛓</span>}
          {s.title}
        </span>
        <span className="list-time">{relTime(s.updated)}</span>
      </div>
      <div className="list-row-bot">
        <span className={`chip chip-${s.agent}`}>{s.agent}</span>
        <span className="list-preview">{s.preview}</span>
      </div>
    </button>
  );

  return (
    <div className="list">
      {visible.length === 0 && <div className="empty">no chats yet</div>}

      {starred.length > 0 && (
        <>
          <button className="section-head" onClick={() => setStarOpen((o) => !o)}>
            <span className="section-chevron">{starOpen ? '▾' : '▸'}</span>
            <span className="section-star">★</span>
            <span className="section-title">Starred</span>
            <span className="section-count">{starred.length}</span>
          </button>
          {starOpen && starred.map(row)}
        </>
      )}

      {rest.map(row)}

      {state.source === 'locket' ? (
        <div className="new-row">
          <button className="new-btn chip-serena" onClick={() => { newSession('serena'); onOpen(); }}>
            + new serena
          </button>
        </div>
      ) : (
        <div className="new-row">
          <button className="new-btn chip-claude" onClick={() => { newSession('claude'); onOpen(); }}>
            + new claude
          </button>
          <button className="new-btn chip-codex" onClick={() => { newSession('codex'); onOpen(); }}>
            + new codex
          </button>
        </div>
      )}
    </div>
  );
}
