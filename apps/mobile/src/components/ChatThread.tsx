import { useEffect, useRef } from 'react';
import { useSerena } from '../store';
import { MessageBubble } from './MessageBubble';
import { Composer } from './Composer';

export function ChatThread() {
  const { state } = useSerena();
  const id = state.activeId;
  const messages = id ? state.messages[id] ?? [] : [];
  const endRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to the latest content, including each streaming chunk.
  const lastLen = messages.length;
  const lastText = messages[messages.length - 1]?.text ?? '';
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [lastLen, lastText]);

  if (!id) return <div className="empty">pick a chat</div>;

  return (
    <div className="thread">
      <div className="thread-scroll">
        {messages.length === 0 && <div className="empty">no messages yet — say something</div>}
        {messages.map((m) => (
          <MessageBubble key={m.id} msg={m} />
        ))}
        <div ref={endRef} />
      </div>
      <Composer />
    </div>
  );
}
