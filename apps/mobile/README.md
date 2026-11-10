# mobile

The MowDeck apps for iPhone and Android: Capacitor 8 shells that load the web app (`apps/web`) from the owner's MowDeck server ([ADR 0036](../../docs/adr/0036-native-app-shells-with-capacitor.md)). Install and operations guide: [docs/ops/mobile-apps.md](../../docs/ops/mobile-apps.md).

| Path | Content |
| --- | --- |
| `capacitor.config.ts` | App id `app.mowdeck.mobile`, name, plugins; `server.url` is set natively at start from the stored address |
| `index.html`, `offline.html`, `src/` | The two bundled pages: first-launch server screen and "server not reachable" (built to `www/` by Vite); the first-launch screen also offers "Try the demo" (`src/demo-server.ts`, same connect flow as a typed address), a short explanation and links to the project page and the privacy section of [docs/ops/mobile-apps.md](../../docs/ops/mobile-apps.md) (`src/project-links.ts`); both have a German/English toggle (`src/i18n.ts`, choice kept in the bundled origin's localStorage, else the system language) |
| `about.html`, `src/about.ts` | The About page, linked from both bundled pages: app version and build (`@capacitor/app` `App.getInfo()`, neutral fallback in a browser), the app's licence (AGPL-3.0-only) with the pointer to the server's own About section, the source code of exactly this version (`src/source-link.ts`, the web app's `v<version>` convention, kept equal to `apps/web/src/lib/legal/source.ts` by a test), the manufacturer disclaimer and the third-party notices. Pure logic is separate from the page: `src/app-info.ts`, `src/source-link.ts`, `src/notices.ts` |
| `public/third-party-notices.json`, `third-party-policy.toml` | The generated third-party notices of the app bundle (copied into `www/` by Vite, shown by the About page) and the licence policy that guards them (`tools/repo-checks/scripts/app_notices.py`, [docs/ops/mobile-apps.md](../../docs/ops/mobile-apps.md#about-licences-and-third-party-notices)) |
| `src/shell.ts` | JavaScript side of the shell's own plugin `MowDeckShell` (server address; on iOS the installation id for APNs, kept in the Keychain) |
| `ios/` | Xcode project (Swift Package Manager). `MainViewController.swift` sets the server, `MowDeckShellPlugin.swift` (also `openAuthSession`), `App.entitlements` (push), `App/{en,de}.lproj/InfoPlist.strings` (the local network prompt in English and German) |
| `ios/MowDeckWidgets/`, `ios/MowerWidgetKit/` | The iPhone widgets and the Live Activity ([ADR 0059](../../docs/adr/0059-ios-widgets-and-live-activity.md)) and their shared Swift package (models, presentation, API client, watch protocol; `swift test --package-path apps/mobile/ios/MowerWidgetKit` on a Mac) |
| `ios/MowDeckWatch/`, `ios/MowDeckWatchWidgets/` | The Apple Watch app and its complication ([ADR 0060](../../docs/adr/0060-apple-watch-app.md)), SwiftUI; the iPhone side is `ios/App/App/WatchRelay.swift` (WatchConnectivity relay with the widget credential) |
| `android/` | Gradle project. `MainActivity.java`, `ShellSettings.java`, `MowDeckShellPlugin.java` (also `openAuthSession`), `AuthSessionUrls.java`, release signing from the environment |
| `resources/` | Icon and splash sources (`scripts/make-resources.py`), sizes by `pnpm assets` |
| `scripts/` | `provision.sh`, `build-ios.sh`, `sign-and-install.sh` (Mac, development signing), `build-android.sh` |
| `scripts/apple/` | TestFlight from GitHub Actions (`.github/workflows/apple-testflight.yml`): `keychain.sh` (temporary keychain from the `release` environment's secrets), `release-ios.sh` (unsigned build, App Store signature), `upload-testflight.sh` (altool), `entitlements/` (`app-store.plist` with production APNs, and one file per extension and watch target) |

The web app's side of the bridge (detection, deep links, notifications, badge, APNs registration) is `apps/web/src/lib/native/`; it uses the bridge the shell injects, so the web app has no Capacitor dependency.

## Commands

```bash
pnpm --filter mobile build    # bundled pages -> www/
pnpm --filter mobile sync     # build + cap sync (copies www/ and the plugin list into ios/ and android/)
pnpm --filter mobile check    # tsc
pnpm --filter mobile test     # vitest (server address, first-launch check, About page logic)
python3 tools/repo-checks/scripts/app_notices.py generate   # after changing dependencies: rewrite public/third-party-notices.json (needs pnpm install)
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

## Privacy manifest

The iOS bundles that use a required-reason API (the app, the widget extension, the watch app and its complication) each carry a `PrivacyInfo.xcprivacy` next to their `Info.plist`: user defaults with the reasons `CA92.1` (the app's own state, such as the server address) and `1C8F.1` (the App Group shared with the widgets and the watch), no tracking, no collected data. Nothing is sent to the developer, so the App Privacy answer is "Data Not Collected". Adding a required-reason API (file timestamps, boot time, disk space, active keyboards, or a new `UserDefaults` use in another target) means updating the manifest of that bundle; `tools/repo-checks/scripts/ios_release_check.py source` fails otherwise. Details, the caveats of the answer and the plugin inventory: [Privacy manifests](../../docs/ops/mobile-apps.md#privacy-manifests-and-the-app-privacy-answer).

## License

AGPL-3.0-only, like the web app. The bundle also contains third-party code (Capacitor, the plugins, the IBM Plex fonts under the SIL OFL 1.1); its licences and copyright notices are in `public/third-party-notices.json` and on the app's About page.
