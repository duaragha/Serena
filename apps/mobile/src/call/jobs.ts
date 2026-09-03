import type { CallControlEvent } from './index.ts';

export interface CallArtifactJob {
  jobId: string;
  eventSeq?: number;
  state: 'accepted' | 'running' | 'artifact_ready' | 'failed';
  name?: string;
  url?: string;
  error?: string;
}

export interface CallJobFeed {
  jobs: CallArtifactJob[];
  seen: ReadonlySet<number>;
  cursor: number;
}

export function emptyCallJobFeed(): CallJobFeed {
  return { jobs: [], seen: new Set<number>(), cursor: 0 };
}

export function ingestCallJobEvent(
  current: CallJobFeed,
  event: CallControlEvent,
  socketUrl: string,
): CallJobFeed {
  if (!['job.accepted', 'job.progress', 'artifact.ready', 'job.failed'].includes(event.type)) {
    return current;
  }
  const eventSeq = event.event_seq;
  const jobId = eventText(event, 'job_id');
  if (
    typeof eventSeq !== 'number' ||
    !Number.isSafeInteger(eventSeq) ||
    eventSeq < 1 ||
    !jobId ||
    current.seen.has(eventSeq) ||
    eventSeq <= current.cursor
  ) {
    return current;
  }

  const previous = current.jobs.find((job) => job.jobId === jobId);
  let next: CallArtifactJob;
  if (event.type === 'artifact.ready') {
    next = {
      jobId,
      eventSeq,
      state: 'artifact_ready',
      name: eventText(event, 'name') || 'draft.md',
      url: artifactUrl(eventText(event, 'url'), socketUrl),
    };
  } else if (event.type === 'job.failed') {
    next = {
      jobId,
      eventSeq,
      state: 'failed',
      error: eventText(event, 'error') || 'draft failed',
    };
  } else {
    next = {
      ...previous,
      jobId,
      eventSeq,
      state: event.type === 'job.progress' ? 'running' : 'accepted',
    };
  }
  return {
    jobs: [...current.jobs.filter((job) => job.jobId !== jobId), next].slice(-4),
    seen: new Set(current.seen).add(eventSeq),
    cursor: eventSeq,
  };
}

function eventText(event: CallControlEvent, key: string): string {
  const value = event[key];
  return typeof value === 'string' ? value.trim() : '';
}

function artifactUrl(raw: string, socketUrl: string): string {
  if (!raw.startsWith('/artifacts/') || !socketUrl) return '';
  try {
    const origin = new URL(socketUrl);
    if (origin.protocol === 'wss:') origin.protocol = 'https:';
    else if (origin.protocol === 'ws:') origin.protocol = 'http:';
    origin.pathname = '/';
    origin.search = '';
    origin.hash = '';
    const resolved = new URL(raw, origin);
    return resolved.origin === origin.origin ? resolved.toString() : '';
  } catch {
    return '';
  }
}
