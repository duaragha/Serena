import { useEffect, useRef, useState } from 'react';
import { App as CapacitorApp } from '@capacitor/app';
import { useSerena } from './store';
import { SessionList } from './components/SessionList';
import { ChatThread } from './components/ChatThread';
import { SettingsScreen } from './components/SettingsScreen';
import { ConnectionBadge } from './components/ConnectionBadge';
import { LinkedToggle } from './components/LinkedToggle';
import { CallScreen } from './components/CallScreen';

type View = 'list' | 'chat' | 'settings' | 'call';

function isCallDeepLink(raw: string | undefined): boolean {
  if (!raw) return false;
  try {
    const url = new URL(raw);
    return url.protocol === 'serena:' && (url.host === 'call' || url.pathname === '/call');
  } catch {
    return false;
  }
}

export default function App() {
  const { state, settings } = useSerena();
  const coldCallClaimedRef = useRef(false);
  const [view, setView] = useState<View>('list');
  const [callRequest, setCallRequest] = useState({
    id: 0,
    autoStart: false,
    coldStart: false,
  });

  useEffect(() => {
    let disposed = false;
    let listener: Awaited<ReturnType<typeof CapacitorApp.addListener>> | undefined;
    const openCall = (raw: string | undefined, coldStart: boolean) => {
      if (disposed || !isCallDeepLink(raw)) return;
      const measureColdStart = coldStart && !coldCallClaimedRef.current;
      coldCallClaimedRef.current = true;
      setCallRequest((current) => ({
        id: current.id + 1,
        autoStart: true,
        coldStart: measureColdStart,
      }));
      setView('call');
    };
    void CapacitorApp.getLaunchUrl().then((result) => openCall(result?.url, true));
    void CapacitorApp.addListener('appUrlOpen', (event) => openCall(event.url, false)).then(
      (handle) => {
        if (disposed) void handle.remove();
        else listener = handle;
      },
    );
    return () => {
      disposed = true;
      if (listener) void listener.remove();
    };
  }, []);

  // Surface connection/daemon errors instead of swallowing them.
  const showBanner = state.status !== 'open' || Boolean(state.lastError);

  const active = state.sessions.find((s) => s.id === state.activeId);
  const title =
    view === 'settings' ? 'settings' : view === 'chat' ? active?.title ?? 'chat' : 'serena';

  if (view === 'call') {
    return (
      <div className="app call-app">
        <CallScreen
          autoStartRequest={callRequest.autoStart ? callRequest.id : 0}
          coldStartRequest={callRequest.coldStart}
          onExit={() => setView('list')}
        />
      </div>
    );
  }

  return (
    <div className="app">
      <header className="topbar">
        {view === 'list' ? (
          <span className="brand">{title}</span>
        ) : (
          <button className="icon-btn" onClick={() => setView('list')} aria-label="back">
            ‹
          </button>
        )}
        {view !== 'list' && <span className="topbar-title">{title}</span>}
        <span className="topbar-right">
          {view !== 'settings' && <ConnectionBadge />}
          <button className="icon-btn" onClick={() => setView('settings')} aria-label="settings">
            ⚙
          </button>
        </span>
      </header>

      {showBanner && (
        <div className={`conn-banner ${state.lastError ? 'conn-banner-err' : ''}`}>
          <span className="conn-banner-msg">
            {state.lastError
              ? state.lastError
              : state.status === 'connecting'
                ? 'connecting…'
                : 'offline'}
          </span>
          <span className="conn-banner-url">
            {state.source === 'locket'
              ? settings.locketBaseUrl || 'no Locket URL set'
              : settings.serverUrl || 'no daemon URL set'}
          </span>
        </div>
      )}

      {view === 'chat' && active?.group && (
        <LinkedToggle group={active.group} activeId={active.id} />
      )}

      <main className="content">
        {view === 'list' && (
          <>
            <button
              className="call-contact-card"
              onClick={() => {
                const coldStart = !coldCallClaimedRef.current;
                coldCallClaimedRef.current = true;
                setCallRequest((current) => ({
                  id: current.id + 1,
                  autoStart: true,
                  coldStart,
                }));
                setView('call');
              }}
              aria-label="call Serena"
              data-testid="serena-contact-card"
            >
              <span className="call-contact-orb" aria-hidden="true" />
              <span className="call-contact-copy">
                <strong>serena</strong>
                <small>call me. i pick up.</small>
              </span>
              <span className="call-contact-action">call</span>
            </button>
            <SessionList onOpen={() => setView('chat')} />
          </>
        )}
        {view === 'chat' && <ChatThread />}
        {view === 'settings' && <SettingsScreen onDone={() => setView('list')} />}
      </main>
    </div>
  );
}
