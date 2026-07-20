import {
  Capacitor,
  registerPlugin,
  type PermissionState,
  type PluginListenerHandle,
} from '@capacitor/core';
import { webCallTransport } from './web';

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

function nativeAvailable(): boolean {
  return Capacitor.getPlatform() === 'android' && Capacitor.isPluginAvailable('SerenaCall');
}

function requireAvailable(): void {
  if (!nativeAvailable() && !webCallTransport.isAvailable()) {
    throw new Error('Serena call audio requires the native Android app or a secure browser');
  }
}

export const callTransport = {
  isAvailable(): boolean {
    return nativeAvailable() || webCallTransport.isAvailable();
  },

  async endpoint(serverUrl?: string): Promise<CallEndpointResult> {
    requireAvailable();
    if (nativeAvailable()) return nativeCall.getEndpoint();
    return webCallTransport.endpoint(serverUrl);
  },

  async connect(options: CallConnectOptions): Promise<CallConnectResult> {
    requireAvailable();
    const url = toCallWebSocketUrl(options.url);
    if (nativeAvailable()) return nativeCall.connect({ ...options, url });
    return webCallTransport.connect(options, url);
  },

  async pttBegin(): Promise<CallGenerationResult> {
    requireAvailable();
    return nativeAvailable() ? nativeCall.beginPushToTalk() : webCallTransport.pttBegin();
  },

  async pttEnd(): Promise<CallEndPushToTalkResult> {
    requireAvailable();
    return nativeAvailable() ? nativeCall.endPushToTalk() : webCallTransport.pttEnd();
  },

  async cancel(): Promise<CallGenerationResult> {
    requireAvailable();
    return nativeAvailable() ? nativeCall.cancel() : webCallTransport.cancel();
  },

  async hangup(): Promise<void> {
    requireAvailable();
    if (nativeAvailable()) await nativeCall.hangup();
    else await webCallTransport.hangup();
  },

  async getState(): Promise<CallNativeState> {
    requireAvailable();
    return nativeAvailable() ? nativeCall.getState() : webCallTransport.getState();
  },

  async artifactOpened(options: ArtifactOpenedOptions): Promise<void> {
    requireAvailable();
    if (nativeAvailable()) await nativeCall.artifactOpened(options);
    else await webCallTransport.artifactOpened(options);
  },

  async fetchArtifact(url: string): Promise<ArtifactFetchResult> {
    requireAvailable();
    return nativeAvailable() ? nativeCall.fetchArtifact({ url }) : webCallTransport.fetchArtifact(url);
  },

  async checkPermissions(): Promise<CallPermissionStatus> {
    requireAvailable();
    return nativeAvailable() ? nativeCall.checkPermissions() : webCallTransport.checkPermissions();
  },

  async requestPermissions(): Promise<CallPermissionStatus> {
    requireAvailable();
    return nativeAvailable() ? nativeCall.requestPermissions() : webCallTransport.requestPermissions();
  },

  onState(listener: (event: CallStateEvent) => void): Promise<PluginListenerHandle> {
    return nativeAvailable()
      ? nativeCall.addListener('state', listener)
      : webCallTransport.addListener('state', listener);
  },

  onControl(listener: (event: CallControlEvent) => void): Promise<PluginListenerHandle> {
    return nativeAvailable()
      ? nativeCall.addListener('control', listener)
      : webCallTransport.addListener('control', listener);
  },

  onRtt(listener: (event: CallRttEvent) => void): Promise<PluginListenerHandle> {
    return nativeAvailable()
      ? nativeCall.addListener('rtt', listener)
      : webCallTransport.addListener('rtt', listener);
  },

  onSequenceGap(listener: (event: CallSequenceGapEvent) => void): Promise<PluginListenerHandle> {
    return nativeAvailable()
      ? nativeCall.addListener('sequenceGap', listener)
      : webCallTransport.addListener('sequenceGap', listener);
  },

  onPlayback(listener: (event: CallPlaybackEvent) => void): Promise<PluginListenerHandle> {
    return nativeAvailable()
      ? nativeCall.addListener('playback', listener)
      : webCallTransport.addListener('playback', listener);
  },

  onQueueDepth(listener: (event: CallQueueDepthEvent) => void): Promise<PluginListenerHandle> {
    return nativeAvailable()
      ? nativeCall.addListener('queueDepth', listener)
      : webCallTransport.addListener('queueDepth', listener);
  },

  onError(listener: (event: CallErrorEvent) => void): Promise<PluginListenerHandle> {
    return nativeAvailable()
      ? nativeCall.addListener('error', listener)
      : webCallTransport.addListener('error', listener);
  },

  removeAllListeners(): Promise<void> {
    return nativeAvailable() ? nativeCall.removeAllListeners() : webCallTransport.removeAllListeners();
  },
};
