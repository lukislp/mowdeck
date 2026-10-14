package app.mowdeck.mobile;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;

/**
 * The shell's activity (ADR 0036): loads the web app from the server the user chose on the
 * bundled first-launch screen. The stored origin becomes Capacitor's {@code server.url}, so the
 * web app runs on its own origin (the HttpOnly refresh cookie keeps working) and the native
 * bridge is injected into it. Without a stored origin the bundled first-launch screen is shown.
 */
public class MainActivity extends BridgeActivity {

    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(MowDeckShellPlugin.class);
        String server = ShellSettings.serverUrl(this);
        if (server != null) {
            config = ShellSettings.configFor(this, server);
        }
        super.onCreate(savedInstanceState);
    }
}
