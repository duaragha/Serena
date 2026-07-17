import { useSerena } from '../store';
import type { AgentKind } from '../types';

// Phones can't do the desktop's side-by-side claude+codex panes, so for a
// linked chat we show a claude/codex switch at the top of the thread. Tapping
// the other agent opens its sibling session in the same group.
export function LinkedToggle({ group, activeId }: { group: string; activeId: string }) {
  const { state, openSession } = useSerena();
  const peers = state.sessions.filter((s) => s.group === group);
  const pick = (a: AgentKind) => peers.find((s) => s.agent === a);
  const claude = pick('claude');
  const codex = pick('codex');

  // Only meaningful when both sides of the pair exist.
  if (!claude || !codex) return null;

  return (
    <div className="linked-toggle">
      <span className="lt-label">⛓ linked</span>
      <div className="lt-segs">
        {([['claude', claude], ['codex', codex]] as const).map(([agent, s]) => {
          const isActive = s.id === activeId;
          return (
            <button
              key={agent}
              className={`lt-seg lt-${agent} ${isActive ? 'lt-active' : ''}`}
              onClick={() => !isActive && openSession(s.id)}
            >
              {agent}
            </button>
          );
        })}
      </div>
    </div>
  );
}
