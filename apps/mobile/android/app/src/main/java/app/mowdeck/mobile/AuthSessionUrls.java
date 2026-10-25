package app.mowdeck.mobile;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.Locale;

/**
 * Which pages the shell opens for a sign-in in the system browser (plain Java, unit tested):
 * only http(s) pages of the configured MowDeck server, never another site.
 */
final class AuthSessionUrls {

    private AuthSessionUrls() {}

    /** True when {@code raw} is an http(s) URL on the origin {@code server} (scheme://host[:port]). */
    static boolean sameOrigin(String raw, String server) {
        if (raw == null || server == null) return false;
        URI uri;
        try {
            uri = new URI(raw.trim());
        } catch (URISyntaxException e) {
            return false;
        }
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.ROOT);
        String host = uri.getHost();
        if (!scheme.equals("https") && !scheme.equals("http")) return false;
        if (host == null || host.isEmpty() || uri.getRawUserInfo() != null) return false;
        String port = uri.getPort() == -1 ? "" : ":" + uri.getPort();
        return (scheme + "://" + host.toLowerCase(Locale.ROOT) + port).equals(server);
    }
}
