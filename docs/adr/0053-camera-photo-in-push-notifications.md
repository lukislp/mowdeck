# ADR 0053: Camera photo in push notifications for errors and theft alarms

## Status

Accepted (2026-10-27)

## Context

A notification such as "mower lifted" or "mower stuck" is much easier to judge with the mower's own camera picture next to it: a person carrying it away, the fence it ran into, the branch it sits under. The owner asked for exactly that on the iPhone (Android later), with three conditions:

1. **Only error and theft kinds** get a photo: every `mower_stuck` (stuck, trapped, obstacle, outside the area, wheel slip all arrive as that kind), `mower_lifted`, `mower_left_dock_unexpectedly`, `geofence_exit`. Routine kinds (finished, battery, offline, ...) never do.
2. **The picture is never stored at Apple.** The APNs payload may only point to the owner's own server.
3. **Nothing may delay a theft alarm** more than necessary, and the mower's 4G data is limited.

Existing pieces: the Device-Gateway answers `gateway.mower.<id>.photo` ([ADR 0035](0035-server-side-camera-frames-and-automatic-analysis.md), [ADR 0045](0045-camera-relay.md)) with the mower's own photo function or a frame of the camera relay's tap (reusing an open viewer session); the demand `error_analysis` is the one Vision uses for a mower with a problem. Notifications already queues APNs deliveries through an outbox with retries, has cooldowns for the alarms and an inbox per user.

## Decision

**When the photo is taken: in parallel, after the push is queued.** The fan-out reserves an image id for the event, stores the inbox items and the deliveries with it, and only then starts the photo request in the background (`Fanout._take_photo`). The push never waits for the camera. The alternative, taking the photo first with a tight timeout (about 8 s) and then sending, would delay every channel (Web Push, ntfy) and the alarm itself by the camera latency in the common case; a photo that arrives at 9 s would be lost. In parallel, the push leaves at once and the iOS extension (below) decides how long to wait.

**One photo per event,** shared by all recipients: `PhotoRequest` with demand `error_analysis` and one frame, timeout 20 s. The Notifications service keeps a budget of its own on top of the alarm cooldowns (10 min for theft alarms, 5 min for stuck): at least 60 s between two photos of one mower and at most 6 per mower and 24 hours (`MOWDECK_NOTIFICATIONS_IMAGE_MIN_INTERVAL_SECONDS`, `..._IMAGE_DAILY_LIMIT`, per replica, kept in memory). The Vision budget does not apply to `error_analysis`, which is why Notifications needs its own. A mower that is asleep or has no camera simply yields no photo.

**Storage.** Table `notification_images` (migration 0007): the JPEG bytes in PostgreSQL, one row per event, 512 KB at most each, kept 24 hours (`..._IMAGE_RETENTION_HOURS`) and at most 200 rows (`..._IMAGE_KEEP`), so the table is bounded to about 100 MB. Expired rows go with the next write and in the hourly prune. A failed capture leaves a marker row without bytes (1 hour), so the push URL can answer "gone" immediately instead of making the phone wait. `inbox.image_id` points the inbox items of the event at the row; `GET /inbox/{id}/image` (normal session, own items only) returns it and `InboxItemOut.has_image` tells the web app, whose inbox shows the photo under the item.

**Push payload.** APNs messages with a photo get `aps.mutable-content = 1` and `image_url`. Everything else is unchanged, so older app builds ignore both keys and show the text. The URL is `<public origin>/api/v1/notifications/push-images/<notification id>?e=<expiry>&s=<signature>`: the notification id (a random UUID, one per user and event) is the only identifier in it, no user, account, mower or device id. The signature is an HMAC-SHA256 over a purpose string, the id and the expiry, compared in constant time; the key is `MOWDECK_NOTIFICATIONS_IMAGE_SECRET`, by default derived from the APNs key (so no new secret has to be configured). The expiry is ten minutes after the delivery is *sent* (a retried delivery gets a fresh URL). The public origin is `MOWDECK_NOTIFICATIONS_IMAGE_BASE_URL`, default `MOWDECK_NOTIFICATIONS_APP_URL`; without it or without a key, pushes carry no image.

**Serving it.** No login: the signed URL is the credential and works for exactly one photo until it expires. Answers: wrong signature or unknown notification 404; genuine but expired 410; photo not ready yet 404; capture failed or photo expired 410; otherwise the JPEG with `Cache-Control: private, no-store` and `nosniff`. Requests are rate limited in memory (40 per notification and 600 in total per minute, 429). The "not ready" 404 and the "never" 410 are distinct on purpose: they are how the extension knows whether to keep asking.

**iOS Notification Service Extension** (`apps/mobile/ios/App/NotificationService`, bundle id `app.mowdeck.mobile.NotificationService`, embedded in the app, same team and version). It reads `image_url` (https only, the push image path only, no credentials in the URL), asks for the photo once a second for at most 14 seconds (each request 6 s, so it finishes well inside the 30 s iOS allows), writes it to a temporary file and attaches it. Any failure, a 410, a timeout or the system ending the extension shows the unchanged text notification. The decisions live in `PushImage.swift`, a plain Swift file also built as a Swift package with unit tests (`swift test --package-path apps/mobile/ios/NotificationServiceKit`). Latency: if the photo is not ready, the notification appears up to 14 s later than the text-only push would. Typical relay frames take a few seconds; the window is a constant in `PushImage.swift` if the owner prefers less.

**Signing in CI.** The TestFlight workflow signs manually (profiles from secrets, `codesign` inside out). The extension is another bundle with its own App ID and App Store profile, so the workflow takes a second profile secret `APPLE_PROFILE_IOS_NOTIFICATION_BASE64`; `release-ios.sh` finds, checks and signs the `.appex` with it and an empty entitlement set (plus the profile's identity keys) before it signs the app, and verifies `get-task-allow` is false. `ios_release_check.py` checks the extension target, its Info.plist and its presence and version in the built bundle, on every pull request and in the job. The Mac development scripts use automatic signing for the extension as for the app.

## Consequences

- Error and theft notifications can carry a picture on the iPhone and in the inbox; nothing about the picture reaches Apple except a URL that expires in ten minutes.
- The owner must create one App ID and one profile in the Apple portal and add one secret before the next TestFlight build (documented in [Mobile apps](../ops/mobile-apps.md)). Until then the TestFlight job fails early at the secret check, nothing else changes.
- Each event costs at most one camera frame over the mower's connection; the budget bounds it at six per day per mower.
- Images sit in PostgreSQL for a day: a database backup contains them. They are not encrypted at rest beyond what the database offers.
- A leaked push URL exposes one photo for at most ten minutes; the rate limits make guessing signatures pointless (HMAC-SHA256).
- A multi-replica installation keeps the photo budget per replica; the worst case is the daily limit times the number of replicas.
- Android (ntfy and the Capacitor push plugin) and Web Push could use the same URL later; the inbox already shows the photo in the web app.
- Not verified without the hardware: the real camera latency over 4G and the extension on a phone (a TestFlight build is needed).