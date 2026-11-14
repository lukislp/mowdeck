# ADR 0084: Area-clear confirmation on the native surfaces

## Status

Accepted (2026-11-14)

## Context

MowDeck starts a machine with blades remotely. The web app asks before every user-initiated start of blade movement or remote motion (start, resume, spot run, manual drive, leave dock, schedules, chat and AI assist): the area must be clear of people, children and animals, and the user supervises the mower as its manufacturer's manual describes. That check is a client safeguard on top of the server's safety rules ([ADR 0012](0012-mower-control-interface-and-safety-rules.md)); it stores nothing on the server. Its pull request (#367) left the native surfaces out: the medium widget's **Start** ([ADR 0059](0059-ios-widgets-and-live-activity.md)), the "Start mowing" App Intent that Siri and Shortcuts can run, and the Apple Watch app's **Start** ([ADR 0060](0060-apple-watch-app.md)) still started the mower with one tap. App Review (guideline 1.4, physical harm) and plain safety need the same question there.

The native code was searched for every command that makes the mower move. Native code sends only `start_mow` (widget button, `StartMowingIntent`, the watch's Start relayed by the iPhone); `resume_mow` exists in the client but no surface sends it; there is no native drive, spot run or schedule control. The Live Activity and the watch complication have no buttons (tapping opens the app or the watch app). Android has no native controls (the web app runs in the shell and brings its own check).

Apple's constraints:

- An interactive widget runs its App Intent in the widget extension's process, without UI ([Adding interactivity to widgets and Live Activities](https://developer.apple.com/documentation/widgetkit/adding-interactivity-to-widgets-and-live-activities)). Apple's documented way for a widget interaction that opens the app is `Link` or `widgetURL`. `openAppWhenRun` "generates an error if the app intent runs in an app extension" ([openAppWhenRun](https://developer.apple.com/documentation/appintents/appintent/openappwhenrun)), so an intent button in this extension cannot hand over to the app either.
- Siri and Shortcuts show a confirmation for an intent that calls `requestConfirmation`: `requestConfirmation(conditions:actionName:dialog:)` from iOS 18, whose default conditions are empty, that is always ask ([documentation](https://developer.apple.com/documentation/appintents/appintent/requestconfirmation(conditions:actionname:dialog:))); on iOS 17 (the widget extension's deployment target) `requestConfirmation(result:confirmationActionName:showPrompt:)`, deprecated from iOS 18. Both return when the user confirms and throw when they cancel.
- SwiftUI's `confirmationDialog` works on watchOS 10 (the watch app already uses it for the emergency stop).

## Decision

**One policy, one wording, in MowerWidgetCore.** `MotionCommandPolicy.needsAreaClearConfirmation(_:)` is an exhaustive switch over `MotionCommand` (the Fleet command kinds native code can send): `start_mow` and `resume_mow` are asked; `pause_mow`, `return_to_dock` and `emergency_stop` never are, stopping is never delayed. Every surface asks the policy and none decides by itself. `AreaClearWording` holds the text, the web app's `safety_title`, `safety_statement` and `safety_confirm` in English and German, as `Localizable.strings` resources of the package (one catalog for the app, the widget extension and the watch; a unit test checks both languages are complete). Nothing is remembered: every start asks again, as on the web.

**Per surface:**

| Surface | Before | Now | Apple API |
| --- | --- | --- | --- |
| Medium widget, **Start** | intent button, one tap | a `Link` to `mowdeck://mowers/<id>?confirm=start` (`AreaClearLink`). The app shows a system alert (title, mower name, statement, "Confirm" / "Cancel") and only after "Confirm" sends the start natively with the widget credential; "Cancel" sends nothing. Without the app's interception the web app would open the mower page and ignore the query | `Link`; `UIAlertController` in the app |
| Widget **Pause**, **Station** | intent buttons | unchanged, one tap | `Button(intent:)` |
| Siri and Shortcuts, "Start mowing" (`StartMowingIntent`) | started at once | asks before sending; cancelling throws and sends nothing | `requestConfirmation(actionName:dialog:)` on iOS 18, `requestConfirmation(result:confirmationActionName:showPrompt:)` on iOS 17 |
| Apple Watch, **Start** | one tap | `confirmationDialog` with the same wording; the message to the iPhone carries `areaClear: true`, and the iPhone refuses a start without it while parsing (`bad_request`), so an older or forged watch message cannot start the mower unasked | SwiftUI `confirmationDialog` |
| Apple Watch, **Pause**, **Station**, **emergency stop** | one tap (stop: its own confirmation) | unchanged | |
| Live Activity, watch complication | no buttons | nothing to guard | |

The confirmation link only ever brings up the question; whoever opens it (another app, a web page) cannot start the mower through it.

The decision for the widget was between asking in the extension (not possible: no UI in the intent's process), dropping Start from the widget (loses a feature the owner uses), and opening the app. Opening the app costs one more step and shows the same question as everywhere else.

**Accessibility.** System alerts and confirmation dialogs support VoiceOver and Dynamic Type. The watch's Start and the widget's Start get an accessibility hint saying they ask first (the widget's: it opens MowDeck to confirm). After the app sent the start, VoiceOver announces "Start sent."

**Server unchanged.** As on the web, this is a client safeguard; no "confirmed at" or other field goes to the server, and the server validates every command as before (ADR 0012).

## Consequences

- A start from the widget opens the app; Pause and Station still work without it. The user guides say so.
- A new native command that moves the mower does not compile until `MotionCommandPolicy` decides about it; a new surface asks the policy and uses `AreaClearWording`.
- The wording is kept in step with the web app by hand (the unit test pins the English title and statement to the web text).
- A watch app older than this change cannot start the mower (the iPhone refuses its unconfirmed start); watch and iPhone app ship together, so this lasts only until the watch app updates.
- Pull requests that touch the Swift code, the Xcode project or the app scripts now compile the app, the extensions and the watch app unsigned and run the package tests on a macOS runner (job `ios` in ci.yml); before, only the TestFlight run compiled them.
- What a device must show and CI cannot: the Siri and Shortcuts prompt (spoken and shown), the widget link opening the app on a cold and a warm start, and the watch dialog. The owner checks them on the next TestFlight build ([mobile apps](../ops/mobile-apps.md#app-review)).
