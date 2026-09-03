package sh.serena.app;

import android.os.Bundle;
import android.os.SystemClock;
import com.getcapacitor.BridgeActivity;
import sh.serena.app.call.SerenaCallPlugin;

public class MainActivity extends BridgeActivity {
    private static final long APP_STARTED_AT_ELAPSED_REALTIME_MS =
        SystemClock.elapsedRealtime();

    public static long appStartedAtElapsedRealtimeMs() {
        return APP_STARTED_AT_ELAPSED_REALTIME_MS;
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        registerPlugin(SerenaCallPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
