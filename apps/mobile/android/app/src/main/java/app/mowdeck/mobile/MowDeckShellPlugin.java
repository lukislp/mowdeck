package app.mowdeck.mobile;

import android.content.ActivityNotFoundException;
import android.net.Uri;
import androidx.browser.customtabs.CustomTabsIntent;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * {@code MowDeckShell}: stores the server origin and restarts the activity with it, and opens a
 * sign-in page of the server in a Custom Tab ({@code openAuthSession}, passkeys; ADR 0050). The
 * JavaScript side is apps/web/src/lib/native/ (shell.svelte.ts, handoff.ts).
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

    /**
     * Open {@code url} (a page of the configured server) in a Custom Tab: the system browser has
     * the user's passkeys for any origin, the web view has not without Digital Asset Links.
     * Resolves at once; the page returns the result as an {@code intent:} URL for this package,
     * which arrives as the deep link {@code mowdeck://auth/callback?...} ({@code appUrlOpen}).
     * The activity is {@code singleTask}, so the tab above it closes when it comes back.
     */
    @PluginMethod
    public void openAuthSession(PluginCall call) {
        String raw = call.getString("url");
        String server = ShellSettings.serverUrl(getContext());
        if (!AuthSessionUrls.sameOrigin(raw, server)) {
            call.reject("expected a page of the MowDeck server");
            return;
        }
        String scheme = call.getString("callbackScheme", "mowdeck");
        if (!"mowdeck".equals(scheme)) {
            call.reject("unsupported callback scheme");
            return;
        }
        getActivity()
            .runOnUiThread(() -> {
                try {
                    CustomTabsIntent tab = new CustomTabsIntent.Builder().setShowTitle(true).build();
                    tab.launchUrl(getActivity(), Uri.parse(raw));
                    call.resolve();
                } catch (ActivityNotFoundException e) {
                    call.reject("no browser to sign in with", "failed", e);
                }
            });
    }

    /** A recreated activity reads the stored origin again (a running bridge's config is fixed). */
    private void restart() {
        getActivity().runOnUiThread(() -> getActivity().recreate());
    }
}
