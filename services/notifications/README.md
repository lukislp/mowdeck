# notifications

Turns mower states into notifications and delivers them by Web Push, APNs, ntfy or other channels.

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; the layer rules and the definition of done are in `docs/dev/service-template.md`. Decision record: [ADR 0022](../../docs/adr/0022-notifications-rules-and-delivery.md).

## Purpose

- Derives notifications from the Device-Gateway's `MowerState` stream and the `mower.connected`/`mower.disconnected` events: mowing finished, mower stuck, battery low, offline and back online, optionally job started. Every state proves contact (the link heartbeat repeats the latest one every 10 s) except the gateway's offline state after a lost link (`connected: false`, [ADR 0040](../../docs/adr/0040-link-heartbeat-for-online-status.md)), which is skipped: `mower.disconnected` and its grace time decide.
- Theft alarm: mower lifted, mower left the dock without a command, mower outside its mapped garden (geofence from the map snapshots, commands from the command stream). Alarms bypass quiet hours and can be acknowledged.
- Blade maintenance reminder from Telemetry's `telemetry.blade_hours.threshold` event.
- Fans them out to the users who subscribed in their preferences (or, without saved preferences, to the users with a registered push target, with the defaults), after checking `can_view` on the mower for every recipient; renders them per user in German or English; respects quiet hours in the user's time zone.
- Keeps each user's inbox (newest 200) and delivers through an outbox with retries: Web Push to the PWA, APNs to the iOS app, ntfy, the service log; further channels are plug-ins.
- **Live Activities and widget reloads** for the iOS app ([ADR 0059](../../docs/adr/0059-ios-widgets-and-live-activity.md)): installations register their ActivityKit push-to-start token (`PUT /me/live-activities/device`) and the update token of each running activity; while a mower is in a job the service starts, updates (at most every 15 s, only on a visible change) and ends the activity by `liveactivity` pushes, and sends one silent `background` push per change of the mower's activity to installations with widgets (at most every 2 minutes per mower). `can_view` on the mower is checked for every user. Tables `live_activity_devices` and `live_activities`.
- Owns its data (PostgreSQL database `notifications`): preferences, push subscriptions, APNs devices, ntfy targets, inbox, deliveries, a mower directory (account and name), one watch per mower (what the rules remember), one geofence per mower (from its map) and the alarm silences of acknowledged alarms. Scales with the number of messages; every replica consumes states (queue group), sweeps for offline mowers and delivers (`SKIP LOCKED`).

## Rules

The rules are pure functions in `domain/rules.py` (table in the module docstring), tested with fixed timestamps. Defaults (settings in brackets):

