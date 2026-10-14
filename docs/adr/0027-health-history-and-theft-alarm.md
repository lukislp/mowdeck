# ADR 0027: Health history and theft alarm

## Status

Accepted (2026-10-10)

## Context

Phase 3 promises a health history ("Gesundheit: Messerstunden, Akkuverlauf", own history instead of the vendor app) and a theft alarm ("Diebstahlalarm: Anheben, Verlassen der Station", sensors plus a geofence, alarm with position) (project plan, feature table). Telemetry already stores every state per its write rules ([ADR 0017](0017-telemetry-storage-and-retention.md)); Notifications already derives notifications from states with pure rules over a per-mower watch and delivers them with quiet hours and cooldowns ([ADR 0022](0022-notifications-rules-and-delivery.md)). Neither service knows the site's time zone, and services never read each other's databases.

## Decision

**Health is computed incrementally in Telemetry's state consumer.** Inside the transaction of `Ingest.record_state` a pure function advances a per-mower cursor (`mower_health`: totals, blade hours since the last change, the last state's activity, charging flag, blade flag, position, error code, battery arm flag and `last_observed_at`) and per-day rows (`mower_health_daily`). Nothing is recomputed in batches. The interval between two consecutive states counts under the condition of the earlier state; a gap longer than `MOWDECK_TELEMETRY_HEALTH_MAX_GAP_SECONDS` (120, the gateway's heartbeat is 10 s) counts nothing, so a silent mower does not accumulate hours.

- *Blade hours*: when the adapter reports `raw.blade_on` (the simulator does), blades ran exactly while it was true; otherwise blades are assumed to run while the activity is `mowing`, which overcounts transfer drives for adapters without the flag. *Mowing hours*: time while the activity is `mowing`.
- *Distance*: haversine between consecutive positions; a step longer than `MOWDECK_TELEMETRY_HEALTH_MAX_STEP_METERS` (25) or across a long gap is skipped (RTK jumps).
- *Battery cycles*: a drop below 20 % arms the counter, a rise above 80 % while armed counts one cycle. *Charge sessions*: transitions into charging. *Errors*: a count per error code when the mower enters an error or the code changes.
- *Days are UTC days*: Telemetry does not know the site's time zone; an interval crossing midnight belongs to the day it started. Clients that want local days aggregate the UTC days or ask for raw history.
- *Idempotence*: a state whose `observed_at` is not later than `last_observed_at` changes nothing, so redeliveries and out-of-order states never count twice.
- *Blade change*: `POST /mowers/{id}/health/blade-reset` (`can_manage_mowers`) records a blade change with a note and resets the blade hours since the change. When they cross `MOWDECK_TELEMETRY_BLADE_HOURS_THRESHOLD` (50 h) Telemetry publishes `telemetry.blade_hours.threshold` (`BladeHoursThreshold`) once per blade change, after the commit; Notifications turns it into `blade_maintenance` (severity `info`, the contract has no lower one).
- *API*: `GET /mowers/{id}/health` (totals and `since`), `/health/daily?from&to` (dates, at most 366 days), `/battery?from&to&step` (the history query projected to battery values), `/health/blade-changes`; all `can_view`, account filtered, 404 for foreign mowers.

**Theft alarms are Notifications rules.** Three new kinds, all `critical`, delivered also during quiet hours, with a cooldown of 10 minutes per kind and mower:

- `mower_lifted`: a state with activity `error` whose error code is in `MOWDECK_NOTIFICATIONS_LIFT_ERROR_CODES` (default `lifted` of the simulator plus the PyMammotion lift and tilt codes 1004, 1030, 2502, 2801, 11131, 11133) or whose `raw.lifted` is true. The adapters set `raw.lifted` (the simulator for its `lift()`, the Mammotion mapping for the codes above), so Notifications never imports the GPL adapter package ([ADR 0011](0011-gpl-package-for-pymammotion-adapter.md)). A lift replaces the generic `mower_stuck` for the same error.
- `mower_left_dock_unexpectedly`: the activity leaves `docked`/`charging` while no command for the mower was seen within `MOWDECK_NOTIFICATIONS_DOCK_COMMAND_WINDOW_SECONDS`. Notifications consumes `command.mower.*` with its own durable consumer and records the last command per mower in the watch; because commands and states may be processed in either order, the decision is confirmed by a later state before the alarm goes out.
- `geofence_exit`: the position is outside the union of the map's areas, transfer paths and dock, each buffered by `MOWDECK_NOTIFICATIONS_GEOFENCE_MARGIN_METERS` (5). The map comes from the `DEVICE` stream (a durable consumer that starts with the newest snapshot per mower). Positions without an RTK fix are ignored, two consecutive outside states are required, and a recent manual drive frame suppresses it.

The notification body carries the position and the `url` a map link. **Acknowledge**: `POST /api/v1/notifications/inbox/{id}/ack` marks the item read and acknowledged and silences further alarms of the same kind and mower for that user for an hour (`MOWDECK_NOTIFICATIONS_ACK_SILENCE_SECONDS`); inbox dedupe keys stay per occurrence. The new kinds are enabled by default for users who never saved preferences; users with saved preferences opt in.

## Consequences

- Health totals cost one row read and two row writes per stored state in the same transaction; they survive the history retention and are deleted only with the mower.
- Blade hours of adapters without `raw.blade_on` are an upper bound; adapters should report the flag.
- Theft alarms depend on adapters reporting lift codes and positions; a mower whose map was never published gets no geofence.
- A thief who first sends a command with the owner's account defeats the dock rule; the geofence still fires.
- Not covered yet: local-time days, maintenance schedules beyond blade hours, the UI for health and alarms.
