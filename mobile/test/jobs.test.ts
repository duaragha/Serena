import assert from 'node:assert/strict';
import test from 'node:test';

import {
  emptyCallJobFeed,
  ingestCallJobEvent,
} from '../src/call/jobs.ts';

test('ordered replay reaches one ready card and duplicates stay deduped', () => {
  const endpoint = 'ws://100.116.233.56:8766/ws/call';
  const events = [
    { type: 'job.accepted', event_seq: 5, job_id: 'job-1', state: 'accepted' },
    { type: 'job.progress', event_seq: 6, job_id: 'job-1', state: 'running' },
    {
      type: 'artifact.ready',
      event_seq: 7,
      job_id: 'job-1',
      state: 'artifact_ready',
      name: 'draft.md',
      url: '/artifacts/abc.def',
    },
  ];
  let feed = emptyCallJobFeed();
  for (const event of events) feed = ingestCallJobEvent(feed, event, endpoint);
  for (const event of events) feed = ingestCallJobEvent(feed, event, endpoint);

  assert.equal(feed.jobs.length, 1);
  assert.deepEqual(feed.jobs[0], {
    jobId: 'job-1',
    eventSeq: 7,
    state: 'artifact_ready',
    name: 'draft.md',
    url: 'http://100.116.233.56:8766/artifacts/abc.def',
  });
  assert.deepEqual([...feed.seen], [5, 6, 7]);
  assert.equal(feed.cursor, 7);
});

test('a delayed older event cannot regress a ready card', () => {
  const endpoint = 'ws://100.116.233.56:8766/ws/call';
  let feed = emptyCallJobFeed();
  feed = ingestCallJobEvent(
    feed,
    { type: 'job.accepted', event_seq: 5, job_id: 'job-1' },
    endpoint,
  );
  feed = ingestCallJobEvent(
    feed,
    {
      type: 'artifact.ready',
      event_seq: 7,
      job_id: 'job-1',
      url: '/artifacts/abc.def',
    },
    endpoint,
  );
  feed = ingestCallJobEvent(
    feed,
    { type: 'job.progress', event_seq: 6, job_id: 'job-1' },
    endpoint,
  );

  assert.equal(feed.jobs[0]?.state, 'artifact_ready');
  assert.equal(feed.jobs[0]?.eventSeq, 7);
  assert.equal(feed.cursor, 7);
});

test('a capability cannot switch away from the active call origin', () => {
  const feed = ingestCallJobEvent(
    emptyCallJobFeed(),
    {
      type: 'artifact.ready',
      event_seq: 1,
      job_id: 'job-1',
      url: 'http://example.com/artifacts/abc.def',
    },
    'wss://100.116.233.56:8766/ws/call',
  );
  assert.equal(feed.jobs[0]?.url, '');
});
