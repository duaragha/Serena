package sh.serena.app.call;

import android.Manifest;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.Process;
import android.os.SystemClock;
import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.net.URI;
import java.net.URISyntaxException;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import okhttp3.OkHttpClient;
import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okio.ByteString;
import org.json.JSONException;
import org.json.JSONObject;
import sh.serena.app.BuildConfig;
import sh.serena.app.MainActivity;

@CapacitorPlugin(
    name = "SerenaCall",
    permissions = { @Permission(strings = { Manifest.permission.RECORD_AUDIO }, alias = SerenaCallPlugin.MICROPHONE_PERMISSION) }
)
public final class SerenaCallPlugin extends Plugin {
    static final String MICROPHONE_PERMISSION = "microphone";

    private static final int CLOSE_NORMAL = 1000;
    private static final long MIN_PING_INTERVAL_MS = 1_000;
    private static final long MAX_PING_INTERVAL_MS = 60_000;
    private static final long DEFAULT_PING_INTERVAL_MS = 5_000;
    private static final long PING_EXPIRY_NS = TimeUnit.SECONDS.toNanos(30);
    private static final long MAX_RECONNECT_DELAY_MS = 8_000;
    private static final long PLAYBACK_START_TIMEOUT_NS = TimeUnit.SECONDS.toNanos(2);
    // With the protocol's 50 ms frame ceiling this caps queued network PCM at
    // 800 ms. AudioTrack's own bounded buffer applies the final backpressure.
    private static final int MAX_PLAYBACK_QUEUE_ITEMS = 16;
    private static final int MAX_ARTIFACT_BYTES = 512 * 1024;
    private static final int MAX_FETCHED_ARTIFACT_RECEIPTS = 32;
    private static final long ARTIFACT_RECEIPT_TTL_MS = TimeUnit.MINUTES.toMillis(5);

    private final Object captureLock = new Object();
    private final Object connectionLock = new Object();
    private final Object sendLock = new Object();
    private final ExecutorService captureExecutor = Executors.newSingleThreadExecutor(namedThreadFactory("serena-call-capture"));
    private final ThreadPoolExecutor playbackExecutor = new ThreadPoolExecutor(
        1,
        1,
        0L,
        TimeUnit.MILLISECONDS,
        new ArrayBlockingQueue<>(MAX_PLAYBACK_QUEUE_ITEMS),
        namedThreadFactory("serena-call-playback"),
        new ThreadPoolExecutor.AbortPolicy()
    );
    private final ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor(namedThreadFactory("serena-call-timers"));
    private final AtomicInteger playbackQueueDepth = new AtomicInteger();
    private final AtomicLong connectionEpoch = new AtomicLong();
    private final AtomicLong generationCounter = new AtomicLong();
    private final AtomicLong cancelledThroughGeneration = new AtomicLong(-1);
    private final AtomicLong highestJobEventSequence = new AtomicLong();
    private final Map<String, PingSample> pendingPings = new ConcurrentHashMap<>();
    private final Map<Long, Long> pttReleaseNs = new ConcurrentHashMap<>();
    private final Map<Long, Long> firstOutputReceivedNs = new ConcurrentHashMap<>();
    private final Map<Long, Long> firstPcmWriteNs = new ConcurrentHashMap<>();
    private final Map<Long, String> outputSegmentKinds = new ConcurrentHashMap<>();
    private final Map<String, Long> fetchedArtifactReceipts = new ConcurrentHashMap<>();

    private OkHttpClient httpClient;
    private volatile WebSocket socket;
    private volatile boolean connected;
    private volatile boolean serverReady;
    private volatile boolean userClosed = true;
    private volatile String socketUrl = "";
    private volatile String authToken = "";
    private volatile String callId = "";
    private volatile String tailnetPath = "unknown";
    private volatile long pingIntervalMs = DEFAULT_PING_INTERVAL_MS;
    private volatile boolean coldStartMeasurement;
    private volatile boolean helloReported;
    private volatile int reconnectAttempt;
    private volatile ScheduledFuture<?> reconnectTask;
    private volatile ScheduledFuture<?> pingTask;
    private volatile PluginCall pendingConnectCall;
    private volatile boolean permissionRequestInFlight;

    private volatile AudioRecord audioRecord;
    private volatile boolean captureRunning;
    private volatile boolean captureEndRequested;
    private volatile long activeInputGeneration = -1;

    private volatile long wireOutputGeneration = -1;
    private volatile AudioTrack audioTrack;
    private volatile long playbackGeneration = -1;
    private long expectedOutputSequence;
    private int playbackSampleRate;
    private volatile boolean playbackStarted;
    private long playbackFramesWritten;
    private int lastUnderrunCount;
    private ScheduledFuture<?> underrunTask;
    private ScheduledFuture<?> playbackStartTask;
    private long playbackStartGeneration = -1;
    private CallWireProtocol.Header playbackStartHeader;
    private long playbackPlayReturnedNs;
    private ScheduledFuture<?> contentPlaybackStartTask;
    private long contentPlaybackStartGeneration = -1;
    private long contentPlaybackStartSequence = -1;
    private long contentPlaybackStartFrame = -1;
    private long contentPlaybackAcknowledgedGeneration = -1;
    private double rollingRttMs = -1;

    @Override
    public void load() {
        httpClient = new OkHttpClient.Builder()
            .pingInterval(15, TimeUnit.SECONDS)
            .followRedirects(false)
            .followSslRedirects(false)
            .build();
    }

    @PluginMethod
    public void connect(PluginCall call) {
        String requestedUrl = call.getString("url", "").trim();
        if (requestedUrl.isEmpty()) {
            call.reject("url is required");
            return;
        }
        try {
            requestedUrl = normalizeWebSocketUrl(requestedUrl);
        } catch (IllegalArgumentException error) {
            call.reject(error.getMessage());
            return;
        }

        long requestedPingInterval = call.getInt("pingIntervalMs", (int) DEFAULT_PING_INTERVAL_MS);

        PluginCall previousConnect;
        WebSocket previous;
        long replacementEpoch;
        synchronized (connectionLock) {
            replacementEpoch = connectionEpoch.incrementAndGet();
            previousConnect = pendingConnectCall;
            pendingConnectCall = null;
            previous = socket;
            socket = null;
            userClosed = true;
            connected = false;
            serverReady = false;
        }
        if (previousConnect != null) {
            previousConnect.reject("call connection was replaced");
        }
        cancelReconnect();
        cancelPing();
        stopCapture(false);
        long previousGeneration = Math.max(
            generationCounter.get(),
            Math.max(activeInputGeneration, wireOutputGeneration)
        );
        invalidatePlayback(previousGeneration);
        generationCounter.set(
            CallLifecyclePolicy.nextFreshGeneration(
                previousGeneration, cancelledThroughGeneration.get()
            )
        );
        if (previous != null) {
            previous.cancel();
        }

        synchronized (connectionLock) {
            if (connectionEpoch.get() != replacementEpoch) {
                call.reject("call connection was superseded");
                return;
            }
            socketUrl = requestedUrl;
            authToken = call.getString("token", "").trim();
            callId = UUID.randomUUID().toString();
            tailnetPath = CallLifecyclePolicy.normalizeTailnetPath(
                call.getString("path", "unknown")
            );
            pingIntervalMs = Math.max(MIN_PING_INTERVAL_MS, Math.min(MAX_PING_INTERVAL_MS, requestedPingInterval));
            coldStartMeasurement = call.getBoolean("coldStart", false);
            helloReported = false;
            reconnectAttempt = 0;
            rollingRttMs = -1;
            pendingPings.clear();
            pttReleaseNs.clear();
            firstOutputReceivedNs.clear();
            firstPcmWriteNs.clear();
            outputSegmentKinds.clear();
            fetchedArtifactReceipts.clear();
            highestJobEventSequence.set(0);
            userClosed = false;
            serverReady = false;
            pendingConnectCall = call;
        }
        openSocket(false);
    }

