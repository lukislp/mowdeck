# mobile

The MowDeck apps for iPhone and Android: Capacitor 8 shells that load the web app (`apps/web`) from the owner's MowDeck server ([ADR 0036](../../docs/adr/0036-native-app-shells-with-capacitor.md)). Install and operations guide: [docs/ops/mobile-apps.md](../../docs/ops/mobile-apps.md).

| Path | Content |
| --- | --- |
| `capacitor.config.ts` | App id `app.mowdeck.mobile`, name, plugins; `server.url` is set natively at start from the stored address |
| `index.html`, `offline.html`, `src/` | The two bundled pages: first-launch server screen and "server not reachable" (built to `www/` by Vite) |
| `src/shell.ts` | JavaScript side of the shell's own plugin `MowDeckShell` (server address; on iOS the installation id for APNs, kept in the Keychain) |
| `ios/` | Xcode project (Swift Package Manager). `MainViewController.swift` sets the server, `MowDeckShellPlugin.swift` (also `openAuthSession`), `App.entitlements` (push) |
| `android/` | Gradle project. `MainActivity.java`, `ShellSettings.java`, `MowDeckShellPlugin.java` (also `openAuthSession`), `AuthSessionUrls.java`, release signing from the environment |
| `resources/` | Icon and splash sources (`scripts/make-resources.py`), sizes by `pnpm assets` |
| `scripts/` | `provision.sh`, `build-ios.sh`, `sign-and-install.sh` (Mac, development signing), `build-android.sh` |
| `scripts/apple/` | TestFlight from GitHub Actions (`.github/workflows/apple-testflight.yml`): `keychain.sh` (temporary keychain from the `release` environment's secrets), `release-ios.sh` (unsigned build, App Store signature), `upload-testflight.sh` (altool), `entitlements/app-store.plist` (production APNs) |

The web app's side of the bridge (detection, deep links, notifications, badge, APNs registration) is `apps/web/src/lib/native/`; it uses the bridge the shell injects, so the web app has no Capacitor dependency.

## Commands

```bash
pnpm --filter mobile build    # bundled pages -> www/
pnpm --filter mobile sync     # build + cap sync (copies www/ and the plugin list into ios/ and android/)
pnpm --filter mobile check    # tsc
pnpm --filter mobile test     # vitest (server address, first-launch check)
pnpm --filter mobile assets   # icons and splash screens from resources/
```

`cap sync` rewrites `ios/App/CapApp-SPM/Package.swift` and `android/capacitor.settings.gradle` with paths into `node_modules`; the build scripts always run it first, so the committed versions are only a snapshot.

## Passkey sign-in through the system browser

Web views cannot use passkeys without Associated Domains (iOS) or Digital Asset Links (Android), which a self-hosted server at any address cannot provide ([ADR 0050](../../docs/adr/0050-passkeys-and-app-login-hand-off.md)). `MowDeckShell.openAuthSession({url, callbackScheme: 'mowdeck'})` opens a page of the configured server (any other URL is refused) in the system browser:

- **iOS:** `ASWebAuthenticationSession` with an ephemeral session (no cookies shared with Safari); resolves with the `mowdeck://auth/callback?...` URL the page navigates to, rejects with the code `cancelled` when the user closes the sheet.
- **Android:** a Custom Tab (`androidx.browser`); resolves at once. The page returns through an `intent:` URL for the package `app.mowdeck.mobile`, which arrives as the deep link `mowdeck://auth/callback?...` (`@capacitor/app` `appUrlOpen`); the `singleTask` activity closes the tab when it comes back.

The web app (`apps/web/src/lib/native/handoff.ts`) registers the PKCE challenge, checks the state and redeems the one-time code for the app's session (the refresh cookie in the web view, as for a password login). Builds without `openAuthSession` hide the passkey button. The URL check of the Android side is a plain Java class with a JUnit test (`AuthSessionUrlsTest`; Gradle runs it with `./gradlew test`, CI does not build Android).

## Plugins

`@capacitor/app` (deep links), `@capacitor/local-notifications`, `@capacitor/push-notifications` (iOS only, APNs; excluded from Android because it needs Firebase), `@capacitor/splash-screen`, `@capawesome/capacitor-badge`, and the core `SystemBars` (Android insets, light/dark bars).

## License

AGPL-3.0-only, like the web app.
