package sh.serena.app.call;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import org.junit.Test;

public class CallWireProtocolTest {
    @Test
    public void encodesExactMicFrameWithBigEndianHeaderAndLittleEndianPcm() {
        short[] samples = new short[CallWireProtocol.MIC_SAMPLES_PER_FRAME];
        samples[0] = (short) 0x1234;

        byte[] frame = CallWireProtocol.encodeMicFrame(samples, 7, 42, CallWireProtocol.FLAG_FINAL);
        ByteBuffer header = ByteBuffer.wrap(frame).order(ByteOrder.BIG_ENDIAN);

        assertEquals(CallWireProtocol.HEADER_BYTES + CallWireProtocol.MIC_PAYLOAD_BYTES, frame.length);
        assertEquals(CallWireProtocol.MAGIC, header.getInt());
        assertEquals(CallWireProtocol.VERSION, Byte.toUnsignedInt(header.get()));
        assertEquals(CallWireProtocol.KIND_MIC_PCM16, Byte.toUnsignedInt(header.get()));
        assertEquals(CallWireProtocol.FLAG_FINAL, Short.toUnsignedInt(header.getShort()));
        assertEquals(7, Integer.toUnsignedLong(header.getInt()));
        assertEquals(CallWireProtocol.MIC_SAMPLE_RATE, header.getInt());
        assertEquals(42, header.getLong());
        assertEquals(0x34, Byte.toUnsignedInt(frame[CallWireProtocol.HEADER_BYTES]));
        assertEquals(0x12, Byte.toUnsignedInt(frame[CallWireProtocol.HEADER_BYTES + 1]));
    }

    @Test
    public void decodesTtsHeader() {
        byte[] frame = makeFrame(CallWireProtocol.KIND_TTS_PCM16, 0, 11, 24_000, 99, 8);

        CallWireProtocol.Header header = CallWireProtocol.decodeHeader(frame);

        assertEquals(CallWireProtocol.KIND_TTS_PCM16, header.kind());
        assertEquals(11, header.sequence());
        assertEquals(24_000, header.sampleRate());
        assertEquals(99, header.timestampUs());
        assertEquals(8, header.payloadBytes());
    }

    @Test
    public void rejectsUnknownFlagsAndMalformedPayload() {
        byte[] flags = makeFrame(CallWireProtocol.KIND_TTS_PCM16, 2, 0, 16_000, 0, 2);
        byte[] oddPayload = makeFrame(CallWireProtocol.KIND_TTS_PCM16, 0, 0, 16_000, 0, 3);

        assertThrows(IllegalArgumentException.class, () -> CallWireProtocol.decodeHeader(flags));
        assertThrows(IllegalArgumentException.class, () -> CallWireProtocol.decodeHeader(oddPayload));
    }

    @Test
    public void rejectsWrongMagicAndVersion() {
        byte[] wrongMagic = makeFrame(CallWireProtocol.KIND_TTS_PCM16, 0, 0, 16_000, 0, 2);
        wrongMagic[0] = 0;
        byte[] wrongVersion = makeFrame(CallWireProtocol.KIND_TTS_PCM16, 0, 0, 16_000, 0, 2);
        wrongVersion[4] = 2;

        assertThrows(IllegalArgumentException.class, () -> CallWireProtocol.decodeHeader(wrongMagic));
        assertThrows(IllegalArgumentException.class, () -> CallWireProtocol.decodeHeader(wrongVersion));
    }

    @Test
    public void rejectsTtsPayloadOverDurationLimit() {
        int maxPayload = 16_000 * 2 * CallWireProtocol.MAX_TTS_FRAME_MS / 1_000;
        byte[] oversized = makeFrame(
            CallWireProtocol.KIND_TTS_PCM16,
            0,
            0,
            16_000,
            0,
            maxPayload + 2
        );

        assertThrows(
            IllegalArgumentException.class,
            () -> CallWireProtocol.decodeHeader(oversized)
        );
    }

    private static byte[] makeFrame(int kind, int flags, long sequence, int sampleRate, long timestampUs, int payloadBytes) {
        ByteBuffer frame = ByteBuffer.allocate(CallWireProtocol.HEADER_BYTES + payloadBytes).order(ByteOrder.BIG_ENDIAN);
        frame.putInt(CallWireProtocol.MAGIC);
        frame.put((byte) CallWireProtocol.VERSION);
        frame.put((byte) kind);
        frame.putShort((short) flags);
        frame.putInt((int) sequence);
        frame.putInt(sampleRate);
        frame.putLong(timestampUs);
        return frame.array();
    }
}
