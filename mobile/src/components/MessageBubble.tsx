import type { ChatMessage } from '../types';

export function MessageBubble({ msg }: { msg: ChatMessage }) {
  return (
    <div className={`bubble-row bubble-row-${msg.role}`}>
      <div className={`bubble bubble-${msg.role}`}>
        {msg.text}
        {msg.streaming && <span className="caret" />}
      </div>
    </div>
  );
}