    @PluginMethod
    public void beginPushToTalk(PluginCall call) {
        if (getPermissionState(MICROPHONE_PERMISSION) != PermissionState.GRANTED) {
            permissionRequestInFlight = true;
            try {
                requestPermissionForAlias(MICROPHONE_PERMISSION, call, "microphonePermissionCallback");
            } catch (RuntimeException error) {
                permissionRequestInFlight = false;
                call.reject("could not request microphone permission");
            }
            return;
        }
        startPushToTalk(call);
    }

    @PermissionCallback
    private void microphonePermissionCallback(PluginCall call) {
        permissionRequestInFlight = false;
        if (getPermissionState(MICROPHONE_PERMISSION) != PermissionState.GRANTED) {
            call.reject("microphone permission is required");
            return;
        }
        startPushToTalk(call);
    }

    @PluginMethod
    public void endPushToTalk(PluginCall call) {
        long generation;
        synchronized (captureLock) {
            generation = activeInputGeneration;
            if (!captureRunning) {
                JSObject result = new JSObject();
                result.put("active", false);
                result.put("generation", generation);
                call.resolve(result);
                return;
            }
            pttReleaseNs.put(generation, System.nanoTime());
            captureEndRequested = true;
            captureRunning = false;
            stopRecorder(audioRecord);
        }
        JSObject result = new JSObject();
        result.put("active", false);
        result.put("generation", generation);
        call.resolve(result);
    }

    @PluginMethod
    public void cancel(PluginCall call) {
        long target = Math.max(generationCounter.get(), Math.max(activeInputGeneration, wireOutputGeneration));
        synchronized (sendLock) {
            cancelledThroughGeneration.accumulateAndGet(target, Math::max);
            sendControl(json("type", "cancel", "generation", target));
        }
        stopCapture(false);
        invalidatePlayback(target);

        JSObject result = new JSObject();
        result.put("generation", target);
        call.resolve(result);
    }

    @PluginMethod
    public void hangup(PluginCall call) {
        long target = Math.max(generationCounter.get(), Math.max(activeInputGeneration, wireOutputGeneration));
        synchronized (sendLock) {
            cancelledThroughGeneration.accumulateAndGet(target, Math::max);
            sendControl(json("type", "hangup"));
        }
        closeCall();
        call.resolve();
    }

    @PluginMethod
    public void getState(PluginCall call) {
        JSObject state = new JSObject();
        state.put("callId", callId);
        state.put("connected", connected && serverReady);
        state.put("pushToTalk", captureRunning);
        state.put("generation", generationCounter.get());
        call.resolve(state);
    }

    @PluginMethod
    public void getEndpoint(PluginCall call) {
        JSObject endpoint = new JSObject();
        endpoint.put(
            "url",
            CallLifecyclePolicy.pinnedWebSocketUrl(
                BuildConfig.SERENA_CALL_HOST,
                BuildConfig.SERENA_CALL_PORT
            )
        );
        call.resolve(endpoint);
    }

    @PluginMethod
    public void artifactOpened(PluginCall call) {
        Long eventSequence = positiveLong(call.getData().opt("eventSeq"));
        String jobId = call.getString("jobId", "").trim();
        String receipt = call.getString("receipt", "").trim();
        if (
            eventSequence == null ||
            !CallLifecyclePolicy.isValidArtifactOpen(
                eventSequence, jobId, receipt
            )
        ) {
            call.reject("a valid artifact event is required");
            return;
        }
        if (
            !CallLifecyclePolicy.consumeArtifactFetch(
                fetchedArtifactReceipts,
                receipt,
                SystemClock.elapsedRealtime(),
                ARTIFACT_RECEIPT_TTL_MS
            )
        ) {
            call.reject("the artifact must be fetched in-app first");
            return;
        }
        boolean sent = sendControl(
            json(
                "type",
                "artifact.opened",
                "event_seq",
                eventSequence,
                "job_id",
                jobId,
                "receipt",
                receipt
            )
        );
        if (!sent) {
            CallLifecyclePolicy.rememberArtifactFetch(
                fetchedArtifactReceipts,
                receipt,
                SystemClock.elapsedRealtime(),
                ARTIFACT_RECEIPT_TTL_MS,
                MAX_FETCHED_ARTIFACT_RECEIPTS
            );
            call.reject("call websocket is not connected");
            return;
        }
        call.resolve();
    }

    @PluginMethod
    public void fetchArtifact(PluginCall call) {
        String url = call.getString("url", "").trim();
        if (!CallLifecyclePolicy.isAllowedArtifactUrl(socketUrl, url)) {
            call.reject("artifact url is outside the active call endpoint");
            return;
        }
        Request request = new Request.Builder()
            .url(url)
            .header("Cache-Control", "no-store")
            .build();
        httpClient.newCall(request).enqueue(
            new Callback() {
                @Override
                public void onFailure(Call requestCall, IOException error) {
                    call.reject("draft link could not be opened");
                }

                @Override
                public void onResponse(Call requestCall, Response response) {
                    try (response) {
                        ResponseBody body = response.body();
                        if (
                            !response.isSuccessful() ||
                            body == null ||
                            response.priorResponse() != null ||
                            !CallLifecyclePolicy.isAllowedArtifactUrl(
                                socketUrl,
                                response.request().url().toString()
                            )
                        ) {
                            call.reject("draft link could not be opened");
                            return;
                        }
                        String receipt = response.header(
                            "X-Serena-Artifact-Receipt", ""
                        ).trim();
                        if (!CallLifecyclePolicy.isValidArtifactReceipt(receipt)) {
                            call.reject("draft response had no server receipt");
                            return;
                        }
                        long contentLength = body.contentLength();
                        if (contentLength > MAX_ARTIFACT_BYTES) {
                            call.reject("draft response was too large");
                            return;
                        }
                        byte[] content = readBounded(
                            body.byteStream(), MAX_ARTIFACT_BYTES
                        );
                        if (content.length == 0) {
                            call.reject("draft response was empty");
                            return;
                        }
                        CallLifecyclePolicy.rememberArtifactFetch(
                            fetchedArtifactReceipts,
                            receipt,
                            SystemClock.elapsedRealtime(),
                            ARTIFACT_RECEIPT_TTL_MS,
                            MAX_FETCHED_ARTIFACT_RECEIPTS
                        );
                        JSObject result = new JSObject();
                        result.put(
                            "content",
                            new String(content, java.nio.charset.StandardCharsets.UTF_8)
                        );
                        result.put("receipt", receipt);
                        call.resolve(result);
                    } catch (IOException error) {
                        call.reject("draft link could not be opened");
                    }
                }
            }
        );
    }

    @Override
    protected void handleOnPause() {
        if (
            !CallLifecyclePolicy.shouldCloseForBackground(
                permissionRequestInFlight,
                userClosed,
                connected,
                socket != null,
                pendingConnectCall != null
            )
        ) {
            return;
        }
        synchronized (sendLock) {
            sendControl(json("type", "hangup"));
        }
        closeCall(CLOSE_NORMAL, "app backgrounded", "call closed in background");
    }

    @Override
    protected void handleOnDestroy() {
        closeCall();
        captureExecutor.shutdownNow();
        playbackExecutor.shutdownNow();
        scheduler.shutdownNow();
        if (httpClient != null) {
            httpClient.dispatcher().executorService().shutdown();
            httpClient.connectionPool().evictAll();
        }
    }

