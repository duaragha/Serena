import assert from 'node:assert/strict';
import test from 'node:test';

import {
  decodeTtsFrame,
  encodeMicFrame,
  playbackStartPollTimeoutMs,
  shouldReportPlaybackUnderrun,
  StreamingResampler,
} from '../src/call/web_protocol.ts';

test('browser mic frames match the SRCA network header and little-endian PCM contract', () => {
  const samples = new Int16Array(3_200);
  samples[0] = -32_768;
  samples[1] = 32_767;
  const frame = encodeMicFrame(samples, 7, 123_456, true);
  const view = new DataView(frame);

  assert.equal(frame.byteLength, 24 + 6_400);
  assert.equal(view.getUint32(0, false), 0x53524341);
  assert.equal(view.getUint8(4), 1);
  assert.equal(view.getUint8(5), 1);
  assert.equal(view.getUint16(6, false), 1);
  assert.equal(view.getUint32(8, false), 7);
  assert.equal(view.getUint32(12, false), 16_000);
  assert.equal(view.getUint32(20, false), 123_456);
  assert.equal(view.getInt16(24, true), -32_768);
  assert.equal(view.getInt16(26, true), 32_767);
});

test('browser TTS decoder accepts the server wire format', () => {
  const frame = new ArrayBuffer(24 + 8);
  const view = new DataView(frame);
  view.setUint32(0, 0x53524341, false);
  view.setUint8(4, 1);
  view.setUint8(5, 2);
  view.setUint32(8, 3, false);
  view.setUint32(12, 24_000, false);
  view.setUint32(20, 44, false);
  view.setInt16(24, -32_768, true);
  view.setInt16(26, -16_384, true);
  view.setInt16(28, 0, true);
  view.setInt16(30, 32_767, true);

  const decoded = decodeTtsFrame(frame);
  assert.equal(decoded.sequence, 3);
  assert.equal(decoded.sampleRate, 24_000);
  assert.equal(decoded.timestampUs, 44);
  assert.deepEqual(
    Array.from(decoded.samples).map((sample) => Number(sample.toFixed(3))),
    [-1, -0.5, 0, 1],
  );
});

test('streaming resampling stays continuous across browser callback boundaries', () => {
  const input = Float32Array.from(
    { length: 4_800 },
    (_, index) => Math.sin((index / 48_000) * Math.PI * 2 * 440),
  );
  const whole = new StreamingResampler(48_000).push(input);
  const chunkedResampler = new StreamingResampler(48_000);
  const first = chunkedResampler.push(input.slice(0, 2_047));
  const second = chunkedResampler.push(input.slice(2_047));
  const chunked = Float32Array.from([...first, ...second]);

  assert.equal(whole.length, 1_600);
  assert.equal(chunked.length, whole.length);
  for (let index = 0; index < whole.length; index += 1) {
    assert.ok(Math.abs((whole[index] ?? 0) - (chunked[index] ?? 0)) < 1e-6);
  }
});

test('playback gaps are underruns only inside one semantic audio segment', () => {
  assert.equal(shouldReportPlaybackUnderrun(true, 1, 1.020, false), true);
  assert.equal(shouldReportPlaybackUnderrun(true, 1, 1.020, true), false);
  assert.equal(shouldReportPlaybackUnderrun(false, 1, 1.020, false), false);
  assert.equal(shouldReportPlaybackUnderrun(true, 1, 1.010, false), false);
});

test('playback polling allows queued audio to reach its scheduled start', () => {
  assert.equal(playbackStartPollTimeoutMs(2, 1), 3_000);
  assert.equal(playbackStartPollTimeoutMs(1, 2), 2_000);
});
