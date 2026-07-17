import { useState } from 'react';
import { useSerena } from '../store';
import type { ConnectionMode } from '../settings';

const MODES: { value: ConnectionMode; label: string }[] = [
  { value: 'auto', label: 'auto' },
  { value: 'laptop', label: 'daemon' },
  { value: 'locket', label: 'Locket' },
  { value: 'mock', label: 'mock' },
];

export function SettingsScreen({ onDone }: { onDone: () => void }) {
  const { settings, updateSettings, state } = useSerena();
  const [serverUrl, setServerUrl] = useState(settings.serverUrl);
  const [token, setToken] = useState(settings.token);
  const [callToken, setCallToken] = useState(settings.callToken);
  const [locketBaseUrl, setLocketBaseUrl] = useState(settings.locketBaseUrl);
  const [locketApiKey, setLocketApiKey] = useState(settings.locketApiKey);
  const [connectionMode, setConnectionMode] = useState<ConnectionMode>(settings.connectionMode);

  const save = () => {
    updateSettings({
      serverUrl: serverUrl.trim(),
      token: token.trim(),
      callToken: callToken.trim(),
      locketBaseUrl: locketBaseUrl.trim(),
      locketApiKey: locketApiKey.trim(),
      connectionMode,
    });
    onDone();
  };

  const laptopDisabled = connectionMode === 'mock' || connectionMode === 'locket';
  const locketDisabled = connectionMode === 'mock' || connectionMode === 'laptop';

  return (
    <div className="settings">
      <div className="mode-grid" role="radiogroup" aria-label="connection mode">
        {MODES.map((mode) => (
          <button
            key={mode.value}
            className={`mode-btn ${connectionMode === mode.value ? 'mode-btn-active' : ''}`}
            onClick={() => setConnectionMode(mode.value)}
            role="radio"
            aria-checked={connectionMode === mode.value}
          >
            {mode.label}
          </button>
        ))}
      </div>

      <fieldset disabled={laptopDisabled} className="field-group">
        <label className="field">
          <span>Daemon URL (PC)</span>
          <input
            value={serverUrl}
            onChange={(e) => setServerUrl(e.target.value)}
            placeholder="http://100.x.x.x:8765/ws/chat"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
          />
        </label>
        <label className="field">
          <span>Daemon token</span>
          <input
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="shared secret from the daemon"
            type="password"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
          />
        </label>
      </fieldset>

      <fieldset className="field-group">
        <label className="field">
          <span>Call token</span>
          <input
            value={callToken}
            onChange={(e) => setCallToken(e.target.value)}
            placeholder="defaults to the daemon token"
            type="password"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
          />
          <small className="field-hint">
            used only for the pinned tailnet call endpoint
          </small>
        </label>
      </fieldset>

      <fieldset disabled={locketDisabled} className="field-group">
        <label className="field">
          <span>Locket URL</span>
          <input
            value={locketBaseUrl}
            onChange={(e) => setLocketBaseUrl(e.target.value)}
            placeholder="https://locket.example.com"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
          />
        </label>
        <label className="field">
          <span>Locket API key</span>
          <input
            value={locketApiKey}
            onChange={(e) => setLocketApiKey(e.target.value)}
            placeholder="Bearer key"
            type="password"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
          />
        </label>
      </fieldset>

      <div className="settings-status">
        connection: <strong>{state.source}</strong> · <strong>{state.status}</strong>
      </div>

      <button className="save-btn" onClick={save}>
        save & reconnect
      </button>
    </div>
  );
}
