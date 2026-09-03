import { useCallback, useEffect, useRef, useState } from 'react';
import type { PluginListenerHandle } from '@capacitor/core';
import {
  callTransport,
  type CallConnectionState,
  type CallControlEvent,
  type CallErrorEvent,
  type CallPlaybackEvent,
  type CallRttEvent,
  type CallStateEvent,
} from '../call';
import {
  emptyCallJobFeed,
  ingestCallJobEvent,
  type CallArtifactJob,
} from '../call/jobs';
import { useSerena } from '../store';

interface TranscriptLine {
  id: number;
  role: 'raghav' | 'serena';
  text: string;
}

interface ArtifactPreview {
  jobId: string;
  name: string;
  content: string;
}

interface CallScreenProps {
  autoStartRequest: number;
  coldStartRequest: boolean;
  onExit: () => void;
}

const STATUS: Record<CallConnectionState | 'idle', string> = {
  idle: 'ready when you are',
  connecting: 'calling...',
  open: "i'm here",
  listening: 'listening',
  thinking: 'thinking',
  speaking: 'speaking',
  reconnecting: 'reconnecting...',
  closed: 'call ended',
};

function eventText(event: CallControlEvent, key: string): string {
  const value = event[key];
  return typeof value === 'string' ? value.trim() : '';
}