| Kind | Severity | Fires when | Default |
| --- | --- | --- | --- |
| `mowing_finished` | info | a running job reaches `docked` or `charging` (or reports `charging`) after at least the minimum duration; shorter jobs and jobs ending `idle` end silently | 120 s (`NOTIFICATIONS_MOWING_MIN_SECONDS`) |
| `job_started` | info | the activity becomes `mowing` while no job runs (pause and resume stay one job) | off in user preferences |
| `mower_stuck` | critical | activity `error`, once per error code until the mower leaves the error state; or no position change of `STUCK_MOVE_METERS` for `STUCK_NO_MOVE_SECONDS` while `mowing` (0 disables) | 0.5 m, 300 s |
| `battery_low` | warning | the battery falls below the threshold while not charging; it fires again only after the battery was back at the re-arm level (once per discharge) | 20 %, re-arm 30 % |
| `mower_offline` | warning | no state for `OFFLINE_AFTER_SECONDS` (timer every `OFFLINE_CHECK_SECONDS`), a state with activity `offline`, or `mower.disconnected` without a reconnect within `DISCONNECT_GRACE_SECONDS` (a gateway restart notifies nobody) | 120 s, 30 s |
| `mower_online` | info | the first state or connect after a notified `mower_offline` | |
| `mower_lifted` | critical | activity `error` and the state indicates a lift: `raw.lifted` is `true` (set by the simulator and the Mammotion adapter) or the error category is in `LIFT_CATEGORIES` (both provider neutral), or the error code is in the optional override `LIFT_ERROR_CODES`; once per occurrence until the error clears. Replaces `mower_stuck` for that error (other errors keep `mower_stuck`). Body with the position (6 decimals), link to the map at that position | category `lift_tilt`; no codes |
| `mower_left_dock_unexpectedly` | critical | the activity changes from `docked`/`charging` to anything but `docked`, `charging`, `offline`, `unknown` while no command for the mower was seen within `DOCK_COMMAND_WINDOW_SECONDS` before the departure (see below) | 600 s, confirm 20 s |
| `geofence_exit` | critical | two consecutive states with a position (`rtk_fix` not `none`) outside the mower's mapped areas, transfer paths and dock buffered by `GEOFENCE_MARGIN_METERS`; re-armed once the mower is inside again; not while a manual drive frame was seen within `DOCK_COMMAND_WINDOW_SECONDS`. Body with the position and the distance outside the fence, link to the map | 5 m |
| `vision_proposal`, `vision_auto_proposal` | warning | Vision's `vision.proposal.created` (manual, or started by an error); `vision.proposal.updated` of an automatic analysis without a camera photo | |
| `blade_maintenance` | info | Telemetry's `telemetry.blade_hours.threshold` (one per crossing, `dedupe_key` with `crossed_at`); the contract has no lower severity than `info` | |
| `tracking_started` | critical | Telemetry's `tracking.started` ([ADR 0058](../../docs/adr/0058-theft-tracking-mode.md)): theft tracking began; opens `/mowers/<id>/tracking`. Ignores quiet hours, no cooldown (Telemetry sends no spam), no acknowledge |
| `tracking_moved` | warning | `tracking.moved`: the tracked mower moved 50 m or more (at most every 5 minutes); ignores quiet hours |
| `tracking_stale` | warning | `tracking.stale`: no report for a while (once per outage); ignores quiet hours |
| `tracking_ended` | info | `tracking.ended`: false alarm, stopped or maximum duration; respects quiet hours |
| `firmware_update_available` | info | `firmware_info.update_state` of a state is `available` with a version the mower was not notified for yet (once per offered version; the version is the `dedupe_key`). Links to the mower's settings ([ADR 0038](../../docs/adr/0038-dual-link-per-mower-and-firmware-updates.md)) | |
| `firmware_update_finished`, `firmware_update_failed` | info, warning | `firmware_info` went from `installing` to `idle` (the new version is installed) or to `failed` (body with the mower's `reason`); the Device-Gateway's adapter follows the update, this service only watches the transition | |
| `security_new_login` | warning | Identity's `identity.session.new_device` ([ADR 0065](../../docs/adr/0065-security-center-and-new-login-alerts.md)): a login from a device the user never used before. Only that user (never `notifications.raised`, so not to Home Assistant), inbox plus their channels of that account (saved preferences or the defaults), also during quiet hours, one per session; not a per-kind preference (the opt-out lives in Identity). Opens `/settings/security?revoke=<session>`. Body with the browser and system and the shortened address | |

Every kind is sent at most once per `COOLDOWN_SECONDS` (300) and mower, the three theft alarms at most once per `ALARM_COOLDOWN_SECONDS` (600) per kind and mower; a suppressed offline is not followed by an online. The first state of a mower after a restart of this service notifies nothing. Each event has a `dedupe_key`; a user stores the same event once.

Texts are data: `src/notifications/messages/en.json` and `de.json` map message keys (`<kind>.title`, `<kind>.body` or `<kind>.body_<variant>`) to `str.format` templates with the parameters `mower`, `minutes`, `battery`, `error_code`, `error_message`, `error_detail` (`mower_stuck`: the code in plain words, the title and first step of the vendor's error lexicon in the recipient's language, asked from the Device-Gateway on `gateway.errors.explain` and kept for an hour; the vendor's `error_message` when no lexicon knows the code or the gateway does not answer, ADR 0067), `lat`, `lon` (alarms; `body_no_position` without a position), `meters` (geofence), `activity` (dock departure), `hours` and `threshold` (blade reminder, rounded), `steps`, `confidence`, `error_code` (AI assist), `version`, `installed` and `reason` (firmware), `device` and `where` (new sign-in). Every key exists in both languages. A missing key falls back to English, a missing parameter renders empty.

### Theft alarm

- **Lift detection.** Provider neutral: this service does not import the (GPL) Mammotion adapter and holds no vendor codes. A state is a lift when `MowerState.raw.lifted` is true or its manufacturer-neutral `error_category` is in `NOTIFICATIONS_LIFT_CATEGORIES` (default `lift_tilt`, [ADR 0067](../../docs/adr/0067-error-code-lexicon.md)). The simulator reports its `lifted` error with both, the Mammotion adapter the tilt and lift sensor codes of PyMammotion's error table (1004, 1030, 2502, 2801, 11131, 11133) with both, and a new vendor's adapter sets the category from its error lexicon. `NOTIFICATIONS_LIFT_ERROR_CODES` is an optional operator override for an adapter that sets neither (empty by default).
- **Dock departure and the command race.** A second consumer records the newest command time per mower (`issued_at` of any command on `command.mower.*`, manual drive frames included) in the mower's watch under the same row lock as the states, so the rule stays pure. The gateway executes a command and then publishes the new state, but the command and the state are consumed by different consumers in any order. Therefore a command with `issued_at` from `DOCK_COMMAND_WINDOW_SECONDS` before the departure up to 30 s after the evaluated state (clock skew) explains it, and a departure without a known command is only held (`left_dock_at`): the alarm fires when a state at least `DOCK_CONFIRM_SECONDS` later still shows the mower away from the dock and still no command explains it. Back at the dock before that clears the hold. A redelivered or older command changes nothing. Commands only update a mower that already has a watch (its first state notifies nothing anyway), and a replica records at most one command time per mower and kind every 5 s (drive frames arrive several times a second).
- **Geofence.** Map snapshots (`device.mower.<id>.map`) become the mower's fence: the area polygons, transfer paths and dock in local metres plus the geo reference (table `mower_geofence`). Maps without areas or with the placeholder origin `(0, 0)` are skipped, older snapshots ignored. A position is converted into the local frame (equirectangular, as `mowdeck_control.geo`) and allowed when it lies in an area or within the margin of an area, a path or the dock. A normal simulator job (dock 1 m outside `front`, transfer over the side path) never raises it (tested).
- **Severity, quiet hours, cooldown.** The alarms are `critical`, bypass quiet hours (deliveries are queued at night too) and use `ALARM_COOLDOWN_SECONDS`.
- **Acknowledge.** `POST /inbox/{id}/ack` marks the item read and acknowledged. For an alarm it silences repeats of the same alarm, the key `<kind>:<mower_id>` (the inbox `dedupe_key` is per occurrence), for the user for `ACK_SILENCE_SECONDS` (table `alarm_silences`); the fan-out skips a user with an active silence. Acknowledging other kinds silences nothing.
- **Defaults.** The four new kinds are in the default kinds, so a user who never saved preferences gets them preselected. A user whose preferences were stored before must enable them (the stored `enabled_kinds` are not changed).

`vision_proposal` (warning): the Vision service proposed a maneuver (`event.vision.proposal.created`, durable consumer `notifications-vision`). Recipients need `can_drive` on the mower (not only `can_view`), the notification opens the proposal (`/mowers/<id>?assist=<proposal>`), one per proposal and user; quiet hours apply. The body follows the proposal's `action`: the steps of a `drive` (by task), the return to the dock, or continuing the paused job for `resume`.

`vision_auto_proposal` (warning): the same for a proposal the mower's error started automatically (`trigger` `error` in the event, opt-in per user in Vision, [ADR 0035](../../docs/adr/0035-server-side-camera-frames-and-automatic-analysis.md)). Title "KI-Vorschlag für <Mäher>" / "AI proposal for <mower>", the body names the error code (`error_code`) and says that nothing moves without an approval; same recipients (`can_drive`), link and dedupe. It is a default kind; migration 0004 adds it to stored preferences that enable `vision_proposal`. An automatic analysis that got no camera photo (`vision.proposal.updated` with state `failed`, trigger `error` and `photo_problem`) sends the same kind with the title "<Mäher>: KI-Analyse ohne Kamerabild" / "<mower>: AI analysis got no camera photo" and a body that names the cause (weak Wi-Fi or mobile signal with its dBm, offline, no frame) and asks the owner to start AI assist by hand once the connection is better.

`apns` is a default channel; migration 0005 (table `apns_devices`) adds it to the `channels` of every stored preference, so users who saved preferences before still get pushes on their iPhone.

One APNs registration per app installation (migration 0006): the iOS shell sends a stable `installation_id` (a UUID in its Keychain) with every token, and `apns_devices` is unique per `(user_id, installation_id)` besides per `token`. A device that gets a new token (reinstall, TestFlight over a development build, token rotation) therefore replaces its row instead of adding one, which earlier made every notification arrive twice: APNs keeps accepting an installation's old tokens for a while. Rows without an id (older app builds) keep one row per token; the cleanup on Apple's `Unregistered`/`BadDeviceToken` stays the second safety net.

### Deploy maintenance window

The deploy opens a window before it restarts services and closes it after the health checks ([ADR 0055](../../docs/adr/0055-deploy-maintenance-window.md), [operations](../../docs/ops/compose.md#deploying-with-the-maintenance-window)): `python -m notifications.cli maintenance open --minutes 15 | close | status` (table `maintenance_window`, expires by itself). A `mower_offline` whose offline began inside the window is not sent, so no `mower_online` follows either; a mower still offline `MAINTENANCE_GRACE_SECONDS` after the window ended is reported then. Theft alarms, errors and all other kinds are never held.

## Camera photo (errors and theft alarms)

[ADR 0056](../../docs/adr/0056-camera-photo-in-push-notifications.md). Events of the kinds `mower_stuck`, `mower_lifted`, `mower_left_dock_unexpectedly` and `geofence_exit` (`IMAGE_KINDS`; never the routine kinds) get one camera photo. The fan-out reserves an image id, stores the inbox items and deliveries with it and then asks the Device-Gateway for a frame in the background (`gateway.mower.<id>.photo`, demand `error_analysis`, one frame, `..._PHOTO_TIMEOUT_SECONDS` 20): **the push never waits for the camera**. A budget of its own limits it per mower (`..._IMAGE_MIN_INTERVAL_SECONDS` 60, `..._IMAGE_DAILY_LIMIT` 6 per 24 hours, per replica). The JPEG is kept in `notification_images` (24 hours, at most 200 rows, 512 KB each; a failed capture leaves a marker without bytes); `inbox.image_id` links it and `has_image` tells the clients.

APNs messages with a photo carry `mutable-content: 1` and `image_url`: `<origin>/api/v1/notifications/push-images/<notification id>?e=<expiry>&s=<signature>` (HMAC-SHA256 over the notification id and the expiry, valid ten minutes from the moment the delivery is sent; no account data in it; key from `..._IMAGE_SECRET` or derived from the APNs key; origin `..._IMAGE_BASE_URL` or `..._APP_URL`). The endpoint needs no login and answers 404 (bad signature, or photo not ready yet), 410 (expired, or no photo was taken), 429 (rate limit) or the photo. The iOS Notification Service Extension downloads it and falls back to the text. Web: `GET /inbox/{id}/image` with the session.

## Recipients and delivery

1. The users of the mower's account whose preferences follow the mower (`all_mowers` or the mower in `mower_ids`) and enable the kind, plus every user of the account who registered a target (Web Push subscription, APNs device or ntfy target) but never saved preferences: they count with the defaults, that is every mower they can view, the default kinds (all rule kinds except `job_started`), the default channels, no quiet hours, UTC and `MOWDECK_NOTIFICATIONS_DEFAULT_LANGUAGE`. Saved preferences always win. A user with neither preferences nor a target gets nothing ([ADR 0022](../../docs/adr/0022-notifications-rules-and-delivery.md#amendment-2026-10-22-defaults-for-users-who-never-saved-preferences)).
2. `check_many(can_view, mower:<id>)` for all of them in one batch; a user whose access was revoked gets nothing.
3. Per recipient: render in the user's language, store the inbox item (unique per user and `dedupe_key`) and one delivery per configured target (one per push subscription, one per APNs device, one for the ntfy target, one for `log` or any third-party channel), all in one transaction; during quiet hours no delivery (except for the theft alarms); a user who acknowledged the same alarm within the silence gets nothing. Publish `notifications.sent`.
4. The delivery worker of any replica claims due deliveries per channel, at most as many as the channel has free send slots (10, APNs 1), with `FOR UPDATE SKIP LOCKED`, attempts + 1 and a lease of 60 s. Every delivery is sent in its own task, so a slow or hung channel or target never holds up the others, and the worker claims again as soon as a slot is free. Each send runs under a hard deadline (30 s, below the lease); a send without a result by then is cancelled and logged as an error. Results: sent; retry (also an exception or the deadline) with backoff 30 s doubling to 1 h (or `Retry-After`) up to 8 attempts, then failed; gone (Web Push 404/410: the subscription is deleted; APNs `Unregistered`, `BadDeviceToken`, `DeviceTokenNotForTopic`: the device is deleted); failed (permanent). A delivery still pending after the TTL of its severity (critical 24 h, warning 6 h, info 1 h) is dropped unsent (`expired before delivery`): a late "offline" alert after the mower is back misleads. A claim whose worker crashed or hung expires with the lease and is claimed again by any replica or after a restart (at least once). The worker counts as stalled when no delivery finished and no idle round passed for 5 min: `/readyz` fails (check `outbox`) and an error is logged every minute. Finished deliveries are deleted after 7 days.

## Channels

| Channel | What it sends |
| --- | --- |
| `webpush` | `WebPushPayload` JSON `{title, body, url, tag, kind, mower_id, notification_id}`, encrypted with `aes128gcm` (`pywebpush` 2.5.0 `WebPusher.encode`), VAPID `Authorization: vapid t=...,k=...` (`py-vapid` 1.9.4), `TTL` and `Urgency` by severity (critical 24 h `high`, warning 6 h `normal`, info 1 h `low`), `Topic` per kind and mower; payload below 3000 bytes. Needs the VAPID keys and subject. Endpoints must be HTTPS on `NOTIFICATIONS_WEBPUSH_ALLOWED_HOSTS` |
| `ntfy` | JSON publish to the server root: `topic`, `title`, `message`, `priority` (critical 5, warning 4, info 3), `tags` per kind, `click` when an absolute app URL is configured; `Authorization: Bearer <token>` when the user stored a token. Servers must be on `NOTIFICATIONS_NTFY_ALLOWED_SERVERS` (also checked at send time) |
| `apns` | Apple Push Notification service for the iOS app, token based (no Firebase), over HTTP/2 (`httpx[http2]`): `POST https://<host>/3/device/<token>`, provider token ES256 JWT (`kid` key id, `iss` team id, `iat`; reused for 50 minutes, renewed after `ExpiredProviderToken`), `apns-topic` the bundle id, `apns-push-type: alert`, `apns-priority` 10 for every severity (visible alerts; 5 let iOS hold info alerts), `apns-expiration` by severity like the Web Push TTL, `apns-collapse-id` the Web Push `Topic`; `apns-expiration` counts from the queue time. One request at a time over one connection (connect, read, write and pool timeouts of `..._HTTP_TIMEOUT_SECONDS`, plus a request deadline of twice that, at most 90 % of the send deadline); after the deadline, a timeout or an HTTP/2 protocol error the client is replaced. Why one at a time: APNs announces 200 concurrent streams and lowers that to 1 right after the first request, and httpcore 1.0.9 then waits for stream slots while holding the connection's read lock, so concurrent requests on that connection hang for good without any timeout firing (MowDeck 0.9.1). Body `{aps: {alert: {title, body}, sound: default, thread-id: <tag>, interruption-level: time-sensitive (critical) or active}, url, kind, mower_id, notification_id}`, at most 4096 bytes. Answers: 200 sent; 410 and 400 `BadDeviceToken`/`DeviceTokenNotForTopic` gone (device deleted); 403 `ExpiredProviderToken` retry with a new token, other 403 failed; 429 and 5xx retry (`Retry-After`); other 4xx failed. Needs `MOWDECK_APNS_KEY` (`_FILE`), `MOWDECK_APNS_KEY_ID`, `MOWDECK_APNS_TEAM_ID`; host by `MOWDECK_APNS_ENVIRONMENT` (`development`: `api.sandbox.push.apple.com`, `production`: `api.push.apple.com`) |
| `log` | the rendered message as a `notification delivered` log line (development, tests) |
| e-mail, Telegram | interfaces only (`adapters/channels/extensions.py`) |

### Adding a channel

Write a class with `name` and `async def send(self, delivery: Delivery) -> DeliveryResult` (never raise for delivery problems: return `retry`, `gone` or `failed`; an exception counts as `retry`) and a factory `(ChannelContext) -> channel | None` (None when not configured). Register it in the package that ships it:

```toml
[project.entry-points."mowdeck.notification_channels"]
email = "mowdeck_notify_email:create_email_channel"
```

Install the package into the image, add the name to `MOWDECK_NOTIFICATIONS_CHANNELS`, and users can select it in `channels`. The fan-out queues one delivery without a target for it; the channel looks up its own address for `delivery.user_id`.

## Interfaces

All routes are mounted under `/api/v1` (OpenAPI at `/api/v1/openapi.json`). Traefik routes `/api/v1/notifications` to this service. Everything except the VAPID public key belongs to the caller: user and account come from the verified token, a resource of someone else answers 404.

| Method | Path | Access | Description |
| --- | --- | --- | --- |
| GET | `/api/v1/notifications/push/vapid-public-key` | public | `{public_key}` for `pushManager.subscribe` (`applicationServerKey`); 404 without Web Push |
| GET | `/api/v1/notifications/preferences` | user | Preferences (defaults until saved: every mower, the default kinds and channels, no quiet hours) plus the available kinds, channels and languages |
| PUT | `/api/v1/notifications/preferences` | user | `{enabled_kinds, all_mowers, mower_ids, quiet_hours {start, end}, timezone, language, channels}`; a mower outside the account or not viewable: 404 |
| GET | `/api/v1/notifications/push-subscriptions` | user | The caller's browser subscriptions (endpoint host, user agent, created) |
| POST | `/api/v1/notifications/push-subscriptions` | user | Store `PushSubscription.toJSON()`; 201; the same endpoint again replaces it |
| DELETE | `/api/v1/notifications/push-subscriptions/{id}` | user | 204, 404 for any other id |
| GET | `/api/v1/notifications/inbox/{id}/image` | user | The camera photo of one of the caller's error or theft notifications (`InboxItemOut.has_image`); 404 when there is none, it expired or it is not the caller's |
| GET | `/api/v1/notifications/push-images/{id}?e=&s=` | signed URL | The photo behind an iOS push, for the Notification Service Extension: no login, the signature is the credential (404 / 410 / 429 as above) |
| GET | `/api/v1/notifications/me/push/apns` | user | The caller's iOS devices, oldest first: `{items: [{id, device_name, token_suffix, created_at}]}` (the token itself is never returned, `token_suffix` is its last 8 hex characters) |
| POST | `/api/v1/notifications/me/push/apns` | user | Register the iOS app: `{token, device_name?, installation_id?}` (token hex, 64 to 200 characters, case-insensitive, stored in lower case; name up to 100; `installation_id` a UUID); 201; one row per installation: a new token of the caller's known `installation_id` replaces that row's token (id kept, a row that held the new token is removed); the same token again moves it to the caller (id kept). Without `installation_id` (app builds before it) each token is its own row, as before |
| DELETE | `/api/v1/notifications/me/push/apns/{id}` | user | 204 (the app calls it on logout), 404 for any other id |
| GET, PUT, DELETE | `/api/v1/notifications/ntfy` | user | `{server_url, topic, token?}`; the token is stored, never returned (`has_token`); a `PUT` without `token` (or `null`) keeps the stored token, `""` removes it |
| POST | `/api/v1/notifications/test` | user | A test notification over every configured channel of the caller; 202 `{notification_id, channels}`; 429 with `Retry-After` within `TEST_INTERVAL_SECONDS` |
| GET | `/api/v1/notifications/inbox?limit=50&unread=false` | user | Newest first, plus the unread count |
| POST | `/api/v1/notifications/inbox/{id}/read` | user | Mark read (idempotent), 404 for any other id |
| POST | `/api/v1/notifications/inbox/{id}/ack` | user | Mark read and acknowledged (`acked`, `acked_at`; idempotent), 404 for any other id; for a theft alarm also silences repeats of it (same kind and mower) for the caller for `ACK_SILENCE_SECONDS` |
| GET | `/healthz`, `/readyz`, `/metrics`, `/.well-known/mowdeck-module` | none | Operations; ready when the bus, the subscriptions, the database and OpenFGA are and the outbox is not stalled |

### Plans

`POST /push-subscriptions` and `POST /me/push/apns` need the plan feature `notifications.push` (the feature `notifications` grants it); without it 403 `urn:mowdeck:problem:feature-not-in-plan` ([ADR 0028](../../docs/adr/0028-entitlements-plans-and-enforcement.md)). Reading and deleting subscriptions and devices stay possible. While billing is disabled every account has the plan `unlimited`.

### Client flow (web app)

The PWA (`apps/web`, [docs/dev/web-app.md](../../docs/dev/web-app.md#notifications-and-web-push)) uses the routes like this:

1. The settings page reads `GET /preferences` (the defaults until saved, `stored` false; a user with a registered target already gets them) and saves the whole object with `PUT` (kinds, all mowers or `mower_ids`, quiet hours with the device's IANA time zone, the app language, channels from `available_channels`).
2. Web Push, after a user action: `Notification.requestPermission()`, `GET /push/vapid-public-key` (404: no Web Push here), `pushManager.subscribe({userVisibleOnly: true, applicationServerKey})`, `POST /push-subscriptions` with `PushSubscription.toJSON()`; the answer's `id` marks "this device" in `GET /push-subscriptions`, `DELETE` removes one. The service worker shows the `WebPushPayload` (`title`, `body`, `tag`, `url`, `kind`, `mower_id`) and re-subscribes on `pushsubscriptionchange`; it has no token, so the page posts the renewed subscription on its next start. A gone endpoint (404/410 from the push service) is deleted here.
3. ntfy: `GET`, `PUT`, `DELETE /ntfy`; the token field is write-only in the app.
4. `POST /test` shows the channels on 202 and a countdown from `Retry-After` on 429.
5. The inbox polls `GET /inbox` every 60 s and on focus and marks items with `POST /inbox/{id}/read`. Realtime now also pushes each new item to the person's open sockets (`notification` with `scope` `user`, `id` the inbox id); the app does not use it yet.

The module descriptor announces the `ui_area` `/notifications` (required permission `mower.view`), so the app shows the bell only where this service runs.

**API tokens (Home Assistant).** Inbox items, preferences and channels belong to people; an API token has no inbox and gets 403 on these routes. It receives the account's notifications over the Realtime socket instead (`notification` with `scope` `account`, from `notifications.raised`) when its token holds `notifications.view`: an account key of this module, implied by `mower.view` on the whole account, so a token with `mower.view` on the account has it; it applies per subscribed mower, and the AI assist kinds need `mower.drive` (`can_drive`) as well. Reading or acknowledging needs no call (there is no inbox item); alarms are not silenced by a token.

"user": a person (`user` or `subuser` principal) of an account who holds `notifications.manage` in the token (an account key implied by `mower.view`, so it follows account-wide viewing) or may view at least one mower (a viewer of one group). API tokens and services get 403.

Metrics: `mowdeck_notifications_deliveries_total{channel, status}` (`sent`, `retry`, `failed`, `dropped`) besides the kit's HTTP metrics and `mowdeck_authz_*`.

## Events

| Direction | Subject | Consumer | Payload |
| --- | --- | --- | --- |
| consumed | `telemetry.mower.*` | durable queue `notifications-states` | `MowerState`: the rules |
| consumed | `event.mower.>` | durable queue `notifications-mowers` | `mower.registered`, `.updated`, `.removed` (`MowerRegistration`): directory with account and name; `mower.connected`, `.disconnected` (`MowerConnectionEvent`): offline rule |
| consumed | `event.identity.>` | durable queue `notifications-identity` | `identity.user.disabled`, `.deleted` (`UserChanged`): delete the user's preferences, targets, inbox and deliveries (in that account); `identity.session.new_device` (`NewDeviceLogin`): `security_new_login` for that user |
| consumed | `command.mower.*` | durable queue `notifications-commands` | `CommandEnvelope`: newest command time per mower (`issued_at`; manual drive frames, kind `manual_drive` or correlation id `drive:<session>`, also as drive time) |
| consumed | `device.mower.>` | durable queue `notifications-device`, starting with the newest stored message per subject | `MowerMapSnapshot` on `.map`: the geofence; `.capabilities` ignored |
| consumed | `event.telemetry.>` | durable queue `notifications-telemetry` | `telemetry.blade_hours.threshold` (`BladeHoursThreshold`): `blade_maintenance`; other telemetry events ignored |
| consumed | `event.tracking.>` | durable queue `notifications-tracking` | `tracking.started`, `.moved`, `.stale`, `.ended` (`TrackingEvent`): the four `tracking_*` kinds |
| consumed | `event.identity.grants.changed` | every instance | drop cached authorization decisions |
| request | `fleet.mowers.list` | at startup, retried | seeds the directory (`MowerListRequest` with one shard) |
| request | `gateway.errors.explain` | core NATS, per rendered `mower_stuck` (cached an hour) | `ErrorExplainRequest` / `ErrorExplainReply`: the `error_detail` of the push text (ADR 0067) |
| produced | `event.notifications.sent` | | `NotificationSent`: one rendered notification for one user and the channels it was queued for, with `created_at` (the inbox item's) |
| produced | `event.notifications.raised` | | `NotificationRaised`: every derived notification once per account, before the per-user fan-out (also without any subscriber), rendered in English, with `dedupe_key`, `required_relation` (`can_drive` for the AI assist kinds) and a `notification_id` derived from account and `dedupe_key` (also the event id, so a repeat is a JetStream duplicate). Realtime forwards it to API tokens |

Malformed messages, a subject naming another mower, and an account that differs from the directory (states, commands, map snapshots, telemetry events, connection events) are dropped with a warning (never redelivered).

## Settings

All settings of `mowdeck-service-kit` apply (`MOWDECK_*`, any value also from `MOWDECK_<NAME>_FILE`), plus `MOWDECK_OPENFGA_*` (`mowdeck-authz`) and `MOWDECK_AUTH_*` (`mowdeck-auth`). Service settings (prefix `MOWDECK_NOTIFICATIONS_`):

| Variable | Default | Description |
| --- | --- | --- |
| `..._MOWING_MIN_SECONDS` | `120` | Shorter jobs end without `mowing_finished` |
| `..._STUCK_NO_MOVE_SECONDS` | `300` | Standstill while mowing before `mower_stuck` (0: off) |
| `..._STUCK_MOVE_METERS` | `0.5` | Movement below this is standstill |
| `..._BATTERY_LOW_PERCENT` | `20` | Battery threshold |
| `..._BATTERY_REARM_PERCENT` | `30` | Level that re-arms `battery_low` (never below the threshold) |
| `..._OFFLINE_AFTER_SECONDS` | `120` | Offline without a state for this long |
| `..._DISCONNECT_GRACE_SECONDS` | `30` | `mower.disconnected` becomes offline after this long without a reconnect |
| `..._COOLDOWN_SECONDS` | `300` | One notification per kind and mower within this time |
| `..._OFFLINE_CHECK_SECONDS` | `10` | Offline timer interval |
| `..._MAINTENANCE_GRACE_SECONDS` | `120` | An offline noticed this long after the deploy maintenance window ended is still held back; still offline after that: `mower_offline` is sent |
| `..._MAINTENANCE_LEAD_SECONDS` | `60` | An offline that began this much before the window opened still counts as inside it |
| `..._MAINTENANCE_MAX_MINUTES` | `60` | Longest window `maintenance open` accepts |
| `..._ALARM_COOLDOWN_SECONDS` | `600` | One theft alarm per kind and mower within this time |
| `..._LIFT_ERROR_CODES` | (empty) | Optional extra error codes that mean lifted, for an adapter that sets neither `raw.lifted` nor `error_category`; the core ships no vendor codes |
| `..._LIFT_CATEGORIES` | `lift_tilt` | Error categories (`MowerState.error_category`, the lexicon's categories, ADR 0067) that mean lifted; empty: only `raw.lifted` and the override codes |
| `..._DOCK_COMMAND_WINDOW_SECONDS` | `600` | A command this long before a dock departure explains it; a manual drive frame this recent suspends the geofence |
| `..._DOCK_CONFIRM_SECONDS` | `20` | An unexplained departure alarms when a state this much later still shows the mower away without a command (0: at once) |
| `..._GEOFENCE_MARGIN_METERS` | `5` | Margin around the mapped areas, paths and dock |
| `..._ACK_SILENCE_SECONDS` | `3600` | Acknowledging an alarm silences repeats of it for the user this long |
| `..._DEFAULT_LANGUAGE` | `de` | Message language (`de`, `en`) of a user who never saved preferences |
| `..._CHANNELS` | `webpush,ntfy,apns` | Enabled channels (entry point names); a channel without its configuration is skipped with an info log line |
| `..._VAPID_PRIVATE_KEY` (`_FILE`) | unset | Web Push private key (base64url raw, DER or PEM) |
| `..._VAPID_PUBLIC_KEY` | unset | Optional, checked against the private key |
| `..._VAPID_SUBJECT` | unset | `mailto:` or `https:` contact; Web Push is off without it |
| `..._VAPID_KEY_PATH` | unset | Development only: without a private key, load or create the key file here and log the public key |
| `..._WEBPUSH_ALLOWED_HOSTS` | `fcm.googleapis.com,push.services.mozilla.com,notify.windows.com,push.apple.com` | Host suffixes of accepted push endpoints (empty: any HTTPS host) |
| `..._NTFY_ALLOWED_SERVERS` | `https://ntfy.sh` | ntfy servers users may choose (empty disables ntfy) |
| `..._APP_URL` | unset | Public web app URL; links point to `<url>/mowers/<id>` (relative without it) |
| `..._HTTP_TIMEOUT_SECONDS` | `10` | Timeout per push or ntfy request |
| `..._MAX_ATTEMPTS` | `8` | Attempts before a delivery fails |
| `..._BACKOFF_BASE_SECONDS`, `..._BACKOFF_MAX_SECONDS` | `30`, `3600` | Retry delay, doubling |
| `..._WORKER_INTERVAL_SECONDS`, `..._WORKER_BATCH_SIZE` | `1`, `50` | Worker pause when idle, deliveries per round |
| `..._LEASE_SECONDS` | `60` | Claimed deliveries stay invisible this long; a lost claim is sent again after it |
| `..._SEND_DEADLINE_SECONDS` | `30` | Hard limit of one send through any channel (cancelled, retried); must be below the lease |
| `..._STALL_SECONDS` | `300` | No finished delivery and no idle round for this long: `/readyz` fails, an error is logged |
| `..._DELIVERY_RETENTION_DAYS` | `7` | Finished deliveries are deleted after this |
| `..._INBOX_SIZE` | `200` | Inbox items kept per user and account |
| `..._TEST_INTERVAL_SECONDS` | `60` | Minimum time between two test notifications |
| `..._FLEET_TIMEOUT_SECONDS`, `..._FLEET_RETRY_MAX_SECONDS` | `5`, `30` | Directory seed request |

Camera photo (ADR 0056):

| Variable | Default | Meaning |
| --- | --- | --- |
| `..._IMAGE_ENABLED` | `true` | Take a photo per error or theft event |
| `..._IMAGE_BASE_URL` | `..._APP_URL` | Public origin of the push image URLs; without one no image is sent |
| `..._IMAGE_SECRET` | derived from the APNs key | Key of the URL signature |
| `..._IMAGE_URL_TTL_SECONDS` | 600 | Validity of a push image URL from the send |
| `..._IMAGE_RETENTION_HOURS` | 24 | How long a photo is kept |
| `..._IMAGE_KEEP` | 200 | Photos kept at most |
| `..._IMAGE_MAX_BYTES` | 524288 | Largest photo |
| `..._IMAGE_DAILY_LIMIT` | 6 | Photos per mower and 24 hours |
| `..._IMAGE_MIN_INTERVAL_SECONDS` | 60 | Minimum time between two photos of one mower |
| `..._PHOTO_TIMEOUT_SECONDS` | 20 | Timeout of the photo request to the Device-Gateway |

APNs settings (prefix `MOWDECK_APNS_`):

| Variable | Default | Description |
| --- | --- | --- |
| `MOWDECK_APNS_KEY` (`_FILE`) | unset | PEM contents of `AuthKey_<KEY_ID>.p8` (P-256); usually the secret file `MOWDECK_APNS_KEY_FILE`. The `apns` channel is off without it, the key id or the team id |
| `MOWDECK_APNS_KEY_ID` | unset | Key ID (10 characters, JWT `kid`) |
| `MOWDECK_APNS_TEAM_ID` | unset | Team ID (10 characters, JWT `iss`) |
| `MOWDECK_APNS_BUNDLE_ID` | `app.mowdeck.mobile` | Bundle ID of the iOS app (`apns-topic`) |
| `MOWDECK_APNS_ENVIRONMENT` | `development` | `development`: `api.sandbox.push.apple.com` (builds installed from Xcode with a development provisioning profile); `production`: `api.push.apple.com` (TestFlight, App Store) |

Empty values mean unset. Compose mounts `infra/compose/secrets/apns/` (git-ignored) at `/run/secrets/apns` (`APNS_KEY_FILE=/run/secrets/apns/AuthKey_<KEY_ID>.p8`); Helm mounts the key from the Secret `services.notifications.apns.existingSecret`.

VAPID keys for production: `python -m notifications.cli generate-vapid-keys` prints both as environment lines; keep the private key in a secret file (`MOWDECK_NOTIFICATIONS_VAPID_PRIVATE_KEY_FILE`). Changing the pair invalidates every browser subscription.

## Layers

| Package | Content | Rule |
| --- | --- | --- |
| `domain/` | models, rules, geofence geometry, quiet hours, message rendering | pure Python, no I/O |
| `application/` | watcher (rules use case), fan-out, outbox worker, user settings, ports | depends on `domain` and ports only |
| `adapters/api/` | FastAPI routers, the caller gate, problem mapping | translate HTTP to use cases |
| `adapters/bus/` | subscriptions, `notifications.sent` publisher, directory seed | translate messages to use cases |
| `adapters/channels/` | registry, `webpush`, `apns`, `ntfy`, `log`, VAPID keys, extension interfaces | implement `NotificationChannel` |
| `adapters/db/`, `adapters/memory.py` | PostgreSQL store, Alembic migrations, in-memory store | implement the `Store` port |
| `messages/` | DE and EN templates | data |
| `main.py`, `cli.py` | wiring, `generate-vapid-keys` | |

## Local start

```bash
uv sync
docker compose -f infra/compose/docker-compose.yml up -d --wait nats postgres openfga authz-bootstrap
MOWDECK_DATABASE_URL=postgresql://notifications:<password>@127.0.0.1:5432/notifications \
  uv run python -m notifications.adapters.db.migrate
MOWDECK_ENVIRONMENT=dev MOWDECK_NOTIFICATIONS_CHANNELS=log,ntfy \
  MOWDECK_DATABASE_URL=postgresql://notifications:<password>@127.0.0.1:5432/notifications \
  MOWDECK_OPENFGA_API_URL=http://127.0.0.1:8080 MOWDECK_OPENFGA_API_TOKEN=<key> \
  uv run python -m notifications.main
```

Container image (build context is the repository root): `docker build -f services/notifications/Dockerfile -t mowdeck/notifications:dev .` The Compose block is in `compose.snippet.yml` (pasted into `infra/compose/docker-compose.yml`), Kubernetes uses the umbrella chart `infra/helm/mowdeck`.

## Migrations

Migrations run as a separate step before the service starts (Compose: `notifications-migrate`, Helm: a Job). They must stay backward compatible for rolling updates.

```bash
uv run alembic -c services/notifications/alembic.ini revision -m "add table"   # new revision
uv run python -m notifications.adapters.db.migrate                            # upgrade head
```

## Tests

```bash
uv run pytest services/notifications --cov=notifications
```

Integration tests run when their server is configured: PostgreSQL (`MOWDECK_TEST_DATABASE_URL`: migrations up and down, drift, the store contract, a full flow), NATS JetStream (`MOWDECK_TEST_NATS_URL`: states published through JetStream become a delivery) and ntfy (`MOWDECK_TEST_NTFY_URL`, for example `docker run -d -p 8091:80 binwiederhier/ntfy:v2.28.0 serve`). The APNs tests sign provider tokens with a generated P-256 key and check every request and answer against a mock transport. The Web Push test runs the real `pywebpush` encryption and `py-vapid` signature against a fake push service that verifies the VAPID token and decrypts the `aes128gcm` body.
