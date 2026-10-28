# ADR 0060: Apple Watch app

## Status

Accepted (2026-10-28)

## Context

The owner wants MowDeck on the wrist: the mower's state (activity, battery, progress, error, offline), **Start** with the mower's saved mow defaults, **Pause**, **Station** (return to dock) and an **emergency stop with a confirmation step**, plus a **complication** with battery and state on the watch face. iOS only; no Wear OS app.

Forces:

- watchOS has no `WKWebView`. The Capacitor shell ([ADR 0036](0036-native-app-shells-with-capacitor.md)) cannot be reused; the watch app must be native SwiftUI.
- The MowDeck server is **LAN-only**: reachable at home or through the user's VPN on the iPhone. watchOS has no VPN client of its own. When the paired iPhone is nearby, the watch's own `URLSession` traffic is routed through the iPhone; whether that path uses the iPhone's VPN tunnel is not documented by Apple and was not verified. Away from the iPhone (LTE watch, or a watch on a Wi-Fi the iPhone knows) a request goes out directly and reaches a LAN-only server only on the home Wi-Fi.
- The widgets of [ADR 0059](0059-ios-widgets-and-live-activity.md) already gave each iPhone a **device credential** (an API token with `mower.view` and `mower.control`, 90 days, in the iPhone's Keychain), and the Swift package `MowerWidgetKit` already has the API client, the models and the presentation rules (word, glyph, battery ring, progress) the watch needs.
- Commands are not step-up protected ([ADR 0057](0057-step-up-reauthentication.md)); `emergency_stop` needs only `can_emergency_stop`, which equals `can_view`, and Fleet sends it with `priority = emergency` ([ADR 0049](0049-manual-driving-over-the-mammotion-cloud.md) describes what the adapter does on a stop).
- A watch is easy to lose or leave on a table. Whatever it holds can be used by whoever finds it until it locks (wrist detection) or is erased.
- The owner already ships a WatchConnectivity watch app in another project, with proven patterns: activate the session at launch, `sendMessage` with a reply handler for commands, `updateApplicationContext` for the latest state, the watch app writing the state into its own App Group container for the complication.

## Decision

### Architecture: everything relayed through the iPhone app (WatchConnectivity)

Two designs were weighed:

- **(a) The watch calls the API itself** with its own credential, provisioned once from the iPhone over WatchConnectivity (a second device credential, `Apple Watch: <device>`). The watch would work while the iPhone app is not running, but it would only reach the LAN-only server through the iPhone anyway (or on the home Wi-Fi), it would add a credential on the device that is easiest to lose, a second credential to rotate and revoke, and token exchange and HTTP code on the watch.
- **(b) Relay through the iPhone app.** The watch holds **no credential** and never calls the server. It sends each command to the iPhone app with `WCSession.sendMessage(_:replyHandler:errorHandler:)`, which wakes the iPhone app in the background if it is not running; the iPhone runs the command and replies with the outcome. State flows to the watch as the application context.

**Chosen: (b).** The iPhone is the network path to a LAN-only server in every realistic case, so (a) buys little availability; (b) adds no credential and no new attack surface on a lost watch, needs no server change, and reuses the owner's proven WatchConnectivity patterns. The cost: the watch does nothing without its iPhone nearby, and says so ("iPhone nicht erreichbar").

### Which credential the iPhone uses

The iPhone side runs **natively**, not in the web view: a message from the watch can launch the app in the background without a scene, so the Capacitor bridge and the web session (an HttpOnly refresh cookie inside `WKWebView`) are not available. The relay therefore uses the **device credential of ADR 0059** from the iPhone's Keychain with `MowerWidgetKit`'s `MowDeckClient` (exchanged for 10 minute access tokens, the same per-mower checks in OpenFGA as for the user). Consequences:

- **No new credential and no new scope.** `mower.view` covers state and the emergency stop, `mower.control` Start, Pause and Station; nothing on the server changes.
- **The watch needs widgets turned on** in the iPhone app (Settings -> This app). Without the credential the iPhone answers `not_set_up` and the watch shows "Auf dem iPhone einrichten". The settings panel shows the watch's state (no watch paired, app not installed, needs widgets, ready).
- Turning widgets off, logging out, or revoking the token in Settings -> API tokens also stops the watch at once; the iPhone sends the watch a "not configured" state.

### Protocol (`MowerWidgetKit/WatchLink.swift`, tested with `swift test`)

- **Command** (watch -> iPhone, `sendMessage` with a reply handler): `{v: 1, op: "command", command: start|pause|dock|emergency_stop, mowerId: <UUID>}`. The iPhone ignores another version, another operation, another command and a mower id that is not a UUID (it ends up in a URL path). `start` starts all areas with the saved `mow_defaults.pattern`, exactly like the widget button; `dock` is `return_to_dock` with `end_job: false`.
- **Reply**: `{v: 1, ok: true}` or `{v: 1, ok: false, failure: not_set_up|not_allowed|not_found|server_unreachable|bad_request|failed}`. The watch adds `phone_unreachable` (not reachable, nothing sent) and `no_answer` (the reply timed out: the command may have been sent, check the state).
- **Refresh** (watch -> iPhone): `{v: 1, op: "refresh"}` when the watch app comes to the foreground; the answer is a new application context.
- **State** (iPhone -> watch): `{v: 1, state: <JSON>}` with `configured`, `serverReachable`, `updatedAt` and up to eight `MowerSnapshot`s (the widgets' status and schedule shapes). Sent with `updateApplicationContext` (latest wins) after every command, on a refresh, when widgets are turned on or off, when the session activates, and on the silent `widget-reload` push the Notifications service already sends on every state change (ADR 0059).
- **Nothing is queued.** Commands never use `transferUserInfo`: a Start delivered an hour late is not wanted. If the iPhone is unreachable, nothing is sent and the watch says so.

### Emergency stop

- A confirmation dialog on the watch ("Mäher sofort stoppen?" -> "Not-Stopp" / "Abbrechen"), then `sendMessage` at once. When the iPhone does not answer or is not reachable, the watch tries **three times within about two seconds** (a second stop is harmless; the other commands are sent once). It shows the outcome with a haptic ("Not-Stopp gesendet", or why not).
- The button is always offered, also while the iPhone looks unreachable or another command is on its way; only a stop already in flight disables it.
- **No step-up**, deliberately: stopping a mower must never be slowed by a re-authentication, and it needs only the right to see the mower. A stop cannot unlock, delete or reveal anything. (API tokens could not do a step-up anyway.)
- "Sent" means Fleet accepted the command (202). Whether the mower stopped is the next state; the latency over 4G is the adapter's ([ADR 0049](0049-manual-driving-over-the-mammotion-cloud.md), open on hardware).

### Complication

A WidgetKit extension inside the watch app (`accessoryCircular`: battery ring with the state glyph; `accessoryCorner`; `accessoryRectangular`: name, state, battery, progress or error; `accessoryInline`: "Mäht · 64 %"). It shows the mower last opened on the watch (else the first), reads only the state the watch app keeps in the watch's own App Group container, and greys out after 15 minutes without a new state. No network, no credential.

**Budget.** watchOS allows an app about 50 `transferCurrentComplicationUserInfo` calls a day; they wake the watch app in the background. The iPhone spends one only when the watch face would visibly change (`ComplicationPolicy`: another state, a battery step of 10 %, a progress step of 25 %, the mower list or the configuration) and never for a refresh the watch asked for; everything else is the application context, which the watch reads the next time its app runs. A mowing day with a state change every few minutes stays well inside the budget; when it is used up, the face updates the next time the watch app opens.

### Targets and signing

- `MowDeckWatch` (watchOS 10, single-target watch app, `app.mowdeck.mobile.watchkitapp`, `WKCompanionAppBundleIdentifier` = `app.mowdeck.mobile`, `WKRunsIndependentlyOfCompanionApp` = false), embedded in `App.app/Watch`; `MowDeckWatchWidgets` (`app.mowdeck.mobile.watchkitapp.Widgets`) in its `PlugIns`. Both link `MowerWidgetCore` (the package now declares watchOS 10; ActivityKit stays iOS-only). Entitlements: the App Group `group.app.mowdeck.mobile` only, **no Keychain group**; `ios_release_check.py` fails if the watch targets get any other entitlement.
- Text in `Localizable.xcstrings` (English and German), SF Symbols and system colours, accessibility labels on the battery gauge and the complication.
- iPhone side: `WatchRelay.swift` (activated in `didFinishLaunching`, before a background-launch message is delivered; a background task keeps the command and the state that follows alive), `MowDeckShell.getWatchStatus` and `syncWatch`, and the settings panel in the web app.
- TestFlight: two more App IDs and App Store profiles (`APPLE_PROFILE_WATCH_BASE64`, `APPLE_PROFILE_WATCH_WIDGETS_BASE64`); `release-ios.sh` signs inside out (complication, watch app, iPhone app). The owner's steps are in [operations](../ops/mobile-apps.md#apple-watch).
- `ios_release_check.py` now also checks that every object id in the project file is defined once and referenced ids exist, and that every file of the project's groups and every `INFOPLIST_FILE` / `CODE_SIGN_ENTITLEMENTS` exists. That check found that the `MowDeckWidgets` group and settings of ADR 0059 pointed into `App/` instead of `apps/mobile/ios/MowDeckWidgets`; fixed here.

Options considered and rejected:

- **(a) with a watch credential**: see above; it can be added later if a watch must work while the iPhone is away from a reachable server, e.g. on the home Wi-Fi only. The design would be a third credential kind with the same two keys, its own token-list entry and revocation, and a shorter lifetime.
- **Queued commands** (`transferUserInfo`) as a fallback: late, unseen execution of a Start or Station. Rejected; only the state is delivered late.
- **Step-up for the emergency stop**: safety first, see above.
- **A Wear OS app**: not wanted now.

## Consequences

- The watch works only with the iPhone in Bluetooth or Wi-Fi range (the watch shows "iPhone nicht erreichbar" otherwise) and only where the iPhone reaches the server (at home, or with the VPN on the iPhone). A watch with LTE away from its iPhone shows the last known state and cannot command.
- If the iPhone app was force-quit by the user, iOS may not wake it for watch messages until it is opened again; the watch then reports `phone_unreachable` or `no_answer`.
- The watch's values are as fresh as the iPhone's last read: after a command, on opening the watch app, and on the server's silent state pushes (which iOS may throttle, ADR 0059). The watch shows when its state is older than 15 minutes.
- Widgets must be on for the watch: users who do not want widgets on the home screen still turn the feature on (the credential is the same; the widgets need not be added).
- No server change, no migration, no new setting.
- Owner, once: two App IDs, two profiles and two secrets ([operations](../ops/mobile-apps.md#apple-watch)), a TestFlight run, then install the watch app from the Watch app on the iPhone. **Nothing of the watch app was compiled**: there is no macOS CI ([ADR 0036](0036-native-app-shells-with-capacitor.md)); the first TestFlight run compiles it.
