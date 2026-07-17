import {
  Capacitor,
  registerPlugin,
  type PermissionState,
  type PluginListenerHandle,
} from '@capacitor/core';

export type CallConnectionState =
  | 'connecting'
  | 'open'
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'reconnecting'
  | 'closed';

export type TailnetPath = 'direct' | 'relay' | 'unknown';

export interface CallConnectOptions {
  url: string;
  token: string;
  pingIntervalMs?: number;
  path?: TailnetPath;
  coldStart?: boolean;
}

export interface CallConnectResult {
  callId: string;
}

export interface CallGenerationResult {
  generation: number;
}

export interface CallEndPushToTalkResult extends CallGenerationResult {
  active: false;
}

export interface CallNativeState {
  callId: string;
  connected: boolean;
  pushToTalk: boolean;
  generation: number;
}

export interface CallEndpointResult {
  url: string;
}

export interface ArtifactOpenedOptions {
  eventSeq: number;
  jobId: string;
  receipt: string;
}

export interface ArtifactFetchResult {
  content: string;
  receipt: string;
}

export interface CallPermissionStatus {
  microphone: PermissionState;
}

export interface CallStateEvent {
  state: CallConnectionState;
  callId: string;
  reconnectAttempt: number;
  generation: number;
}

export interface CallControlEvent {
  type: string;
  [key: string]: unknown;
}

export interface CallRttEvent {
  rttMs: number;
  rollingRttMs: number;
  path: TailnetPath;
  pathSource: 'client_config' | 'tailscale_probe' | 'unknown';
}

export interface CallSequenceGapEvent {
  direction: 'input' | 'output';
  generation: number;
  expected: number;
  received: number;
}

export interface CallPlaybackEvent {
  state: 'started' | 'content_started' | 'underrun' | 'ended';
  generation: number;
  sequence?: number;
  sampleRate?: number;
  count?: number;
  delta?: number;
  appUptimeMs?: number;
}

export interface CallQueueDepthEvent {
  depth: number;
  generation: number;
}

export interface CallErrorEvent {
  code: string;
  message: string;
  fatal: boolean;
}

interface SerenaCallNativePlugin {
  connect(options: CallConnectOptions): Promise<CallConnectResult>;
  beginPushToTalk(): Promise<CallGenerationResult>;
  endPushToTalk(): Promise<CallEndPushToTalkResult>;
  cancel(): Promise<CallGenerationResult>;
  hangup(): Promise<void>;
  getState(): Promise<CallNativeState>;
  getEndpoint(): Promise<CallEndpointResult>;
  artifactOpened(options: ArtifactOpenedOptions): Promise<void>;
  fetchArtifact(options: { url: string }): Promise<ArtifactFetchResult>;
  checkPermissions(): Promise<CallPermissionStatus>;
  requestPermissions(): Promise<CallPermissionStatus>;
  addListener(eventName: 'state', listener: (event: CallStateEvent) => void): Promise<PluginListenerHandle>;
  addListener(eventName: 'control', listener: (event: CallControlEvent) => void): Promise<PluginListenerHandle>;
  addListener(eventName: 'rtt', listener: (event: CallRttEvent) => void): Promise<PluginListenerHandle>;
  addListener(
    eventName: 'sequenceGap',
    listener: (event: CallSequenceGapEvent) => void,
  ): Promise<PluginListenerHandle>;
  addListener(eventName: 'playback', listener: (event: CallPlaybackEvent) => void): Promise<PluginListenerHandle>;
  addListener(eventName: 'queueDepth', listener: (event: CallQueueDepthEvent) => void): Promise<PluginListenerHandle>;
  addListener(eventName: 'error', listener: (event: CallErrorEvent) => void): Promise<PluginListenerHandle>;
  removeAllListeners(): Promise<void>;
}

const nativeCall = registerPlugin<SerenaCallNativePlugin>('SerenaCall');

