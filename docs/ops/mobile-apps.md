# iPhone and Android apps

The MowDeck apps (`apps/mobile`, [ADR 0036](../adr/0036-native-app-shells-with-capacitor.md)) are Capacitor shells around the web app. They do not contain a copy of it: on first start they ask for your MowDeck server's address and from then on load the web app from that server. A server update reaches the apps at once, and login works exactly like in the browser.

| | iPhone | Android |
| --- | --- | --- |
| Install | TestFlight (internal testing, uploaded from GitHub Actions on demand), or built and installed from your Mac; iPhone and iPad, and Apple silicon Macs as an iPad app | APK from the GitHub release |
| Notifications, app open | yes (local notifications from the inbox, icon badge) | yes (local notifications from the inbox, icon badge where the launcher supports it) |
| Notifications, app in background or closed | Apple Push (APNs) straight from your server, needs your APNs key on the server | ntfy app (no Firebase) |
| Deep links | `mowdeck://mowers/<id>` | `mowdeck://mowers/<id>` |

## What the server needs

- **HTTPS with a certificate the phone trusts.** Login uses a `Secure` cookie, so plain HTTP only gets you to the login page. A public certificate (for example Tailscale HTTPS or Let's Encrypt) works everywhere. With a private CA, install and trust its certificate on the phone: iOS Settings → General → VPN & Device Management (install the profile), then Settings → General → About → Certificate Trust Settings (switch it on); Android Settings → Security → Encryption & credentials → Install a certificate → CA certificate (the MowDeck app trusts user CAs).
- **Reachable from the phone**: same Wi-Fi, or a VPN (Tailscale, WireGuard) when away.
- **For iPhone background notifications**: the APNs key, see [Apple Push](#apple-push-apns-for-the-iphone).

## iPhone and iPad: TestFlight

The workflow **Apple TestFlight** (`.github/workflows/apple-testflight.yml`) builds the iOS app of an existing release, signs it for the App Store and uploads it to App Store Connect, where it appears in TestFlight for the internal testers. It never runs by itself: a release (`release.yml`) does not wait for it, and every run waits for the owner's approval in the GitHub Environment `release`.

What it does, in order:

1. Job `resolve` (ubuntu): the tag (empty = the latest GitHub release) must be `vX.Y.Z` with a GitHub release and its commit on `main`; job `testflight-ios` (`macos-latest`) checks out exactly that commit.
2. The secrets are all set (`apps/mobile/scripts/apple/keychain.sh check`), then `tools/repo-checks/scripts/ios_release_check.py source`: the 1024 px App Store icon without transparency, `ITSAppUsesNonExemptEncryption = false`, the iPad orientations, iPhone and iPad as device families, and the App Store entitlements (`apps/mobile/scripts/apple/entitlements/app-store.plist`) mirroring `App.entitlements` with `aps-environment = production`. The same checks run in the CI job `python` on every pull request, so a problem App Store Connect would only report after the upload fails the pull request instead.
3. `keychain.sh setup`: a temporary keychain with the Apple Distribution certificate (plus Apple's pinned WWDR intermediate), the App Store profile and the App Store Connect API key, all below `RUNNER_TEMP`; `keychain.sh cleanup` removes them in an `always()` step.
4. `apps/mobile/scripts/apple/release-ios.sh`: the profile must be an App Store profile for `app.mowdeck.mobile` of the team and grant every entitlement (the same for the notification extension's own profile) (`ios_signing.py` names the capability to enable otherwise); then `cap sync ios` (only the `mobile` package and its workspace dependencies are installed, `pnpm install --filter mobile...`), an unsigned `xcodebuild build`, `ios_release_check.py app` (versions, compiled icon, no Agora file or reference in the bundle) and `codesign` of the frameworks, the notification extension (`PlugIns/NotificationService.appex`, its own profile), the widget extension, the watch complication and the watch app (`Watch/MowDeckWatch.app`, two more profiles) and the app with the profile embedded. The unsigned build of the `App` scheme also builds the watch targets for watchOS, since the app embeds the watch app. Building unsigned and signing afterwards keeps manual signing settings away from the Swift package targets.
5. `apps/mobile/scripts/apple/upload-testflight.sh`: `xcrun altool --upload-app` with the API key, at most 30 minutes, Apple's error messages as annotations.

**Version and build number**: `CFBundleShortVersionString` is the release version, `CFBundleVersion` the Android `versionCode` formula `major * 10000 + minor * 100 + patch` (v0.3.12 -> `312`), so build numbers only grow. App Store Connect refuses a build number twice: to upload the same tag again, run the workflow with `rebuild` = 1 (then 2, ...), which uploads `312.1`.

### Signing secrets

GitHub Environment `release` of the repository (required reviewer: the owner; deployment branches: `main` only). The Apple secrets live only there, never as repository secrets, and only the TestFlight job reads them, after the approval.

| Secret | Content |
| --- | --- |
| `APPLE_TEAM_ID` | 10-character team id |
| `APPLE_DISTRIBUTION_P12_BASE64`, `APPLE_DISTRIBUTION_P12_PASSWORD` | "Apple Distribution" certificate with its private key, `.p12` (legacy encryption: Keychain Access or `openssl pkcs12 -export -legacy`), base64 |
| `APPLE_PROFILE_IOS_BASE64` | App Store provisioning profile for `app.mowdeck.mobile` (Distribution -> App Store Connect), base64. A development profile from `provision.sh` does not work |
| `APPLE_PROFILE_IOS_NOTIFICATION_BASE64` | App Store provisioning profile for `app.mowdeck.mobile.NotificationService` (the extension that adds the camera photo to error and theft notifications, [ADR 0056](../adr/0056-camera-photo-in-push-notifications.md)), base64. See [Camera photo in notifications](#camera-photo-in-notifications) for the one-time Apple portal steps |
| `APPLE_PROFILE_IOS_WIDGETS_BASE64` | App Store provisioning profile for `app.mowdeck.mobile.Widgets` (the widget extension with the Live Activity, [ADR 0059](../adr/0059-ios-widgets-and-live-activity.md)), base64. See [Widgets and the Live Activity](#widgets-and-the-live-activity) for the one-time Apple portal steps |
| `APPLE_PROFILE_WATCH_BASE64` | App Store provisioning profile for `app.mowdeck.mobile.watchkitapp` (the Apple Watch app, [ADR 0060](../adr/0060-apple-watch-app.md)), base64. See [Apple Watch](#apple-watch) for the one-time Apple portal steps |
| `APPLE_PROFILE_WATCH_WIDGETS_BASE64` | App Store provisioning profile for `app.mowdeck.mobile.watchkitapp.Widgets` (the watch complication), base64 |
| `ASC_API_KEY_P8_BASE64`, `ASC_API_KEY_ID`, `ASC_API_ISSUER_ID` | App Store Connect API key (Users and Access -> Integrations, role Developer or higher), base64 of the `.p8` |

Optional variable `APPLE_RELEASE_EGRESS_POLICY=audit`: the job's network egress is blocked except for an allow-list (Apple's upload and signing hosts, GitHub, the npm registry). If Apple or GitHub move a host, a step fails or altool hangs until its time limit; set the variable, re-run the job, read the new host from the Harden Runner insights, add it to `allowed-endpoints` and delete the variable again.

Apple side, once: the App ID `app.mowdeck.mobile` with Push Notifications and Time Sensitive Notifications (automatic signing from the Mac creates it), an App Store profile for it with the Apple Distribution certificate, and an app record in App Store Connect with an internal testing group. Macs with Apple silicon can run the iPad app (App Store Connect -> the app -> Pricing and Availability -> iPhone and iPad Apps on Apple Silicon Macs); there is no Mac Catalyst build.

### Run it

1. Wait for a release (`release.yml`) that contains the native change you want on the devices.
2. Actions -> **Apple TestFlight** -> Run workflow on `main`; `tag` empty (latest release) or e.g. `v0.3.12`, `rebuild` empty.
3. Approve the deployment to `release` (Review deployments). Apple processes the uploaded build for a few minutes before TestFlight offers it.

TestFlight builds register with **production** APNs: set `APNS_ENVIRONMENT=production` on the server (see [Apple Push](#apple-push-apns-for-the-iphone)). A TestFlight install replaces an app installed from the Mac. App Transport Security stays as it is (`NSAllowsLocalNetworking`, `NSAllowsArbitraryLoadsInWebContent` for a server in the LAN); internal TestFlight testing has no App Review, an App Store submission would have to justify these keys.

## iPhone: build and install from the Mac

Prerequisites on the Mac: Xcode (from the App Store, opened once, `sudo xcode-select -s /Applications/Xcode.app`), Node.js 24 (24.21 or newer) with `corepack enable` (pnpm), git and a clone of this repository. Everything below also works over SSH from the PC.

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
3. **The environment follows the build on the phones**: a build installed from the Mac with a development profile registers with the APNs sandbox (`development`), a TestFlight build with production APNs (`production`). One server talks to one environment, so a phone with the other kind of build gets no background push (Apple answers `BadDeviceToken` and the server drops that token). The key needs "Sandbox & Production"; a team key for all topics works for every app of the team.
4. Check: the notifications log says `notification channels ... apns`; Settings → Notifications in the app shows "Apple Push: on"; "Send test notification" arrives with the app closed.

Device tokens are stored per user (`POST /api/v1/notifications/me/push/apns`), one per app installation, and removed on logout and when Apple reports them as unregistered. The app keeps an installation id in the iOS Keychain (only on this device, kept across updates and reinstalls); a new token of the same installation replaces the old one, so a reinstalled or updated app does not get every notification twice. App builds without the installation id keep one registration per token; install a new TestFlight build to get it. The channel "iPhone app" can be switched off in the notification preferences like Web Push or ntfy; quiet hours apply, theft alarms break through them and through Focus (time-sensitive).

## Camera photo in notifications

Error and theft notifications (`mower_stuck`, `mower_lifted`, `mower_left_dock_unexpectedly`, `geofence_exit`) show the mower's camera picture on the iPhone and in the web inbox ([ADR 0056](../adr/0056-camera-photo-in-push-notifications.md)). The Notifications service takes one frame per event through the Device-Gateway (the camera relay or the mower's photo function), keeps it 24 hours in its database and sends the push at once with a signed link of your own server that expires after ten minutes; the app's Notification Service Extension downloads the picture from that link (it waits at most 14 seconds, otherwise the text is shown as before). The picture never goes through Apple.

**Server**

- `NOTIFICATIONS_APP_URL` (Helm: the public origin by default) must be the public HTTPS origin the phone reaches; without it the pushes carry no image.
- The link is signed with a key derived from the APNs key; `MOWDECK_NOTIFICATIONS_IMAGE_SECRET` sets a separate one. `MOWDECK_NOTIFICATIONS_IMAGE_ENABLED=false` turns the photos off. Budget per mower: `..._IMAGE_MIN_INTERVAL_SECONDS` (60) and `..._IMAGE_DAILY_LIMIT` (6 per 24 hours); retention `..._IMAGE_RETENTION_HOURS` (24).
- Reverse proxy: `/api/v1/notifications/push-images/*` must be reachable without a login (Traefik routes the whole `/api/v1/notifications` prefix to Notifications already).

**One-time Apple steps (before the first TestFlight build with the extension)**

1. developer.apple.com -> Certificates, Identifiers & Profiles -> Identifiers -> "+" -> App IDs -> App -> description "MowDeck Notification Service", Bundle ID **Explicit** `app.mowdeck.mobile.NotificationService`. No capability is needed. Register.
2. Profiles -> "+" -> **App Store Connect** (Distribution) -> choose that App ID -> choose the same Apple Distribution certificate as for the app -> name it, e.g. "MowDeck NotificationService AppStore" -> Generate -> Download.
3. `base64 -w0 MowDeck_NotificationService_AppStore.mobileprovision` (macOS: `base64 -i file | tr -d '\n'`), and store the result as the secret **`APPLE_PROFILE_IOS_NOTIFICATION_BASE64`** in the GitHub Environment `release`.
4. Run **Apple TestFlight** as usual. The job fails early with a readable message if the secret or the profile is missing or belongs to another App ID.

Installing from the Mac (`build-ios.sh`) uses automatic signing: Xcode creates the extension's App ID and development profile itself (`-allowProvisioningUpdates`), nothing to do. Only a build that contains the extension shows pictures; older builds show the text.

**Try it on the phone (safely).** With the new TestFlight build installed and logged in, make sure the kinds are on in Settings -> Notifications. Lift the docked, idle mower a few centimetres by hand until it reports "lifted" (the lift sensor), or, with the mower docked and not working, cover the route: `geofence_exit` and `mower_stuck` are harder to provoke safely. The lift alarm arrives as a time-sensitive notification; pull down / long-press it to see the picture (a dock-side frame if it is asleep: the relay may need a few seconds to wake it). The same picture is in the web app's inbox under the item. No picture and no error means the photo was not available in time (see the Notifications log: "no photo for the notification").

## Widgets and the Live Activity

Home and lock screen widgets (status, lock screen, map, schedule and weather) and a Live Activity for a mowing job on the Lock Screen and in the Dynamic Island ([ADR 0059](../adr/0059-ios-widgets-and-live-activity.md), [user guide](../guides/ios-widgets.md)). They need iOS 17 (the Live Activity iOS 16.2, started by push 17.2).

**How the widgets get their data.** The widget extension cannot use the app's login. In the app: Settings -> "This app" -> **Turn on widgets** creates a *widget credential* on the server: an API token `iOS widgets: <device>` that can only see and control mowers, valid 90 days, renewed by the app when 30 days remain. It sits in the Keychain (this device only, readable while the phone is locked after the first unlock) and is exchanged for 10 minute access tokens. You see and can revoke it under Settings -> API tokens; logging out or turning widgets off revokes it and removes it from the phone. The account's users need `mower.view` on the whole account (owner, admin, manager, operator, viewer roles do).

**Server.** Nothing to configure: the same APNs key and `MOWDECK_APNS_ENVIRONMENT` as for notifications. The Notifications service sends Live Activity pushes (`liveactivity`) and silent reload pushes (`background`) with them; new tables come with its migration. Telemetry draws the map picture (about 100-300 ms of CPU per picture, cached 30 seconds). Reverse proxy: nothing new (`/api/v1/telemetry`, `/api/v1/scheduler`, `/api/v1/identity/token` are routed already).

**One-time Apple steps (before the first TestFlight build with widgets)**

1. developer.apple.com -> Certificates, Identifiers & Profiles -> Identifiers -> **App Groups** -> "+" -> description "MowDeck", identifier **`group.app.mowdeck.mobile`**. Register.
2. Identifiers -> the App ID **`app.mowdeck.mobile`** -> enable **App Groups** (Configure -> select `group.app.mowdeck.mobile`) and keep Push Notifications and Time Sensitive Notifications. Keychain Sharing needs no portal setting (the profile's `<TeamID>.*` covers `<TeamID>.app.mowdeck.mobile.shared`). Save. (Live Activities need no capability, only `NSSupportsLiveActivities` in the app, which the project has.)
3. Identifiers -> "+" -> App IDs -> App -> description "MowDeck Widgets", Bundle ID **Explicit** `app.mowdeck.mobile.Widgets`, capability **App Groups** with `group.app.mowdeck.mobile`. Register.
4. Profiles: **regenerate** the App Store profile of `app.mowdeck.mobile` (edit -> save -> download) so it contains the App Group, and update the secret `APPLE_PROFILE_IOS_BASE64`. Create a new profile -> **App Store Connect** -> the App ID `app.mowdeck.mobile.Widgets` -> the same Apple Distribution certificate -> download.
5. `base64 -w0 MowDeck_Widgets_AppStore.mobileprovision` (macOS: `base64 -i file | tr -d '\n'`) as the secret **`APPLE_PROFILE_IOS_WIDGETS_BASE64`** in the GitHub Environment `release`.
6. Run **Apple TestFlight** as usual (the job fails early with a readable message when a secret or a profile does not fit: e.g. "does not grant: com.apple.security.application-groups"). **This is the first time the extension is compiled**: there is no macOS CI, so expect to read the log of the first run and fix compile errors; send them over.

Installing from the Mac (`build-ios.sh`) uses automatic signing and creates the extension's App ID and the App Group itself with `-allowProvisioningUpdates`.

**Try it on the phone (safely).** After the TestFlight build: open the app, Settings -> This app -> Turn on widgets. Long-press the home screen -> "+" -> MowDeck -> add "Mower status" (medium), long-press it -> Edit Widget -> choose the mower. With the mower docked the Start button starts all areas with the saved mow defaults: **only press it when the lawn is clear and the mower may mow**, or test Pause and Station while a job runs. Start a job from the app, and the Live Activity appears on the Lock Screen; start one from a schedule ("Run now" on a schedule) with the app closed, and it appears by push (see the ADR for the limit: its updates start once the app has reported the activity's token).

| Symptom | Likely cause |
| --- | --- |
| Widget says "Open MowDeck and turn on widgets" | Not turned on, the credential expired or was revoked (Settings -> API tokens); turn it on again |
| Turning widgets on fails | The role lacks `mower.view` on the whole account (403), or the server is not reachable from the phone |
| Buttons do nothing | iOS 16 (needs 17), no `mower.control`, or the credential is revoked; the widget shows nothing special, press and watch the mower's command history |
| Widget shows old data | iOS decides when to reload (the budget is a few dozen a day); the data refreshes with every reload and silent push, in the app via the widget gallery |
| No Live Activity | Live Activities are off for MowDeck (Settings -> MowDeck), the push-to-start token is not registered yet (open the app once with widgets on), or the build predates the extension |

## Apple Watch

A native watch app with the mower's state, Start, Pause, Station and the emergency stop, and a complication with battery and state ([ADR 0060](../adr/0060-apple-watch-app.md), [user guide](../guides/apple-watch.md)). watchOS 10 or newer.

**How it works.** The watch holds no credential and never calls the server: it sends every command to the iPhone app over WatchConnectivity, and the iPhone runs it natively with the widget credential of this iPhone (see [Widgets and the Live Activity](#widgets-and-the-live-activity)). So widgets must be on in the iPhone app, the iPhone must be near the watch, and the iPhone must reach the server (LAN or VPN). Nothing to configure on the server; the state the watch shows comes with the same silent reload pushes as the widgets.

**One-time Apple steps (before the first TestFlight build with the watch app)**

The App Group `group.app.mowdeck.mobile` from the widget steps must exist.

1. developer.apple.com -> Certificates, Identifiers & Profiles -> Identifiers -> "+" -> **App IDs** -> **App** -> description "MowDeck Watch", Bundle ID **Explicit** **`app.mowdeck.mobile.watchkitapp`**, capability **App Groups** (Configure -> `group.app.mowdeck.mobile`). Nothing else. Register.
2. Identifiers -> "+" -> App IDs -> App -> description "MowDeck Watch Complication", Bundle ID **Explicit** **`app.mowdeck.mobile.watchkitapp.Widgets`**, capability **App Groups** with `group.app.mowdeck.mobile`. Register.
3. Profiles -> "+" -> **Distribution: App Store Connect** -> the App ID `app.mowdeck.mobile.watchkitapp` -> the same Apple Distribution certificate as the app -> name "MowDeck Watch AppStore" -> Generate -> Download.
4. The same for `app.mowdeck.mobile.watchkitapp.Widgets` -> name "MowDeck Watch Complication AppStore" -> Download.
5. Base64 of each profile, in PowerShell (copies to the clipboard):

    ```powershell
    [Convert]::ToBase64String([IO.File]::ReadAllBytes("$HOME\Downloads\MowDeck_Watch_AppStore.mobileprovision")) | Set-Clipboard
    ```

    Paste it as the secret **`APPLE_PROFILE_WATCH_BASE64`** in the GitHub Environment `release` (Settings -> Environments -> release -> Add environment secret). Then the same for `MowDeck_Watch_Complication_AppStore.mobileprovision` as **`APPLE_PROFILE_WATCH_WIDGETS_BASE64`**. (macOS: `base64 -i file | tr -d '\n' | pbcopy`.)
6. Run **Apple TestFlight** as usual. The job checks first that both secrets are set and that each profile is an App Store profile for its bundle id with the App Group ("does not grant: com.apple.security.application-groups" means step 1 or 2 lacks the App Group: fix it, then regenerate and download the profile again). **This is the first time the watch targets are compiled** (no macOS CI): expect to read the log of the first run and send compile errors over.
7. On the iPhone: install the TestFlight build, then the **Watch** app on the iPhone -> Available Apps -> **MowDeck** -> Install. In MowDeck on the iPhone: Settings -> This app -> widgets on; the panel shows "Apple Watch: ready".

App Store Connect needs no separate app record: the watch app ships inside the iPhone app. For an App Store release (not TestFlight) it would need watch screenshots.

Installing from the Mac (`build-ios.sh`) uses automatic signing, which creates the two App IDs and development profiles itself; `sign-and-install.sh` with `MOWDECK_RESIGN=1` re-signs the watch app and its complication when their profiles exist.

**Try it on the watch (safely).** With the mower docked: open MowDeck on the watch, check state and battery against the app. Turn the iPhone's Bluetooth and Wi-Fi off for a moment: the watch says "iPhone nicht erreichbar" and its buttons wait (the emergency stop stays). **Only press Start when the lawn is clear and the mower may mow**; then Pause, Station, and the emergency stop (confirm) while the job runs, and compare with the mower's command history in the app. Add the complication to a face and watch it follow the state.

| Symptom | Likely cause |
| --- | --- |
| Watch: "Auf dem iPhone einrichten" | Widgets are off on the iPhone, or the widget credential expired or was revoked: turn widgets on again |
| Watch: "iPhone nicht erreichbar" | The iPhone is out of range, in flight mode, or the MowDeck iPhone app was swiped away (open it once) |
| Watch: "Das iPhone erreicht den MowDeck-Server nicht" | The iPhone is away from home without the VPN |
| Watch: "Keine Antwort vom iPhone" | The iPhone did not answer in time; the command may have been sent: check the state |
| Complication shows old values | It greys out after 15 minutes without a new state; watchOS allows about 50 complication updates a day, open the watch app to refresh |
| MowDeck missing under Available Apps | The TestFlight build predates the watch app, or the watch runs watchOS 9 or older |

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

Prerequisites: Node.js 24 (24.21 or newer) with pnpm, JDK 21, the Android SDK (`ANDROID_HOME`; Android Studio installs it).

```bash
bash apps/mobile/scripts/build-android.sh   # -> apps/mobile/build/mowdeck-<version>.apk
```

Without a keystore the APK is debug-signed. Release signing:

1. Create the keystore once and keep it safe (losing it means users must uninstall to update):

    ```bash
    keytool -genkeypair -v -keystore mowdeck-release.jks -alias mowdeck -keyalg RSA -keysize 4096 -validity 10000
    ```

2. Locally: `MOWDECK_ANDROID_KEYSTORE=mowdeck-release.jks MOWDECK_ANDROID_KEYSTORE_PASSWORD=... MOWDECK_ANDROID_KEY_ALIAS=mowdeck bash apps/mobile/scripts/build-android.sh`.
3. For the release workflow, add repository secrets: `ANDROID_KEYSTORE_BASE64` (`base64 -w0 mowdeck-release.jks`), `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, optionally `ANDROID_KEY_PASSWORD` (defaults to the keystore password). The job `android` of `release.yml` builds the APK on ubuntu for every release ([Releases](release.md)) and the release attaches it, with its checksum, Sigstore bundle and build provenance; without the secret the job only warns and the release has no APK. Pull requests never build the native apps (minute budget); the CI job `web` runs the mobile package's lint, `tsc` check and tests, and the CI job `python` the TestFlight readiness checks of the iOS project (`tools/repo-checks/tests/test_ios_release.py`).

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
| Every notification arrives twice on one device | Old registrations of the same device from a build without the installation id. Install the current build (it replaces its own older token); a token that is no longer used is removed when Apple reports it unregistered, or delete the device's rows on the server. |
| Apple Push on, but nothing arrives in the background | Server without APNs key (the panel says so), `APNS_ENVIRONMENT` does not match the build (`development` for a Mac-installed build, `production` for TestFlight), or a different bundle id on server and app. The notifications log names Apple's reason (`BadDeviceToken`, `TopicDisallowed`, ...). |
| Notification arrives without the picture | The build has no extension (older than ADR 0056), `NOTIFICATIONS_APP_URL` is empty or not reachable from the phone over HTTPS, the photo was not ready within 14 s, or the mower had no camera frame (Notifications log: `no photo for the notification`, `the gateway delivered no photo`). The inbox shows the picture when it exists. |
| TestFlight job: `APPLE_PROFILE_IOS_NOTIFICATION_BASE64` missing, or no profile for `...NotificationService` | The extension's App ID and profile are not set up: see [Camera photo in notifications](#camera-photo-in-notifications). |
| TestFlight job: `APPLE_PROFILE_WATCH_BASE64` or `APPLE_PROFILE_WATCH_WIDGETS_BASE64` missing, or no profile for `...watchkitapp` | The watch App IDs and profiles are not set up: see [Apple Watch](#apple-watch). |
| `errSecInternalComponent` over SSH | `~/.mowdeck-sign-pass` missing or wrong. |
| `sign-and-install.sh`: no iPhone found | Unlock the phone; pair by USB once; Wi-Fi install needs the same network. |
| iPhone: the top bar can be dragged under the status bar, or the page bounces | A build from before the edge-to-edge layout (`ios.contentInset: 'always'`, page bounce on). Rebuild with `build-ios.sh` (it runs `cap sync`, which carries `contentInset: 'never'` into the Xcode project) and install again. |
| "Mit Passkey anmelden" missing in the app | The build is older than the passkey hand-off (`MowDeckShell.openAuthSession`): install a current build. Or passkeys are off on the server ([Passkeys](passkeys.md)). |
| The browser opens for the passkey but the app does not take over | Tap "App öffnen" on the page; if more than a minute passed, sign in again from the app. |
| Android: nothing in the background | Expected without ntfy; set up ntfy. Some vendors also kill background apps aggressively ([dontkillmyapp.com](https://dontkillmyapp.com)). |

## What has been verified where

The configuration, scripts and native code were written against the Capacitor 8.5 sources and are covered by unit tests (the web app's bridge module, the first-launch checks, the APNs channel with a fake Apple endpoint). Building the iOS app, installing it with `devicectl`, APNs delivery to a phone, the passkey sign-in through `ASWebAuthenticationSession` and the Custom Tab, the Android Gradle build and the remote-content bridge on real devices are verified on the owner's Mac and phones, not in CI. The TestFlight workflow's project (including the notification extension target), profile and entitlement checks are unit-tested on every pull request, but no CI job compiles the Swift code of the app, the extensions or the watch app (only the TestFlight run does; the project file's object references and file paths are checked statically; `swift test --package-path apps/mobile/ios/NotificationServiceKit` runs the extension's decision tests on a Mac); the macOS build, the App Store signature and the upload are proven by its runs.
