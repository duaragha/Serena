import type { PluginListenerHandle } from '@capacitor/core';
import type {
  ArtifactFetchResult,
  ArtifactOpenedOptions,
  CallConnectOptions,
  CallConnectResult,
  CallConnectionState,
  CallControlEvent,
  CallEndPushToTalkResult,
  CallErrorEvent,
  CallGenerationResult,
  CallNativeState,
  CallPermissionStatus,
  CallPlaybackEvent,
  CallQueueDepthEvent,
  CallRttEvent,
  CallSequenceGapEvent,
  CallStateEvent,
  TailnetPath,
} from './index';
import {
  decodeTtsFrame,
  encodeMicFrame,
  playbackStartPollTimeoutMs,
  floatToPcm16,
  shouldReportPlaybackUnderrun,
  StreamingResampler,
  WEB_CALL_PROTOCOL,
} from './web_protocol';

type WebCallEventMap = {
  state: CallStateEvent;
  control: CallControlEvent;
  rtt: CallRttEvent;
  sequenceGap: CallSequenceGapEvent;
  playback: CallPlaybackEvent;
  queueDepth: CallQueueDepthEvent;
  error: CallErrorEvent;
};

type Listener<K extends keyof WebCallEventMap> = (event: WebCallEventMap[K]) => void;
type ListenerStore = {
  [K in keyof WebCallEventMap]: Set<Listener<K>>;
};

interface PendingConnect {
  resolve: (value: CallConnectResult) => void;
  reject: (reason: Error) => void;
  timeout: number;
}

interface PendingPing {
  startedAtMs: number;
}

interface SegmentMarker {
  kind: 'acknowledgement' | 'content';
}

const MAX_RECONNECT_DELAY_MS = 8_000;
const DEFAULT_PING_INTERVAL_MS = 5_000;
const MIN_PING_INTERVAL_MS = 1_000;
const MAX_PING_INTERVAL_MS = 60_000;
const CONNECT_TIMEOUT_MS = 30_000;
const HANGUP_ACK_TIMEOUT_MS = 1_500;
const PLAYBACK_START_LEAD_SECONDS = 0.1;
const MAX_PLAYBACK_AHEAD_SECONDS = 30;
const ARTIFACT_RECEIPT_TTL_MS = 5 * 60_000;
const APP_STARTED_AT_MS = typeof performance === 'undefined' ? 0 : performance.now();

function randomId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function monotonicMs(): number {
  return typeof performance === 'undefined' ? Date.now() : performance.now();
}

function monotonicUs(): number {
  return Math.floor(monotonicMs() * 1_000);
}

function websocketToHttp(input: string, path: string): string {
  const url = new URL(input);
  url.protocol = url.protocol === 'wss:' ? 'https:' : 'http:';
  url.pathname = path;
  url.search = '';
  url.hash = '';
  return url.toString();
}

function audioContextConstructor(): typeof AudioContext | null {
  if (typeof window === 'undefined') return null;
  const extended = window as typeof window & { webkitAudioContext?: typeof AudioContext };
  return window.AudioContext ?? extended.webkitAudioContext ?? null;
}

function normalizePath(value: unknown): TailnetPath {
  return value === 'direct' || value === 'relay' ? value : 'unknown';
}

function isReplayableJobEvent(type: string): boolean {
  return ['job.accepted', 'job.progress', 'artifact.ready', 'job.failed'].includes(type);
}

export class WebCallTransport {
  private readonly listeners: ListenerStore = {
    state: new Set(),
    control: new Set(),
    rtt: new Set(),
    sequenceGap: new Set(),
    playback: new Set(),
    queueDepth: new Set(),
    error: new Set(),
  };

  private socket: WebSocket | null = null;
  private socketEpoch = 0;
  private socketUrl = '';
  private authToken = '';
  private callId = '';
  private connected = false;
  private serverReady = false;
  private userClosed = true;
  private generation = 0;
  private reconnectAttempt = 0;
  private reconnectTimer: number | null = null;
  private pingTimer: number | null = null;
  private pingIntervalMs = DEFAULT_PING_INTERVAL_MS;
  private pendingConnect: PendingConnect | null = null;
  private hangupPromise: Promise<void> | null = null;
  private hangupAck: (() => void) | null = null;
  private hangupAckTimer: number | null = null;
  private pendingPings = new Map<string, PendingPing>();
  private rollingRttMs = -1;
  private configuredPath: TailnetPath = 'unknown';
  private highestJobEventSequence = 0;
  private coldStart = false;
  private helloReported = false;

