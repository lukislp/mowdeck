# iPhone and Android apps

The MowDeck apps (`apps/mobile`, [ADR 0036](../adr/0036-native-app-shells-with-capacitor.md)) are Capacitor shells around the web app. They do not contain a copy of it: on first start they ask for your MowDeck server's address and from then on load the web app from that server. A server update reaches the apps at once, and login works exactly like in the browser.

| | iPhone | Android |
| --- | --- | --- |
| Install | built and installed from your Mac (paid Apple developer account, no TestFlight, no App Store) | APK from the GitHub release |
| Notifications, app open | yes (local notifications from the inbox, icon badge) | yes (local notifications from the inbox, icon badge where the launcher supports it) |
| Notifications, app in background or closed | Apple Push (APNs) straight from your server, needs your APNs key on the server | ntfy app (no Firebase) |
| Deep links | `mowdeck://mowers/<id>` | `mowdeck://mowers/<id>` |

## What the server needs

- **HTTPS with a certificate the phone trusts.** Login uses a `Secure` cookie, so plain HTTP only gets you to the login page. A public certificate (for example Tailscale HTTPS or Let's Encrypt) works everywhere. With a private CA, install and trust its certificate on the phone: iOS Settings → General → VPN & Device Management (install the profile), then Settings → General → About → Certificate Trust Settings (switch it on); Android Settings → Security → Encryption & credentials → Install a certificate → CA certificate (the MowDeck app trusts user CAs).
- **Reachable from the phone**: same Wi-Fi, or a VPN (Tailscale, WireGuard) when away.
- **For iPhone background notifications**: the APNs key, see [Apple Push](#apple-push-apns-for-the-iphone).

## iPhone: build and install from the Mac

Prerequisites on the Mac: Xcode (from the App Store, opened once, `sudo xcode-select -s /Applications/Xcode.app`), Node.js 22 with `corepack enable` (pnpm), git and a clone of this repository. Everything below also works over SSH from the PC.

### One-time setup

1. **Apple ID of the paid team in Xcode**: Xcode → Settings → Accounts → "+" → Apple ID. Note the **Team ID** (Xcode → Settings → Accounts → your team, or developer.apple.com → Membership details).
2. **Settings file on the Mac** (not in the repository):

    ```bash
    cat > ~/.mowdeck-ios.env <<'EOF'
    MOWDECK_APPLE_TEAM_ID=ABCDE12345
    EOF
    ```

    Optional: `MOWDECK_DEVICE_ID=<udid>` when more than one iPhone is paired, `MOWDECK_VERSION=1.2.3` to override the version (default: the newest `v*` tag).
3. **Keychain password for SSH sessions** (stays on the Mac): `printf '%s' 'MAC-LOGIN-PASSWORD' > ~/.mowdeck-sign-pass && chmod 600 ~/.mowdeck-sign-pass`. SSH sessions get a locked keychain; the scripts unlock it with this file.
4. **iPhone**: connect it by USB once, unlock it and tap "Trust this computer". Switch on Developer Mode: Settings → Privacy & Security → Developer Mode → on, restart, confirm. For later installs over Wi-Fi, keep the iPhone on the same network as the Mac (Xcode → Window → Devices and Simulators → "Connect via network" if it does not show up).
5. **Provision** (creates the Apple Development certificate, registers the iPhone, creates the development profile for `app.mowdeck.mobile` with Push Notifications and Time Sensitive Notifications):

    ```bash
    bash apps/mobile/scripts/provision.sh
    ```

    It ends with the profile's expiry date and `APNs: development`. If it says `MISSING`, the team is not a paid account or the capability could not be added; open `apps/mobile/ios/App/App.xcodeproj` in Xcode once, select the team under Signing & Capabilities and run the script again.

    The bundle id `app.mowdeck.mobile` must be free in Apple's developer portal. If another team already uses it, change `appId` in `apps/mobile/capacitor.config.ts`, `PRODUCT_BUNDLE_IDENTIFIER` in the Xcode project, `BUNDLE_ID` in `apps/mobile/scripts/common.sh` and `MOWDECK_APNS_BUNDLE_ID` on the server to one of your own.

### Build and install (any time after)

```bash
bash apps/mobile/scripts/build-ios.sh        # cap sync ios + xcodebuild archive (signed)
bash apps/mobile/scripts/sign-and-install.sh # signature check + devicectl install (USB or Wi-Fi)
```

`sign-and-install.sh` prints the APNs environment of the build and the profile's expiry. Data and login are kept on every update. Because the web app comes from the server, you only need a new build for native changes (a new MowDeck release with changes in `apps/mobile`).

### First start on the iPhone

1. If iOS says "Untrusted Developer": Settings → General → VPN & Device Management → your developer certificate → Trust. (With a paid team and a development profile this usually is not needed; it is needed once per team on a new phone.)
2. The app asks for the **server address**, for example `mowdeck.home.lan` or `https://mowdeck.example.org`. It checks that a MowDeck server answers there (`/manifest.webmanifest`); "Use anyway" skips the check.
3. Log in as in the browser. The app asks once for permission to send notifications; allow it.
4. To switch servers later: Settings → Notifications → "This app" → "Change server". If the server is unreachable at start, the app shows "Server not reachable" with "Try again" and "Choose another server".

### Once a year

Paid profiles and development certificates are valid for a year. There is no 7-day renewal (that only applies to free Personal Team signing, which cannot use push), so no launchd agent is needed. Before the date `sign-and-install.sh` prints: run `provision.sh`, then `build-ios.sh` and `sign-and-install.sh` again (or `MOWDECK_RESIGN=1 bash apps/mobile/scripts/sign-and-install.sh` to re-sign the existing build with the new profile).

## Apple Push (APNs) for the iPhone

The Notifications service sends straight to Apple with your APNs auth key (token-based, no Firebase, no third party):

1. developer.apple.com → Certificates, Identifiers & Profiles → Keys → "+" → name it, tick **Apple Push Notifications service (APNs)**, choose "Sandbox & Production", Continue, Register, **Download** `AuthKey_<KEYID>.p8` (only once possible). Note the **Key ID**.
2. Give it to the server:
    - Compose: copy the file to `infra/compose/secrets/apns/AuthKey_<KEYID>.p8` and set in `infra/compose/.env`:

        ```bash
        APNS_KEY_FILE=/run/secrets/apns/AuthKey_<KEYID>.p8
        APNS_KEY_ID=<KEYID>
        APNS_TEAM_ID=<TEAMID>
        APNS_BUNDLE_ID=app.mowdeck.mobile
        APNS_ENVIRONMENT=development
        ```

        then `docker compose up -d notifications`.
    - Helm: create a Secret with the key (`kubectl -n mowdeck create secret generic mowdeck-apns --from-file=AuthKey.p8=AuthKey_<KEYID>.p8`) and set `services.notifications.apns.enabled=true`, `existingSecret=mowdeck-apns`, `keyId`, `teamId` (and `bundleId`, `environment` if they differ).
3. **`development` is right for apps installed from the Mac**: a build with a development profile registers with the APNs sandbox. `production` is only for TestFlight or App Store builds.
4. Check: the notifications log says `notification channels ... apns`; Settings → Notifications in the app shows "Apple Push: on"; "Send test notification" arrives with the app closed.

Device tokens are stored per user (`POST /api/v1/notifications/me/push/apns`) and removed on logout and when Apple reports them as unregistered. The channel "iPhone app" can be switched off in the notification preferences like Web Push or ntfy; quiet hours apply, theft alarms break through them and through Focus (time-sensitive).

## Android

### Install the APK

1. Download `mowdeck-<version>.apk` from the GitHub release (Assets) on the phone.
2. Open it; Android asks to allow installs from this source (browser or file manager) once. Install.
3. Updates: install the newer APK over the old one; data and login stay. Every release APK is signed with the same release key, so updates work; a debug-signed APK you built yourself cannot update a release one (uninstall first).

First start, server address and "Change server" work as on the iPhone. On Android 13 and later the app asks for permission to show notifications after login.

### Notifications without Firebase

Android has no system push without Google's Firebase Cloud Messaging, and MowDeck does not use it. Instead:

- **While the app is open** (and for a while in the background, until Android suspends it) it shows every new notification from your inbox itself.
- **In the background, ntfy**: install the ntfy app (Google Play, F-Droid, see [ntfy.sh/docs/subscribe/phone](https://ntfy.sh/docs/subscribe/phone/)), set up a topic in MowDeck under Settings → Notifications → ntfy, then tap "Subscribe in the ntfy app" in the "This app" panel (opens `ntfy://<server>/<topic>`) or subscribe to the same server and topic in ntfy by hand. Once an ntfy target exists, the MowDeck app stops showing its own notifications so nothing arrives twice.

### Build the APK yourself

Prerequisites: Node.js 22 with pnpm, JDK 21, the Android SDK (`ANDROID_HOME`; Android Studio installs it).

```bash
bash apps/mobile/scripts/build-android.sh   # -> apps/mobile/build/mowdeck-<version>.apk
```

Without a keystore the APK is debug-signed. Release signing:

1. Create the keystore once and keep it safe (losing it means users must uninstall to update):

    ```bash
    keytool -genkeypair -v -keystore mowdeck-release.jks -alias mowdeck -keyalg RSA -keysize 4096 -validity 10000
    ```

2. Locally: `MOWDECK_ANDROID_KEYSTORE=mowdeck-release.jks MOWDECK_ANDROID_KEYSTORE_PASSWORD=... MOWDECK_ANDROID_KEY_ALIAS=mowdeck bash apps/mobile/scripts/build-android.sh`.
3. For the release workflow, add repository secrets: `ANDROID_KEYSTORE_BASE64` (`base64 -w0 mowdeck-release.jks`), `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, optionally `ANDROID_KEY_PASSWORD` (defaults to the keystore password). The job `android` of `release.yml` builds the APK on ubuntu for every release ([Releases](release.md)) and the release attaches it, with its checksum, Sigstore bundle and build provenance; without the secret the job only warns and the release has no APK. Pull requests never build the native apps (minute budget); the CI job `web` runs the mobile package's lint, `tsc` check and tests.

## Icons and splash screens

The artwork is the web app's icon. To change it, edit `apps/mobile/scripts/make-resources.py`, then:

```bash
uv run python apps/mobile/scripts/make-resources.py   # sources in apps/mobile/resources/
pnpm --filter mobile assets                          # every iOS and Android size (@capacitor/assets)
```

On Windows `@capacitor/assets` can fail with `EBUSY` on `mipmap-anydpi-v26/ic_launcher_round.xml`; delete the two files in that folder and run it again.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| "No MowDeck server answers at this address" | Wrong address, phone not in the network/VPN, or a certificate the phone does not trust (see [What the server needs](#what-the-server-needs)). |
| Login page shows, login fails | The server is on plain HTTP; the session cookie needs HTTPS. |
| "Apple Push: failed" | No network to Apple, or the build has no `aps-environment` (re-run `provision.sh`, check its `APNs:` line). |
| Apple Push on, but nothing arrives in the background | Server without APNs key (the panel says so), `APNS_ENVIRONMENT=production` for a Mac-installed build (must be `development`), or a different bundle id on server and app. The notifications log names Apple's reason (`BadDeviceToken`, `TopicDisallowed`, ...). |
| `errSecInternalComponent` over SSH | `~/.mowdeck-sign-pass` missing or wrong. |
| `sign-and-install.sh`: no iPhone found | Unlock the phone; pair by USB once; Wi-Fi install needs the same network. |
| iPhone: the top bar can be dragged under the status bar, or the page bounces | A build from before the edge-to-edge layout (`ios.contentInset: 'always'`, page bounce on). Rebuild with `build-ios.sh` (it runs `cap sync`, which carries `contentInset: 'never'` into the Xcode project) and install again. |
| Android: nothing in the background | Expected without ntfy; set up ntfy. Some vendors also kill background apps aggressively ([dontkillmyapp.com](https://dontkillmyapp.com)). |

## What has been verified where

The configuration, scripts and native code were written against the Capacitor 8.5 sources and are covered by unit tests (the web app's bridge module, the first-launch checks, the APNs channel with a fake Apple endpoint). Building the iOS app, installing it with `devicectl`, APNs delivery to a phone, the Android Gradle build and the remote-content bridge on real devices are verified on the owner's Mac and phones, not in CI.