    private void startPushToTalk(PluginCall call) {
        if (
            !CallLifecyclePolicy.canBeginPushToTalk(
                connected, serverReady, socket != null
            )
        ) {
            call.reject("call is not ready yet");
            return;
        }

        AudioRecord recorder;
        long generation;
        synchronized (captureLock) {
            if (captureRunning || audioRecord != null) {
                call.reject("push to talk is already active");
                return;
            }
            generation = generationCounter.incrementAndGet();
            invalidatePlayback(generation - 1);
            int minimumBytes = AudioRecord.getMinBufferSize(
                CallWireProtocol.MIC_SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            );
            if (minimumBytes <= 0) {
                call.reject("16 kHz mono PCM16 recording is not supported on this device");
                return;
            }
            int bufferBytes = Math.max(minimumBytes, CallWireProtocol.MIC_PAYLOAD_BYTES * 4);
            try {
                recorder = new AudioRecord(
                    MediaRecorder.AudioSource.VOICE_RECOGNITION,
                    CallWireProtocol.MIC_SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    bufferBytes
                );
                if (recorder.getState() != AudioRecord.STATE_INITIALIZED) {
                    recorder.release();
                    call.reject("could not initialize 16 kHz mono PCM16 recording");
                    return;
                }
                recorder.startRecording();
            } catch (SecurityException error) {
                call.reject("microphone permission is required");
                return;
            } catch (RuntimeException error) {
                call.reject("could not start microphone capture");
                return;
            }
            audioRecord = recorder;
            activeInputGeneration = generation;
            captureEndRequested = false;
            captureRunning = true;
        }
        boolean announced;
        synchronized (sendLock) {
            WebSocket current = socket;
            announced = CallLifecyclePolicy.canAnnouncePushToTalk(
                connected,
                current != null,
                captureRunning,
                generation,
                activeInputGeneration,
                cancelledThroughGeneration.get()
            ) && current.send(
                json("type", "ptt.begin", "generation", generation).toString()
            );
        }
        if (!announced) {
            abandonUnscheduledCapture(recorder, generation);
            call.reject("call websocket closed before push to talk began");
            return;
        }

        captureExecutor.execute(() -> captureLoop(recorder, generation));
        emitState("listening");
        JSObject result = new JSObject();
        result.put("generation", generation);
        call.resolve(result);
    }

