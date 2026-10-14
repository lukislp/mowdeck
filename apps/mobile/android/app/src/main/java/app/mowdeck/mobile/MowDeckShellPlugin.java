package app.mowdeck.mobile;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * {@code MowDeckShell}: stores the server origin and restarts the activity with it (see
 * apps/mobile/src/shell.ts for the JavaScript side).
 */
@CapacitorPlugin(name = "MowDeckShell")
public class MowDeckShellPlugin extends Plugin {

    @PluginMethod
    public void getServerUrl(PluginCall call) {
        JSObject result = new JSObject();
        result.put("url", ShellSettings.serverUrl(getContext()));
        call.resolve(result);
    }

    @PluginMethod
    public void setServerUrl(PluginCall call) {
        String origin = ShellSettings.origin(call.getString("url"));
        if (origin == null) {
            call.reject("expected an http(s) origin");
            return;
        }
        ShellSettings.setServerUrl(getContext(), origin);
        call.resolve();
        restart();
    }

    @PluginMethod
    public void resetServerUrl(PluginCall call) {
        ShellSettings.setServerUrl(getContext(), null);
        call.resolve();
        restart();
    }

    @PluginMethod
    public void reload(PluginCall call) {
        call.resolve();
        restart();
    }

    /** A recreated activity reads the stored origin again (a running bridge's config is fixed). */
    private void restart() {
        getActivity().runOnUiThread(() -> getActivity().recreate());
    }
}
