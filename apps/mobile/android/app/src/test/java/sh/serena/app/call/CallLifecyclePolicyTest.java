package sh.serena.app.call;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import org.junit.Test;

public class CallLifecyclePolicyTest {
    @Test
    public void callTargetIsPinnedToTheBuiltEndpoint() {
        String host = "100.116.233.56";
        int port = 8766;
        assertTrue(
            CallLifecyclePolicy.isAllowedCallEndpoint(host, port, host, port)
        );
        assertFalse(
            CallLifecyclePolicy.isAllowedCallEndpoint(
                "100.78.252.39", port, host, port
            )
        );
        assertFalse(
            CallLifecyclePolicy.isAllowedCallEndpoint(host, 8080, host, port)
        );
        assertFalse(
            CallLifecyclePolicy.isAllowedCallEndpoint(
                "example.com", port, host, port
            )
        );
        assertFalse(
            CallLifecyclePolicy.isAllowedCallEndpoint(
                "8.8.8.8", port, "8.8.8.8", port
            )
        );
    }

    @Test
    public void cleartextCallTargetsStayInsideTailscaleAddressSpace() {
        assertTrue(CallLifecyclePolicy.isTailscaleAddress("100.64.0.0"));
        assertTrue(CallLifecyclePolicy.isTailscaleAddress("100.127.255.255"));
        assertTrue(
            CallLifecyclePolicy.isTailscaleAddress("fd7a:115c:a1e0::1")
        );
        assertTrue(
            CallLifecyclePolicy.isTailscaleAddress("[FD7A:115C:A1E0::99]")
        );
        assertFalse(CallLifecyclePolicy.isTailscaleAddress("100.63.255.255"));
        assertFalse(CallLifecyclePolicy.isTailscaleAddress("100.128.0.0"));
        assertFalse(CallLifecyclePolicy.isTailscaleAddress("8.8.8.8"));
        assertFalse(CallLifecyclePolicy.isTailscaleAddress("example.com"));
        assertFalse(CallLifecyclePolicy.isTailscaleAddress("dead.beef"));
        assertFalse(CallLifecyclePolicy.isTailscaleAddress("face"));
        assertFalse(CallLifecyclePolicy.isTailscaleAddress("fd7a:115c:a1df::1"));
    }

    @Test
    public void pinnedEndpointFormatsIpv4AndIpv6WithoutDns() {
        assertEquals(
            "ws://100.116.233.56:8766/ws/call",
            CallLifecyclePolicy.pinnedWebSocketUrl("100.116.233.56", 8766)
        );
        assertEquals(
            "ws://[fd7a:115c:a1e0::99]:8766/ws/call",
            CallLifecyclePolicy.pinnedWebSocketUrl(
                "[fd7a:115c:a1e0::99]", 8766
            )
        );
    }

    @Test
    public void rttRouteUsesServerProbeUnlessExplicitlyConfigured() {
        assertEquals(
            "relay",
            CallLifecyclePolicy.resolveTailnetPath("unknown", "relay")
        );
        assertEquals(
            "tailscale_probe",
            CallLifecyclePolicy.resolveTailnetPathSource(
                "unknown", "relay", "tailscale_probe"
            )
        );
        assertEquals(
            "direct",
            CallLifecyclePolicy.resolveTailnetPath("direct", "relay")
        );
        assertEquals(
            "client_config",
            CallLifecyclePolicy.resolveTailnetPathSource(
                "direct", "relay", "tailscale_probe"
            )
        );
        assertEquals(
            "unknown",
            CallLifecyclePolicy.resolveTailnetPathSource(
                "unknown", "direct", "unknown"
            )
        );
        assertEquals(
            "direct",
            CallLifecyclePolicy.normalizeTailnetPath(" DIRECT ")
        );
        assertEquals(
            24.5,
            CallLifecyclePolicy.networkRttMillis(30_000_000L, 5_500L),
            0.001
        );
        assertEquals(
            0.0,
            CallLifecyclePolicy.networkRttMillis(1_000_000L, 5_000L),
            0.001
        );
        assertEquals(
            "server-sample",
            CallLifecyclePolicy.normalizeServerSampleId(" server-sample ")
        );
        assertEquals("", CallLifecyclePolicy.normalizeServerSampleId(""));
        assertEquals(
            "",
            CallLifecyclePolicy.normalizeServerSampleId("x".repeat(129))
        );
    }

