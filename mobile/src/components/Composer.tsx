import { useRef, useState, type ChangeEvent } from 'react';
import { useSerena } from '../store';

// ws(s)://host:port/ws/chat  ->  http(s)://host:port  (for REST calls like upload)
function httpBase(wsUrl: string): string {
  return wsUrl
    .trim()
    .replace(/^wss/i, 'https')
    .replace(/^ws/i, 'http')
    .replace(/\/ws\/chat.*$/i, '');
}

export function Composer() {
  const { sendMessage, settings, state } = useSerena();
  const [text, setText] = useState('');
  const [uploading, setUploading] = useState(false);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const submit = () => {
    const t = text.trim();
    if (!t) return;
    sendMessage(t);
    setText('');
    if (taRef.current) taRef.current.style.height = 'auto';
  };

  // On a phone, Enter inserts a newline (default textarea behavior). Sending is
  // the ↑ button only — soft keyboards have no Shift+Enter, and Enter-to-send
  // makes multi-line messages impossible.
  const autoGrow = (e: ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value);
    e.target.style.height = 'auto';
    e.target.style.height = Math.min(e.target.scrollHeight, 140) + 'px';
  };

  const onFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = ''; // allow re-picking the same file
    const base = httpBase(settings.serverUrl);
    if (!file || !base) return;
    setUploading(true);
    try {
      const form = new FormData();
      form.append('file', file);
      const r = await fetch(`${base}/api/upload-image`, { method: 'POST', body: form });
      const data = await r.json();
      if (data?.path) {
        // Drop the saved path into the message — the agent reads the image there.
        setText((t) => (t ? t.replace(/\s*$/, ' ') : '') + data.path + ' ');
        taRef.current?.focus();
      }
    } catch {
      /* upload failed — leave the message as-is */
    }
    setUploading(false);
  };

  return (
    <div className="composer">
      <input
        ref={fileRef}
        type="file"
        accept="image/*"
        style={{ display: 'none' }}
        onChange={onFile}
      />
      <button
        className="composer-attach"
        onClick={() => fileRef.current?.click()}
        disabled={uploading || state.source !== 'laptop'}
        aria-label="attach image"
      >
        {uploading ? '…' : '+'}
      </button>
      <textarea
        ref={taRef}
        className="composer-input"
        value={text}
        onChange={autoGrow}
        placeholder="message serena…"
        rows={1}
      />
      <button className="composer-send" onClick={submit} disabled={!text.trim()} aria-label="send">
        ↑
      </button>
    </div>
  );
}
