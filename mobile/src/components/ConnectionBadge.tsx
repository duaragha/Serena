import { useSerena } from '../store';

const LABEL: Record<string, string> = {
  connecting: 'connecting…',
  open: 'connected',
  closed: 'offline',
  error: 'error',
};

export function ConnectionBadge() {
  const { state } = useSerena();
  const status = state.status;
  const source = status === 'open' ? state.source : state.source === 'offline' ? 'offline' : state.source;
  return (
    <span className={`badge badge-${status} badge-source-${source}`}>
      <span className="badge-dot" />
      {source === 'offline' ? 'offline' : `${source} ${LABEL[status] ?? status}`}
    </span>
  );
}
