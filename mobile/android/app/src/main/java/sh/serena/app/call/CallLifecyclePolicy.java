package sh.serena.app.call;

import java.net.InetAddress;
import java.net.URI;
import java.net.URISyntaxException;
import java.net.UnknownHostException;
import java.util.Locale;
import java.util.Map;

final class CallLifecyclePolicy {
    private CallLifecyclePolicy() {}

    static boolean isAllowedCallEndpoint(
        String rawHost,
        int port,
        String configuredHost,
        int configuredPort
    ) {
        if (rawHost == null || configuredHost == null) {
            return false;
        }
        String host = rawHost.trim().toLowerCase();
        if (host.startsWith("[") && host.endsWith("]")) {
            host = host.substring(1, host.length() - 1);
        }
        String allowed = configuredHost.trim().toLowerCase();
        if (allowed.startsWith("[") && allowed.endsWith("]")) {
            allowed = allowed.substring(1, allowed.length() - 1);
        }
        return port == configuredPort &&
            host.equals(allowed) &&
            isTailscaleAddress(allowed);
    }

    static boolean isTailscaleAddress(String rawHost) {
        if (rawHost == null) {
            return false;
        }
        String host = rawHost.trim().toLowerCase();
        if (host.startsWith("[") && host.endsWith("]")) {
            host = host.substring(1, host.length() - 1);
        }
        if (host.isEmpty() || !host.matches("[0-9a-f:.]+")) {
            return false;
        }
        if (host.contains(".")) {
            if (host.contains(":")) {
                return false;
            }
            String[] octets = host.split("\\.", -1);
            if (octets.length != 4) {
                return false;
            }
            int[] values = new int[4];
            for (int index = 0; index < octets.length; index += 1) {
                if (!octets[index].matches("\\d{1,3}")) {
                    return false;
                }
                values[index] = Integer.parseInt(octets[index]);
                if (values[index] > 255) {
                    return false;
                }
            }
            return values[0] == 100 && values[1] >= 64 && values[1] <= 127;
        }
        if (!host.contains(":")) {
            return false;
        }
        try {
            byte[] address = InetAddress.getByName(host).getAddress();
            return address.length == 16 &&
                Byte.toUnsignedInt(address[0]) == 0xfd &&
                Byte.toUnsignedInt(address[1]) == 0x7a &&
                Byte.toUnsignedInt(address[2]) == 0x11 &&
                Byte.toUnsignedInt(address[3]) == 0x5c &&
                Byte.toUnsignedInt(address[4]) == 0xa1 &&
                Byte.toUnsignedInt(address[5]) == 0xe0;
        } catch (UnknownHostException ignored) {
            return false;
        }
    }

    static String pinnedWebSocketUrl(String host, int port) {
        String normalized = host == null ? "" : host.trim();
        if (!isTailscaleAddress(normalized) || port < 1 || port > 65535) {
            throw new IllegalArgumentException("invalid pinned call endpoint");
        }
        if (normalized.startsWith("[") && normalized.endsWith("]")) {
            normalized = normalized.substring(1, normalized.length() - 1);
        }
        String authority = normalized.contains(":")
            ? "[" + normalized + "]"
            : normalized;
        return "ws://" + authority + ":" + port + "/ws/call";
    }

    static String normalizeTailnetPath(String path) {
        String normalized = path == null
            ? ""
            : path.trim().toLowerCase(Locale.ROOT);
        return switch (normalized) {
            case "direct", "relay" -> normalized;
            default -> "unknown";
        };
    }

    static String resolveTailnetPath(String configuredPath, String probedPath) {
        String configured = normalizeTailnetPath(configuredPath);
        return "unknown".equals(configured)
            ? normalizeTailnetPath(probedPath)
            : configured;
    }

    static String resolveTailnetPathSource(
        String configuredPath,
        String probedPath,
        String probedSource
    ) {
        if (!"unknown".equals(normalizeTailnetPath(configuredPath))) {
            return "client_config";
        }
        if (
            !"unknown".equals(normalizeTailnetPath(probedPath)) &&
            "tailscale_probe".equals(probedSource)
        ) {
            return "tailscale_probe";
        }
        return "unknown";
    }