  private micStream: MediaStream | null = null;
  private audioContext: AudioContext | null = null;
  private micSource: MediaStreamAudioSourceNode | null = null;
  private micProcessor: ScriptProcessorNode | null = null;
  private micSink: GainNode | null = null;
  private resampler: StreamingResampler | null = null;
  private micPcm: number[] = [];
  private micSequence = 0;
  private micGeneration = -1;
  private pttReleaseMs = new Map<number, number>();

  private playbackGeneration = -1;
  private playbackSampleRate = 0;
  private expectedOutputSequence = 0;
  private nextPlaybackAt = 0;
  private playbackSources = new Set<AudioBufferSourceNode>();
  private playbackTimers = new Set<number>();
  private segmentMarkers = new Map<number, SegmentMarker>();
  private firstPlaybackAcknowledged = new Set<number>();
  private contentPlaybackAcknowledged = new Set<number>();
  private firstPlaybackPolling = new Set<number>();
  private contentPlaybackPolling = new Set<number>();
  private firstOutputReceivedMs = new Map<number, number>();
  private playbackHasStarted = false;
  private underrunCount = 0;

  private fetchedArtifactReceipts = new Map<string, number>();

  isAvailable(): boolean {
    return Boolean(
      typeof window !== 'undefined' &&
        window.isSecureContext &&
        typeof WebSocket !== 'undefined' &&
        navigator.mediaDevices &&
        audioContextConstructor(),
    );
  }

  endpoint(input?: string): { url: string } {
    const origin = input?.trim() || (typeof window !== 'undefined' ? window.location.origin : '');
    if (!origin) throw new Error('call server url is required');
    return { url: origin };
  }

  async connect(options: CallConnectOptions, normalizedUrl: string): Promise<CallConnectResult> {
    if (!this.isAvailable()) {
      throw new Error('call audio needs a secure browser with microphone access');
    }
    await this.hangup(false);
    this.socketUrl = normalizedUrl;
    this.authToken = options.token.trim();
    if (!this.authToken) throw new Error('call token is required');
    this.callId = randomId();
    this.connected = false;
    this.serverReady = false;
    this.userClosed = false;
    this.generation = 0;
    this.reconnectAttempt = 0;
    this.rollingRttMs = -1;
    this.configuredPath = normalizePath(options.path);
    this.pingIntervalMs = Math.max(
      MIN_PING_INTERVAL_MS,
      Math.min(MAX_PING_INTERVAL_MS, options.pingIntervalMs ?? DEFAULT_PING_INTERVAL_MS),
    );
    this.highestJobEventSequence = 0;
    this.coldStart = Boolean(options.coldStart);
    this.helloReported = false;
    this.pttReleaseMs.clear();
    this.firstOutputReceivedMs.clear();
    this.firstPlaybackAcknowledged.clear();
    this.contentPlaybackAcknowledged.clear();
    this.fetchedArtifactReceipts.clear();
    this.emitState('connecting');

    return new Promise<CallConnectResult>((resolve, reject) => {
      const timeout = window.setTimeout(() => {
        if (this.pendingConnect?.resolve !== resolve) return;
        this.pendingConnect = null;
        void this.hangup(false);
        reject(new Error('call server did not become ready'));
      }, CONNECT_TIMEOUT_MS);
      this.pendingConnect = { resolve, reject, timeout };
      void this.openSocket(false);
    });
  }

  async pttBegin(): Promise<CallGenerationResult> {
    if (!this.connected || !this.serverReady || !this.socketIsOpen()) {
      throw new Error('call is not ready yet');
    }
    await this.ensureAudioContext();
    await this.ensureMicStream();
    this.stopCapture(false);
    this.resetPlayback(this.generation);
    this.generation += 1;
    this.micGeneration = this.generation;
    this.micSequence = 0;
    this.micPcm = [];
    const context = this.audioContext;
    const stream = this.micStream;
    if (!context || !stream) throw new Error('microphone is unavailable');
    this.resampler = new StreamingResampler(context.sampleRate);
    this.micSource = context.createMediaStreamSource(stream);
    this.micProcessor = context.createScriptProcessor(4_096, 1, 1);
    this.micSink = context.createGain();
    this.micSink.gain.value = 0;
    const generation = this.generation;
    this.micProcessor.onaudioprocess = (event) => {
      if (this.micGeneration !== generation || !this.micProcessor) return;
      const input = event.inputBuffer.getChannelData(0);
      this.acceptMicSamples(this.resampler?.push(input) ?? new Float32Array(0), generation);
    };
    this.micSource.connect(this.micProcessor);
    this.micProcessor.connect(this.micSink);
    this.micSink.connect(context.destination);
    if (!this.sendControl({ type: 'ptt.begin', generation })) {
      this.stopCapture(false);
      throw new Error('call websocket closed before push to talk began');
    }
    this.emitState('listening');
    return { generation };
  }

