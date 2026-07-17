export type ConnectionMode = 'auto' | 'laptop' | 'locket' | 'mock';

export interface Settings {
  serverUrl: string;
  token: string;
  callToken: string;
  locketBaseUrl: string;
  locketApiKey: string;
  connectionMode: ConnectionMode;
}

const KEY = 'serena.settings';
const DEFAULTS: Settings = {
  serverUrl: '',
  token: '',
  callToken: '',
  locketBaseUrl: '',
  locketApiKey: '',
  connectionMode: 'auto',
};

interface BootCfg {
  url?: string;
  token?: string;
}

function boot(): Settings | null {
  const b = (window as unknown as { SERENA_BOOT?: BootCfg }).SERENA_BOOT;
  if (b && b.url) {
    return {
      ...DEFAULTS,
      serverUrl: b.url,
      token: b.token ?? '',
      callToken: b.token ?? '',
      connectionMode: 'laptop',
    };
  }
  return null;
}

function normalizeSettings(raw: unknown): Settings {
  const saved = (raw && typeof raw === 'object' ? raw : {}) as Partial<Settings> & {
    useMock?: boolean;
  };
  const migratedMode: ConnectionMode =
    saved.connectionMode ??
    (saved.useMock === true ? 'mock' : saved.useMock === false ? 'laptop' : DEFAULTS.connectionMode);

  return {
    ...DEFAULTS,
    ...saved,
    connectionMode: migratedMode,
    serverUrl: saved.serverUrl ?? '',
    token: saved.token ?? '',
    callToken: saved.callToken ?? saved.token ?? '',
    locketBaseUrl: saved.locketBaseUrl ?? '',
    locketApiKey: saved.locketApiKey ?? '',
  };
}

export function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return normalizeSettings(JSON.parse(raw));
  } catch {
    /* ignore */
  }
  return boot() ?? DEFAULTS;
}

export function saveSettings(s: Settings) {
  localStorage.setItem(KEY, JSON.stringify(s));
}