    static double networkRttMillis(long elapsedNs, long serverProcessingUs) {
        double elapsedMs = Math.max(0L, elapsedNs) / 1_000_000.0;
        double serverMs = Math.max(0L, serverProcessingUs) / 1_000.0;
        return Math.max(0.0, elapsedMs - serverMs);
    }

    static String normalizeServerSampleId(String rawSampleId) {
        String sampleId = rawSampleId == null ? "" : rawSampleId.trim();
        return sampleId.isEmpty() || sampleId.length() > 128 ? "" : sampleId;
    }

    static boolean shouldSendPushToTalkEnd(
        boolean endRequested,
        long activeGeneration,
        long generation,
        boolean connected,
        long cancelledThroughGeneration
    ) {
        return endRequested &&
            activeGeneration == generation &&
            connected &&
            generation > cancelledThroughGeneration;
    }

    static boolean generationIsActive(
        long generation,
        long activeGeneration,
        long cancelledThroughGeneration
    ) {
        return generation == activeGeneration &&
            generation > cancelledThroughGeneration;
    }

    static boolean canAnnouncePushToTalk(
        boolean connected,
        boolean socketPresent,
        boolean captureRunning,
        long generation,
        long activeGeneration,
        long cancelledThroughGeneration
    ) {
        return captureRunning && canSendGeneration(
            connected,
            socketPresent,
            generation,
            activeGeneration,
            cancelledThroughGeneration
        );
    }

    static boolean canBeginPushToTalk(
        boolean connected,
        boolean serverReady,
        boolean socketPresent
    ) {
        return connected && serverReady && socketPresent;
    }

    static boolean canSendGeneration(
        boolean connected,
        boolean socketPresent,
        long generation,
        long activeGeneration,
        long cancelledThroughGeneration
    ) {
        return connected &&
            socketPresent &&
            generationIsActive(
                generation, activeGeneration, cancelledThroughGeneration
            );
    }

    static boolean shouldAckPlaybackHead(
        long generation,
        long polledGeneration,
        long playbackGeneration,
        long cancelledThroughGeneration,
        boolean trackPresent,
        long playbackHeadPosition
    ) {
        return generation == polledGeneration &&
            generationIsActive(
                generation, playbackGeneration, cancelledThroughGeneration
            ) &&
            trackPresent &&
            playbackHeadPosition > 0;
    }

    static boolean isCurrentConnection(
        long currentEpoch,
        long callbackEpoch,
        boolean userClosed,
        boolean socketMatches
    ) {
        return currentEpoch == callbackEpoch && !userClosed && socketMatches;
    }

    static boolean isCurrentOrPendingConnection(
        long currentEpoch,
        long callbackEpoch,
        boolean userClosed,
        boolean socketAbsentOrMatches
    ) {
        return currentEpoch == callbackEpoch &&
            !userClosed &&
            socketAbsentOrMatches;
    }

    static boolean canOpenSocket(
        long currentEpoch,
        long expectedEpoch,
        boolean userClosed
    ) {
        return !userClosed &&
            (expectedEpoch < 0 || currentEpoch == expectedEpoch);
    }

    static boolean shouldReconnect(
        long currentEpoch,
        long failedEpoch,
        boolean userClosed,
        boolean connected
    ) {
        return currentEpoch == failedEpoch && !userClosed && !connected;
    }

    static boolean shouldResetPlayback(
        long cancelledThroughGeneration,
        long playbackGeneration
    ) {
        return playbackGeneration < 0 ||
            playbackGeneration <= cancelledThroughGeneration;
    }

    static long nextFreshGeneration(
        long currentGeneration,
        long cancelledThroughGeneration
    ) {
        return Math.max(currentGeneration, cancelledThroughGeneration) + 1;
    }

    static boolean shouldCancelFailedCapture(
        boolean abnormalExit,
        long generation,
        long activeGeneration,
        long cancelledThroughGeneration
    ) {
        return abnormalExit && generationIsActive(
            generation, activeGeneration, cancelledThroughGeneration
        );
    }

    static boolean shouldCloseForBackground(
        boolean permissionRequestInFlight,
        boolean userClosed,
        boolean connected,
        boolean socketPresent,
        boolean connectionPending
    ) {
        return !permissionRequestInFlight &&
            !userClosed &&
            (connected || socketPresent || connectionPending);
    }

    static boolean shouldEmitState(boolean userClosed, String state) {
        return !userClosed || "closed".equals(state);
    }

