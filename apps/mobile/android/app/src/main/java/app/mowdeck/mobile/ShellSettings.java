package app.mowdeck.mobile;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;
import androidx.annotation.Nullable;
import com.getcapacitor.CapConfig;
import com.getcapacitor.Logger;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** The server origin, stored natively so it is known before the web view loads. */
final class ShellSettings {

    private static final String PREFS = "mowdeck_shell";
    private static final String KEY = "serverUrl";
    private static final String CONFIG_DIR = "mowdeck-shell";

    private ShellSettings() {}

    @Nullable
    static String serverUrl(Context context) {
        String value = prefs(context).getString(KEY, null);
        return value == null || value.isEmpty() ? null : value;
    }

    static void setServerUrl(Context context, @Nullable String origin) {
        SharedPreferences.Editor editor = prefs(context).edit();
        if (origin == null) {
            editor.remove(KEY);
        } else {
            editor.putString(KEY, origin);
        }
        editor.commit();
    }

    /** {@code scheme://host[:port]} of an http(s) URL without path, query or credentials. */
    @Nullable
    static String origin(@Nullable String raw) {
        if (raw == null) return null;
        Uri uri = Uri.parse(raw.trim());
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.ROOT);
        String host = uri.getHost();
        String path = uri.getPath();
        if (!scheme.equals("https") && !scheme.equals("http")) return null;
        if (host == null || host.isEmpty() || uri.getUserInfo() != null) return null;
        if (path != null && !path.isEmpty() && !path.equals("/")) return null;
        if (uri.getQuery() != null || uri.getFragment() != null) return null;
        String port = uri.getPort() == -1 ? "" : ":" + uri.getPort();
        return scheme + "://" + host.toLowerCase(Locale.ROOT) + port;
    }

    /**
     * The bundled capacitor.config.json with {@code server.url} set to the origin and its host
     * allowed for navigation, written to the app's files and loaded from there (the plugin
     * settings stay as configured). Null (first-launch screen) when that fails.
     */
    @Nullable
    static CapConfig configFor(Context context, String origin) {
        try {
            JSONObject json = new JSONObject(readAsset(context, "capacitor.config.json"));
            JSONObject server = json.optJSONObject("server");
            if (server == null) {
                server = new JSONObject();
                json.put("server", server);
            }
            server.put("url", origin);
            JSONArray allow = server.optJSONArray("allowNavigation");
            if (allow == null) allow = new JSONArray();
            allow.put(Uri.parse(origin).getHost());
            server.put("allowNavigation", allow);
            File dir = new File(context.getFilesDir(), CONFIG_DIR);
            if (!dir.isDirectory() && !dir.mkdirs()) throw new IOException("cannot create " + dir);
            try (OutputStream out = new FileOutputStream(new File(dir, "capacitor.config.json"))) {
                out.write(json.toString().getBytes(StandardCharsets.UTF_8));
            }
            return CapConfig.loadFromFile(context, dir.getAbsolutePath());
        } catch (IOException | JSONException ex) {
            Logger.error("MowDeckShell: cannot apply the server address, showing the first-launch screen", ex);
            return null;
        }
    }

    private static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private static String readAsset(Context context, String name) throws IOException {
        try (InputStream in = context.getAssets().open(name); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192];
            int read;
            while ((read = in.read(buffer)) != -1) out.write(buffer, 0, read);
            return out.toString(StandardCharsets.UTF_8.name());
        }
    }
}
