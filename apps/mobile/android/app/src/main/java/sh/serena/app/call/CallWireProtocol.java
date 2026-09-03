package sh.serena.app.call;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;

final class CallWireProtocol {
    static final int MAGIC = 0x53524341;
    static final int VERSION = 1;
    static final int HEADER_BYTES = 24;
    static final int KIND_MIC_PCM16 = 1;
    static final int KIND_TTS_PCM16 = 2;
    static final int FLAG_FINAL = 1;
    static final int KNOWN_FLAGS = FLAG_FINAL;
    static final int MIC_SAMPLE_RATE = 16_000;
    static final int MIC_SAMPLES_PER_FRAME = 3_200;
    static final int MIC_PAYLOAD_BYTES = MIC_SAMPLES_PER_FRAME * 2;
    static final int MAX_TTS_FRAME_MS = 50;
    static final int MAX_TTS_SAMPLE_RATE = 48_000;
    static final int MAX_TTS_PAYLOAD_BYTES = MAX_TTS_SAMPLE_RATE * 2 * MAX_TTS_FRAME_MS / 1_000;
    static final int MAX_TTS_FRAME_BYTES = HEADER_BYTES + MAX_TTS_PAYLOAD_BYTES;

    private CallWireProtocol() {}

    static byte[] encodeMicFrame(short[] samples, long sequence, long timestampUs, int flags) {
        if (samples.length != MIC_SAMPLES_PER_FRAME) {
            throw new IllegalArgumentException("mic frames must contain exactly 3200 samples");
        }
        if (sequence < 0 || sequence > 0xffff_ffffL) {
            throw new IllegalArgumentException("sequence is outside uint32 range");
        }
        validateFlags(flags);

        ByteBuffer frame = ByteBuffer.allocate(HEADER_BYTES + MIC_PAYLOAD_BYTES);
        frame.order(ByteOrder.BIG_ENDIAN);
        frame.putInt(MAGIC);
        frame.put((byte) VERSION);
        frame.put((byte) KIND_MIC_PCM16);
        frame.putShort((short) flags);
        frame.putInt((int) sequence);
        frame.putInt(MIC_SAMPLE_RATE);
        frame.putLong(timestampUs);
        frame.order(ByteOrder.LITTLE_ENDIAN);
        for (short sample : samples) {
            frame.putShort(sample);
        }
        return frame.array();
    }

    static Header decodeHeader(byte[] frame) {
        if (frame.length < HEADER_BYTES) {
            throw new IllegalArgumentException("binary frame is shorter than the 24-byte header");
        }
        ByteBuffer header = ByteBuffer.wrap(frame, 0, HEADER_BYTES).order(ByteOrder.BIG_ENDIAN);
        int magic = header.getInt();
        int version = Byte.toUnsignedInt(header.get());
        int kind = Byte.toUnsignedInt(header.get());
        int flags = Short.toUnsignedInt(header.getShort());
        long sequence = Integer.toUnsignedLong(header.getInt());
        int sampleRate = header.getInt();
        long timestampUs = header.getLong();

        if (magic != MAGIC) {
            throw new IllegalArgumentException("binary frame has the wrong magic");
        }
        if (version != VERSION) {
            throw new IllegalArgumentException("unsupported binary protocol version");
        }
        if (kind != KIND_MIC_PCM16 && kind != KIND_TTS_PCM16) {
            throw new IllegalArgumentException("unsupported binary frame kind");
        }
        validateFlags(flags);
        int payloadBytes = frame.length - HEADER_BYTES;
        if (payloadBytes <= 0 || (payloadBytes & 1) != 0) {
            throw new IllegalArgumentException("PCM16 payload must contain a whole number of samples");
        }
        if (kind == KIND_MIC_PCM16) {
            if (sampleRate != MIC_SAMPLE_RATE || payloadBytes != MIC_PAYLOAD_BYTES) {
                throw new IllegalArgumentException("mic frames must be 16 kHz with exactly 3200 samples");
            }
        } else {
            if (!isSupportedTtsSampleRate(sampleRate)) {
                throw new IllegalArgumentException("unsupported TTS sample rate");
            }
            int maxPayloadBytes = sampleRate * 2 * MAX_TTS_FRAME_MS / 1_000;
            if (payloadBytes > maxPayloadBytes) {
                throw new IllegalArgumentException("TTS payload exceeds the frame duration limit");
            }
        }
        return new Header(kind, flags, sequence, sampleRate, timestampUs, payloadBytes);
    }

    static boolean isSupportedTtsSampleRate(int sampleRate) {
        return sampleRate == 16_000 ||
            sampleRate == 22_050 ||
            sampleRate == 24_000 ||
            sampleRate == 44_100 ||
            sampleRate == 48_000;
    }

    private static void validateFlags(int flags) {
        if ((flags & ~KNOWN_FLAGS) != 0) {
            throw new IllegalArgumentException("binary frame contains unknown required flags");
        }
    }

    static final class Header {
        private final int kind;
        private final int flags;
        private final long sequence;
        private final int sampleRate;
        private final long timestampUs;
        private final int payloadBytes;

        Header(int kind, int flags, long sequence, int sampleRate, long timestampUs, int payloadBytes) {
            this.kind = kind;
            this.flags = flags;
            this.sequence = sequence;
            this.sampleRate = sampleRate;
            this.timestampUs = timestampUs;
            this.payloadBytes = payloadBytes;
        }

        int kind() {
            return kind;
        }

        int flags() {
            return flags;
        }

        long sequence() {
            return sequence;
        }

        int sampleRate() {
            return sampleRate;
        }

        long timestampUs() {
            return timestampUs;
        }

        int payloadBytes() {
            return payloadBytes;
        }
    }
}