    static boolean isReplayableJobEvent(String type) {
        return "job.accepted".equals(type) ||
            "job.progress".equals(type) ||
            "artifact.ready".equals(type) ||
            "job.failed".equals(type);
    }

    static long acknowledgedJobCursor(
        long current,
        String type,
        long eventSequence,
        boolean acknowledgementSent
    ) {
        if (
            !acknowledgementSent ||
            !isReplayableJobEvent(type) ||
            eventSequence < 1
        ) {
            return current;
        }
        return Math.max(current, eventSequence);
    }

    static boolean isValidArtifactOpen(
        long eventSequence,
        String jobId,
        String receipt
    ) {
        return eventSequence > 0 &&
            jobId != null &&
            !jobId.isBlank() &&
            jobId.length() <= 128 &&
            isValidArtifactReceipt(receipt);
    }

    static boolean isValidArtifactReceipt(String receipt) {
        return receipt != null &&
            receipt.length() >= 16 &&
            receipt.length() <= 2_048 &&
            receipt.matches("v1\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+");
    }

    static void rememberArtifactFetch(
        Map<String, Long> receipts,
        String receipt,
        long nowMs,
        long ttlMs,
        int limit
    ) {
        if (!isValidArtifactReceipt(receipt) || ttlMs < 1 || limit < 1) {
            return;
        }
        receipts.entrySet().removeIf(
            item -> item.getValue() == null ||
                item.getValue() > nowMs ||
                nowMs - item.getValue() > ttlMs
        );
        while (receipts.size() >= limit) {
            String oldest = receipts.entrySet().stream()
                .min(Map.Entry.comparingByValue())
                .map(Map.Entry::getKey)
                .orElse(null);
            if (oldest == null) {
                break;
            }
            receipts.remove(oldest);
        }
        receipts.put(receipt, nowMs);
    }

    static boolean consumeArtifactFetch(
        Map<String, Long> receipts,
        String receipt,
        long nowMs,
        long ttlMs
    ) {
        Long fetchedAt = receipts.remove(receipt);
        return fetchedAt != null &&
            fetchedAt <= nowMs &&
            nowMs - fetchedAt <= ttlMs;
    }

    static boolean isAllowedArtifactUrl(String socketUrl, String artifactUrl) {
        try {
            URI socket = new URI(socketUrl);
            URI artifact = new URI(artifactUrl);
            String expectedScheme = switch (socket.getScheme().toLowerCase()) {
                case "ws" -> "http";
                case "wss" -> "https";
                default -> "";
            };
            String path = artifact.getRawPath();
            return !expectedScheme.isEmpty() &&
                expectedScheme.equalsIgnoreCase(artifact.getScheme()) &&
                socket.getHost() != null &&
                socket.getHost().equalsIgnoreCase(artifact.getHost()) &&
                effectivePort(socket) == effectivePort(artifact) &&
                artifact.getRawUserInfo() == null &&
                artifact.getRawQuery() == null &&
                artifact.getRawFragment() == null &&
                path != null &&
                path.matches("/artifacts/[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+");
        } catch (NullPointerException | URISyntaxException error) {
            return false;
        }
    }

    private static int effectivePort(URI uri) {
        if (uri.getPort() >= 0) {
            return uri.getPort();
        }
        return switch (uri.getScheme().toLowerCase()) {
            case "ws", "http" -> 80;
            case "wss", "https" -> 443;
            default -> -1;
        };
    }

    static void clearGenerationTiming(
        long generation,
        Map<Long, Long> pttReleaseNs,
        Map<Long, Long> firstOutputReceivedNs,
        Map<Long, Long> firstPcmWriteNs
    ) {
        pttReleaseNs.remove(generation);
        firstOutputReceivedNs.remove(generation);
        firstPcmWriteNs.remove(generation);
    }

    static void clearGenerationTimingThrough(
        long generation,
        Map<Long, Long> pttReleaseNs,
        Map<Long, Long> firstOutputReceivedNs,
        Map<Long, Long> firstPcmWriteNs
    ) {
        pttReleaseNs.keySet().removeIf(item -> item <= generation);
        firstOutputReceivedNs.keySet().removeIf(item -> item <= generation);
        firstPcmWriteNs.keySet().removeIf(item -> item <= generation);
    }
}
