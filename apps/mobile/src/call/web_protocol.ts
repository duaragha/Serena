const MAGIC = 0x53524341;
const VERSION = 1;
const HEADER_BYTES = 24;
const KIND_MIC_PCM16 = 1;
const KIND_TTS_PCM16 = 2;
const FLAG_FINAL = 1;
const MIC_SAMPLE_RATE = 16_000;
const MIC_FRAME_SAMPLES = 3_200;
const TTS_SAMPLE_RATES = new Set([16_000, 22_050, 24_000, 44_100, 48_000]);

export interface DecodedAudioFrame {
  flags: number;
  sequence: number;
  sampleRate: number;
  timestampUs: number;
  samples: Float32Array;
}

function writeUint64(view: DataView, offset: number, value: number): void {
  const safe = Math.max(0, Math.floor(value));
  view.setUint32(offset, Math.floor(safe / 0x1_0000_0000), false);
  view.setUint32(offset + 4, safe >>> 0, false);
}

function readUint64(view: DataView, offset: number): number {
  return view.getUint32(offset, false) * 0x1_0000_0000 + view.getUint32(offset + 4, false);
}

export function encodeMicFrame(
  samples: Int16Array,
  sequence: number,
  timestampUs: number,
  final = false,
): ArrayBuffer {
  if (samples.length !== MIC_FRAME_SAMPLES) {
    throw new Error('mic frames must contain exactly 3200 samples');
  }
  if (!Number.isInteger(sequence) || sequence < 0 || sequence > 0xffff_ffff) {
    throw new Error('sequence is outside uint32 range');
  }
  const frame = new ArrayBuffer(HEADER_BYTES + samples.byteLength);
  const view = new DataView(frame);
  view.setUint32(0, MAGIC, false);
  view.setUint8(4, VERSION);
  view.setUint8(5, KIND_MIC_PCM16);
  view.setUint16(6, final ? FLAG_FINAL : 0, false);
  view.setUint32(8, sequence, false);
  view.setUint32(12, MIC_SAMPLE_RATE, false);
  writeUint64(view, 16, timestampUs);
  for (let index = 0; index < samples.length; index += 1) {
    view.setInt16(HEADER_BYTES + index * 2, samples[index] ?? 0, true);
  }
  return frame;
}

export function decodeTtsFrame(frame: ArrayBuffer): DecodedAudioFrame {
  if (frame.byteLength <= HEADER_BYTES || (frame.byteLength - HEADER_BYTES) % 2 !== 0) {
    throw new Error('binary audio frame has an invalid size');
  }
  const view = new DataView(frame);
  if (view.getUint32(0, false) !== MAGIC) throw new Error('binary audio frame has the wrong magic');
  if (view.getUint8(4) !== VERSION) throw new Error('unsupported binary audio version');
  if (view.getUint8(5) !== KIND_TTS_PCM16) throw new Error('server frame is not TTS PCM16');
  const flags = view.getUint16(6, false);
  if ((flags & ~FLAG_FINAL) !== 0) throw new Error('binary audio frame has unknown flags');
  const sequence = view.getUint32(8, false);
  const sampleRate = view.getUint32(12, false);
  if (!TTS_SAMPLE_RATES.has(sampleRate)) throw new Error('unsupported TTS sample rate');
  const sampleCount = (frame.byteLength - HEADER_BYTES) / 2;
  if (sampleCount > (sampleRate * 50) / 1_000) {
    throw new Error('TTS frame exceeds the 50 ms duration limit');
  }
  const samples = new Float32Array(sampleCount);
  for (let index = 0; index < sampleCount; index += 1) {
    const pcm = view.getInt16(HEADER_BYTES + index * 2, true);
    samples[index] = pcm < 0 ? pcm / 0x8000 : pcm / 0x7fff;
  }
  return {
    flags,
    sequence,
    sampleRate,
    timestampUs: readUint64(view, 16),
    samples,
  };
}

export function floatToPcm16(samples: Float32Array): Int16Array {
  const pcm = new Int16Array(samples.length);
  for (let index = 0; index < samples.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, samples[index] ?? 0));
    pcm[index] = sample < 0 ? Math.round(sample * 0x8000) : Math.round(sample * 0x7fff);
  }
  return pcm;
}

export class StreamingResampler {
  private readonly step: number;
  private pending = new Float32Array(0);
  private position = 0;

  constructor(inputRate: number, outputRate = MIC_SAMPLE_RATE) {
    if (!Number.isFinite(inputRate) || inputRate <= 0 || outputRate <= 0) {
      throw new Error('audio sample rates must be positive');
    }
    this.step = inputRate / outputRate;
  }

  push(input: Float32Array): Float32Array {
    if (input.length === 0) return new Float32Array(0);
    const joined = new Float32Array(this.pending.length + input.length);
    joined.set(this.pending);
    joined.set(input, this.pending.length);
    const output: number[] = [];
    while (this.position + 1 < joined.length) {
      const left = Math.floor(this.position);
      const fraction = this.position - left;
      const a = joined[left] ?? 0;
      const b = joined[left + 1] ?? a;
      output.push(a + (b - a) * fraction);
      this.position += this.step;
    }
    const consumed = Math.floor(this.position);
    this.pending = joined.slice(consumed);
    this.position -= consumed;
    return Float32Array.from(output);
  }

  reset(): void {
    this.pending = new Float32Array(0);
    this.position = 0;
  }
}

export const WEB_CALL_PROTOCOL = {
  headerBytes: HEADER_BYTES,
  micSampleRate: MIC_SAMPLE_RATE,
  micFrameSamples: MIC_FRAME_SAMPLES,
};

export function shouldReportPlaybackUnderrun(
  playbackHasStarted: boolean,
  nextPlaybackAt: number,
  now: number,
  segmentBoundary: boolean,
): boolean {
  return (
    playbackHasStarted &&
    !segmentBoundary &&
    nextPlaybackAt > 0 &&
    now > nextPlaybackAt + 0.015
  );
}

export function playbackStartPollTimeoutMs(startAt: number, currentTime: number): number {
  return Math.max(2_000, Math.max(0, startAt - currentTime) * 1_000 + 2_000);
}