    @Test
    public void pushToTalkEndRequiresRecorderOwnershipAndLiveGeneration() {
        assertTrue(
            CallLifecyclePolicy.canBeginPushToTalk(true, true, true)
        );
        assertFalse(
            CallLifecyclePolicy.canBeginPushToTalk(true, false, true)
        );
        assertFalse(
            CallLifecyclePolicy.canBeginPushToTalk(false, true, true)
        );
        assertTrue(
            CallLifecyclePolicy.shouldSendPushToTalkEnd(
                true, 7, 7, true, 6
            )
        );
        assertFalse(
            CallLifecyclePolicy.shouldSendPushToTalkEnd(
                true, 8, 7, true, 6
            )
        );
        assertFalse(
            CallLifecyclePolicy.shouldSendPushToTalkEnd(
                true, 7, 7, true, 7
            )
        );
        assertFalse(
            CallLifecyclePolicy.shouldSendPushToTalkEnd(
                true, 7, 7, false, 6
            )
        );
        assertTrue(
            CallLifecyclePolicy.canAnnouncePushToTalk(
                true, true, true, 7, 7, 6
            )
        );
        assertFalse(
            CallLifecyclePolicy.canAnnouncePushToTalk(
                true, true, true, 7, 7, 7
            )
        );
        assertTrue(
            CallLifecyclePolicy.canSendGeneration(
                true, true, 7, 7, 6
            )
        );
        assertFalse(
            CallLifecyclePolicy.canSendGeneration(
                true, true, 7, 7, 7
            )
        );
    }

    @Test
    public void playbackAckRequiresHeadAdvanceOnCurrentUncancelledGeneration() {
        assertTrue(
            CallLifecyclePolicy.shouldAckPlaybackHead(
                4, 4, 4, 3, true, 1
            )
        );
        assertFalse(
            CallLifecyclePolicy.shouldAckPlaybackHead(
                4, 4, 4, 3, true, 0
            )
        );
        assertFalse(
            CallLifecyclePolicy.shouldAckPlaybackHead(
                4, 4, 5, 3, true, 1
            )
        );
        assertFalse(
            CallLifecyclePolicy.shouldAckPlaybackHead(
                4, 4, 4, 4, true, 1
            )
        );
    }

    @Test
    public void staleConnectionAndReconnectEpochsAreRejected() {
        assertTrue(
            CallLifecyclePolicy.isCurrentConnection(12, 12, false, true)
        );
        assertFalse(
            CallLifecyclePolicy.isCurrentConnection(13, 12, false, true)
        );
        assertFalse(
            CallLifecyclePolicy.isCurrentConnection(12, 12, true, true)
        );
        assertFalse(
            CallLifecyclePolicy.isCurrentConnection(12, 12, false, false)
        );
        assertTrue(
            CallLifecyclePolicy.isCurrentOrPendingConnection(
                12, 12, false, true
            )
        );
        assertFalse(
            CallLifecyclePolicy.isCurrentOrPendingConnection(
                13, 12, false, true
            )
        );
        assertTrue(CallLifecyclePolicy.canOpenSocket(12, -1, false));
        assertTrue(CallLifecyclePolicy.canOpenSocket(12, 12, false));
        assertFalse(CallLifecyclePolicy.canOpenSocket(13, 12, false));
        assertFalse(CallLifecyclePolicy.canOpenSocket(12, 12, true));
        assertTrue(CallLifecyclePolicy.shouldReconnect(12, 12, false, false));
        assertFalse(CallLifecyclePolicy.shouldReconnect(13, 12, false, false));
        assertFalse(CallLifecyclePolicy.shouldReconnect(12, 12, true, false));
        assertFalse(CallLifecyclePolicy.shouldReconnect(12, 12, false, true));
    }

    @Test
    public void cancellationRetiresOnlyTargetedTimingState() {
        Map<Long, Long> release = timings();
        Map<Long, Long> output = timings();
        Map<Long, Long> write = timings();

        CallLifecyclePolicy.clearGenerationTiming(
            2, release, output, write
        );
        assertTrue(release.containsKey(1L));
        assertFalse(release.containsKey(2L));
        assertTrue(release.containsKey(3L));
        assertFalse(output.containsKey(2L));
        assertFalse(write.containsKey(2L));

        CallLifecyclePolicy.clearGenerationTimingThrough(
            2, release, output, write
        );
        assertFalse(release.containsKey(1L));
        assertTrue(release.containsKey(3L));
        assertFalse(output.containsKey(1L));
        assertTrue(output.containsKey(3L));
        assertFalse(write.containsKey(1L));
        assertTrue(write.containsKey(3L));
        assertTrue(CallLifecyclePolicy.shouldResetPlayback(2, -1));
        assertTrue(CallLifecyclePolicy.shouldResetPlayback(2, 2));
        assertFalse(CallLifecyclePolicy.shouldResetPlayback(2, 3));
    }