export function toCallWebSocketUrl(input: string): string {
  let raw = input.trim();
  if (!raw) throw new Error('call server url is required');
  if (!/^(wss?|https?):\/\//i.test(raw)) raw = `ws://${raw}`;
  raw = raw.replace(/^http:/i, 'ws:').replace(/^https:/i, 'wss:');

  const parsed = new URL(raw);
  parsed.hash = '';
  parsed.searchParams.delete('token');
  if (/\/ws\/chat\/?$/.test(parsed.pathname)) {
    parsed.pathname = parsed.pathname.replace(/\/ws\/chat\/?$/, '/ws/call');
  } else if (/\/ws\/call\/?$/.test(parsed.pathname)) {
    parsed.pathname = parsed.pathname.replace(/\/ws\/call\/?$/, '/ws/call');
  } else {
    parsed.pathname = `${parsed.pathname.replace(/\/+$/, '')}/ws/call`;
  }
  return parsed.toString();
}

function requireAndroid(): void {
  if (Capacitor.getPlatform() !== 'android' || !Capacitor.isPluginAvailable('SerenaCall')) {
    throw new Error('Serena call audio transport requires the native Android app');
  }
}

export const callTransport = {
  isAvailable(): boolean {
    return Capacitor.getPlatform() === 'android' && Capacitor.isPluginAvailable('SerenaCall');
  },

  async endpoint(): Promise<CallEndpointResult> {
    requireAndroid();
    return nativeCall.getEndpoint();
  },

  async connect(options: CallConnectOptions): Promise<CallConnectResult> {
    requireAndroid();
    return nativeCall.connect({ ...options, url: toCallWebSocketUrl(options.url) });
  },

  async pttBegin(): Promise<CallGenerationResult> {
    requireAndroid();
    return nativeCall.beginPushToTalk();
  },

  async pttEnd(): Promise<CallEndPushToTalkResult> {
    requireAndroid();
    return nativeCall.endPushToTalk();
  },

  async cancel(): Promise<CallGenerationResult> {
    requireAndroid();
    return nativeCall.cancel();
  },

  async hangup(): Promise<void> {
    requireAndroid();
    await nativeCall.hangup();
  },

  async getState(): Promise<CallNativeState> {
    requireAndroid();
    return nativeCall.getState();
  },

  async artifactOpened(options: ArtifactOpenedOptions): Promise<void> {
    requireAndroid();
    await nativeCall.artifactOpened(options);
  },

  async fetchArtifact(url: string): Promise<ArtifactFetchResult> {
    requireAndroid();
    return nativeCall.fetchArtifact({ url });
  },

  async checkPermissions(): Promise<CallPermissionStatus> {
    requireAndroid();
    return nativeCall.checkPermissions();
  },

  async requestPermissions(): Promise<CallPermissionStatus> {
    requireAndroid();
    return nativeCall.requestPermissions();
  },

  onState(listener: (event: CallStateEvent) => void): Promise<PluginListenerHandle> {
    return nativeCall.addListener('state', listener);
  },

  onControl(listener: (event: CallControlEvent) => void): Promise<PluginListenerHandle> {
    return nativeCall.addListener('control', listener);
  },

  onRtt(listener: (event: CallRttEvent) => void): Promise<PluginListenerHandle> {
    return nativeCall.addListener('rtt', listener);
  },

  onSequenceGap(listener: (event: CallSequenceGapEvent) => void): Promise<PluginListenerHandle> {
    return nativeCall.addListener('sequenceGap', listener);
  },

  onPlayback(listener: (event: CallPlaybackEvent) => void): Promise<PluginListenerHandle> {
    return nativeCall.addListener('playback', listener);
  },

  onQueueDepth(listener: (event: CallQueueDepthEvent) => void): Promise<PluginListenerHandle> {
    return nativeCall.addListener('queueDepth', listener);
  },

  onError(listener: (event: CallErrorEvent) => void): Promise<PluginListenerHandle> {
    return nativeCall.addListener('error', listener);
  },

  removeAllListeners(): Promise<void> {
    return nativeCall.removeAllListeners();
  },
};