    private void captureLoop(AudioRecord recorder, long generation) {
        Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO);
        short[] samples = new short[CallWireProtocol.MIC_SAMPLES_PER_FRAME];
        int filled = 0;
        long sequence = 0;
        boolean abnormalExit = false;
        try {
            while (captureRunning && activeInputGeneration == generation) {
                int read = recorder.read(
                    samples,
                    filled,
                    CallWireProtocol.MIC_SAMPLES_PER_FRAME - filled,
                    AudioRecord.READ_BLOCKING
                );
                if (read > 0) {
                    filled += read;
                    if (filled == CallWireProtocol.MIC_SAMPLES_PER_FRAME) {
                        if (!sendMicFrame(samples, sequence++, 0, generation)) {
                            abnormalExit = true;
                            break;
                        }
                        samples = new short[CallWireProtocol.MIC_SAMPLES_PER_FRAME];
                        filled = 0;
                    }
                } else if (read == AudioRecord.ERROR_DEAD_OBJECT) {
                    emitError("audio_record_dead", "microphone capture stopped", false);
                    abnormalExit = true;
                    break;
                } else if (read < 0 && captureRunning) {
                    emitError("audio_record_read", "microphone capture failed", false);
                    abnormalExit = true;
                    break;
                }
            }
        } catch (RuntimeException error) {
            if (captureRunning && activeInputGeneration == generation) {
                emitError("audio_record_read", "microphone capture failed", false);
                abnormalExit = true;
            }
        } finally {
            boolean graceful;
            synchronized (captureLock) {
                graceful = CallLifecyclePolicy.shouldSendPushToTalkEnd(
                    captureEndRequested,
                    activeInputGeneration,
                    generation,
                    connected,
                    cancelledThroughGeneration.get()
                );
                captureRunning = false;
            }
            stopRecorder(recorder);
            recorder.release();

            if (graceful) {
                boolean finalFrameSent = true;
                if (filled > 0) {
                    Arrays.fill(samples, filled, samples.length, (short) 0);
                    finalFrameSent = sendMicFrame(
                        samples,
                        sequence++,
                        CallWireProtocol.FLAG_FINAL,
                        generation
                    );
                }
                JSONObject end = json("type", "ptt.end", "generation", generation);
                Long releaseNs = pttReleaseNs.get(generation);
                if (releaseNs != null) {
                    putJson(end, "eou_monotonic_us", releaseNs / 1_000L);
                }
                boolean endSent = finalFrameSent && sendPushToTalkEnd(end, generation);
                abnormalExit = !endSent;
            }
            boolean socketUsable = connected;
            if (
                CallLifecyclePolicy.shouldCancelFailedCapture(
                    abnormalExit,
                    generation,
                    activeInputGeneration,
                    cancelledThroughGeneration.get()
                )
            ) {
                socketUsable = cancelFailedCapture(generation);
            }
            synchronized (captureLock) {
                if (audioRecord == recorder) {
                    audioRecord = null;
                }
                captureEndRequested = false;
            }
            if (abnormalExit) {
                emitState(socketUsable && connected ? "open" : "reconnecting");
            } else if (graceful) {
                emitState(connected ? "thinking" : "reconnecting");
            } else {
                emitState(connected ? "open" : "reconnecting");
            }
        }
    }

    private boolean cancelFailedCapture(long generation) {
        WebSocket current;
        boolean sent = false;
        synchronized (sendLock) {
            if (
                generation <= cancelledThroughGeneration.get() ||
                activeInputGeneration != generation
            ) {
                return connected;
            }
            cancelledThroughGeneration.accumulateAndGet(generation, Math::max);
            current = socket;
            if (connected && current != null) {
                sent = current.send(
                    json("type", "cancel", "generation", generation).toString()
                );
            }
        }
        invalidatePlayback(generation);
        if (!sent && current != null) {
            current.cancel();
        }
        return sent;
    }

    private boolean sendMicFrame(short[] samples, long sequence, int flags, long generation) {
        byte[] frame;
        try {
            frame = CallWireProtocol.encodeMicFrame(samples, sequence, monotonicMicros(), flags);
        } catch (IllegalArgumentException error) {
            emitError("mic_protocol", error.getMessage(), true);
            return false;
        }
        synchronized (sendLock) {
            if (!connected || generation <= cancelledThroughGeneration.get() || activeInputGeneration != generation) {
                return false;
            }
            WebSocket current = socket;
            return current != null && current.send(ByteString.of(frame));
        }
    }

    private boolean sendPushToTalkEnd(JSONObject message, long generation) {
        synchronized (sendLock) {
            WebSocket current = socket;
            if (
                !CallLifecyclePolicy.canSendGeneration(
                    connected,
                    current != null,
                    generation,
                    activeInputGeneration,
                    cancelledThroughGeneration.get()
                )
            ) {
                return false;
            }
            return current.send(message.toString());
        }
    }

    private void stopCapture(boolean graceful) {
        synchronized (captureLock) {
            captureEndRequested = graceful;
            captureRunning = false;
            stopRecorder(audioRecord);
        }
    }

    private void abandonUnscheduledCapture(
        AudioRecord recorder,
        long generation
    ) {
        synchronized (captureLock) {
            if (
                audioRecord == recorder &&
                activeInputGeneration == generation
            ) {
                captureRunning = false;
                captureEndRequested = false;
                audioRecord = null;
            }
        }
        stopRecorder(recorder);
        recorder.release();
    }

    private static void stopRecorder(AudioRecord recorder) {
        if (recorder == null) {
            return;
        }
        try {
            if (recorder.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                recorder.stop();
            }
        } catch (IllegalStateException ignored) {}
    }

    private void openSocket(boolean resume) {
        openSocket(resume, -1);
    }

    private void openSocket(boolean resume, long expectedEpoch) {
        long epoch;
        String url;
        String token;
        synchronized (connectionLock) {
            if (
                !CallLifecyclePolicy.canOpenSocket(
                    connectionEpoch.get(), expectedEpoch, userClosed
                )
            ) {
                return;
            }
            epoch = connectionEpoch.incrementAndGet();
            url = socketUrl;
            token = authToken;
        }
        emitState(resume ? "reconnecting" : "connecting");
        Request.Builder request = new Request.Builder().url(url);
        if (!token.isEmpty()) {
            request.header("Authorization", "Bearer " + token);
        }
        CallSocketListener listener = new CallSocketListener(epoch);
        WebSocket created = httpClient.newWebSocket(request.build(), listener);
        synchronized (connectionLock) {
            if (
                connectionEpoch.get() != epoch ||
                userClosed ||
                listener.isTerminal()
            ) {
                created.cancel();
                return;
            }
            socket = created;
        }
    }

    private final class CallSocketListener extends WebSocketListener {
        private final long epoch;
        private final AtomicBoolean terminal = new AtomicBoolean();

        CallSocketListener(long epoch) {
            this.epoch = epoch;
        }

        boolean isTerminal() {
            return terminal.get();
        }

        @Override
        public void onOpen(WebSocket webSocket, Response response) {
            String currentCallId;
            long currentGeneration;
            synchronized (connectionLock) {
                if (
                    terminal.get() ||
                    connectionEpoch.get() != epoch ||
                    userClosed ||
                    (socket != null && socket != webSocket)
                ) {
                    webSocket.close(CLOSE_NORMAL, "stale connection");
                    return;
                }
                socket = webSocket;
                connected = true;
                serverReady = false;
                reconnectAttempt = 0;
                currentCallId = callId;
                currentGeneration = generationCounter.get();
            }
            JSONObject start = json(
                "type",
                "call.start",
                "call_id",
                currentCallId,
                "generation",
                currentGeneration,
                "greeting",
                true,
                "job_cursor",
                highestJobEventSequence.get()
            );
            if (!webSocket.send(start.toString())) {
                webSocket.cancel();
                return;
            }
            startPing();
        }

        @Override
        public void onMessage(WebSocket webSocket, String text) {
            synchronized (connectionLock) {
                if (!isCurrentLocked(webSocket, epoch)) {
                    return;
                }
                handleControl(text);
            }
        }

        @Override
        public void onMessage(WebSocket webSocket, ByteString bytes) {
            if (bytes.size() > CallWireProtocol.MAX_TTS_FRAME_BYTES) {
                emitError(
                    "audio_frame_size",
                    "received TTS frame exceeds the maximum size",
                    true
                );
                closeCall(1009, "oversized audio frame", "oversized TTS frame");
                return;
            }
            long generation;
            synchronized (connectionLock) {
                if (!isCurrentLocked(webSocket, epoch)) {
                    return;
                }
                generation = wireOutputGeneration;
                if (generation >= 0) {
                    firstOutputReceivedNs.putIfAbsent(
                        generation, System.nanoTime()
                    );
                }
            }
            byte[] frame = bytes.toByteArray();
            if (!enqueuePlayback(generation, () -> handleAudioFrame(frame, generation))) {
                failPlaybackQueue(generation);
            }
        }

        @Override
        public void onClosed(WebSocket webSocket, int code, String reason) {
            if (terminal.compareAndSet(false, true)) {
                handleDisconnect(webSocket, epoch);
            }
        }

        @Override
        public void onFailure(WebSocket webSocket, Throwable error, Response response) {
            if (!terminal.compareAndSet(false, true)) {
                return;
            }
            synchronized (connectionLock) {
                if (isCurrentOrPendingLocked(webSocket, epoch)) {
                    emitError("websocket", "call websocket failed", false);
                }
            }
            handleDisconnect(webSocket, epoch);
        }
    }

    private void handleControl(String text) {
        JSONObject message;
        try {
            message = new JSONObject(text);
        } catch (JSONException error) {
            emitError("control_json", "received malformed JSON control", false);
            return;
        }
        String type = message.optString("type", "");
        switch (type) {
            case "call.ready" -> handleCallReady(message);
            case "audio.start" -> handleAudioStart(message);
            case "audio.segment" -> handleAudioSegment(message);
            case "audio.end" -> handleAudioEnd(message);
            case "ping" -> {}
            case "pong" -> handlePong(message);
            case "sequence.gap" -> emitServerSequenceGap(message);
            case "error" -> handleServerError(message);
            default -> {
                if (type.isEmpty()) {
                    emitError("control_type", "received control without a type", false);
                }
            }
        }
        emitControl(message);
        acknowledgeJobEvent(message);
    }

    private void acknowledgeJobEvent(JSONObject message) {
        String type = message.optString("type", "");
        if (!CallLifecyclePolicy.isReplayableJobEvent(type)) {
            return;
        }
        long eventSequence = message.optLong("event_seq", -1);
        if (eventSequence < 1) {
            return;
        }
        boolean sent = sendControl(
            json("type", "job.ack", "event_seq", eventSequence)
        );
        highestJobEventSequence.updateAndGet(
            current -> CallLifecyclePolicy.acknowledgedJobCursor(
                current, type, eventSequence, sent
            )
        );
    }

    private void handleCallReady(JSONObject message) {
        if (!message.optBoolean("ready", false)) {
            String text = "call models did not become ready";
            emitError("call_not_ready", text, true);
            PluginCall connectCall = pendingConnectCall;
            pendingConnectCall = null;
            if (connectCall != null) {
                connectCall.reject(text);
            }
            closeCall(1011, "call not ready", text);
            return;
        }
        serverReady = true;
        resolveConnect();
        emitState("open");
    }

    private void resolveConnect() {
        PluginCall connectCall = pendingConnectCall;
        pendingConnectCall = null;
        if (connectCall == null) {
            return;
        }
        JSObject result = new JSObject();
        result.put("callId", callId);
        connectCall.resolve(result);
    }

    private void handleServerError(JSONObject message) {
        boolean fatal = message.optBoolean("fatal", false);
        String code = message.optString("code", "server");
        String text = message.optString("message", "call server error");
        emitError(code, text, fatal);
        if (!fatal) {
            return;
        }
        PluginCall connectCall = pendingConnectCall;
        pendingConnectCall = null;
        if (connectCall != null) {
            connectCall.reject(text);
        }
        closeCall(1008, "fatal server error", text);
    }

    private void handleAudioStart(JSONObject message) {
        long generation = message.optLong("generation", -1);
        int sampleRate = message.optInt("sample_rate", 0);
        if (generation < 0 || !CallWireProtocol.isSupportedTtsSampleRate(sampleRate)) {
            emitError("audio_start", "audio.start has an invalid generation or sample rate", false);
            return;
        }
        if (generation <= cancelledThroughGeneration.get()) {
            return;
        }
        wireOutputGeneration = generation;
        outputSegmentKinds.clear();
        contentPlaybackAcknowledgedGeneration = -1;
        if (!enqueuePlayback(
            generation,
            () -> {
                if (generation <= cancelledThroughGeneration.get()) {
                    return;
                }
                releaseAudioTrack();
                playbackGeneration = generation;
                expectedOutputSequence = 0;
                playbackSampleRate = sampleRate;
                playbackStarted = false;
                playbackFramesWritten = 0;
                lastUnderrunCount = 0;
                emitState("speaking");
            }
        )) {
            failPlaybackQueue(generation);
        }
    }

    private void handleAudioSegment(JSONObject message) {
        long generation = message.optLong("generation", -1);
        long sequence = message.optLong("sequence", -1);
        String kind = message.optString("kind", "");
        if (
            generation < 0 ||
            sequence < 0 ||
            sequence > 0xffff_ffffL ||
            (!kind.equals("acknowledgement") && !kind.equals("content"))
        ) {
            emitError("audio_segment", "audio.segment is invalid", false);
            return;
        }
        if (
            generation <= cancelledThroughGeneration.get() ||
            generation != wireOutputGeneration
        ) {
            return;
        }
        outputSegmentKinds.put(sequence, kind);
    }

    private void handleAudioEnd(JSONObject message) {
        long generation = message.optLong("generation", -1);
        if (generation == wireOutputGeneration) {
            wireOutputGeneration = -1;
        }
        if (!enqueuePlayback(generation, () -> finishPlayback(generation))) {
            failPlaybackQueue(generation);
        }
    }

    private void handleAudioFrame(byte[] frame, long generation) {
        if (generation < 0) {
            emitError("audio_generation", "audio frame arrived before audio.start", false);
            return;
        }
        if (generation <= cancelledThroughGeneration.get() || generation != playbackGeneration) {
            return;
        }

        CallWireProtocol.Header header;
        try {
            header = CallWireProtocol.decodeHeader(frame);
        } catch (IllegalArgumentException error) {
            emitError("audio_protocol", error.getMessage(), false);
            return;
        }
        if (header.kind() != CallWireProtocol.KIND_TTS_PCM16) {
            emitError("audio_kind", "received non-TTS binary frame from server", false);
            return;
        }
        if (header.sampleRate() != playbackSampleRate) {
            emitError("audio_rate", "binary frame sample rate does not match audio.start", false);
            return;
        }

        if (header.sequence() != expectedOutputSequence) {
            reportSequenceGap(generation, expectedOutputSequence, header.sequence());
            long distance = (header.sequence() - expectedOutputSequence) & 0xffff_ffffL;
            if (distance >= 0x8000_0000L) {
                return;
            }
        }
        expectedOutputSequence = (header.sequence() + 1) & 0xffff_ffffL;
        String segmentKind = outputSegmentKinds.remove(header.sequence());
        long segmentStartFrame = playbackFramesWritten;
        boolean contentPollingStarted = false;

        if (audioTrack == null && !createAudioTrack(header.sampleRate(), header.payloadBytes())) {
            return;
        }
        int offset = CallWireProtocol.HEADER_BYTES;
        int remaining = header.payloadBytes();
        while (remaining > 0 && generation == playbackGeneration && generation > cancelledThroughGeneration.get()) {
            int written = audioTrack.write(frame, offset, remaining, AudioTrack.WRITE_BLOCKING);
            if (written <= 0) {
                emitError("audio_track_write", "AudioTrack could not accept PCM output", false);
                return;
            }
            offset += written;
            remaining -= written;
            playbackFramesWritten += written / 2;
            firstPcmWriteNs.putIfAbsent(generation, System.nanoTime());
            if (generation <= cancelledThroughGeneration.get()) {
                return;
            }
            if (!playbackStarted) {
                try {
                    audioTrack.play();
                } catch (IllegalStateException error) {
                    emitError("audio_track_play", "AudioTrack could not start playback", false);
                    return;
                }
                long playReturnedNs = System.nanoTime();
                playbackStarted = true;
                startUnderrunPolling(generation);
                startPlaybackStartPolling(generation, header, playReturnedNs);
            }
            if (
                !contentPollingStarted &&
                "content".equals(segmentKind)
            ) {
                startContentPlaybackPolling(
                    generation,
                    header.sequence(),
                    segmentStartFrame
                );
                contentPollingStarted = true;
            }
        }
        checkUnderruns(generation);
    }

    private void finishPlayback(long generation) {
        if (generation != playbackGeneration || generation <= cancelledThroughGeneration.get()) {
            return;
        }
        if (underrunTask != null) {
            underrunTask.cancel(false);
            underrunTask = null;
        }
        if (audioTrack != null && playbackStarted) {
            long played = Integer.toUnsignedLong(audioTrack.getPlaybackHeadPosition());
            long remainingFrames = Math.max(0, playbackFramesWritten - played);
            long estimatedMs = playbackSampleRate > 0
                ? (remainingFrames * 1_000L) / playbackSampleRate
                : 0;
            long waitMs = Math.min(5_000L, Math.max(250L, estimatedMs + 500L));
            long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(waitMs);
            while (
                generation == playbackGeneration &&
                generation > cancelledThroughGeneration.get() &&
                Integer.toUnsignedLong(audioTrack.getPlaybackHeadPosition()) < playbackFramesWritten &&
                System.nanoTime() < deadline
            ) {
                checkUnderruns(generation);
                try {
                    Thread.sleep(10);
                } catch (InterruptedException error) {
                    Thread.currentThread().interrupt();
                    return;
                }
            }
        }
        if (generation != playbackGeneration || generation <= cancelledThroughGeneration.get()) {
            return;
        }
        checkUnderruns(generation);
        pollPlaybackStart(generation);
        pollContentPlaybackStart(generation);
        clearGenerationTiming(generation);
        releaseAudioTrack();
        playbackGeneration = -1;
        playbackSampleRate = 0;
        playbackStarted = false;
        playbackFramesWritten = 0;

        JSObject event = new JSObject();
        event.put("state", "ended");
        event.put("generation", generation);
        notifyListeners("playback", event);
        emitState(connected ? "open" : "reconnecting");
    }

    private boolean createAudioTrack(int sampleRate, int firstPayloadBytes) {
        int minimumBytes = AudioTrack.getMinBufferSize(
            sampleRate,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        );
        if (minimumBytes <= 0) {
            emitError("audio_track_rate", "device cannot play the declared sample rate", false);
            return false;
        }
        int bufferBytes = Math.max(minimumBytes * 2, firstPayloadBytes * 2);
        AudioAttributes attributes = new AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
            .build();
        AudioFormat format = new AudioFormat.Builder()
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setSampleRate(sampleRate)
            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
            .build();
        try {
            AudioTrack.Builder builder = new AudioTrack.Builder()
                .setAudioAttributes(attributes)
                .setAudioFormat(format)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .setBufferSizeInBytes(bufferBytes);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                builder.setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY);
            }
            audioTrack = builder.build();
        } catch (RuntimeException error) {
            emitError("audio_track_init", "could not initialize streaming audio output", false);
            audioTrack = null;
            return false;
        }
        if (audioTrack.getState() != AudioTrack.STATE_INITIALIZED) {
            audioTrack.release();
            audioTrack = null;
            emitError("audio_track_init", "streaming audio output is not initialized", false);
            return false;
        }
        return true;
    }

    private void sendPlaybackStarted(
        long generation,
        CallWireProtocol.Header header,
        long playbackNs,
        long playReturnedNs
    ) {
        boolean sent;
        synchronized (sendLock) {
            WebSocket current = socket;
            if (
                !CallLifecyclePolicy.canSendGeneration(
                    connected,
                    current != null,
                    generation,
                    playbackGeneration,
                    cancelledThroughGeneration.get()
                )
            ) {
                return;
            }
            JSONObject ack = json(
                "type",
                "playback.started",
                "generation",
                generation,
                "sequence",
                header.sequence(),
                "timestamp_us",
                playbackNs / 1_000L,
                "measurement_point",
                "playback_head_advanced",
                "play_return_to_head_ms",
                (playbackNs - playReturnedNs) / 1_000_000.0
            );
            Long releaseNs = pttReleaseNs.get(generation);
            if (releaseNs != null && playbackNs >= releaseNs) {
                putJson(
                    ack,
                    "eou_to_playback_ms",
                    (playbackNs - releaseNs) / 1_000_000.0
                );
            }
            Long outputReceivedNs = firstOutputReceivedNs.remove(generation);
            if (outputReceivedNs != null && playbackNs >= outputReceivedNs) {
                putJson(
                    ack,
                    "first_output_to_playback_ms",
                    (playbackNs - outputReceivedNs) / 1_000_000.0
                );
            }
            Long pcmWriteNs = firstPcmWriteNs.remove(generation);
            if (pcmWriteNs != null && playbackNs >= pcmWriteNs) {
                putJson(
                    ack,
                    "first_pcm_write_to_playback_ms",
                    (playbackNs - pcmWriteNs) / 1_000_000.0
                );
            }
            sent = current.send(ack.toString());
        }
        if (!sent) {
            return;
        }
        JSObject event = new JSObject();
        event.put("state", "started");
        event.put("generation", generation);
        event.put("sequence", header.sequence());
        event.put("sampleRate", header.sampleRate());
        notifyListeners("playback", event);
    }

    private synchronized void startPlaybackStartPolling(
        long generation,
        CallWireProtocol.Header header,
        long playReturnedNs
    ) {
        cancelPlaybackStartPollingLocked();
        playbackStartGeneration = generation;
        playbackStartHeader = header;
        playbackPlayReturnedNs = playReturnedNs;
        playbackStartTask = scheduler.scheduleAtFixedRate(
            () -> pollPlaybackStart(generation),
            5,
            5,
            TimeUnit.MILLISECONDS
        );
    }

    private void pollPlaybackStart(long generation) {
        CallWireProtocol.Header header = null;
        long playbackNs = 0;
        long playReturnedNs = 0;
        boolean timedOut = false;
        synchronized (this) {
            if (generation != playbackStartGeneration) {
                return;
            }
            AudioTrack track = audioTrack;
            long headPosition = track == null
                ? 0
                : Integer.toUnsignedLong(track.getPlaybackHeadPosition());
            if (
                generation != playbackGeneration ||
                generation <= cancelledThroughGeneration.get() ||
                track == null
            ) {
                cancelPlaybackStartPollingLocked();
                return;
            }
            long nowNs = System.nanoTime();
            if (
                CallLifecyclePolicy.shouldAckPlaybackHead(
                    generation,
                    playbackStartGeneration,
                    playbackGeneration,
                    cancelledThroughGeneration.get(),
                    true,
                    headPosition
                )
            ) {
                header = playbackStartHeader;
                playbackNs = nowNs;
                playReturnedNs = playbackPlayReturnedNs;
                cancelPlaybackStartPollingLocked();
            } else if (
                nowNs - playbackPlayReturnedNs >= PLAYBACK_START_TIMEOUT_NS
            ) {
                timedOut = true;
                cancelPlaybackStartPollingLocked();
            }
        }
        if (header != null) {
            sendPlaybackStarted(
                generation, header, playbackNs, playReturnedNs
            );
        } else if (timedOut) {
            emitError(
                "playback_start_timeout",
                "AudioTrack playback head did not advance",
                false
            );
        }
    }

    private synchronized void cancelPlaybackStartPolling() {
        cancelPlaybackStartPollingLocked();
    }

    private void cancelPlaybackStartPollingLocked() {
        ScheduledFuture<?> task = playbackStartTask;
        playbackStartTask = null;
        playbackStartGeneration = -1;
        playbackStartHeader = null;
        playbackPlayReturnedNs = 0;
        if (task != null) {
            task.cancel(false);
        }
    }

    private synchronized void startContentPlaybackPolling(
        long generation,
        long sequence,
        long startFrame
    ) {
        if (
            contentPlaybackAcknowledgedGeneration == generation ||
            (
                contentPlaybackStartTask != null &&
                contentPlaybackStartGeneration == generation
            )
        ) {
            return;
        }
        cancelContentPlaybackPollingLocked();
        contentPlaybackStartGeneration = generation;
        contentPlaybackStartSequence = sequence;
        contentPlaybackStartFrame = startFrame;
        contentPlaybackStartTask = scheduler.scheduleAtFixedRate(
            () -> pollContentPlaybackStart(generation),
            5,
            5,
            TimeUnit.MILLISECONDS
        );
    }

    private void pollContentPlaybackStart(long generation) {
        long sequence = -1;
        long playbackNs = 0;
        synchronized (this) {
            if (generation != contentPlaybackStartGeneration) {
                return;
            }
            AudioTrack track = audioTrack;
            if (
                generation != playbackGeneration ||
                generation <= cancelledThroughGeneration.get() ||
                track == null
            ) {
                cancelContentPlaybackPollingLocked();
                return;
            }
            long headPosition = Integer.toUnsignedLong(
                track.getPlaybackHeadPosition()
            );
            if (headPosition > contentPlaybackStartFrame) {
                sequence = contentPlaybackStartSequence;
                playbackNs = System.nanoTime();
                cancelContentPlaybackPollingLocked();
            }
        }
        if (sequence >= 0) {
            sendContentPlaybackStarted(generation, sequence, playbackNs);
        }
    }

    private void sendContentPlaybackStarted(
        long generation,
        long sequence,
        long playbackNs
    ) {
        long appUptimeMs = SystemClock.elapsedRealtime() -
            MainActivity.appStartedAtElapsedRealtimeMs();
        boolean callHello = !helloReported;
        boolean sent;
        synchronized (sendLock) {
            WebSocket current = socket;
            if (
                !CallLifecyclePolicy.canSendGeneration(
                    connected,
                    current != null,
                    generation,
                    playbackGeneration,
                    cancelledThroughGeneration.get()
                )
            ) {
                return;
            }
            JSONObject ack = json(
                "type",
                "playback.segment_started",
                "generation",
                generation,
                "sequence",
                sequence,
                "kind",
                "content",
                "timestamp_us",
                playbackNs / 1_000L,
                "measurement_point",
                "playback_head_advanced",
                "call_hello",
                callHello,
                "cold_start",
                callHello && coldStartMeasurement,
                "app_uptime_ms",
                appUptimeMs
            );
            Long releaseNs = pttReleaseNs.get(generation);
            if (releaseNs != null && playbackNs >= releaseNs) {
                putJson(
                    ack,
                    "eou_to_playback_ms",
                    (playbackNs - releaseNs) / 1_000_000.0
                );
            }
            sent = current.send(ack.toString());
        }
        if (!sent) {
            return;
        }
        if (callHello) {
            helloReported = true;
        }
        contentPlaybackAcknowledgedGeneration = generation;
        JSObject event = new JSObject();
        event.put("state", "content_started");
        event.put("generation", generation);
        event.put("sequence", sequence);
        event.put("appUptimeMs", appUptimeMs);
        notifyListeners("playback", event);
    }

    private synchronized void cancelContentPlaybackPolling() {
        cancelContentPlaybackPollingLocked();
    }

    private void cancelContentPlaybackPollingLocked() {
        ScheduledFuture<?> task = contentPlaybackStartTask;
        contentPlaybackStartTask = null;
        contentPlaybackStartGeneration = -1;
        contentPlaybackStartSequence = -1;
        contentPlaybackStartFrame = -1;
        if (task != null) {
            task.cancel(false);
        }
    }

    private void startUnderrunPolling(long generation) {
        if (underrunTask != null) {
            underrunTask.cancel(false);
        }
        underrunTask = scheduler.scheduleAtFixedRate(
            () -> enqueuePlayback(
                generation,
                () -> checkUnderruns(generation)
            ),
            250,
            250,
            TimeUnit.MILLISECONDS
        );
    }

    private void checkUnderruns(long generation) {
        if (
            generation != playbackGeneration ||
            generation <= cancelledThroughGeneration.get() ||
            audioTrack == null ||
            !playbackStarted
        ) {
            return;
        }
        int count = audioTrack.getUnderrunCount();
        if (count <= lastUnderrunCount) {
            return;
        }
        int delta = count - lastUnderrunCount;
        lastUnderrunCount = count;
        JSONObject ack = json(
            "type",
            "playback.underrun",
            "generation",
            generation,
            "count",
            count,
            "timestamp_us",
            monotonicMicros()
        );
        sendControl(ack);
        JSObject event = new JSObject();
        event.put("state", "underrun");
        event.put("generation", generation);
        event.put("count", count);
        event.put("delta", delta);
        notifyListeners("playback", event);
    }

    private void reportSequenceGap(long generation, long expected, long received) {
        JSONObject gap = json(
            "type",
            "sequence.gap",
            "direction",
            "output",
            "generation",
            generation,
            "expected",
            expected,
            "received",
            received
        );
        sendControl(gap);
        JSObject event = new JSObject();
        event.put("direction", "output");
        event.put("generation", generation);
        event.put("expected", expected);
        event.put("received", received);
        notifyListeners("sequenceGap", event);
    }

    private void emitServerSequenceGap(JSONObject message) {
        JSObject event = new JSObject();
        event.put("direction", "input");
        event.put("generation", message.optLong("generation", -1));
        event.put("expected", message.optLong("expected", -1));
        event.put("received", message.optLong("received", -1));
        notifyListeners("sequenceGap", event);
    }

    private boolean enqueuePlayback(long generation, Runnable work) {
        int depth = playbackQueueDepth.incrementAndGet();
        emitQueueDepth(depth, generation);
        PlaybackWork item = new PlaybackWork(generation, work);
        try {
            playbackExecutor.execute(item);
            return true;
        } catch (RejectedExecutionException error) {
            item.discard();
            return false;
        }
    }

    private void failPlaybackQueue(long generation) {
        if (generation <= cancelledThroughGeneration.get() || userClosed) {
            return;
        }
        emitError(
            "playback_queue_full",
            "call audio exceeded the bounded playback buffer",
            true
        );
        closeCall(1011, "playback queue full", "playback queue full");
    }

    private void discardQueuedPlayback() {
        List<Runnable> discarded = new ArrayList<>();
        playbackExecutor.getQueue().drainTo(discarded);
        for (Runnable item : discarded) {
            if (item instanceof PlaybackWork playbackWork) {
                playbackWork.discard();
            }
        }
    }

    private void invalidatePlayback(long generation) {
        cancelledThroughGeneration.accumulateAndGet(generation, Math::max);
        CallLifecyclePolicy.clearGenerationTimingThrough(
            generation,
            pttReleaseNs,
            firstOutputReceivedNs,
            firstPcmWriteNs
        );
        if (wireOutputGeneration <= generation) {
            wireOutputGeneration = -1;
        }
        outputSegmentKinds.clear();
        discardQueuedPlayback();
        enqueuePlayback(
            generation,
            () -> {
                if (
                    !CallLifecyclePolicy.shouldResetPlayback(
                        generation, playbackGeneration
                    )
                ) {
                    return;
                }
                releaseAudioTrack();
                playbackGeneration = -1;
                expectedOutputSequence = 0;
                playbackSampleRate = 0;
                playbackStarted = false;
                playbackFramesWritten = 0;
            }
        );
    }

    private void releaseAudioTrack() {
        cancelPlaybackStartPolling();
        cancelContentPlaybackPolling();
        if (underrunTask != null) {
            underrunTask.cancel(false);
            underrunTask = null;
        }
        if (audioTrack == null) {
            return;
        }
        try {
            audioTrack.pause();
            audioTrack.flush();
            audioTrack.stop();
        } catch (IllegalStateException ignored) {}
        audioTrack.release();
        audioTrack = null;
    }

    private void clearGenerationTiming(long generation) {
        CallLifecyclePolicy.clearGenerationTiming(
            generation,
            pttReleaseNs,
            firstOutputReceivedNs,
            firstPcmWriteNs
        );
    }

    private void startPing() {
        cancelPing();
        pingTask = scheduler.scheduleAtFixedRate(this::sendPing, 0, pingIntervalMs, TimeUnit.MILLISECONDS);
    }

    private void sendPing() {
        if (!connected || userClosed) {
            return;
        }
        long nowNs = System.nanoTime();
        pendingPings.entrySet().removeIf(entry -> nowNs - entry.getValue().monotonicNs > PING_EXPIRY_NS);
        String nonce = UUID.randomUUID().toString();
        long sentAtUs = monotonicMicros();
        pendingPings.put(nonce, new PingSample(nowNs));
        if (!sendControl(json("type", "ping", "nonce", nonce, "sent_at_us", sentAtUs))) {
            pendingPings.remove(nonce);
        }
    }

    private void handlePong(JSONObject message) {
        String nonce = message.optString("nonce", "");
        PingSample sample = pendingPings.remove(nonce);
        if (sample == null) {
            return;
        }
        String sampleId = CallLifecyclePolicy.normalizeServerSampleId(
            message.optString("sample_id", "")
        );
        if (sampleId.isEmpty()) {
            return;
        }
        double rttMs = CallLifecyclePolicy.networkRttMillis(
            System.nanoTime() - sample.monotonicNs,
            message.optLong("server_processing_us", 0L)
        );
        rollingRttMs = rollingRttMs < 0 ? rttMs : (rollingRttMs * 0.8) + (rttMs * 0.2);
        String pongPath = CallLifecyclePolicy.normalizeTailnetPath(
            message.optString("path", "unknown")
        );
        String path = CallLifecyclePolicy.resolveTailnetPath(
            tailnetPath, pongPath
        );
        String pathSource = CallLifecyclePolicy.resolveTailnetPathSource(
            tailnetPath,
            pongPath,
            message.optString("path_source", "unknown")
        );
        sendControl(
            json(
                "type",
                "rtt.report",
                "rtt_ms",
                rttMs,
                "path",
                path,
                "path_source",
                pathSource,
                "sample_id",
                sampleId
            )
        );
        JSObject event = new JSObject();
        event.put("rttMs", rttMs);
        event.put("rollingRttMs", rollingRttMs);
        event.put("path", path);
        event.put("pathSource", pathSource);
        notifyListeners("rtt", event);
    }

    private void handleDisconnect(WebSocket webSocket, long epoch) {
        boolean closed;
        synchronized (connectionLock) {
            if (!isCurrentOrPendingLocked(webSocket, epoch)) {
                return;
            }
            socket = null;
            connected = false;
            serverReady = false;
            cancelPing();
            pendingPings.clear();
            pttReleaseNs.clear();
            firstOutputReceivedNs.clear();
            firstPcmWriteNs.clear();
            stopCapture(false);
            long staleGeneration = Math.max(
                generationCounter.get(),
                Math.max(activeInputGeneration, wireOutputGeneration)
            );
            cancelledThroughGeneration.accumulateAndGet(
                staleGeneration, Math::max
            );
            invalidatePlayback(staleGeneration);
            generationCounter.set(
                CallLifecyclePolicy.nextFreshGeneration(
                    staleGeneration, cancelledThroughGeneration.get()
                )
            );
            closed = userClosed;
        }
        if (closed) {
            emitState("closed");
            return;
        }
        scheduleReconnect(epoch);
    }

    private void scheduleReconnect(long failedEpoch) {
        long delayMs;
        synchronized (connectionLock) {
            if (
                !CallLifecyclePolicy.shouldReconnect(
                    connectionEpoch.get(), failedEpoch, userClosed, connected
                )
            ) {
                return;
            }
            cancelReconnect();
            reconnectAttempt += 1;
            int exponent = Math.min(reconnectAttempt - 1, 4);
            delayMs = Math.min(MAX_RECONNECT_DELAY_MS, 500L << exponent);
            reconnectTask = scheduler.schedule(
                () -> {
                    boolean shouldOpen;
                    synchronized (connectionLock) {
                        shouldOpen =
                            CallLifecyclePolicy.shouldReconnect(
                                connectionEpoch.get(),
                                failedEpoch,
                                userClosed,
                                connected
                            );
                        if (shouldOpen) {
                            reconnectTask = null;
                        }
                    }
                    if (shouldOpen) {
                        openSocket(true, failedEpoch);
                    }
                },
                delayMs,
                TimeUnit.MILLISECONDS
            );
        }
        emitState("reconnecting");
    }

    private void closeCall() {
        closeCall(CLOSE_NORMAL, "hangup", "call was closed before it became ready");
    }

    private void closeCall(int closeCode, String closeReason, String pendingError) {
        PluginCall connectCall;
        WebSocket current;
        synchronized (connectionLock) {
            connectionEpoch.incrementAndGet();
            userClosed = true;
            connected = false;
            serverReady = false;
            connectCall = pendingConnectCall;
            pendingConnectCall = null;
            current = socket;
            socket = null;
        }
        if (connectCall != null) {
            connectCall.reject(pendingError);
        }
        cancelReconnect();
        cancelPing();
        pendingPings.clear();
        pttReleaseNs.clear();
        firstOutputReceivedNs.clear();
        firstPcmWriteNs.clear();
        stopCapture(false);
        long target = Math.max(generationCounter.get(), Math.max(activeInputGeneration, wireOutputGeneration));
        cancelledThroughGeneration.accumulateAndGet(target, Math::max);
        invalidatePlayback(target);
        if (current != null) {
            current.close(closeCode, closeReason);
        }
        emitState("closed");
    }

    private boolean isCurrentLocked(WebSocket candidate, long epoch) {
        return CallLifecyclePolicy.isCurrentConnection(
            connectionEpoch.get(), epoch, userClosed, socket == candidate
        );
    }

    private boolean isCurrentOrPendingLocked(WebSocket candidate, long epoch) {
        return CallLifecyclePolicy.isCurrentOrPendingConnection(
            connectionEpoch.get(),
            epoch,
            userClosed,
            socket == null || socket == candidate
        );
    }

    private boolean sendControl(JSONObject message) {
        synchronized (sendLock) {
            WebSocket current = socket;
            return connected && current != null && current.send(message.toString());
        }
    }

    private static Long positiveLong(Object value) {
        if (!(value instanceof Number number)) {
            return null;
        }
        double decimal = number.doubleValue();
        long integer = number.longValue();
        if (!Double.isFinite(decimal) || decimal != integer || integer < 1) {
            return null;
        }
        return integer;
    }

    private static byte[] readBounded(InputStream input, int limit)
        throws IOException {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[8 * 1024];
        int total = 0;
        while (true) {
            int read = input.read(buffer);
            if (read < 0) {
                return output.toByteArray();
            }
            total += read;
            if (total > limit) {
                throw new IOException("artifact exceeded size limit");
            }
            output.write(buffer, 0, read);
        }
    }

    private void cancelReconnect() {
        synchronized (connectionLock) {
            ScheduledFuture<?> task = reconnectTask;
            reconnectTask = null;
            if (task != null) {
                task.cancel(false);
            }
        }
    }

    private void cancelPing() {
        ScheduledFuture<?> task = pingTask;
        pingTask = null;
        if (task != null) {
            task.cancel(false);
        }
    }

    private void emitState(String state) {
        synchronized (connectionLock) {
            if (!CallLifecyclePolicy.shouldEmitState(userClosed, state)) {
                return;
            }
            JSObject event = new JSObject();
            event.put("state", state);
            event.put("callId", callId);
            event.put("reconnectAttempt", reconnectAttempt);
            event.put("generation", generationCounter.get());
            notifyListeners("state", event, true);
        }
    }

    private void emitControl(JSONObject message) {
        try {
            notifyListeners("control", JSObject.fromJSONObject(message));
        } catch (JSONException error) {
            emitError("control_event", "could not emit server control", false);
        }
    }

    private void emitQueueDepth(int depth, long generation) {
        JSObject event = new JSObject();
        event.put("depth", depth);
        event.put("generation", generation);
        notifyListeners("queueDepth", event);
    }

    private void emitError(String code, String message, boolean fatal) {
        JSObject event = new JSObject();
        event.put("code", code == null ? "call" : code);
        event.put("message", message == null ? "call transport error" : message);
        event.put("fatal", fatal);
        notifyListeners("error", event, true);
    }

    private static String normalizeWebSocketUrl(String raw) {
        String normalized = raw.trim();
        if (normalized.startsWith("http://")) {
            normalized = "ws://" + normalized.substring("http://".length());
        } else if (normalized.startsWith("https://")) {
            normalized = "wss://" + normalized.substring("https://".length());
        }
        if (!normalized.startsWith("ws://") && !normalized.startsWith("wss://")) {
            throw new IllegalArgumentException("url must use ws, wss, http, or https");
        }
        try {
            URI parsed = new URI(normalized);
            if (
                parsed.getUserInfo() != null ||
                !CallLifecyclePolicy.isAllowedCallEndpoint(
                    parsed.getHost(),
                    parsed.getPort(),
                    BuildConfig.SERENA_CALL_HOST,
                    BuildConfig.SERENA_CALL_PORT
                ) ||
                !"/ws/call".equals(parsed.getRawPath()) ||
                parsed.getRawQuery() != null ||
                parsed.getRawFragment() != null
            ) {
                throw new IllegalArgumentException(
                    "call url does not match the pinned Serena call endpoint"
                );
            }
        } catch (URISyntaxException error) {
            throw new IllegalArgumentException("call url is invalid");
        }
        return normalized;
    }

    private static long monotonicMicros() {
        return System.nanoTime() / 1_000L;
    }

    private static void putJson(JSONObject object, String key, Object value) {
        try {
            object.put(key, value);
        } catch (JSONException error) {
            throw new IllegalStateException("could not encode call control", error);
        }
    }

    private static JSONObject json(Object... values) {
        JSONObject object = new JSONObject();
        for (int index = 0; index + 1 < values.length; index += 2) {
            try {
                object.put(String.valueOf(values[index]), values[index + 1]);
            } catch (JSONException error) {
                throw new IllegalStateException("could not encode call control", error);
            }
        }
        return object;
    }

    private static ThreadFactory namedThreadFactory(String name) {
        return runnable -> {
            Thread thread = new Thread(runnable, name);
            thread.setDaemon(true);
            return thread;
        };
    }

    private final class PlaybackWork implements Runnable {
        private final long generation;
        private final Runnable work;
        private final AtomicBoolean finished = new AtomicBoolean();

        PlaybackWork(long generation, Runnable work) {
            this.generation = generation;
            this.work = work;
        }

        @Override
        public void run() {
            if (!finished.compareAndSet(false, true)) {
                return;
            }
            try {
                work.run();
            } finally {
                emitQueueDepth(
                    playbackQueueDepth.decrementAndGet(), generation
                );
            }
        }

        void discard() {
            if (finished.compareAndSet(false, true)) {
                emitQueueDepth(
                    playbackQueueDepth.decrementAndGet(), generation
                );
            }
        }
    }

    private static final class PingSample {
        final long monotonicNs;

        PingSample(long monotonicNs) {
            this.monotonicNs = monotonicNs;
        }
    }
}
