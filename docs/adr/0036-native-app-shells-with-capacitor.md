# ADR 0036: Native app shells with Capacitor, loading the web app from the server

## Status

Accepted (2026-10-11)

## Context

The owner wants MowDeck as an app on the iPhone and on Android, built from the same TypeScript code base as the web app (ADR 0003, ADR 0020) and installed the way their other project (StudyLife, .NET MAUI) is: iOS builds go straight from the Mac to the phone with a paid Apple developer account (no TestFlight, no App Store), Android gets an APK attached to the GitHub release, and notifications must work **without Firebase**.

The forces:

- **Authentication.** The web app and the API share one origin behind Traefik (ADR 0008, `infra/compose/docker-compose.yml`, no CORS). The refresh token lives only in Identity's HttpOnly cookie `mowdeck_refresh` on that origin, access tokens only in memory (ADR 0021). A web view showing a bundled copy of the web app runs on another origin (`capacitor://localhost`, `https://localhost`): the cookie would be third-party, and every service would need CORS with credentials.
- **Updates.** The server is updated often; a bundled web app would need a new app build and install per server release, and an app older or newer than its server breaks the generated API types.
- **Notifications.** Web Push does not reach a WKWebView. iOS background delivery needs APNs; on Android the only system push transport is Firebase Cloud Messaging, which the owner rejects.
- **Internal servers.** A MowDeck server usually lives in the home network or behind a VPN, often with a private CA or plain HTTP in the LAN, and its address differs per installation.

## Decision

**Capacitor 8 shells (`apps/mobile`) that load the web app from the owner's MowDeck server** (option A). The shells bundle only two small pages: the first-launch screen and the offline page.

