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
2. The secrets are all set (`apps/mobile/scripts/apple/keychain.sh check`), then `tools/repo-checks/scripts/ios_release_check.py source`: the 1024 px App Store icon without transparency, `ITSAppUsesNonExemptEncryption = false`, the iPad orientations, iPhone and iPad as device families, the App Store entitlements (`apps/mobile/scripts/apple/entitlements/app-store.plist`) mirroring `App.entitlements` with `aps-environment = production`, and a privacy manifest in every target that uses a required-reason API ([below](#privacy-manifests-and-the-app-privacy-answer)). The same checks run in the CI job `python` on every pull request, so a problem App Store Connect would only report after the upload fails the pull request instead.
3. `keychain.sh setup`: a temporary keychain with the Apple Distribution certificate (plus Apple's pinned WWDR intermediate), the App Store profile and the App Store Connect API key, all below `RUNNER_TEMP`; `keychain.sh cleanup` removes them in an `always()` step.
4. `apps/mobile/scripts/apple/release-ios.sh`: the profile must be an App Store profile for `app.mowdeck.mobile` of the team and grant every entitlement (the same for the notification extension's own profile) (`ios_signing.py` names the capability to enable otherwise); then `cap sync ios` (only the `mobile` package and its workspace dependencies are installed, `pnpm install --filter mobile...`), an unsigned `xcodebuild build`, `ios_release_check.py app` (versions, compiled icon, the privacy manifests, no Agora file or reference in the bundle) and `codesign` of the frameworks, the notification extension (`PlugIns/NotificationService.appex`, its own profile), the widget extension, the watch complication and the watch app (`Watch/MowDeckWatch.app`, two more profiles) and the app with the profile embedded. The unsigned build of the `App` scheme also builds the watch targets for watchOS, since the app embeds the watch app. Building unsigned and signing afterwards keeps manual signing settings away from the Swift package targets.
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
2. The app asks for the **server address**, for example `mowdeck.home.lan` or `https://mowdeck.example.org`. It checks that a MowDeck server answers there (`/manifest.webmanifest`); "Use anyway" skips the check. Without a server of your own, "Try the demo" on the same screen connects to the [public demo](../adr/0068-public-demo-mode.md) (`https://mowdeck-demo.lktec.org`) through the same address check and the same error display; the screen also says in two sentences what the app needs and links to the project page and to [Privacy](#privacy). The demo address is the constant `DEMO_SERVER_URL` in `apps/mobile/src/demo-server.ts`. The "Server not reachable" page has no demo action. It needs an app build with this change.
3. Log in as in the browser. The app asks once for permission to send notifications; allow it.
4. To switch servers later: "Change server" on the sign-in and registration pages (under the form), or Settings → Notifications → "Server" when signed in. Both ask for confirmation, then sign out of the old server (push token, widget credential, session; best effort, an unreachable server does not block it) and return to the first-launch screen. The public demo does not lock it. If the server is unreachable at start, the app shows "Server not reachable" with "Try again" and "Choose another server".
5. The language: the web app has a German/English switch in the footer of the sign-in and registration pages and under Settings → Account. The two bundled pages (first-launch and "Server not reachable") have their own German/English toggle that remembers the choice on the device and otherwise follows the system language; it needs an app build with this change, everything else arrives with a server update.

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

First start, server address, "Change server" and the language toggle work as on the iPhone. On Android 13 and later the app asks for permission to show notifications after login.

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

## App Review

For an App Store review (guideline 2.1: the reviewer must be able to use the app) the path is: first launch -> **Try the demo** -> the demo's "Try the demo" sign-in, which gives every visitor an own sandbox account with simulated mowers ([ADR 0076](../adr/0076-demo-sandboxes.md)). No account, server or mower is needed. State that in the review notes. What the demo cannot show, because [ADR 0068](../adr/0068-public-demo-mode.md) switches it off there: push notifications (no APNs devices, no ntfy), the widgets, the Live Activity and the Apple Watch app (they need a widget credential or a push channel). The demo is reset regularly and may answer `503 demo-full` when its sandbox pool is busy; "Try again" later. See [ADR 0036](../adr/0036-native-app-shells-with-capacitor.md) for the shell itself.

## Privacy

The app has no analytics, advertising or tracking SDK and talks only to the MowDeck server the user entered, plus the system services of the notification channel (APNs on the iPhone). This holds for the App Privacy answer in App Store Connect with one exception: **"Try the demo" connects the app to the public demo, which the developer operates** (`https://mowdeck-demo.lktec.org`). It is the only case where the app talks to a developer-operated server, and only after the user taps the button; the demo holds simulated data and sandbox accounts that are deleted after 30 minutes without a visit, and it stores no IP address and no browser string of its visitors ([what the demo stores](demo.md#what-the-demo-stores-about-a-visitor)). The first-launch screen links the project page (`https://github.com/lukislp/mowdeck`), this section and the app's [About page](#about-licences-and-third-party-notices). A separate privacy policy page of the developer, as App Store Connect asks for (a "Privacy Policy URL"), does not exist yet; until it does, this section is what the screen links to.

## Privacy manifests and the App Privacy answer

App Store Connect rejects an upload whose code uses a "required reason" API without a privacy manifest (ITMS-91053). Every bundle with an executable needs its own `PrivacyInfo.xcprivacy`; the manifest of the app does not cover an extension or the watch app ([Describing use of required reason API](https://developer.apple.com/documentation/bundleresources/describing-use-of-required-reason-api)). MowDeck ships four, each declaring the same single category:

| Bundle | Manifest | Declared | Why |
| --- | --- | --- | --- |
| iPhone app | `apps/mobile/ios/App/App/PrivacyInfo.xcprivacy` | user defaults `CA92.1`, `1C8F.1` | the server address (`MainViewController.swift`, key `mowdeck.serverUrl`) and the App Group state (`SharedState`, `WatchLink` in `MowerWidgetKit`); the badge plugin also keeps its count in the app's own defaults |
| Widgets and Live Activity | `apps/mobile/ios/MowDeckWidgets/PrivacyInfo.xcprivacy` | the same | reads and writes the App Group suite `group.app.mowdeck.mobile` through `MowerWidgetKit` |
| Watch app | `apps/mobile/ios/MowDeckWatch/PrivacyInfo.xcprivacy` | the same | `MowerWidgetKit` (`WatchLink`, `SharedState`) |
| Watch complication | `apps/mobile/ios/MowDeckWatchWidgets/PrivacyInfo.xcprivacy` | the same | `MowerWidgetKit` |

`CA92.1` is reading and writing information only the app itself can access; `1C8F.1` is the same for the apps, extensions and App Clips of one App Group. Both are declared in each bundle because `SharedState` and `WatchLink` open the App Group suite and fall back to the app's own defaults when it is unavailable. The notification extension (`NotificationService`, `PushImage`) uses no required-reason API (it writes one temporary file and reads no timestamps), so it has no manifest. No bundle uses the file timestamp, system boot time, disk space or active keyboard categories.

All manifests say `NSPrivacyTracking` false, no tracking domains and no collected data types: MowDeck has no analytics, crash reporting or tracking SDK, and the APNs device token and the credentials go only to the server the user entered, never to the developer. Capacitor's iOS packages ship their own manifest (empty), the plugins `@capacitor/app`, `local-notifications`, `push-notifications`, `splash-screen` and `@capawesome/capacitor-badge` ship none; of those only the badge plugin uses a required-reason API (`UserDefaults.standard`, covered by `CA92.1` in the app's manifest). Third-party manifests are not copied into the repository.

**Adding a required-reason API means updating the manifest.** The release gate (`ios_release_check.py source`, in the CI job `python` on every pull request and again in the TestFlight workflow) scans the Swift and Objective-C sources of each target and of the local packages it links, and fails when a target uses an API of one of the five categories without declaring it with an approved reason code, declares one it does not use, has tracking or collected data switched on, or has an invalid file. `ios_release_check.py app` checks the built `App.app`, `PlugIns/MowDeckWidgets.appex`, `Watch/MowDeckWatch.app` and its complication for their manifest. A new Swift package or target goes into the `TARGETS` table of the script.

**App Privacy answer in App Store Connect: "Data Not Collected"** for the app as distributed from this repository, because nothing is sent to the developer. Two caveats keep that true only while they hold:

- The developer-operated demo (`mowdeck-demo.lktec.org`): a user who signs in there sends their sign-in and usage to a server the developer runs. The first-launch screen offers it as "Try the demo" (see [Privacy](#privacy)), so review the answer before a store submission: the sandbox accounts are anonymous and deleted after 30 minutes without a visit and the data is simulated, and the demo stores no IP address and no browser string (the services' access log is off in demo mode). What remains is the sandbox account with simulated data, session and sign-in times and a random device cookie's hash, deleted with the sandbox. The reverse proxy in front of the demo is outside the application: keep its access log off or anonymised, otherwise that log still counts as a developer-run record.
- A push relay operated by the developer (for example to deliver APNs pushes for hosted installs without each owner holding an Apple key) would receive device tokens and notification content. That changes the answer (Device ID, possibly Other User Content, for App Functionality) and the manifest's `NSPrivacyCollectedDataTypes`, and needs an ADR.

## About, licences and third-party notices

The app shows its own About page, `about.html`, one of the bundled pages: a quiet link "About and licences" ("Über die App und Lizenzen") sits in the links row of the first-launch screen and of the offline page, so it is reachable before any server is configured. It shows

- the **version and build** the native shell reports (`@capacitor/app` `App.getInfo()`: marketing version and `CFBundleVersion` on iOS, `versionName` and `versionCode` on Android); in a plain browser or a development build, where no shell answers, a neutral "shown in the installed app only";
- the **licence of the app's own code**, AGPL-3.0-only (the shared design styles `@mowdeck/ui` are Apache-2.0), and the pointer that the licence and the source offer of the *server* are in the About section of the server's own interface (the AGPL section 13 link of the server build, [Source code](source-code.md));
- the **source code of exactly this version**: `https://github.com/lukislp/mowdeck/tree/v<version>`, the same convention as the web app and the services (`apps/web/src/lib/legal/source.ts`, `X-Source-Code`), the project itself when no release version is known;
- one neutral sentence that MowDeck is not affiliated with the mower manufacturers;
- the **third-party notices**: per licence the text once, and under it the packages with their copyright statements.

German and English follow the language toggle of the bundled pages.

### Where the notices come from

`apps/mobile/public/third-party-notices.json` is generated, never written by hand, by `tools/repo-checks/scripts/app_notices.py generate` from the installed packages: the production dependencies of `apps/mobile`, followed through the workspace link to `libs/mowdeck-ui` and through the lock file's snapshots (`pnpm-lock.yaml`), minus the packages the policy excludes. Licence, copyright statements and the content of a `NOTICE` file are read from each package's `package.json` and licence files. Vite copies `public/` into the bundle, the page fetches the file, and `ios_release_check.py app` fails a build whose `public/third-party-notices.json` is missing, malformed or different from the committed file.

What it holds today (names and licences, no versions):

| Package | Licence | Holder | Why it is in the app |
| --- | --- | --- | --- |
| `@capacitor/core`, `@capacitor/ios`, `@capacitor/android` | MIT | Drifty Co. | the shell itself (JavaScript bridge, iOS and Android runtime) |
| `@capacitor/app`, `local-notifications`, `push-notifications`, `splash-screen` | MIT | Ionic | the plugins |
| `@capawesome/capacitor-badge` | MIT | Robin Genz | icon badge |
| `tslib` | 0BSD | Microsoft Corporation | runtime helper of `@capacitor/core` |
| `@fontsource-variable/ibm-plex-sans`, `@fontsource/ibm-plex-mono` | OFL-1.1 | IBM Corp. | the IBM Plex webfonts of the bundled pages (the OFL text is part of the notices) |

`leaflet` is a dependency of `@mowdeck/ui` for the web app's map and is **not** in the app: the bundled pages import only the `@mowdeck/ui` stylesheets, never its component barrel. The policy lists it under `[[exclude]]` with that reason, and the gate fails when a page starts importing it.

The repo's own code is not a third-party dependency and is not listed: the two bundled pages and the About page (AGPL-3.0-only) and the Swift and Java shell (AGPL-3.0-only). The web app is not bundled; it is loaded from the user's server.

### The gate

`app_notices.py check` runs in the CI job `ci-config` on every pull request, with no installed packages: the packages the lock file puts into the bundle must be exactly those of the committed notices file (a new dependency fails until the file is regenerated, a version bump never does, because the file names no versions), every licence must be allowed by `apps/mobile/third-party-policy.toml`, and nothing may be GPL, AGPL, LGPL or another copyleft licence whatever the policy says. `app_notices.py check --installed` also compares the file with the installed packages (licence, holder, `NOTICE`; the years of a copyright line are ignored), in the CI job `web` after `pnpm install`. The tests are `tools/repo-checks/tests/test_app_notices.py` and, for the bundle, `test_ios_release.py`.

An unknown licence, a missing licence file or a missing copyright holder fails too, unless the policy names the package under `[[override]]` with the missing value and a reason. The policy is the single list: allowed SPDX identifiers, excluded packages, overrides; stale entries fail, so it does not rot.

**Committed or generated at build time?** Committed. The file is the compliance record of what ships, so a change of it is visible in review; it makes the page and the tests work without a prior build; and the gate can still prove it is current at build time through `check --installed`. Generating it only at build time would leave nothing to review and nothing to check on a pull request that has no `node_modules` (`ci-config`).

### When a dependency changes

1. A version bump (Dependabot): nothing to do. If the licence or holder of the package changed, the `web` job fails; run the steps below.
2. A new or removed package of the app or of `@mowdeck/ui`'s production dependencies, or a changed licence: `pnpm install`, then `python3 tools/repo-checks/scripts/app_notices.py generate`, commit `apps/mobile/public/third-party-notices.json`. If a new package is copyleft or has an unlisted licence, the script stops with the reason: replace the package, or (permissive licence only) extend `third-party-policy.toml` with the reasoning in the pull request.
3. A new bundled page that imports a package: nothing else; the gate derives the set from the lock file.

### Not decided here

LICENSING.md only describes what the bundle contains. The AGPL-3.0 compatibility of distributing the app through the App Store (its terms, and an "additional permission" some projects add for it) is the maintainer's legal decision; no exception has been added.

## The local network prompt in German

iOS asks once, in the language of the device, whether MowDeck may find devices in the local network (`NSLocalNetworkUsageDescription`), because the server is usually at an address like `mowdeck.home.lan`. The English default stays in `Info.plist`; `de.lproj/InfoPlist.strings` and `en.lproj/InfoPlist.strings` in `apps/mobile/ios/App/App` carry the localised texts and are wired into `App.xcodeproj` as the variant group `InfoPlist.strings`, a resource of the app target (`knownRegions` already had `en` and `de`). `ios_release_check.py source` checks that both files exist and are referenced, that the English text equals the `Info.plist` default and that the German one is translated. Another language shows the default.

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

## App Store notes: payment

App Store guideline 3.1.1 forbids sending users from the app to an external payment for digital goods, and a call to action pointing to one. On a server with billing enabled, the web app therefore shows the plan inside the iOS and Android app shells (name, state, renewal or end date), but no plans with prices, no Upgrade or Subscribe, no customer portal and no invoice links to the provider's pages: one sentence says that the plan is managed in the web app in a browser, without a link. The decision is `canPurchaseHere` in `apps/web/src/lib/billing/billing.ts` (false when `nativeShell.active`). A browser, including the PWA, keeps the full billing panel. Billing is off by default and in the demo, so most installations show no billing element in either case.

## What has been verified where

The configuration, scripts and native code were written against the Capacitor 8.5 sources and are covered by unit tests (the web app's bridge module, the first-launch checks, the APNs channel with a fake Apple endpoint). Building the iOS app, installing it with `devicectl`, APNs delivery to a phone, the passkey sign-in through `ASWebAuthenticationSession` and the Custom Tab, the Android Gradle build and the remote-content bridge on real devices are verified on the owner's Mac and phones, not in CI. The TestFlight workflow's project (including the notification extension target), profile and entitlement checks are unit-tested on every pull request, but no CI job compiles the Swift code of the app, the extensions or the watch app (only the TestFlight run does; the project file's object references and file paths are checked statically; `swift test --package-path apps/mobile/ios/NotificationServiceKit` runs the extension's decision tests on a Mac); the macOS build, the App Store signature and the upload are proven by its runs. Likewise the About page, the localised local network prompt and the `InfoPlist.strings` entries of the project were checked by unit tests, the static project checks, a Vite build and `plistlib`, not on a device or in Xcode: the next TestFlight run is the first build that compiles them, and the prompt text is seen on a device in German only after that build.
