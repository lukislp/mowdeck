package app.mowdeck.mobile;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class AuthSessionUrlsTest {

    private static final String SERVER = "https://mowdeck.example.org";

    @Test
    public void opensOnlyPagesOfTheServer() {
        assertTrue(AuthSessionUrls.sameOrigin(SERVER + "/login?app_request=x&app_state=y", SERVER));
        assertTrue(AuthSessionUrls.sameOrigin("HTTPS://MowDeck.Example.org/login", SERVER));
        assertTrue(AuthSessionUrls.sameOrigin("https://m.lan:8443/login", "https://m.lan:8443"));
    }

    @Test
    public void refusesOtherSitesSchemesAndCredentials() {
        assertFalse(AuthSessionUrls.sameOrigin("https://evil.example.net/login", SERVER));
        assertFalse(AuthSessionUrls.sameOrigin("https://mowdeck.example.org.evil.net/", SERVER));
        assertFalse(AuthSessionUrls.sameOrigin("https://mowdeck.example.org:444/", SERVER));
        assertFalse(AuthSessionUrls.sameOrigin("http://mowdeck.example.org/login", SERVER));
        assertFalse(AuthSessionUrls.sameOrigin("javascript:alert(1)", SERVER));
        assertFalse(AuthSessionUrls.sameOrigin("https://u:p@mowdeck.example.org/", SERVER));
        assertFalse(AuthSessionUrls.sameOrigin("not a url", SERVER));
        assertFalse(AuthSessionUrls.sameOrigin(SERVER + "/login", null));
    }
}