- **Server address at runtime.** Capacitor's `server.url` is fixed when the bridge starts, so the shells set it natively: `MainViewController` (iOS, overrides `instanceDescriptor()`) and `MainActivity` (Android, builds a `CapConfig` from the bundled `capacitor.config.json` plus the stored origin) read the origin from native storage (UserDefaults, SharedPreferences) before the web view loads, set it as `server.url` and add its host to `allowNavigation`. The bundled first-launch screen (`www/index.html`) asks for the address, checks `<origin>/manifest.webmanifest` through Capacitor's native HTTP (the page's own origin would be blocked by CORS) and hands the origin to the shell's own plugin `MowDeckShell` (`setServerUrl`, `resetServerUrl`, `getServerUrl`, `reload`), which stores it and restarts the bridge (iOS: a new root view controller; Android: `recreate()`). `server.errorPath` shows `offline.html` (retry, change server) when the server cannot be reached. The settings page of the web app offers "change server".
- **Plugins on remote content** (verified in the Capacitor 8.5 sources): iOS injects the bridge as a `WKUserScript` into every main-frame document and accepts its messages from any page the web view shows; Android injects it with `addDocumentStartJavaScript` for the app origin, which is the server origin once `server.url` is set, and its `WebViewLocalServer` does not proxy remote requests on a current WebView (the request goes straight to the server, cookies are first-party). So every plugin works on the remote web app.
- **The web app detects the shell** through the injected `window.Capacitor` (`isNativePlatform()`, `getPlatform()`, `PluginHeaders`) and calls plugins through the bridge's `nativePromise` and `addListener`; it does not bundle `@capacitor/core`, and all Capacitor packages are dependencies of `apps/mobile` only. The bridge module is `apps/web/src/lib/native/` (detection, deep links, notification taps, local notifications, badge, APNs registration, server change); in a browser it is inert. Inside the shell the Web Push panel gives way to the app panel.
- **Notifications without Firebase:**
  1. *Foreground on both platforms:* the inbox store (polled every 60 s while the app runs) feeds `@capacitor/local-notifications` with every new unread item (baseline on first start, ids remembered, no repeats); the icon badge (`@capawesome/capacitor-badge`) follows the unread count.
  2. *iOS background:* a new Notifications channel `apns` sends directly to Apple with the owner's APNs auth key (token-based, ES256 JWT, HTTP/2; `MOWDECK_APNS_KEY_FILE`, `_KEY_ID`, `_TEAM_ID`, `_BUNDLE_ID`, `_ENVIRONMENT`), with the same preferences, quiet hours and alarm rules as Web Push and ntfy. The shell registers through `@capacitor/push-notifications` and posts the device token to `POST /api/v1/notifications/me/push/apns`; logout deletes it (`DELETE .../me/push/apns/{id}`); Apple's `Unregistered` or `BadDeviceToken` deletes it as well. Theft alarms are `time-sensitive`. With APNs on, the local notifications stay off (no duplicates).
  3. *Android background:* ntfy (already a channel, ADR 0022) in the ntfy app; the shell links to the ntfy app and offers the `ntfy://<server>/<topic>` subscribe link. The push plugin is excluded from the Android build (`android.includePlugins`), so the APK carries no Firebase code. With an ntfy target set up, the app's local notifications stay off.
- **Deep links** use the custom scheme `mowdeck://` (`mowdeck://mowers/<id>` opens `/mowers/<id>`); notification taps open the payload's app path through the same same-origin rule as Web Push. Universal links / App Links are not possible: Apple fetches `apple-app-site-association` only through its CDN from a public domain, and Android verifies `assetlinks.json` the same way; an internal server can never pass.
- **Distribution.** iOS: `apps/mobile/scripts/provision.sh` (automatic signing with `-allowProvisioningUpdates -allowProvisioningDeviceRegistration` creates the development certificate, registers the iPhone and the profile with Push and Time Sensitive Notifications), `build-ios.sh` (`cap sync ios`, `xcodebuild archive`), `sign-and-install.sh` (`xcrun devicectl device install app`), all usable over SSH with the keychain password in `~/.mowdeck-sign-pass`. Paid profiles last a year, so no 7-day renewal agent. Android: `build-android.sh` (`gradlew assembleRelease` with the release keystore from the environment) and the job `android` of `release.yml` on `v*` tags (ubuntu), whose APK the release attaches; never on pull requests. The CI job `web` runs `apps/mobile`'s lint, `tsc` check and tests with the rest of the workspace.
- **App id** `app.mowdeck.mobile`, name "MowDeck"; icons and splash screens come from the web app's icon (`apps/mobile/scripts/make-resources.py` renders the sources, `@capacitor/assets` the sizes); the shell follows the system light/dark setting.

Options considered:

1. **(B) Bundled web build with `CapacitorHttp` / `CapacitorCookies` and an API base URL**: the refresh cookie becomes a cross-site cookie (or the native cookie jar), every service needs CORS with credentials and a configurable base URL, the realtime WebSocket needs another origin check, and each server update needs a new app build. Rejected; it duplicates the web build and weakens the cookie model of ADR 0021.
2. **A generous `allowNavigation: ['*']` with a bundled launcher that just navigates to the server**: works on iOS, but Android injects the bridge only for the app origin, so plugins would be missing until the next cold start, and every link to any site would open inside the app. Rejected in favour of setting `server.url` natively.
3. **Firebase Cloud Messaging for Android**: rejected by the owner (Google dependency, Firebase project, `google-services.json`); ntfy covers the background without it.
4. **TestFlight or the App Store**: the owner installs directly from the Mac; not needed for an app that points at a private server. Revised on 2026-10-22 for TestFlight, see the amendment below.
5. **React Native, Flutter or MAUI**: a second UI code base. Rejected.

## Consequences

- Server updates reach the apps at once; the app only changes for native changes (plugins, icons, permissions).
- The app needs the server: without a connection it shows the offline page, not a cached web app (the web app's service worker may serve cached assets on Android; on iOS a WKWebView service worker needs App-Bound Domains and is not used).
- The server must present a certificate the phone trusts for login (the refresh cookie is `Secure`): a public CA, or a private CA installed and trusted on the phone; Android trusts user-installed CAs for this app (`network_security_config.xml`). Plain HTTP is allowed in the LAN but login fails there.
- iOS background notifications need the owner's APNs auth key on the server and `MOWDECK_APNS_ENVIRONMENT=development`, because a build installed from Xcode with a development profile registers with the APNs sandbox. Android background notifications need the ntfy app and a topic; while the app is open, it notifies by itself.
- A paid developer account is required for APNs on iOS; the profiles are renewed once a year by running `provision.sh` again.
- Verified only by reading the sources and with the unit tests: the iOS and Android builds, the device install, APNs delivery and the remote bridge behaviour run on the owner's Mac and phones (docs/ops/mobile-apps.md lists the checks).

### Amendment 2026-10-22: TestFlight from GitHub Actions

The owner wants the app on iPhone and iPad without the Mac at hand. The on-demand workflow `.github/workflows/apple-testflight.yml` builds an existing release, signs it with an Apple Distribution certificate and an App Store profile and uploads it to TestFlight for internal testing (no App Review, no public App Store listing). The Apple signing secrets are no longer kept only on the Mac: they live in the GitHub Environment `release` (required reviewer: the owner, deployment branch `main` only) and only that job reads them, after an approval. Devices: iPhone and iPad; Apple silicon Macs run the iPad app, there is no Mac Catalyst build. TestFlight builds use production APNs (`aps-environment = production`, `APNS_ENVIRONMENT=production` on the server); the Mac path with development signing stays for direct installs. Details: [docs/ops/mobile-apps.md](../ops/mobile-apps.md), "TestFlight".