function elapsedLabel(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

export function CallScreen({
  autoStartRequest,
  coldStartRequest,
  onExit,
}: CallScreenProps) {
  const { settings } = useSerena();
  const [phase, setPhase] = useState<CallConnectionState | 'idle'>('idle');
  const [listenersReady, setListenersReady] = useState(false);
  const [error, setError] = useState('');
  const [callId, setCallId] = useState('');
  const [lines, setLines] = useState<TranscriptLine[]>([]);
  const [artifactJobs, setArtifactJobs] = useState<CallArtifactJob[]>([]);
  const [artifactPreview, setArtifactPreview] = useState<ArtifactPreview | null>(null);
  const [openingArtifact, setOpeningArtifact] = useState('');
  const [partial, setPartial] = useState('');
  const [rttMs, setRttMs] = useState<number | null>(null);
  const [helloMs, setHelloMs] = useState<number | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [connected, setConnected] = useState(false);
  const [listening, setListening] = useState(false);
  const connectedRef = useRef(false);
  const callActiveRef = useRef(false);
  const listeningRef = useRef(false);
  const startingRef = useRef(false);
  const mountedRef = useRef(true);
  const attemptRef = useRef(0);
  const generationRef = useRef(0);
  const coldMeasurementRef = useRef(false);
  const coldAttemptClaimedRef = useRef(false);
  const partialRef = useRef('');
  const lineIdRef = useRef(0);
  const callStartedAtRef = useRef(0);
  const helloBaselineRef = useRef(0);
  const helloMeasuredRef = useRef(false);
  const autoStartedRef = useRef(0);
  const callEndpointRef = useRef('');
  const jobFeedRef = useRef(emptyCallJobFeed());

  const appendLine = useCallback(
    (role: TranscriptLine['role'], text: string) => {
      const clean = text.trim();
      if (!clean) return;
      lineIdRef.current += 1;
      const line = { id: lineIdRef.current, role, text: clean };
      setLines((current) => [...current, line].slice(-8));
    },
    [],
  );

  useEffect(() => {
    let disposed = false;
    mountedRef.current = true;
    const handles: PluginListenerHandle[] = [];
    const keep = async (pending: Promise<PluginListenerHandle>) => {
      const handle = await pending;
      if (disposed) {
        await handle.remove();
      } else {
        handles.push(handle);
      }
    };

    const onState = (event: CallStateEvent) => {
      if (disposed) return;
      const connected = ['open', 'listening', 'thinking', 'speaking'].includes(
        event.state,
      );
      callActiveRef.current = event.state !== 'closed';
      generationRef.current = event.generation;
      connectedRef.current = connected;
      listeningRef.current = event.state === 'listening';
      setConnected(connected);
      setListening(event.state === 'listening');
      setPhase(event.state);
      if (event.callId) setCallId(event.callId);
    };
    const onControl = (event: CallControlEvent) => {
      if (disposed) return;
      if (['job.accepted', 'job.progress', 'artifact.ready', 'job.failed'].includes(event.type)) {
        const next = ingestCallJobEvent(
          jobFeedRef.current,
          event,
          callEndpointRef.current,
        );
        if (next === jobFeedRef.current) return;
        jobFeedRef.current = next;
        setArtifactJobs(next.jobs);
        return;
      }
      const generation =
        typeof event.generation === 'number' && Number.isFinite(event.generation)
          ? event.generation
          : null;
      if (event.type === 'generation' && generation !== null) {
        generationRef.current = generation;
        partialRef.current = '';
        setPartial('');
        return;
      }
      if (
        generation !== null &&
        ['stt.result', 'brain.delta', 'brain.done', 'cancelled'].includes(event.type) &&
        generation !== generationRef.current
      ) {
        return;
      }
      if (event.type === 'stt.result') {
        appendLine('raghav', eventText(event, 'text'));
        setPhase('thinking');
      } else if (event.type === 'brain.delta') {
        const delta = typeof event.delta === 'string' ? event.delta : '';
        partialRef.current += delta;
        setPartial(partialRef.current.trimStart());
      } else if (event.type === 'brain.done') {
        appendLine('serena', eventText(event, 'text') || partialRef.current);
        partialRef.current = '';
        setPartial('');
      } else if (event.type === 'cancelled') {
        partialRef.current = '';
        setPartial('');
      }
    };
    const onPlayback = (event: CallPlaybackEvent) => {
      if (disposed || event.generation !== generationRef.current) return;
      if (
        event.state === 'content_started' &&
        !helloMeasuredRef.current &&
        helloBaselineRef.current > 0
      ) {
        helloMeasuredRef.current = true;
        const coldValue = event.appUptimeMs;
        setHelloMs(
          coldMeasurementRef.current && typeof coldValue === 'number'
            ? coldValue
            : performance.now() - helloBaselineRef.current,
        );
      }
    };
    const onRtt = (event: CallRttEvent) => {
      if (disposed) return;
      setRttMs(event.rollingRttMs >= 0 ? event.rollingRttMs : event.rttMs);
    };
    const onError = (event: CallErrorEvent) => {
      if (disposed) return;
      setError(event.message || 'call transport failed');
      if (event.fatal) {
        connectedRef.current = false;
        callActiveRef.current = false;
        listeningRef.current = false;
        setConnected(false);
        setListening(false);
        setPhase('closed');
      }
    };

    const setup = async () => {
      if (!callTransport.isAvailable()) {
        if (!disposed) setListenersReady(true);
        return;
      }
      try {
        await Promise.all([
          keep(callTransport.onState(onState)),
          keep(callTransport.onControl(onControl)),
          keep(callTransport.onPlayback(onPlayback)),
          keep(callTransport.onRtt(onRtt)),
          keep(callTransport.onError(onError)),
        ]);
        if (!disposed) setListenersReady(true);
      } catch {
        if (!disposed) setError('could not attach the call controls');
      }
    };
    void setup();

    return () => {
      disposed = true;
      mountedRef.current = false;
      attemptRef.current += 1;
      const shouldHangup = callActiveRef.current || startingRef.current;
      startingRef.current = false;
      for (const handle of handles) void handle.remove();
      if (shouldHangup && callTransport.isAvailable()) {
        void callTransport.hangup().catch(() => undefined);
      }
    };
  }, [appendLine]);

  const startCall = useCallback(
    async (coldStart: boolean) => {
      if (startingRef.current || connectedRef.current) return;
      if (!callTransport.isAvailable()) {
        setError('calls need the native android app or a secure browser');
        return;
      }
      const token = (settings.callToken || settings.token).trim();
      if (!token) {
        setError('save the call token in settings first');
        return;
      }
      startingRef.current = true;
      const measureColdStart = coldStart && !coldAttemptClaimedRef.current;
      if (coldStart) coldAttemptClaimedRef.current = true;
      callActiveRef.current = true;
      const attempt = attemptRef.current + 1;
      attemptRef.current = attempt;
      const isCurrentAttempt = () =>
        mountedRef.current && attemptRef.current === attempt;
      setError('');
      setLines([]);
      setArtifactJobs([]);
      setArtifactPreview(null);
      setOpeningArtifact('');
      jobFeedRef.current = emptyCallJobFeed();
      setPartial('');
      partialRef.current = '';
      setHelloMs(null);
      helloMeasuredRef.current = false;
      setElapsedSeconds(0);
      setPhase('connecting');
      callStartedAtRef.current = performance.now();
      helloBaselineRef.current = callStartedAtRef.current;
      coldMeasurementRef.current = measureColdStart;
      try {
        let permission = await callTransport.checkPermissions();
        if (!isCurrentAttempt()) return;
        if (permission.microphone !== 'granted') {
          permission = await callTransport.requestPermissions();
          if (!isCurrentAttempt()) return;
        }
        if (permission.microphone !== 'granted') {
          throw new Error('microphone permission is required');
        }
        const endpoint = await callTransport.endpoint(settings.serverUrl);
        if (!isCurrentAttempt()) return;
        callEndpointRef.current = endpoint.url;
        const result = await callTransport.connect({
          url: endpoint.url,
          token,
          coldStart: measureColdStart,
        });
        if (!isCurrentAttempt()) return;
        connectedRef.current = true;
        setConnected(true);
        setCallId(result.callId);
        setPhase((current) => (current === 'connecting' ? 'open' : current));
      } catch (reason) {
        if (!isCurrentAttempt()) return;
        connectedRef.current = false;
        callActiveRef.current = false;
        setConnected(false);
        setPhase('closed');
        setError(reason instanceof Error ? reason.message : 'call failed');
      } finally {
        if (attemptRef.current === attempt) startingRef.current = false;
      }
    },
    [settings.callToken, settings.serverUrl, settings.token],
  );

  const openArtifact = useCallback(async (job: CallArtifactJob) => {
    if (!job.url || !job.eventSeq || openingArtifact) return;
    setOpeningArtifact(job.jobId);
    setError('');
    try {
      const { content, receipt } = await callTransport.fetchArtifact(job.url);
      if (!content || content.length > 512 * 1024 || !receipt) {
        throw new Error('draft response was invalid');
      }
      setArtifactPreview({
        jobId: job.jobId,
        name: job.name || 'draft.md',
        content,
      });
      await new Promise<void>((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
      });
      await callTransport.artifactOpened({
        eventSeq: job.eventSeq,
        jobId: job.jobId,
        receipt,
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'draft link could not be opened');
    } finally {
      setOpeningArtifact('');
    }
  }, [openingArtifact]);

  useEffect(() => {
    if (
      listenersReady &&
      autoStartRequest > 0 &&
      autoStartedRef.current !== autoStartRequest
    ) {
      autoStartedRef.current = autoStartRequest;
      void startCall(coldStartRequest);
    }
  }, [autoStartRequest, coldStartRequest, listenersReady, startCall]);

  useEffect(() => {
    if (!connectedRef.current || callStartedAtRef.current <= 0) return;
    const update = () => {
      setElapsedSeconds(
        Math.max(0, Math.floor((performance.now() - callStartedAtRef.current) / 1000)),
      );
    };
    update();
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, [phase]);

  const togglePushToTalk = async () => {
    if (!connectedRef.current) return;
    setError('');
    try {
      if (listeningRef.current) {
        listeningRef.current = false;
        setListening(false);
        setPhase('thinking');
        const result = await callTransport.pttEnd();
        generationRef.current = result.generation;
      } else {
        partialRef.current = '';
        setPartial('');
        const result = await callTransport.pttBegin();
        generationRef.current = result.generation;
        listeningRef.current = true;
        setListening(true);
        setPhase('listening');
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'microphone failed');
    }
  };

  const endCall = async () => {
    const shouldHangup = callActiveRef.current || startingRef.current;
    attemptRef.current += 1;
    startingRef.current = false;
    if (callTransport.isAvailable() && shouldHangup) {
      try {
        await callTransport.hangup();
      } catch {
        setError('the call closed without a clean hangup');
      }
    }
    connectedRef.current = false;
    callActiveRef.current = false;
    listeningRef.current = false;
    setConnected(false);
    setListening(false);
    setPhase('closed');
  };

  const leave = async () => {
    await endCall();
    onExit();
  };

  const callIsLive = connected && phase !== 'closed';
  const callIsActive =
    callIsLive || phase === 'connecting' || phase === 'reconnecting';
  const actionLabel = listening ? 'done talking' : 'talk to serena';

  return (
    <section
      className={`call-screen call-phase-${phase}`}
      data-testid="serena-call-screen"
      aria-label="Serena call"
    >
      <div className="call-ambient" aria-hidden="true" />
      <header className="call-header">
        <button className="call-close" onClick={() => void leave()} aria-label="close call">
          ‹
        </button>
        <div className="call-metrics" aria-label="call metrics">
          {callIsLive && <span>{elapsedLabel(elapsedSeconds)}</span>}
          {rttMs !== null && <span>{Math.round(rttMs)} ms</span>}
          {helloMs !== null && <span>hello {(helloMs / 1000).toFixed(1)}s</span>}
        </div>
      </header>

      <div className="call-presence">
        <div className="call-rings" aria-hidden="true">
          <span className="call-ring call-ring-one" />
          <span className="call-ring call-ring-two" />
          <span className="call-core" />
        </div>
        <h1>serena</h1>
        <p className="call-status" aria-live="polite">
          {error || STATUS[phase]}
        </p>
      </div>

      <div className="call-transcript" aria-live="polite">
        {lines.length === 0 && !partial ? (
          <p className="call-transcript-empty">
            {callIsLive ? 'stay there. i have you.' : 'one tap and i pick up.'}
          </p>
        ) : (
          <>
            {lines.map((line) => (
              <p key={line.id} className={`call-line call-line-${line.role}`}>
                <span>{line.role}</span>
                {line.text}
              </p>
            ))}
            {partial && (
              <p className="call-line call-line-serena call-line-partial">
                <span>serena</span>
                {partial}
              </p>
            )}
          </>
        )}
        {artifactJobs.length > 0 && (
          <div className="call-artifacts" aria-label="call work">
            {artifactJobs.map((job) => (
              <div
                key={job.jobId}
                className={`call-artifact call-artifact-${job.state}`}
              >
                <span className="call-artifact-dot" aria-hidden="true" />
                <div>
                  <strong>
                    {job.state === 'artifact_ready'
                      ? job.name
                      : job.state === 'failed'
                        ? 'draft failed'
                        : 'drafting'}
                  </strong>
                  {job.state === 'artifact_ready' && job.url ? (
                    <button
                      type="button"
                      className="call-artifact-open"
                      disabled={openingArtifact === job.jobId}
                      onClick={() => void openArtifact(job)}
                    >
                      {openingArtifact === job.jobId ? 'opening...' : 'open draft'}
                    </button>
                  ) : (
                    <small>{job.error || 'working on the pc'}</small>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {artifactPreview && (
        <section className="call-artifact-preview" role="dialog" aria-modal="true">
          <header>
            <strong>{artifactPreview.name}</strong>
            <button
              type="button"
              aria-label="close draft"
              onClick={() => setArtifactPreview(null)}
            >
              ×
            </button>
          </header>
          <pre>{artifactPreview.content}</pre>
        </section>
      )}

      <div className="call-controls">
        {!callIsActive ? (
          <button
            className="call-start"
            onClick={() => void startCall(coldStartRequest)}
            data-testid="start-serena-call"
          >
            <span className="call-control-icon">●</span>
            call serena
          </button>
        ) : (
          <>
            <button
              className={`call-talk ${listening ? 'call-talk-live' : ''}`}
              onClick={() => void togglePushToTalk()}
              disabled={phase === 'connecting' || phase === 'reconnecting'}
              aria-label={actionLabel}
            >
              <span className="call-control-icon">{listening ? '■' : '●'}</span>
              {actionLabel}
            </button>
            <button
              className="call-hangup"
              onClick={() => void endCall()}
              aria-label="hang up"
            >
              <span className="call-control-icon">×</span>
              hang up
            </button>
          </>
        )}
      </div>

      {callId && <span className="call-id">{callId.slice(0, 8)}</span>}
    </section>
  );
}