    @Test
    public void backgroundClosesLiveOrConnectingCallButNotPermissionPrompt() {
        assertTrue(
            CallLifecyclePolicy.shouldCloseForBackground(
                false, false, true, true, false
            )
        );
        assertTrue(
            CallLifecyclePolicy.shouldCloseForBackground(
                false, false, false, false, true
            )
        );
        assertFalse(
            CallLifecyclePolicy.shouldCloseForBackground(
                true, false, true, true, false
            )
        );
        assertFalse(
            CallLifecyclePolicy.shouldCloseForBackground(
                false, true, true, true, false
            )
        );
        assertFalse(
            CallLifecyclePolicy.shouldCloseForBackground(
                false, false, false, false, false
            )
        );
        assertTrue(CallLifecyclePolicy.shouldEmitState(false, "open"));
        assertTrue(CallLifecyclePolicy.shouldEmitState(true, "closed"));
        assertFalse(CallLifecyclePolicy.shouldEmitState(true, "reconnecting"));
    }

    @Test
    public void freshConnectionBaselineIsNewerThanEveryCancelledGeneration() {
        assertTrue(CallLifecyclePolicy.nextFreshGeneration(0, 0) > 0);
        assertTrue(CallLifecyclePolicy.nextFreshGeneration(4, 9) > 9);
        assertTrue(CallLifecyclePolicy.nextFreshGeneration(12, 7) > 12);
    }

    @Test
    public void jobReplayCursorAdvancesOnlyAfterNativeAcknowledgement() {
        assertTrue(CallLifecyclePolicy.isReplayableJobEvent("job.accepted"));
        assertTrue(CallLifecyclePolicy.isReplayableJobEvent("job.progress"));
        assertTrue(CallLifecyclePolicy.isReplayableJobEvent("artifact.ready"));
        assertTrue(CallLifecyclePolicy.isReplayableJobEvent("job.failed"));
        assertFalse(CallLifecyclePolicy.isReplayableJobEvent("audio.start"));
        assertEquals(
            9,
            CallLifecyclePolicy.acknowledgedJobCursor(
                7, "artifact.ready", 9, true
            )
        );
        assertEquals(
            7,
            CallLifecyclePolicy.acknowledgedJobCursor(
                7, "artifact.ready", 9, false
            )
        );
        assertEquals(
            7,
            CallLifecyclePolicy.acknowledgedJobCursor(
                7, "audio.start", 9, true
            )
        );
        String receipt = "v1.payload_with-enough.signature_with-enough";
        assertTrue(CallLifecyclePolicy.isValidArtifactOpen(9, "job-1", receipt));
        assertFalse(CallLifecyclePolicy.isValidArtifactOpen(0, "job-1", receipt));
        assertFalse(CallLifecyclePolicy.isValidArtifactOpen(9, "", receipt));
        assertFalse(CallLifecyclePolicy.isValidArtifactOpen(9, "job-1", "forged"));
        Map<String, Long> fetched = new ConcurrentHashMap<>();
        CallLifecyclePolicy.rememberArtifactFetch(
            fetched, receipt, 1_000, 300_000, 4
        );
        assertTrue(
            CallLifecyclePolicy.consumeArtifactFetch(
                fetched, receipt, 1_100, 300_000
            )
        );
        assertFalse(
            CallLifecyclePolicy.consumeArtifactFetch(
                fetched, receipt, 1_200, 300_000
            )
        );
        CallLifecyclePolicy.rememberArtifactFetch(
            fetched, receipt, 1_000, 300_000, 4
        );
        assertFalse(
            CallLifecyclePolicy.consumeArtifactFetch(
                fetched, receipt, 301_001, 300_000
            )
        );
        assertTrue(
            CallLifecyclePolicy.isAllowedArtifactUrl(
                "ws://100.116.233.56:8766/ws/call",
                "http://100.116.233.56:8766/artifacts/abc_123.def-456"
            )
        );
        assertFalse(
            CallLifecyclePolicy.isAllowedArtifactUrl(
                "ws://100.116.233.56:8766/ws/call",
                "http://100.78.2.3:8766/artifacts/abc.def"
            )
        );
        assertFalse(
            CallLifecyclePolicy.isAllowedArtifactUrl(
                "ws://100.116.233.56:8766/ws/call",
                "http://100.116.233.56:8766/artifacts/abc.def?token=leak"
            )
        );
    }

    @Test
    public void failedCaptureCancelsOnlyItsStillLiveGeneration() {
        assertTrue(
            CallLifecyclePolicy.shouldCancelFailedCapture(true, 7, 7, 6)
        );
        assertFalse(
            CallLifecyclePolicy.shouldCancelFailedCapture(false, 7, 7, 6)
        );
        assertFalse(
            CallLifecyclePolicy.shouldCancelFailedCapture(true, 7, 8, 6)
        );
        assertFalse(
            CallLifecyclePolicy.shouldCancelFailedCapture(true, 7, 7, 7)
        );
    }

    private static Map<Long, Long> timings() {
        Map<Long, Long> values = new ConcurrentHashMap<>();
        values.put(1L, 10L);
        values.put(2L, 20L);
        values.put(3L, 30L);
        return values;
    }
}