  async pttEnd(): Promise<CallEndPushToTalkResult> {
    const generation = this.micGeneration;
    if (generation < 1 || !this.micProcessor) {
      return { generation: this.generation, active: false };
    }
    const releasedAt = monotonicMs();
    this.pttReleaseMs.set(generation, releasedAt);
    this.stopCapture(true);
    if (this.micPcm.length > 0) {
      const frame = new Int16Array(WEB_CALL_PROTOCOL.micFrameSamples);
      frame.set(this.micPcm.slice(0, WEB_CALL_PROTOCOL.micFrameSamples));
      this.sendMicFrame(frame, generation, true);
      this.micPcm = [];
    }
    if (
      !this.sendControl({
        type: 'ptt.end',
        generation,
        eou_monotonic_us: Math.floor(releasedAt * 1_000),
      })
    ) {
      throw new Error('call websocket closed before push to talk ended');
    }
    this.emitState('thinking');
    return { generation, active: false };
  }

  async cancel(): Promise<CallGenerationResult> {
    const target = Math.max(this.generation, this.micGeneration, this.playbackGeneration);
    this.stopCapture(false);
    this.resetPlayback(target);
    this.sendControl({ type: 'cancel', generation: target });
    this.emitState(this.serverReady ? 'open' : 'reconnecting');
    return { generation: target };
  }

  async hangup(emit = true): Promise<void> {
    if (this.hangupPromise) return this.hangupPromise;
    const pending = this.finishHangup(emit);
    this.hangupPromise = pending;
    try {
      await pending;
    } finally {
      if (this.hangupPromise === pending) this.hangupPromise = null;
    }
  }

  private async finishHangup(emit: boolean): Promise<void> {
    this.userClosed = true;
    this.clearReconnect();
    this.clearPing();
    this.stopCapture(false);
    this.resetPlayback(Number.MAX_SAFE_INTEGER);
    const socket = this.socket;
    if (this.socketIsOpen()) {
      const acknowledged = new Promise<void>((resolve) => {
        this.hangupAck = resolve;
        this.hangupAckTimer = window.setTimeout(
          () => this.resolveHangupAck(),
          HANGUP_ACK_TIMEOUT_MS,
        );
      });
      this.sendControl({ type: 'hangup' });
      await acknowledged;
    }
    this.socket = null;
    this.socketEpoch += 1;
    if (socket) socket.close(1_000, 'hangup');
    this.connected = false;
    this.serverReady = false;
    if (this.pendingConnect) {
      window.clearTimeout(this.pendingConnect.timeout);
      this.pendingConnect.reject(new Error('call connection was closed'));
      this.pendingConnect = null;
    }
    this.releaseMicrophone();
    if (this.audioContext) {
      const context = this.audioContext;
      this.audioContext = null;
      await context.close().catch(() => undefined);
    }
    if (emit && this.callId) this.emitState('closed');
  }

  private resolveHangupAck(): void {
    if (this.hangupAckTimer !== null) window.clearTimeout(this.hangupAckTimer);
    this.hangupAckTimer = null;
    const resolve = this.hangupAck;
    this.hangupAck = null;
    resolve?.();
  }

  getState(): CallNativeState {
    return {
      callId: this.callId,
      connected: this.connected && this.serverReady,
      pushToTalk: Boolean(this.micProcessor),
      generation: this.generation,
    };
  }

