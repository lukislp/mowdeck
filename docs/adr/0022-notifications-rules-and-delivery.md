# ADR 0022: Notifications: rules, recipients and delivery

## Status

Accepted (2026-10-09)

## Context

Phase 2 promises notifications for "finished, stuck, battery, offline" over Web Push to the PWA, with ntfy as an alternative (project plan, feature table). The Notifications service owns subscriptions and preferences, is reusable, and is the `NotificationChannel` extension point (Web Push, ntfy, e-mail, Telegram). The inputs exist already: the Device-Gateway publishes a `MowerState` on every significant change and as a heartbeat every 10 s on `telemetry.mower.<id>`, and `mower.connected`/`mower.disconnected` events ([ADR 0013](0013-device-gateway-registry-and-sharding.md)); Fleet publishes the `mower.*` registry events; Identity publishes `identity.user.*` and `identity.grants.changed` ([ADR 0015](0015-tokens-sessions-and-key-rotation.md)). Authorization is OpenFGA through `mowdeck-authz` ([ADR 0014](0014-authorization-model-and-tuple-ownership.md)). Services are stateless and scale out (Twelve-Factor); all persistence is PostgreSQL ([ADR 0006](0006-postgresql-for-all-persistence.md)).

Four questions had to be answered: where the rules run, who receives a notification, how delivery survives failures and replicas, and how channels are added.

## Decision

**Rules run in the Notifications service, derived from telemetry.** The gateway stays a translator of mower protocols and publishes facts (states, links); "a mow finished" or "the battery is low" is an interpretation that depends on thresholds, hysteresis and cooldowns, and other consumers (Telemetry, Realtime) do not need it. The rules are pure functions over a `MowerWatch` (what the service remembers per mower: last activity, job start, battery arming, notified error, standstill anchor, disconnect time, offline flag, last notification per kind), tested with fixed timestamps:

| Kind | Fires when | Default |
| --- | --- | --- |
| `job_started` | the activity becomes `mowing` while no job runs | off in user preferences |
| `mowing_finished` | a running job reaches `docked` or `charging` after at least the minimum duration | 120 s |
| `mower_stuck` | activity `error` (once per error until it clears), or no position change while `mowing` | 300 s, 0.5 m |
| `battery_low` | below the threshold while not charging; again only after the re-arm level (hysteresis) | 20 %, 30 % |
| `mower_offline` | no state for a while, a state with activity `offline`, or `mower.disconnected` without reconnect within a grace time | 120 s, 30 s |
| `mower_online` | the first contact after a notified `mower_offline` | |

A kind is sent at most once per cooldown (300 s) and mower. The first state of a mower notifies nothing, so a restart does not repeat old news. The watch is stored as JSON per mower and updated `FOR UPDATE` in one transaction per message; the queue group consumer `notifications-states` may hand messages of one mower to different replicas. The offline timer runs on every replica and takes its batch with `FOR UPDATE SKIP LOCKED`. Every event carries a `dedupe_key` (kind, mower and the occurrence, for example the job start time), message keys with parameters, and a severity.

**Recipients are subscribers, re-checked with `can_view` at delivery time.** `list_objects` answers "which mowers may this user view", not "which users may view this mower", and Identity offers no request for an account's users. Instead, a user subscribes in their preferences, to "all my mowers" or to selected mowers (each must be in the account and viewable, otherwise 404), and enables kinds. For an event the service loads the subscribers of the mower's account, filters kinds, and asks `check_many(can_view, mower)` for all of them in one batch before anything is stored: a user whose access was revoked after subscribing gets nothing, and "all my mowers" follows grants automatically. Quiet hours in the user's time zone keep the inbox item but queue no delivery. Messages are rendered per recipient from data templates (`messages/de.json`, `messages/en.json`). `identity.user.disabled` and `.deleted` delete the user's preferences, targets, inbox and deliveries; `identity.grants.changed` drops cached decisions.

**An outbox with `SKIP LOCKED` delivers.** The fan-out writes the inbox item (unique per user and `dedupe_key`, so a repeated event is stored once) and one `deliveries` row per channel target in one transaction, then publishes `notifications.sent` for in-app feeds. A worker loop on every replica claims due rows with `FOR UPDATE SKIP LOCKED` in a short transaction that increments `attempts` and moves `next_attempt_at` by a lease (60 s), sends outside the transaction with bounded concurrency, and records the result: sent; retry with exponential backoff (30 s doubling to 1 h, or the server's `Retry-After` when longer) up to 8 attempts, then failed; gone (Web Push 404/410: the subscription is deleted, the delivery dropped); failed (permanent). A crash after sending means one more send after the lease (at least once); a crash between the rule commit and the fan-out loses that one notification (at most once for derived events), which we accept over holding a database transaction across an OpenFGA call. Finished deliveries are pruned after 7 days; the inbox keeps the newest 200 per user.

**Channels are plug-ins.** A channel is a `NotificationChannel` (`name`, `async send(delivery) -> DeliveryResult`), created by a factory from the entry point group `mowdeck.notification_channels`, like the connection registry of `mowdeck-control`. The service ships `webpush` (VAPID with `py-vapid`, `aes128gcm` encryption with `pywebpush`'s `WebPusher.encode`, sent with the shared async `httpx` client; TTL and `Urgency` by severity; `Topic` per kind and mower), `ntfy` (JSON publish to the server root, optional access token, priority and tags by kind) and `log` (development). E-mail and Telegram are documented interfaces only. Targets are validated: a push endpoint must be HTTPS on an allowed push service host, an ntfy server must be on an allow list, so users cannot make the service call internal addresses. VAPID keys come from settings (or secret files); in development Compose creates a key file in a volume on first start and logs the public key.

## Consequences

- Thresholds change in one service without touching the gateway; a new rule is a pure function with table tests.
- Nobody is notified until they subscribe; the app has to offer "notify me about all my mowers" on first use. `job_started` is opt-in.
- The `can_view` batch per event costs one OpenFGA round trip per event, independent of the number of subscribers (`check_many`); a user who can view nothing cannot use the API at all (403).
- Delivery is at least once per channel; Web Push `Topic` and the service worker's `tag` collapse repeats on the device.
- A Web Push subscription belongs to the last user who registered the endpoint (one browser, one owner). ntfy access tokens are stored in the service database and never returned by the API.
- Follow-up: the PWA service worker and the settings page (frontend task), e-mail and Telegram channels as separate packages, and per-mower thresholds if users ask for them.
