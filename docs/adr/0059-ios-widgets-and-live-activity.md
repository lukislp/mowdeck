# ADR 0059: iOS widgets and the mowing Live Activity

## Status

Accepted (2026-10-27)

## Context

The owner wants MowDeck on the home screen and the lock screen of the iPhone without opening the app:

1. **Status** (small): state icon, state (mowing, station, error, rain pause), battery ring.
2. **Status** (medium): the same plus progress, current area and next run, and (iOS 17) the buttons Start (with the mower's mow defaults), Pause and Station.
3. **Lock screen**: circular (battery ring with the state glyph), rectangular ("Mäht · 42 % · Großer Garten") and inline ("Nächster Lauf 14:00", "Regenpause bis 16:00").
4. **Map** (large): the mower and the mowed path with progress and state, drawn by the server as a picture.
5. **Schedule and weather** (medium): the next runs with the rain pause's verdict (will run, rain pause, uncertain) and the rain status.
6. A **Live Activity** (lock screen and Dynamic Island) during a mowing job: progress, area, remaining time and state; started by the app and, when a schedule or Home Assistant starts a job, by push; updated and ended by the server.

Forces:

- **A widget runs in an extension process the system starts and stops, without the app.** It cannot use the web view's session (an HttpOnly refresh cookie in `WKWebView`, [ADR 0021](0021-browser-auth-flow.md), [ADR 0036](0036-native-app-shells-with-capacitor.md)), and a refresh token shared with the extension would be rotated by whichever process refreshes first and log the other out.
- Whatever the extension holds can be read by a stolen unlocked phone or a backup, so it must be narrow, revocable and short lived where it matters.
- WidgetKit has tight memory and time budgets and no map view; vector map data does not belong in a widget.
- iOS decides when widgets reload (a budget of a few dozen reloads a day) and when a silent push wakes the app (a few per hour); both must be treated as hints.
- Live Activities are started and updated by APNs (`apns-push-type: liveactivity`, topic `<bundle>.push-type.liveactivity`) with two kinds of tokens: a per-device **push-to-start token** (iOS 17.2) and a per-activity **update token**.
- There is no macOS CI ([ADR 0036](0036-native-app-shells-with-capacitor.md)); the extension compiles for the first time in the TestFlight run.

## Decision

### Credential: a device-bound API token, exchanged for 10 minute access tokens

The app's session is not shared. Instead each iPhone gets its own **widget credential**, built on the API tokens of [ADR 0014](0014-authorization-model-and-tuple-ownership.md) and [ADR 0015](0015-tokens-sessions-and-key-rotation.md):

- `POST /api/v1/identity/me/widget-credentials` (a signed-in *user*, not an API token) creates an API token named `iOS widgets: <device>` with `mower.view` and `mower.control` on the account, as far as the caller holds them (a viewer gets `mower.view` only; no `mower.view` on the whole account: 403). It never exceeds the creator's rights, can hold no administrative key, expires after **90 days** and is revoked with its creator. At most ten active credentials per user; `replaces` revokes the earlier one of the device in the same transaction (rotation). Audit: `api_token.widget_created`, `api_token.revoked`.
- The web app (which has the session) creates the credential and hands the secret to the shell (`MowDeckShell.configureWidgets`). The shell only accepts it for the server it is configured for. It is stored in the **Keychain**, accessible `AfterFirstUnlockThisDeviceOnly` (readable by the widget while the phone is locked after the first unlock, never synced to iCloud, never restored from a backup to another device), in the **keychain access group** `<TeamID>.app.mowdeck.mobile.shared` that the app and the extension both list first. The App Group `group.app.mowdeck.mobile` holds only non-secret state (mower list, last snapshot per mower, tokens waiting for the web app).
- The widget exchanges the secret at `POST /api/v1/identity/token` for a 10 minute access token (cached in memory only) and calls the normal APIs with it. The rights are those of the token: the same checks as for the user, per mower, in OpenFGA. Nothing of the widget path is step-up protected (the step-up re-authentication that is being added for critical actions): Start, Pause and Station are ordinary control commands. If step-up ever covers them, the widget needs the same exception or the buttons must open the app.
- The app renews the credential when fewer than 30 days remain (`maintain` on every start of the signed-in app). Logging out and turning widgets off revoke it and clear the Keychain item. It is also listed under Settings -> API tokens, so an admin sees and can revoke it; revoked or expired credentials make the widgets show "Open MowDeck and turn on widgets".
- Why not a refresh token: it rotates. Why not a long-lived user token: it would carry the user's whole rights and sessions. Why not no credential (the app pushes data into the App Group): the widget could not show current data or run its buttons while the app is closed, which is the point.

Residual risk: whoever has the unlocked phone's Keychain (or a jailbroken device) holds a token that can see and control the account's mowers for up to 90 days, until revoked. It cannot reach users, billing, tokens or the audit log, and the owner can revoke it at once (Settings -> API tokens).

### Server

- **Telemetry** `GET /telemetry/mowers/{id}/widget`: a compact state (activity, `offline` while stale, battery, rain sensor, error (since [ADR 0067](0067-error-code-lexicon.md) also its title, category and severity from the error lexicon in the requested `locale`), job with progress, area *name*, remaining time) and `GET /telemetry/mowers/{id}/widget-map.png`: a PNG of the map (areas, no-go zones, the mow path split at its progress, else the last three hours of the track; dock; mower), light or dark, 160..1200 x 96..800 pixels and at most 700,000 pixels, anti-aliased, drawn by numpy and zlib in a worker thread, at most two at a time per replica (503 with the others), cached 30 seconds in memory by an ETag over map revision, path hash, progress, mower position in 0.5 m steps, size and theme (`304` on `If-None-Match`, `Cache-Control: private, max-age=30`). It needs no aerial tile service: an aerial photo under the map is a later parameter that draws the tiles first; the aerial tile proxy of the aerial photo work can feed it.
- **Scheduler** `GET /scheduler/mowers/{id}/widget`: the next runs (at most 3: enabled, not skipped, aimed at the mower directly or through a group), the verdict of the rain pause for now (`runs`, `rain_pause`, `unsure`) and rain facts (raining now, expected millimetres, lawn wet until, pause until).
- Both check `can_view` on the mower and answer 404 for anything else. The widgets make three calls per reload (state, schedule, and the picture for the map widget), plus the mower list when the user edits a widget.
- **Notifications** owns the push side:
  - `PUT /me/live-activities/device` (installation id, push-to-start token, whether the installation has widgets), `DELETE /me/live-activities/device/{installation_id}`, `PUT /me/live-activities/mowers/{mower_id}` (the update token of a running activity; 404 unless the user can view the mower), `DELETE ...?installation_id=` (the user ended it on the device). Tables `live_activity_devices` and `live_activities` (migration 0009). Tokens are never echoed.
  - On every state of a mower the service starts the activity by **push-to-start** on each registered installation of each user who can view the mower (when a job begins: mowing, paused or returning), **updates** the activities the app reported (at most every 15 seconds, only when what the user sees changed; priority 5, 10 on a change of state; `stale-date` after 15 minutes) and **ends** them when the job is over (finished: stays 5 minutes; error: 30 minutes). A user who dismissed the activity gets no restart in that job; a dead token (`410`, `BadDeviceToken`) is dropped.
  - On every change of a mower's activity, installations with widgets get one **silent background push** (`apns-push-type: background`, priority 5, `content-available`, `kind: widget-reload`), at most every two minutes per mower and installation; the app answers it with `WidgetCenter.reloadAllTimelines()` (`UIBackgroundModes: remote-notification`). iOS may drop these; the widgets also reload on their own: every 5 minutes while mowing, 10 on errors, else 15.
  - The channel `ApnsChannel` got `push()` for these types (same HTTP/2 connection, lock, provider token and answer mapping as alerts, 4096 bytes). The `content-state` is camelCase with whole epoch seconds so the Swift `Codable` types decode it without date strategies.
- The OpenAPI documents and the web app's types are regenerated.

### iOS

- A new **Widget Extension** target `MowDeckWidgets` (bundle id `app.mowdeck.mobile.Widgets`, iOS 17.0: `AppIntentConfiguration`, interactive buttons) with five widgets (`MowDeckStatusWidget`, `...LockScreenWidget`, `...MapWidget`, `...ScheduleWidget`) and `MowingLiveActivityWidget`; the mower is chosen per widget with an `AppIntent` (`SelectMowerIntent`, `MowerEntity`). The buttons are `AppIntent`s (`StartMowingIntent` starts all map areas with the mower's saved `mow_defaults.pattern`, `PauseMowingIntent`, `ReturnToStationIntent` = `return_to_dock` without ending the job) that run in the extension against the API.
- **Local Swift package** `apps/mobile/ios/MowerWidgetKit` (library `MowerWidgetCore`, linked by the app and the extension; tests with `swift test`): models, presentation rules (which word and glyph, the inline line, refresh intervals), the credential and Keychain store, the API client (token exchange and cache, one retry on 401, command bodies) and the Live Activity types. No SwiftUI in it.
- **App side**: `WidgetBridge.swift` and new `MowDeckShell` methods (`configureWidgets`, `setWidgetMowers`, `clearWidgets`, `getWidgetStatus`, `reloadWidgets`, `startLiveActivity`, `endLiveActivity`, pending-registration methods) and events (`pushToStartToken`, `liveActivityToken`, `liveActivityEnded`). `LiveActivityCoordinator` observes `Activity.activityUpdates` (so activities started by push are seen too), their `pushTokenUpdates` and `pushToStartTokenUpdates`. The web app (`apps/web/src/lib/native/widgets.ts`, panel "This app" in the settings) turns widgets on and off, rotates the credential and registers the tokens with the server; tokens the shell learns while the web app is not running wait in the App Group (`pending`) and are handed over at the next start.
- Text in `Localizable.xcstrings` (English, German), system colours and SF Symbols (light and dark), `accessibilityLabel`s on rings and the map, Dynamic Type through the system text styles.
- Signing: three App IDs now (app, `.NotificationService`, `.Widgets`), App Group and Keychain sharing on the app and the extension, `NSSupportsLiveActivities`. `release-ios.sh` signs the extension with its own App Store profile (`APPLE_PROFILE_IOS_WIDGETS_BASE64`), `ios_signing.py` expands `$(AppIdentifierPrefix)` in declared entitlements from the profile's team id, and `ios_release_check.py` checks the target, its plist, the shared groups and the built `PlugIns/MowDeckWidgets.appex`.

Options considered:

- **Share the web session or its refresh cookie** with the extension: rotation races and a long-lived user-wide token in the Keychain. Rejected.
- **Direct URL with a token in the query** for the map picture (so `AsyncImage` could load it): leaks into logs. Rejected; the extension fetches with the Authorization header and embeds the PNG in the timeline entry.
- **An aggregating endpoint** that returns state, schedule and weather in one call: it would make Telemetry or Notifications depend on the Scheduler. Three small calls from the extension keep the service boundaries (cost: three requests per reload).
- **Channel-based Live Activities** (iOS 18 broadcast): one push for all devices, but the server must manage channels and a per-device activity still needs its own start. Later, if updates per device become a cost.
- **WidgetKit push updates** (iOS 26 `WidgetPushHandler`, `apns-push-type: widgets`): reloads widgets without waking the app. Not used; the background push works on iOS 17 and up, and the widgets' own schedule covers the gaps. A candidate for a follow-up once the minimum is iOS 26.
- **A refresh interval of one minute while mowing**: the budget would run out; the Live Activity is the real-time surface.

## Consequences

- Owner, once: Apple portal steps and the secret (see [Widgets and the Live Activity](../ops/mobile-apps.md#widgets-and-the-live-activity)), a TestFlight run, then "Turn on widgets" in the app's settings panel.
- Users need `mower.view` on the whole account for widgets (a role limited to one mower or group cannot create the credential: 403, the panel says so). Per-mower credentials would fix that later.
- **Live Activities started by push-to-start get their updates only once the app has told the server the activity's update token.** The system wakes the app briefly when such an activity starts; the shell stores the token in the App Group and emits it to the web view, and the web app sends it. If the web view does not run in that moment, the token is handed over the next time the app is opened and the activity stays at its initial state until then (it ends by its stale date; the end push needs the token as well). Activities started from the app report their token at once. A server-side registration with the widget credential (so the shell can register without the web view) is the follow-up; it needs Notifications to accept API-token principals for these three routes and to resolve their creator.
- The map picture costs CPU on the server (about 100-300 ms per picture, cached 30 s) and a PNG of up to ~100 KB per reload of the large widget, light and dark both.
- iOS below 17 shows no widgets (the extension requires iOS 17); the Live Activity needs 16.2, push-to-start 17.2.
- The first compile of the extension, the entitlements and the profile matching happens in the TestFlight job. Nothing in this change was compiled or run on iOS: see the PR for what is verified and what is inferred.
- Operational: new Notifications tables (migration 0009), no new settings; Live Activity and reload pushes use the existing APNs key and environment (`MOWDECK_APNS_ENVIRONMENT`).