  async checkPermissions(): Promise<CallPermissionStatus> {
    if (this.liveMicStream()) return { microphone: 'granted' };
    if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      return { microphone: 'denied' };
    }
    // Force acquisition from the call button even when the browser remembers
    // permission. This primes both the stream and AudioContext inside the user
    // gesture, which iOS requires before greeting playback can begin.
    return { microphone: 'prompt' };
  }

  async requestPermissions(): Promise<CallPermissionStatus> {
    try {
      await this.ensureAudioContext();
      await this.ensureMicStream();
      return { microphone: 'granted' };
    } catch {
      return { microphone: 'denied' };
    }
  }

  async fetchArtifact(url: string): Promise<ArtifactFetchResult> {
    const activeOrigin = new URL(websocketToHttp(this.socketUrl, '/')).origin;
    const target = new URL(url, activeOrigin);
    if (
      target.origin !== activeOrigin ||
      !target.pathname.startsWith('/artifacts/') ||
      target.username ||
      target.password
    ) {
      throw new Error('artifact url is outside the active call endpoint');
    }
    const response = await fetch(target, {
      cache: 'no-store',
      credentials: 'same-origin',
      redirect: 'error',
    });
    if (!response.ok) throw new Error('draft link could not be opened');
    const receipt = response.headers.get('X-Serena-Artifact-Receipt')?.trim() ?? '';
    const content = await response.text();
    if (!receipt || !content || content.length > 512 * 1_024) {
      throw new Error('draft response was invalid');
    }
    this.fetchedArtifactReceipts.set(receipt, Date.now());
    return { content, receipt };
  }

  async artifactOpened(options: ArtifactOpenedOptions): Promise<void> {
    const issuedAt = this.fetchedArtifactReceipts.get(options.receipt);
    this.fetchedArtifactReceipts.delete(options.receipt);
    if (issuedAt === undefined || Date.now() - issuedAt > ARTIFACT_RECEIPT_TTL_MS) {
      throw new Error('the artifact must be fetched in-app first');
    }
    if (
      !this.sendControl({
        type: 'artifact.opened',
        event_seq: options.eventSeq,
        job_id: options.jobId,
        receipt: options.receipt,
      })
    ) {
      this.fetchedArtifactReceipts.set(options.receipt, issuedAt);
      throw new Error('call websocket is not connected');
    }
  }

  addListener<K extends keyof WebCallEventMap>(
    eventName: K,
    listener: Listener<K>,
  ): Promise<PluginListenerHandle> {
    const store = this.listeners[eventName] as Set<Listener<K>>;
    store.add(listener);
    return Promise.resolve({
      remove: async () => {
        store.delete(listener);
      },
    });
  }

  async removeAllListeners(): Promise<void> {
    for (const listeners of Object.values(this.listeners)) listeners.clear();
  }

  private emit<K extends keyof WebCallEventMap>(eventName: K, event: WebCallEventMap[K]): void {
    const listeners = this.listeners[eventName] as Set<Listener<K>>;
    for (const listener of listeners) listener(event);
  }

  private emitState(state: CallConnectionState): void {
    this.emit('state', {
      state,
      callId: this.callId,
      reconnectAttempt: this.reconnectAttempt,
      generation: this.generation,
    });
  }

  private emitError(code: string, message: string, fatal: boolean): void {
    this.emit('error', { code, message, fatal });
    if (fatal) void this.hangup();
  }

  private socketIsOpen(): boolean {
    return this.socket?.readyState === WebSocket.OPEN;
  }

  private sendControl(control: Record<string, unknown>): boolean {
    if (!this.socketIsOpen()) return false;
    this.socket?.send(JSON.stringify(control));
    return true;
  }

  private async authorizeSocket(): Promise<void> {
    const response = await fetch(websocketToHttp(this.socketUrl, '/api/call/socket-auth'), {
      method: 'POST',
      headers: { Authorization: `Bearer ${this.authToken}` },
      cache: 'no-store',
      credentials: 'same-origin',
    });
    if (!response.ok) {
      throw new Error(response.status === 401 ? 'call token was rejected' : 'call authorization failed');
    }
  }

  private async openSocket(resume: boolean): Promise<void> {
    const epoch = this.socketEpoch + 1;
    this.socketEpoch = epoch;
    try {
      await this.authorizeSocket();
    } catch (reason) {
      if (epoch !== this.socketEpoch || this.userClosed) return;
      this.emit('error', {
        code: 'socket_auth',
        message: reason instanceof Error ? reason.message : 'call authorization failed',
        fatal: false,
      });
      this.scheduleReconnect();
      return;
    }
    if (epoch !== this.socketEpoch || this.userClosed) return;
    this.emitState(resume ? 'reconnecting' : 'connecting');
    const socket = new WebSocket(this.socketUrl);
    socket.binaryType = 'arraybuffer';
    this.socket = socket;
    socket.onopen = () => {
      if (epoch !== this.socketEpoch || this.userClosed || this.socket !== socket) {
        socket.close(1_000, 'stale connection');
        return;
      }
      this.connected = true;
      this.serverReady = false;
      this.sendControl({
        type: 'call.start',
        call_id: this.callId,
        generation: this.generation,
        greeting: true,
        job_cursor: this.highestJobEventSequence,
      });
      this.startPing();
    };
    socket.onmessage = (event) => {
      if (epoch !== this.socketEpoch || this.socket !== socket) return;
      if (typeof event.data === 'string') {
        this.handleControl(event.data);
      } else if (event.data instanceof ArrayBuffer) {
        this.handleAudioFrame(event.data);
      } else if (event.data instanceof Blob) {
        void event.data.arrayBuffer().then((frame) => {
          if (epoch === this.socketEpoch && this.socket === socket) this.handleAudioFrame(frame);
        });
      }
    };
    socket.onerror = () => {
      if (epoch === this.socketEpoch && this.socket === socket) {
        this.emit('error', {
          code: 'websocket',
          message: 'call websocket failed',
          fatal: false,
        });
      }
    };
    socket.onclose = () => {
      if (epoch !== this.socketEpoch || this.socket !== socket) return;
      this.socket = null;
      this.handleDisconnect();
    };
  }

  private handleDisconnect(): void {
    this.connected = false;
    this.serverReady = false;
    this.clearPing();
    this.pendingPings.clear();
    this.stopCapture(false);
    this.resetPlayback(this.generation);
    this.generation += 1;
    if (this.userClosed) {
      this.resolveHangupAck();
      this.emitState('closed');
      return;
    }
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.userClosed || this.reconnectTimer !== null) return;
    this.reconnectAttempt += 1;
    const delay = Math.min(MAX_RECONNECT_DELAY_MS, 500 * 2 ** Math.min(this.reconnectAttempt - 1, 4));
    this.emitState('reconnecting');
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.userClosed && !this.connected) void this.openSocket(true);
    }, delay);
  }

  private clearReconnect(): void {
    if (this.reconnectTimer !== null) window.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  private handleControl(raw: string): void {
    let message: CallControlEvent;
    try {
      const parsed = JSON.parse(raw) as unknown;
      if (!parsed || typeof parsed !== 'object' || typeof (parsed as CallControlEvent).type !== 'string') {
        throw new Error('invalid call control');
      }
      message = parsed as CallControlEvent;
    } catch {
      this.emitError('control_json', 'received malformed JSON control', false);
      return;
    }
    const generation = typeof message.generation === 'number' ? message.generation : -1;
    if (message.type === 'call.ready') {
      if (message.ready !== true) {
        this.emitError('call_not_ready', 'call models did not become ready', true);
        return;
      }
      this.serverReady = true;
      this.reconnectAttempt = 0;
      this.clearReconnect();
      if (this.pendingConnect) {
        window.clearTimeout(this.pendingConnect.timeout);
        this.pendingConnect.resolve({ callId: this.callId });
        this.pendingConnect = null;
      }
      this.emitState('open');
    } else if (message.type === 'generation' && generation >= 0) {
      this.generation = generation;
    } else if (message.type === 'audio.start') {
      const sampleRate = typeof message.sample_rate === 'number' ? message.sample_rate : 0;
      if (generation < 0 || ![16_000, 22_050, 24_000, 44_100, 48_000].includes(sampleRate)) {
        this.emitError('audio_start', 'audio.start is invalid', false);
      } else {
        this.beginPlayback(generation, sampleRate);
      }
    } else if (message.type === 'audio.segment') {
      const sequence = typeof message.sequence === 'number' ? message.sequence : -1;
      const kind = message.kind;
      if (
        generation === this.playbackGeneration &&
        sequence >= 0 &&
        (kind === 'acknowledgement' || kind === 'content')
      ) {
        this.segmentMarkers.set(sequence, { kind });
      }
    } else if (message.type === 'audio.end') {
      if (generation === this.playbackGeneration) this.finishPlayback(generation);
    } else if (message.type === 'pong') {
      this.handlePong(message);
    } else if (message.type === 'sequence.gap') {
      this.emit('sequenceGap', {
        direction: 'input',
        generation,
        expected: Number(message.expected ?? -1),
        received: Number(message.received ?? -1),
      });
    } else if (message.type === 'error') {
      this.emitError(
        typeof message.code === 'string' ? message.code : 'server',
        typeof message.message === 'string' ? message.message : 'call server error',
        message.fatal === true,
      );
    } else if (message.type === 'call.ended') {
      this.resolveHangupAck();
    }
    if (isReplayableJobEvent(message.type)) {
      const eventSequence = Number(message.event_seq ?? 0);
      if (Number.isInteger(eventSequence) && eventSequence > 0) {
        if (this.sendControl({ type: 'job.ack', event_seq: eventSequence })) {
          this.highestJobEventSequence = Math.max(this.highestJobEventSequence, eventSequence);
        }
      }
    }
    this.emit('control', message);
  }

  private startPing(): void {
    this.clearPing();
    this.sendPing();
    this.pingTimer = window.setInterval(() => this.sendPing(), this.pingIntervalMs);
  }

  private clearPing(): void {
    if (this.pingTimer !== null) window.clearInterval(this.pingTimer);
    this.pingTimer = null;
  }

  private sendPing(): void {
    if (!this.socketIsOpen()) return;
    const nonce = randomId();
    this.pendingPings.set(nonce, { startedAtMs: monotonicMs() });
    if (!this.sendControl({ type: 'ping', nonce, sent_at_us: monotonicUs() })) {
      this.pendingPings.delete(nonce);
    }
    const cutoff = monotonicMs() - 30_000;
    for (const [key, sample] of this.pendingPings) {
      if (sample.startedAtMs < cutoff) this.pendingPings.delete(key);
    }
  }

  private handlePong(message: CallControlEvent): void {
    const nonce = String(message.nonce ?? '');
    const sample = this.pendingPings.get(nonce);
    this.pendingPings.delete(nonce);
    const sampleId = typeof message.sample_id === 'string' ? message.sample_id : '';
    if (!sample || !sampleId) return;
    const serverMs = Math.max(0, Number(message.server_processing_us ?? 0) / 1_000);
    const rttMs = Math.max(0, monotonicMs() - sample.startedAtMs - serverMs);
    this.rollingRttMs = this.rollingRttMs < 0 ? rttMs : this.rollingRttMs * 0.8 + rttMs * 0.2;
    const serverPath = normalizePath(message.path);
    const path = this.configuredPath === 'unknown' ? serverPath : this.configuredPath;
    const pathSource =
      this.configuredPath === 'unknown' && message.path_source === 'tailscale_probe'
        ? 'tailscale_probe'
        : this.configuredPath === 'unknown'
          ? 'unknown'
          : 'client_config';
    this.sendControl({
      type: 'rtt.report',
      rtt_ms: rttMs,
      path,
      path_source: pathSource,
      sample_id: sampleId,
    });
    this.emit('rtt', {
      rttMs,
      rollingRttMs: this.rollingRttMs,
      path,
      pathSource,
    });
  }

  private async ensureAudioContext(): Promise<AudioContext> {
    if (!this.audioContext || this.audioContext.state === 'closed') {
      const Constructor = audioContextConstructor();
      if (!Constructor) throw new Error('Web Audio is unavailable');
      this.audioContext = new Constructor({ latencyHint: 'interactive' });
    }
    if (this.audioContext.state === 'suspended') await this.audioContext.resume();
    return this.audioContext;
  }

  private liveMicStream(): MediaStream | null {
    if (this.micStream?.getAudioTracks().some((track) => track.readyState === 'live')) {
      return this.micStream;
    }
    return null;
  }

  private async ensureMicStream(): Promise<MediaStream> {
    const live = this.liveMicStream();
    if (live) return live;
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('microphone is unavailable');
    this.releaseMicrophone();
    this.micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
    return this.micStream;
  }

  private releaseMicrophone(): void {
    for (const track of this.micStream?.getTracks() ?? []) track.stop();
    this.micStream = null;
  }

  private acceptMicSamples(samples: Float32Array, generation: number): void {
    if (generation !== this.micGeneration || !this.micProcessor) return;
    const pcm = floatToPcm16(samples);
    for (const sample of pcm) this.micPcm.push(sample);
    while (this.micPcm.length >= WEB_CALL_PROTOCOL.micFrameSamples) {
      const frame = Int16Array.from(this.micPcm.splice(0, WEB_CALL_PROTOCOL.micFrameSamples));
      if (!this.sendMicFrame(frame, generation, false)) {
        this.stopCapture(false);
        this.emitError('mic_socket', 'call websocket closed during microphone capture', false);
        return;
      }
    }
  }

  private sendMicFrame(samples: Int16Array, generation: number, final: boolean): boolean {
    if (generation !== this.micGeneration || !this.socketIsOpen()) return false;
    const frame = encodeMicFrame(samples, this.micSequence, monotonicUs(), final);
    this.micSequence = (this.micSequence + 1) >>> 0;
    this.socket?.send(frame);
    return true;
  }

  private stopCapture(keepGeneration: boolean): void {
    if (this.micProcessor) {
      this.micProcessor.onaudioprocess = null;
      this.micProcessor.disconnect();
    }
    this.micSource?.disconnect();
    this.micSink?.disconnect();
    this.micProcessor = null;
    this.micSource = null;
    this.micSink = null;
    this.resampler?.reset();
    this.resampler = null;
    if (!keepGeneration) {
      this.micGeneration = -1;
      this.micPcm = [];
    }
  }

  private beginPlayback(generation: number, sampleRate: number): void {
    this.resetPlayback(generation - 1);
    this.playbackGeneration = generation;
    this.playbackSampleRate = sampleRate;
    this.expectedOutputSequence = 0;
    this.nextPlaybackAt = 0;
    this.playbackHasStarted = false;
    this.underrunCount = 0;
    this.segmentMarkers.clear();
    this.firstPlaybackPolling.delete(generation);
    this.contentPlaybackPolling.delete(generation);
    this.emitState('speaking');
  }

  private handleAudioFrame(raw: ArrayBuffer): void {
    const generation = this.playbackGeneration;
    if (generation < 0) {
      this.emitError('audio_generation', 'audio frame arrived before audio.start', false);
      return;
    }
    let frame;
    try {
      frame = decodeTtsFrame(raw);
    } catch (reason) {
      this.emitError(
        'audio_protocol',
        reason instanceof Error ? reason.message : 'invalid audio frame',
        false,
      );
      return;
    }
    if (frame.sampleRate !== this.playbackSampleRate) {
      this.emitError('audio_rate', 'audio frame sample rate changed', false);
      return;
    }
    if (frame.sequence !== this.expectedOutputSequence) {
      const expected = this.expectedOutputSequence;
      const distance = (frame.sequence - expected) >>> 0;
      this.sendControl({
        type: 'sequence.gap',
        direction: 'output',
        generation,
        expected,
        received: frame.sequence,
      });
      this.emit('sequenceGap', {
        direction: 'output',
        generation,
        expected,
        received: frame.sequence,
      });
      if (distance >= 0x8000_0000) return;
    }
    this.expectedOutputSequence = (frame.sequence + 1) >>> 0;
    this.firstOutputReceivedMs.set(
      generation,
      this.firstOutputReceivedMs.get(generation) ?? monotonicMs(),
    );
    void this.schedulePlaybackFrame(generation, frame.sequence, frame.sampleRate, frame.samples);
  }

  private async schedulePlaybackFrame(
    generation: number,
    sequence: number,
    sampleRate: number,
    samples: Float32Array,
  ): Promise<void> {
    const context = await this.ensureAudioContext();
    if (generation !== this.playbackGeneration) return;
    const now = context.currentTime;
    const marker = this.segmentMarkers.get(sequence);
    if (
      shouldReportPlaybackUnderrun(
        this.playbackHasStarted,
        this.nextPlaybackAt,
        now,
        marker !== undefined,
      )
    ) {
      this.underrunCount += 1;
      this.sendControl({
        type: 'playback.underrun',
        generation,
        count: this.underrunCount,
        timestamp_us: monotonicUs(),
      });
      this.emit('playback', {
        state: 'underrun',
        generation,
        count: this.underrunCount,
        delta: 1,
      });
    }
    const startAt = Math.max(now + PLAYBACK_START_LEAD_SECONDS, this.nextPlaybackAt || 0);
    if (startAt - now > MAX_PLAYBACK_AHEAD_SECONDS) {
      this.emitError('playback_queue_full', 'call audio exceeded the playback buffer', true);
      return;
    }
    const buffer = context.createBuffer(1, samples.length, sampleRate);
    buffer.getChannelData(0).set(samples);
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    this.playbackSources.add(source);
    this.nextPlaybackAt = startAt + samples.length / sampleRate;
    this.segmentMarkers.delete(sequence);
    source.onended = () => {
      source.disconnect();
      this.playbackSources.delete(source);
      this.emit('queueDepth', { depth: this.playbackSources.size, generation });
    };
    source.start(startAt);
    this.emit('queueDepth', { depth: this.playbackSources.size, generation });
    const pollFirst =
      !this.firstPlaybackAcknowledged.has(generation) &&
      !this.firstPlaybackPolling.has(generation);
    const pollContent =
      marker?.kind === 'content' &&
      !this.contentPlaybackAcknowledged.has(generation) &&
      !this.contentPlaybackPolling.has(generation);
    if (!pollFirst && !pollContent) return;
    if (pollFirst) this.firstPlaybackPolling.add(generation);
    if (pollContent) this.contentPlaybackPolling.add(generation);
    this.pollPlaybackStart(startAt, () => {
      if (generation !== this.playbackGeneration) return;
      this.playbackHasStarted = true;
      const startedAtMs = monotonicMs();
      if (pollFirst) this.firstPlaybackPolling.delete(generation);
      if (pollContent) this.contentPlaybackPolling.delete(generation);
      if (!this.firstPlaybackAcknowledged.has(generation)) {
        this.firstPlaybackAcknowledged.add(generation);
        const releasedAt = this.pttReleaseMs.get(generation);
        const receivedAt = this.firstOutputReceivedMs.get(generation);
        const payload: Record<string, unknown> = {
          type: 'playback.started',
          generation,
          sequence,
          timestamp_us: Math.floor(startedAtMs * 1_000),
          measurement_point: 'playback_head_advanced',
        };
        if (releasedAt !== undefined) payload.eou_to_playback_ms = startedAtMs - releasedAt;
        if (receivedAt !== undefined) payload.first_output_to_playback_ms = startedAtMs - receivedAt;
        this.sendControl(payload);
        this.emit('playback', { state: 'started', generation, sequence, sampleRate });
      }
      if (marker?.kind === 'content' && !this.contentPlaybackAcknowledged.has(generation)) {
        this.contentPlaybackAcknowledged.add(generation);
        const releasedAt = this.pttReleaseMs.get(generation);
        const callHello = !this.helloReported;
        const payload: Record<string, unknown> = {
          type: 'playback.segment_started',
          generation,
          sequence,
          kind: 'content',
          timestamp_us: Math.floor(startedAtMs * 1_000),
          measurement_point: 'playback_head_advanced',
          call_hello: callHello,
          cold_start: callHello && this.coldStart,
          app_uptime_ms: Math.max(0, Math.floor(startedAtMs - APP_STARTED_AT_MS)),
        };
        if (releasedAt !== undefined) payload.eou_to_playback_ms = startedAtMs - releasedAt;
        if (this.sendControl(payload) && callHello) this.helloReported = true;
        this.emit('playback', {
          state: 'content_started',
          generation,
          sequence,
          appUptimeMs: Number(payload.app_uptime_ms),
        });
      }
    });
  }

  private pollPlaybackStart(startAt: number, callback: () => void): void {
    const startedPollingAt = monotonicMs();
    const initialContextTime = this.audioContext?.currentTime ?? startAt;
    const timeoutMs = playbackStartPollTimeoutMs(startAt, initialContextTime);
    const poll = () => {
      const context = this.audioContext;
      if (!context || context.state === 'closed') return;
      if (context.currentTime >= startAt + 1 / Math.max(1, this.playbackSampleRate)) {
        callback();
        return;
      }
      if (monotonicMs() - startedPollingAt > timeoutMs) {
        this.emitError('playback_start_timeout', 'browser audio playback did not advance', false);
        return;
      }
      const timer = window.setTimeout(() => {
        this.playbackTimers.delete(timer);
        poll();
      }, 5);
      this.playbackTimers.add(timer);
    };
    poll();
  }

  private finishPlayback(generation: number): void {
    const context = this.audioContext;
    if (!context) return;
    const delayMs = Math.max(0, (this.nextPlaybackAt - context.currentTime) * 1_000 + 10);
    const timer = window.setTimeout(() => {
      this.playbackTimers.delete(timer);
      if (generation !== this.playbackGeneration) return;
      this.emit('playback', { state: 'ended', generation });
      this.emitState('open');
      this.pttReleaseMs.delete(generation);
      this.firstOutputReceivedMs.delete(generation);
      this.firstPlaybackAcknowledged.delete(generation);
      this.contentPlaybackAcknowledged.delete(generation);
    }, delayMs);
    this.playbackTimers.add(timer);
  }

  private resetPlayback(throughGeneration: number): void {
    if (this.playbackGeneration > throughGeneration) return;
    for (const source of this.playbackSources) {
      try {
        source.stop();
      } catch {
        // The source may already have ended.
      }
      source.disconnect();
    }
    for (const timer of this.playbackTimers) window.clearTimeout(timer);
    this.playbackSources.clear();
    this.playbackTimers.clear();
    this.segmentMarkers.clear();
    this.playbackGeneration = -1;
    this.playbackSampleRate = 0;
    this.expectedOutputSequence = 0;
    this.nextPlaybackAt = 0;
    this.playbackHasStarted = false;
    this.firstPlaybackPolling.clear();
    this.contentPlaybackPolling.clear();
  }
}

export const webCallTransport = new WebCallTransport();
