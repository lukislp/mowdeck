# telemetry

Latest state, history, position track and command timeline of every mower. Telemetry consumes what the Device-Gateway publishes, keeps it in PostgreSQL and serves it to clients; it never talks to a mower.

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; layer rules in `docs/dev/service-template.md`. Design: [Design: fleet, telemetry, realtime](../../docs/dev/design-fleet-telemetry-realtime.md), section 2. Decision: [ADR 0017](../../docs/adr/0017-telemetry-storage-and-retention.md) (plain tables now, heartbeat de-duplication, retention under an advisory lock, directory from Fleet).

## Purpose

- **Latest state** per mower (the full `MowerState`), guarded so an older `observed_at` never overwrites a newer one. The Device-Gateway's link heartbeat (every 10 s, the same `observed_at` with a fresh `heartbeat_at`) and its offline state after a lost link (`connected: false`) only replace this row ([ADR 0040](../../docs/adr/0040-link-heartbeat-for-online-status.md)).
- **History** of state changes for charts. A state with the same `observed_at` as the stored one (a link heartbeat, the offline state, a redelivery) writes no history or position row; an unchanged report of the mower is stored at most once per `MOWDECK_TELEMETRY_HEARTBEAT_HISTORY_SECONDS` (default 300), a change always. Downsampling with `date_bin` on request.
- **Position track** (with RTK fix and source, Wi-Fi and mobile signal, blade state), one row per distinct position.
- **Heatmaps** over the mower's map: cut share and missed areas, RTK fix quality, Wi-Fi and mobile signal per cell (see [Heatmaps](#heatmaps)).
- **Command timeline**: every status transition from `command.result.mower.*` (Fleet stays the authority for the command record).
- **Map and capabilities** of every mower as the Device-Gateway last read them (snapshots from the stream `DEVICE`), for the web app's area picker and setpoint ranges.
- **Mow path** of the running job (the route the mower planned, with its progress; snapshot `device.mower.<id>.path`), for the path on the web app's map.
- **iOS widget data** ([ADR 0059](../../docs/adr/0059-ios-widgets-and-live-activity.md)): `GET /mowers/{id}/widget` (compact state with the job's area name) and `GET /mowers/{id}/widget-map.png`, a bounded, cached PNG of the map with the mowed path and the mower, drawn with numpy and zlib in a worker thread (at most two at a time; 503 when busy; ETag, 30 s cache).
- **Error codes in plain words** ([ADR 0067](../../docs/adr/0067-error-code-lexicon.md)): the mower's current error and any vendor code explained in German or English from the vendor's error lexicon (see [Error lexicon](#error-lexicon)).
- **Mower directory** (`mower_id` to `account_id`) from Fleet's events and a startup request, because command results carry no account id.
- **Health history**: blade hours (since the last blade change, too), mowing hours, distance, battery cycles, charge sessions and error counts per mower, as running totals and per UTC day, plus a reminder event when the blade hours cross a threshold (see [Health history](#health-history)).
- **Retention** of history, positions and the timeline.

## Interfaces

All routes are mounted under `/api/v1` (OpenAPI at `/api/v1/openapi.json`); Traefik routes `/api/v1/telemetry` to this service. The account is always the verified token's `acc`; every row is read with that account filter. Mower routes answer 404 for a mower the caller cannot see (other accounts, unknown ids), so existence never leaks.

| Method | Path | Required relation | Notes |
| --- | --- | --- | --- |
| GET | `/api/v1/telemetry/mowers/{mower_id}/state` | `can_view` on the mower (hide) | latest `MowerState` (with the typed `job`, `gnss`, `signal`, `self_check` and `error_category` when the connection reports them, see `mowdeck-contracts`) with `received_at` and `stale`; 404 problem `no-telemetry-yet` ("no telemetry yet") when the mower is visible but silent |
| GET | `/api/v1/telemetry/mowers/{mower_id}/history` | `can_view` (hide) | `?from=&to=&step=`; without `step` the stored rows, with `step` (`5s`, `1m`, `1h`, `1d`) one point per bin: last activity and RTK fix, battery mean, min and max, charging, samples, last position |
| GET | `/api/v1/telemetry/mowers/{mower_id}/positions` | `can_view` (hide) | `?from=&to=&limit=` the newest `limit` positions, oldest first |
| GET | `/api/v1/telemetry/mowers/{mower_id}/commands` | `can_view` (hide) | `?limit=` newest first: `command_id`, `status`, `reason`, `at` |
| GET | `/api/v1/telemetry/mowers/{mower_id}/map` | `can_view` (hide) | the stored `MowerMap` (local metres plus geo reference) with `observed_at` and `received_at`; `ETag` is the map revision plus a short content hash (the mower's revision does not change when the dock or the geo reference arrive) or, without a revision, the content hash alone; `If-None-Match` with it answers 304; 404 problem `no-map-yet` before the gateway published a map |
| GET | `/api/v1/telemetry/mowers/{mower_id}/path` | `can_view` (hide) | `MowPathOut`: the route the mower planned for its running job (`MowPath`: polylines in local metres, `kinds`, `points_total`, `path_hash`, `progress {index, direction}`) as the gateway last published it; 404 problem `no-path` without a job, before the route arrived, or after the gateway cleared it (the job ended). The web app polls it every 10 s while the mower works |
| GET | `/api/v1/telemetry/mowers/{mower_id}/capabilities` | `can_view` (hide) | the stored `MowerCapabilities` (blade height range, maximum speed, ...); 404 problem `no-capabilities-yet` before the first report |
| GET | `/api/v1/telemetry/mowers/{mower_id}/heatmap` | `can_view` (hide) | `?layer=coverage|rtk|wifi|mnet&from=&to=&cell=` (`cell` 0.5, 1 or 2 metres, default 1; default range the last 7 days, same range limit as the history): `mower_id`, `layer`, `cell_m` (larger than asked when coarsened), `from`, `to`, `reference` (the map's geo reference), `cells` (`x`, `y` cell indexes in the map's local frame, `value`, `n`), `legend` (`kind` `ratio`, `fix` or `dbm`, `min`, `max`), `gaps` (`polygon` in local metres, `area_m2`; coverage only), `samples`, `truncated`; 404 problem `no-map-yet` without a map, 422 for another `cell` or `layer` (see [Heatmaps](#heatmaps)) |
| GET | `/api/v1/telemetry/mowers/{mower_id}/aerial` | `can_view` (hide) | whether an aerial photo is offered (`enabled`), tile URL template, credit line, zoom range and the box where tiles are served; 404 `no-map-yet` before a map exists (ADR 0054) |
| GET | `/api/v1/telemetry/mowers/{mower_id}/aerial/{z}/{x}/{y}` | `can_view` (hide) | one Web Mercator tile of the aerial photo as `image/jpeg` or `image/png`; only tiles around this mower's map and inside the zoom range (else 404 `tile-outside-map`; 404 `aerial-disabled` without a source), cached on disk, new tiles limited per account (429 `rate-limited` with `Retry-After`), 502 `aerial-source-failed` |
| GET | `/api/v1/telemetry/mowers/{mower_id}/health` | `can_view` (hide) | running totals: `since`, `blade_hours`, `blade_hours_since_change`, `blade_changed_at`, `blade_hours_threshold` (MowDeck's threshold: the one set for the mower, else the default) and `blade_hours_threshold_custom`, `blade_hours_threshold_effective` and `blade_hours_threshold_source` (the one threshold in effect, used by the reminder, the web app and Home Assistant: `custom` set in MowDeck, else `device` the mower's own warning while it reports its own blade counter, else `default`), `device_blade_hours` and `device_blade_warn_hours` (the mower's own counter and threshold, null when it never sent them), `blade_source` (`device` when the mower's counter is known, else `telemetry`), `mowing_hours`, `distance_m` (MowDeck's own counts since `since`), `device_mowing_hours` and `device_distance_m` (the mower's own lifetime working time and distance, null when it never sent them), `battery_cycles` (the mower's own lifetime counter when it reports one, else MowDeck's count), `battery_cycles_tracked` (MowDeck's count since `since`), `device_battery_cycles` (the mower's own counter, null when it never sent one), `battery_cycles_source` (`device` when the mower's counter is known, else `telemetry`), `charge_sessions`, `errors` (count per code), `updated_at`; 404 problem `no-telemetry-yet` when nothing was recorded |
| GET | `/api/v1/telemetry/mowers/{mower_id}/health/daily` | `can_view` (hide) | `?from=YYYY-MM-DD&to=YYYY-MM-DD` inclusive UTC days (default: the last 30 days ending today, at most 366, else 422); days without data are absent |
| GET | `/api/v1/telemetry/mowers/{mower_id}/battery` | `can_view` (hide) | `?from=&to=&step=` battery series from the history (same ranges, bins and limits): `at`, `battery_percent`, `battery_min`, `battery_max`, `charging` |
| GET | `/api/v1/telemetry/mowers/{mower_id}/health/blade-changes` | `can_view` (hide) | recorded blade changes, newest first (at most 100): `id`, `changed_at`, `note`, `changed_by` |
| POST | `/api/v1/telemetry/mowers/{mower_id}/health/blade-reset` | `can_manage_mowers` on the mower (403 when only visible, 404 when hidden) | body `{"note": str or null (max 500)}`; records a blade change, restarts the blade hours since the change, re-arms the reminder; answers the new totals. Works before the first telemetry (the totals row is created with `since: null`) |
| PUT | `/api/v1/telemetry/mowers/{mower_id}/health/blade-threshold` | `can_manage_mowers` on the mower (403 when only visible, 404 when hidden) | body `{"hours": number (0 < hours <= 10000) or null}`; the mower's own blade reminder threshold, `null` drops it: the mower's own warning (`device_blade_warn_hours`) applies again when it reports its own counter, else `MOWDECK_TELEMETRY_BLADE_HOURS_THRESHOLD`; re-arms the reminder when the blade hours shown lie below the new effective threshold (a reminder already sent above it stays sent); creates the totals before the first telemetry; answers the totals (`blade_hours_threshold_effective`, `blade_hours_threshold_source`, `blade_hours_threshold_custom`). The warning stored on the mower itself is not changed |
| GET | `/api/v1/telemetry/mowers/{mower_id}/error` | `can_view` (hide) | `?locale=de|en` (else `Accept-Language`, else English): `MowerErrorOut` with the current error code in plain words from the vendor's error lexicon (`ErrorExplanation`: `code`, `vendor`, `vendor_name`, `known`, `locale`, `title`, `description`, `cause`, `steps`, `severity`, `category`, `user_action`, `links`, `verified`), `error` null without one; 404 `no-telemetry-yet` before the first state (see [Error lexicon](#error-lexicon)) |
| GET | `/api/v1/telemetry/mowers/{mower_id}/error-codes` | `can_view` (hide) | `?code=..&code=..` (1 to 20) `&locale=`: explanations with the mower's lexicon, in order (the health page's error counts) |
| GET | `/api/v1/telemetry/error-codes/{vendor}/{code}` | any signed-in principal | one code of a vendor (`mammotion`, `simulated`, ...); an unknown vendor or code answers 200 with `known: false` and generic advice |
| GET | `/api/v1/telemetry/error-lexicons` | any signed-in principal | the installed lexicons: `vendor`, `vendor_name`, `connection_kinds`, `entries`, `unverified`, `locales`, `source`; empty while the Device-Gateway does not answer |
| GET | `/api/v1/telemetry/accounts/me/mowers/states` | `can_view` per mower (`list_objects`, or the whole account) | latest state of every viewable mower of the caller's account, bounded by `MOWDECK_TELEMETRY_STATES_LIMIT` (`truncated`) |
| GET | `/api/v1/telemetry/accounts/me/mowers/capabilities` | `can_view` per mower (`list_objects`, or the whole account) | stored capabilities of every viewable mower that reported them, bounded by `MOWDECK_TELEMETRY_STATES_LIMIT` (`truncated`) |
| GET | `/api/v1/telemetry/reports/summary` | `can_view` per mower (`list_objects`, or the whole account) | `?from=YYYY-MM-DD&to=YYYY-MM-DD` (same UTC day rules as `/health/daily`) and optional repeated `mower_id` (invisible ids are left out silently): day totals of every viewable mower with data in the range in one call, grouped per mower (`items[].days[]` like `/health/daily` plus `jobs` and `online_hours`); at most 50 000 day rows (`truncated`). Telemetry does not know sites: the web app's reports page filters by Fleet's `site_id` |
| GET | `/healthz`, `/readyz`, `/metrics`, `/.well-known/mowdeck-module` | none | Operations (kit); readiness covers the bus, the subscriptions, the database and OpenFGA |

`from` and `to` are ISO 8601 with a time zone (default: the last 24 hours). A range longer than `MOWDECK_TELEMETRY_QUERY_MAX_RANGE_DAYS`, a `from` after `to`, a malformed `step` or a `step` that needs more than `MOWDECK_TELEMETRY_QUERY_MAX_ROWS` bins answers 422; raw history and positions are cut at that many rows (`truncated: true`).

Permission defined by this service (module descriptor): `telemetry.view`, an account key implied by `mower.view` (`implied_by`), so it appears in Identity's catalogue. The descriptor announces the web app's `ui_area` `/reports` (required permission `mower.view`). The routes check the computed `can_view` on the mower, not the token's `perms`, so a viewer of one group sees exactly that group's mowers.

## Events and subjects

| Direction | Subject | Consumer | Purpose |
| --- | --- | --- | --- |
| consumed | `telemetry.mower.*` (`MowerState`, stream `TELEMETRY`) | durable and queue group `telemetry-states` | latest, history, positions |
| consumed | `command.result.mower.*` (`CommandResult`, stream `COMMANDS`) | durable and queue group `telemetry-results` | command timeline (account from the directory) |
| consumed | `device.mower.>` (`MowerMapSnapshot`, `MowerCapabilitiesSnapshot`, `MowerPathSnapshot`, stream `DEVICE`) | durable and queue group `telemetry-device` | newest map, capabilities and mow path per mower (an older `observed_at` never replaces a newer one) |
| consumed | `event.mower.*` (`mower.registered|updated|removed`, stream `EVENTS`) | durable and queue group `telemetry-mowers` | mower directory; removal purges the mower's rows |
| request | `fleet.mowers.list` (core NATS) | at startup, retried with backoff | seeds the directory |
| request | `gateway.errors.explain`, `gateway.errors.lexicons` (core NATS) | per API request, answers cached an hour | the error lexicons of the adapters installed at the Device-Gateway (see [Error lexicon](#error-lexicon)) |
| answered | `tracks.positions.query` (core NATS, `PositionTrackRequest` / `PositionTrackReply`) | queue group `telemetry-track` | the position track of one mower of one account for a time range (at most 50 000 points, the range limit of the HTTP queries) as an encoded polyline, for the Planner's coverage and gaps ([ADR 0030](../../docs/adr/0030-mowing-patterns-and-planner.md)); an invalid range is answered with `error` |
| produced | `event.telemetry.blade_hours.threshold` (`telemetry.blade_hours.threshold`, `BladeHoursThreshold`, stream `EVENTS`) | Notifications | the blade hours since the last blade change crossed the mower's threshold (its own, else `MOWDECK_TELEMETRY_BLADE_HOURS_THRESHOLD`); published once per blade change, after the commit (a failed publish is logged, not retried) |

Handler rules: malformed payloads, payloads whose subject names another mower and states of a foreign account are dropped with a warning (never redelivered). A command result for a mower missing from the directory is redelivered and dropped with a warning on the `MOWDECK_TELEMETRY_UNKNOWN_MOWER_ATTEMPTS`-th delivery. A snapshot whose account differs from the directory entry of its mower is dropped with a warning; an older snapshot never replaces a newer one. Manual drive frames (ten per second) produce command results whose correlation id starts with `drive:`; their `accepted` results are not stored, so they do not flood the timeline (rejected, failed and expired ones are).

## Error lexicon

Vendor error codes in plain words ([ADR 0067](../../docs/adr/0067-error-code-lexicon.md)). Telemetry holds no code table and installs no adapter package: the lexicons ship with the adapters at the Device-Gateway, which answers `gateway.errors.explain`. The mower routes pick the lexicon by the mower's connection (`MowerState.connection`) and pass the state's `error_category`, so an unknown code still gets advice for its kind. Answers are cached in memory per vendor or connection, code, language and category for an hour (at most 2048). When the gateway does not answer, the routes serve the generic explanation ("Unknown error 1234 from ...", `known: false`) and cache nothing.

## Tables (PostgreSQL, Alembic)

Every table carries `account_id`, taken from the message or the directory, never from a request.

| Table | Key | Content |
| --- | --- | --- |
| `mower_directory` | `mower_id` | owning account (Fleet's events, startup seed) |
| `mower_latest` | `mower_id` | the full state as JSONB, `observed_at`, `received_at`, history and position fingerprints |
| `mower_history` | `(mower_id, observed_at)` | activity, battery, charging, RTK fix, connection, blade height, speed, error code; indexes `(mower_id, observed_at desc)`, `(account_id, observed_at desc)`, BRIN on `observed_at` |
| `mower_positions` | `(mower_id, observed_at)` | lat, lon, alt, heading, RTK fix and source; `wifi_rssi` and `mnet_rssi` (dBm from `raw.wifi_rssi` / `raw.mnet_rssi`, null when not reported or not in `[-150, -1]`) and `blades_on` (`raw.blade_on` when reported, else activity `mowing`), all three nullable and null in rows from before migration `0007`; same indexes A standing mower (typically in its dock) still gets a row when its WLAN or mobile signal moves to another 5 dBm bucket or with each history heartbeat, so the signal heatmap has dock samples |
| `command_timeline` | `(command_id, status)` | mower, status, reason, `at`; indexes `(mower_id, at desc)`, `(account_id, at desc)`, BRIN on `at` |
| `mower_health` | `mower_id` | running health totals (`since`, blade and mowing seconds, distance, battery cycles, charge sessions, `errors` JSONB), blade seconds since the last change, `blade_changed_at`, `blade_threshold_notified`, the newest device blade counter (`device_blade_seconds`, `device_blade_warn_seconds`, nullable, migration `0006`), the newest device battery cycle counter (`device_battery_cycles`, nullable, migration `0008`), the mower's own blade reminder threshold (`blade_threshold_seconds`, nullable, migration `0009`), the newest device lifetime totals (`device_mowing_seconds`, `device_distance_m`, nullable, migration `0010`), and the incremental cursor (`last_observed_at`, last activity, charging, blade flag, position, error code, `battery_armed`); index on `account_id`; migration `0004` |
| `mower_health_daily` | `(mower_id, day)` | the same totals per UTC day, plus `jobs` (transitions into mowing, not from paused) and `online_seconds` (intervals whose start state was not offline); migrations `0004` and `0005` (the two report columns, 0 for older days) |
| `blade_changes` | `id` | `mower_id`, `account_id`, `changed_at`, `note`, `changed_by` (subject); index `(mower_id, changed_at desc)`; migration `0004` |
| `mower_snapshots` | `(mower_id, kind)` | `kind` `map`, `capabilities` or `path` (a cleared path is stored as the body `{}`, served as 404 `no-path`; no migration, the column is a string), `account_id`, `observed_at`, `received_at`, `revision` (ETag), `body` JSONB; index on `account_id`; migration `0003`. One table for both kinds (the design proposed two); deleted with the mower |

## Health history

The state consumer advances the health totals in the same transaction as the latest row (no batch recompute). Rules (module `telemetry.domain.health`):

- **Intervals**: the time between two consecutive states of a mower, `[previous.observed_at, current.observed_at)`, counts for the condition of the previous state. A gap longer than `MOWDECK_TELEMETRY_HEALTH_MAX_GAP_SECONDS` (default 120 s; the heartbeat is 10 s) counts nothing: the mower was silent.
- **Blade hours** (heuristic): when the previous state's `raw` has a boolean `blade_on` (the simulator reports it), blades ran iff it was true; otherwise blades are assumed to run while the activity is `mowing`. Adapters without the flag therefore count transfer drives (activity `mowing`, blades off) as blade time.
- **Mowing hours**: intervals whose previous state had activity `mowing`.
- **Distance**: haversine metres between consecutive positions; a step longer than `MOWDECK_TELEMETRY_HEALTH_MAX_STEP_METERS` (default 25 m, RTK jumps) or across a gap longer than the maximum gap is left out.
- **Battery cycles**: a drop below 20 % arms the counter, a rise above 80 % while armed counts one cycle (constants). This is MowDeck's own count since it started tracking (`battery_cycles_tracked`, and `battery_cycles` of the daily rows).
- **Device battery cycle counter**: mowers that count charge cycles over the battery's lifetime (Mammotion: `report_data.maintenance.bat_cycles`) send the counter as `raw.bat_cycles`. The newest positive value is stored on the totals and answered as `battery_cycles` / `device_battery_cycles` with `battery_cycles_source: "device"`; the web app labels it as the mower's lifetime counter and shows MowDeck's count beside it. The simulator sends none (`battery_cycles_source: "telemetry"`, `battery_cycles` is MowDeck's count).
- **Charge sessions**: transitions into charging (`charging` true or activity `charging`); a mower's first state counts as a transition when it charges.
- **Errors**: one count per `error_code` (`unknown` without one) when the activity becomes `error` or the code changes while in `error`.
- **Jobs** (for reports): a transition into `mowing` from anything but `mowing` or `paused` (a resume continues the job); a mower's first state counts when it mows.
- **Online time** (availability in reports): intervals whose previous state was not `offline`; a gap longer than the maximum gap counts nothing.
- **Days are UTC days**: Telemetry does not know the site's time zone. An interval crossing midnight UTC belongs wholly to the day it started; cycles, sessions and errors to the day of the state that shows them.
- **Idempotent**: a state whose `observed_at` is not after the stored cursor (`last_observed_at`) changes nothing, so redeliveries and late states are not counted twice.
- **Device blade counter**: mowers that count blade time themselves (Mammotion: `report_data.maintenance.blade_used_time`, reset in the Mammotion app) send it as `raw.blade_used_s` and the warning threshold as `raw.blade_used_warn_s`. The newest positive values are stored on the totals and answered as `device_blade_hours` / `device_blade_warn_hours` with `blade_source: "device"`; the web app then shows them for "blades since change" (as the vendor app does) and keeps MowDeck's own counter as a secondary readout. A blade reset in MowDeck does not reset the device counter. The simulator sends none (`blade_source: "telemetry"`). The reminder compares the counter shown (the device's when known) with the effective threshold.
- **Device lifetime totals**: mowers that count their working time and distance over their lifetime (Mammotion: `report_data.maintenance.work_time` and `.mileage`, the vendor app's total working time and total distance) send them as `raw.work_time_s` (seconds) and `raw.mileage_m` (metres). The newest positive values are stored on the totals and answered as `device_mowing_hours` / `device_distance_m`; the web app and Home Assistant show them as the mower's lifetime values and keep MowDeck's own `mowing_hours` / `distance_m` (counted since `since`) beside them. Day totals and reports stay MowDeck's own counts. The simulator sends none (both null).
- **Blade reminder**: one effective threshold per mower: the one set with `PUT .../health/blade-threshold` (stored as `mower_health.blade_threshold_seconds`, migration `0009`), else the mower's own warning (`device_blade_warn_hours`) while it reports its own blade counter, else `MOWDECK_TELEMETRY_BLADE_HOURS_THRESHOLD` (default 50 h); the totals answer it as `blade_hours_threshold_effective` with `blade_hours_threshold_source` (`custom`, `device`, `default`). When the blade hours shown (the device counter when known, else MowDeck's hours since the change) reach it, the totals are marked notified and `telemetry.blade_hours.threshold` is published once with those hours and that threshold (Notifications' `blade_maintenance` names exactly them). Whenever the hours shown lie below the threshold again the reminder is re-armed: a blade reset in MowDeck (MowDeck's counter) or in the vendor app (the device counter drops), or a higher threshold.

## Heatmaps

`GET /mowers/{id}/heatmap` (module `telemetry.application.heatmap`, pure) reads the position track of the range in one query (`ix_mower_positions_mower_time`, newest first, streamed in chunks of 5 000 rows, at most `MOWDECK_TELEMETRY_HEATMAP_MAX_ROWS`; with more the oldest are left out and `truncated` is true) and converts every position to the map's local metres with the stored map's geo reference (`mowdeck_control.geo.from_wgs84`). Cell indexes are `floor(metres / cell_m)` per axis, so negative coordinates give negative indexes. A response holds at most 20 000 cells: with more the cell size doubles until they fit (`cell_m` says which size was used, it can exceed 2 m).

| Layer | `value` | `n` | Legend |
| --- | --- | --- | --- |
| `coverage` | share of the cell the blades cut, 0..1 | blades-on positions in the cell | `ratio`, 0..1 |
| `rtk` | share of fixed samples (float counts 0.5, none 0; `unknown` left out) | samples | `fix`, 0..1 |
| `wifi`, `mnet` | median signal in dBm; cells without a reading are absent | readings | `dbm`, min and max over the cells |

Coverage: only rows with `blades_on` true count (older rows without the column count as not cut). Each is a disc of the cutting width; consecutive blades-on positions at most `MOWDECK_TELEMETRY_HEATMAP_JOIN_SECONDS` (30 s) and at most `MOWDECK_TELEMETRY_HEATMAP_JOIN_METRES` (10 m) apart, both limits, are joined by a straight swath (the cloud reports a mowing LUBA only every 1 to 20 s); farther apart they stay separate discs. The cutting width is `cutting_width_mm` of the stored capabilities, 400 mm without it. The ground is rasterised into pixels of at most a quarter of the cutting width (0.1 m for 400 mm); a pixel is cut when its centre lies within half the cutting width of the swath, a cell's share is its cut pixels over its pixels. `gaps` are the map's areas minus no-go zones and obstacles minus the cut pixels (`mowdeck_patterns.coverage.gaps`: slivers narrower than 5 cm and pieces below 0.25 m² dropped, at most 500, largest first), outlines simplified to about 7.5 cm, in local metres without the closing point. Telemetry depends on `mowdeck-patterns` and `mowdeck-control` for this (both Apache-2.0).

Example (`layer=wifi&cell=1`, two cells):

```json
{"mower_id": "5f0c…", "layer": "wifi", "cell_m": 1.0,
 "from": "2026-10-04T12:00:00Z", "to": "2026-10-11T12:00:00Z",
 "reference": {"origin_lat": 48.1, "origin_lon": 11.5, "rotation_deg": 30.0},
 "cells": [{"x": 0, "y": 0, "value": -60.0, "n": 3}, {"x": 5, "y": 0, "value": -80.5, "n": 2}],
 "legend": {"kind": "dbm", "min": -80.5, "max": -60.0},
 "gaps": [], "samples": 5, "truncated": false}
```

Cost on a laptop (synthetic week, 200 000 positions on a 40 m by 30 m lawn): signal and RTK layers about 0.2 s (around 1 million rows per second), coverage 1.2 to 2 s (100 000 to 170 000 rows per second) plus the database read; the computation runs in a worker thread.

## Retention

Every `MOWDECK_TELEMETRY_RETENTION_INTERVAL_SECONDS` each replica tries `pg_try_advisory_lock`; only the one that gets it deletes (batches of 5 000 rows) and releases the lock afterwards. The in-memory store ignores the lock. Defaults: history 30 days, command timeline 30 days, positions 730 days (`0` keeps them forever).

### Position downsampling

Positions younger than `MOWDECK_TELEMETRY_POSITIONS_DETAIL_DAYS` (90) keep every row. Older ones are thinned in the nightly window (`MOWDECK_TELEMETRY_THINNING_WINDOW_START_HOUR` to `_END_HOUR` in `MOWDECK_TELEMETRY_MAINTENANCE_TIMEZONE`, DST-safe) by the same maintenance loop: one row per 30 s while the blades run or the mower moves, one per 5 min while it stands, always the rows next to gaps, blade or RTK changes, bends over 25 degrees and error instants. Slices of 6 hours per mower and statement, a watermark per mower (table `mower_positions_thinning`, migration 0011), time budget per round, metrics `mowdeck_telemetry_position_thinning_*`. The heatmap joins thinned swaths with a wider limit. Anything that needs raw positions (theft tracking) must stay inside the detail window. Details: [ADR 0053](../../docs/adr/0053-position-retention-and-downsampling.md), operations and the size-check SQL: [ops guide](../../docs/ops/telemetry-retention.md). The health totals, the day totals (one small row per mower and day) and the blade changes are kept; they are deleted only with the mower.

## Settings

All settings of `mowdeck-service-kit` apply (`MOWDECK_*`, any value also from `MOWDECK_<NAME>_FILE`); OpenFGA is configured with the `MOWDECK_OPENFGA_*` variables of `mowdeck-authz` and token verification with `MOWDECK_AUTH_JWKS_URL` (and the other `MOWDECK_AUTH_*` variables) of `mowdeck-auth`. Service specific settings:

| Variable | Default | Description |
| --- | --- | --- |
| `MOWDECK_SERVICE_NAME` | `telemetry` | Service name in logs, metrics and traces |
| `MOWDECK_HTTP_PORT` | `8000` | HTTP port |
| `MOWDECK_DATABASE_URL` | required | PostgreSQL URL (usually `MOWDECK_DATABASE_URL_FILE`) |
| `MOWDECK_TELEMETRY_HEARTBEAT_HISTORY_SECONDS` | `300` | An unchanged state is stored in the history at most once per this many seconds; a change is always stored |
| `MOWDECK_TELEMETRY_RETENTION_HISTORY_DAYS` | `30` | History older than this is deleted |
| `MOWDECK_TELEMETRY_RETENTION_POSITIONS_DAYS` | `730` | Position track older than this is deleted; `0` keeps it forever |
| `MOWDECK_TELEMETRY_POSITIONS_DETAIL_DAYS` | `90` | Positions younger than this keep every row (at least 7) |
| `MOWDECK_TELEMETRY_POSITIONS_THINNING_ENABLED` | `true` | Downsample older positions |
| `MOWDECK_TELEMETRY_POSITIONS_THIN_ACTIVE_SECONDS` / `_IDLE_SECONDS` | `30` / `300` | Rows kept per bin while moving or mowing / standing |
| `MOWDECK_TELEMETRY_MAINTENANCE_TIMEZONE` | `UTC` | Time zone of the thinning window |
| `MOWDECK_TELEMETRY_THINNING_WINDOW_START_HOUR` / `_END_HOUR` | `1` / `5` | Local hours of the window (equal: always) |
| `MOWDECK_TELEMETRY_THINNING_BUDGET_SECONDS` | `900` | Longest one thinning round |
| `MOWDECK_TELEMETRY_RETENTION_COMMANDS_DAYS` | `30` | Command timeline older than this is deleted |
| `MOWDECK_TELEMETRY_RETENTION_INTERVAL_SECONDS` | `3600` | How often the retention job runs (one replica at a time) |
| `MOWDECK_TELEMETRY_QUERY_MAX_RANGE_DAYS` | `31` | Longest time range of a history or position query |
| `MOWDECK_TELEMETRY_QUERY_MAX_ROWS` | `5000` | Most points a query returns; a `step` needing more bins is refused |
| `MOWDECK_TELEMETRY_STATES_LIMIT` | `200` | Most mowers in the account state list |
| `MOWDECK_TELEMETRY_STALE_AFTER_SECONDS` | `60` | A mower is reported `stale` (offline) when its last link heartbeat (`state.heartbeat_at`, else `observed_at`) is older than this (six missed heartbeats) or the state says `connected: false`; old values of a connected mower are not stale (ADR 0040) |
| `MOWDECK_TELEMETRY_UNKNOWN_MOWER_ATTEMPTS` | `3` | Deliveries of a result for an unknown mower before it is dropped |
| `MOWDECK_TELEMETRY_FLEET_TIMEOUT_SECONDS` | `5` | Timeout of the `fleet.mowers.list` seed request |
| `MOWDECK_TELEMETRY_FLEET_RETRY_MAX_SECONDS` | `30` | Upper bound of the backoff between seed attempts |
| `MOWDECK_TELEMETRY_BLADE_HOURS_THRESHOLD` | `50` | Default blade hours since the last blade change after which `telemetry.blade_hours.threshold` is published once (again after the next blade reset); a mower's own threshold (`PUT .../health/blade-threshold`) and, without one, the mower's own warning (when it reports its own blade counter) replace it |
| `MOWDECK_TELEMETRY_HEALTH_MAX_GAP_SECONDS` | `120` | A gap between two states longer than this counts no blade or mowing hours and no distance (the mower was silent) |
| `MOWDECK_TELEMETRY_HEATMAP_MAX_ROWS` | `200000` | Most position rows one heatmap reads, newest first; more: `truncated` |
| `MOWDECK_TELEMETRY_HEATMAP_JOIN_SECONDS` | `30` | Coverage heatmap: consecutive blades-on positions at most this many seconds apart are joined by a straight swath |
| `MOWDECK_TELEMETRY_HEATMAP_JOIN_METRES` | `10` | ... and at most this many metres apart (both limits must hold); farther positions stay separate discs |
| `MOWDECK_TELEMETRY_TRACKING_INTERVAL_SECONDS` | `10` | Theft tracking ([ADR 0058](../../docs/adr/0058-theft-tracking-mode.md)): position report interval asked from the gateway (never below 5) |
| `MOWDECK_TELEMETRY_TRACKING_MAX_HOURS` | `24` | A tracking session ends by itself after this many hours |
| `MOWDECK_TELEMETRY_TRACKING_TICK_SECONDS` | `15` | How often active sessions are looked at (lease renewal, stale and move checks) |
| `MOWDECK_TELEMETRY_TRACKING_LEASE_SECONDS` | `60` | Length of the gateway lease, renewed on every tick |
| `MOWDECK_TELEMETRY_TRACKING_STALE_AFTER_SECONDS` | `120` | No report for this long marks the session stale and notifies once per outage |
| `MOWDECK_TELEMETRY_TRACKING_MOVE_METERS` | `50` | A "moved" notification needs this distance from the previous notice |
| `MOWDECK_TELEMETRY_TRACKING_MOVE_NOTICE_SECONDS` | `300` | ... and this long since it |
| `MOWDECK_TELEMETRY_TRACKING_LINK_TTL_HOURS` | `48` | Lifetime of a share link (and the longest one may ask for) |
| `MOWDECK_TELEMETRY_TRACKING_MAX_LINKS` | `10` | Active share links per session |
| `MOWDECK_TELEMETRY_TRACKING_RETENTION_DAYS` | `30` | Ended sessions with links and photos are deleted after this many days |
| `MOWDECK_TELEMETRY_TRACKING_AUTO_START` | `true` | Start a session on a theft alarm |
| `MOWDECK_TELEMETRY_TRACKING_PHOTOS` | `true` | Take a camera photo at the start for the report |
| `MOWDECK_TELEMETRY_TRACKING_PUBLIC_RATE_PER_MINUTE` | `60` | Requests per minute and client to the public share page |
| `MOWDECK_TELEMETRY_TRACKING_PUBLIC_FAILURES_PER_MINUTE` | `20` | Unusable tokens per minute and client before it is slowed down |
| `MOWDECK_TELEMETRY_TRACKING_PUBLIC_TILES_PER_MINUTE` | `600` | Base map tiles per minute and client on the public share page |
| `MOWDECK_TELEMETRY_TRACKING_MAP_MARGIN_METERS` | `2000` | Base map tiles of a tracking session are served this far around its positions |
| `MOWDECK_TELEMETRY_BASEMAP_URL` | empty (off) | Street map under a tracking track: an https XYZ template with `{z}`, `{x}`, `{y}` in any order, fetched and cached by Telemetry only around the track. Recommended: OpenStreetMap `https://tile.openstreetmap.org/{z}/{x}/{y}.png` (see [ops guide](../../docs/ops/theft-tracking.md#base-map), tile usage policy) |
| `MOWDECK_TELEMETRY_BASEMAP_ATTRIBUTION` / `_MIN_ZOOM` / `_MAX_ZOOM` | empty / `8` / `19` | Its credit line and zoom range |
| `MOWDECK_TELEMETRY_BASEMAP_COVERAGE` / `MOWDECK_TELEMETRY_AERIAL_COVERAGE` | empty (the world) | Box a source covers (`south,west,north,east`); the tracking map switches layers outside it |
| `MOWDECK_TELEMETRY_HEALTH_MAX_STEP_METERS` | `25` | A step between two consecutive positions longer than this is left out of the distance (RTK jumps) |
| `MOWDECK_TELEMETRY_AERIAL_WMS_URL` / `_WMS_LAYER` / `_XYZ_URL` | empty (off) | Aerial photo source (https): a WMS answering `GetMap` in EPSG:3857 with its layer, or an XYZ template. Empty means no aerial layer and no third-party request |
| `MOWDECK_TELEMETRY_AERIAL_ATTRIBUTION` | empty | Credit line the app shows while the photo is on |
| `MOWDECK_TELEMETRY_AERIAL_*` (others) | see [ops guide](../../docs/ops/aerial-photo.md) | `IMAGE_FORMAT`, `WMS_VERSION`, `MIN_ZOOM` 14, `MAX_ZOOM` 19, `TILE_SIZE` 256, `MARGIN_METERS` 60, `CACHE_DIR`, `CACHE_MAX_MB` 256, `CACHE_TTL_DAYS` 30, `MISSES_PER_MINUTE` 240, `TIMEOUT_SECONDS` 10, `ALLOW_HTTP` (tests only) |

## Theft tracking

Sessions, share links and photos of the theft tracking mode ([ADR 0058](../../docs/adr/0058-theft-tracking-mode.md), operations: [docs/ops/theft-tracking.md](../../docs/ops/theft-tracking.md)). Tables `tracking_sessions`, `tracking_links` (token hash only), `tracking_photos`; the directory also holds each mower's name and device name. Routes (all under `/api/v1/telemetry`): `GET|POST /mowers/{id}/tracking`, `POST .../tracking/stop`, `GET .../tracking/sessions`, `DELETE .../tracking/sessions/{id}`, `POST .../tracking/photo`, `GET|POST .../tracking/links`, `DELETE .../tracking/links/{id}`, `GET .../tracking/report.pdf`, `GET .../tracking/map/{layer}/{z}/{x}/{y}` (base map tiles), and `POST /public/tracking/view` and `POST /public/tracking/tile` without login (the token in the body). The base map (`map` street map from `MOWDECK_TELEMETRY_BASEMAP_*`, `aerial` from the aerial photo source) is served only for tiles that overlap the session's positions plus the margin, through the aerial photo's cache and source (`application/tracking_map.py`). Upstream requests carry `User-Agent: MowDeck/<version> (+repo)`, no `Referer`, and tiles are kept as long as `Cache-Control`/`Expires` allow (at least 7 days without them), as the OpenStreetMap tile usage policy asks; `map_layers` in the tracking status and the public view says which layers exist. Reading needs `can_view`, writing `can_control`. A session starts on `notifications.raised` of a theft alarm kind or by hand, holds a lease at the Device-Gateway (`gateway.mower.<id>.tracking`), publishes `event.tracking.{started,moved,stale,ended}` and ends by hand or after the maximum duration. The PDF is built with fpdf2.
## Layers

| Package | Content | Rule |
| --- | --- | --- |
| `domain/` | rows, write rules (`decide`), fingerprints, bin arithmetic, downsampling, health accounting (`health.py`) | pure Python, no I/O |
| `application/` | `Ingest`, `Queries`, `Health`, `Retention`, the heatmap binning (`heatmap.py`, pure, Shapely and numpy), the `Store` and `HealthEvents` ports | depends on `domain` and ports only |
| `adapters/api/` | FastAPI routes, schemas, the object gate, error mapping | translate HTTP to use cases |
| `adapters/bus/` | the consumers, the Fleet seed and the health event publisher | translate messages to use cases |
| `adapters/db/` | SQLAlchemy tables, `SqlStore` (`date_bin`, advisory lock), Alembic migrations | implement the `Store` port |
| `adapters/memory.py` | in-memory store with the same semantics | tests |
| `main.py` | wiring (`build_app`), background tasks, `main()` | the only place that knows every layer |

## Local start

```bash
uv sync
docker compose -f infra/compose/docker-compose.yml up -d --wait nats postgres openfga authz-bootstrap
MOWDECK_DATABASE_URL=postgresql://telemetry:<password>@127.0.0.1:5432/telemetry \
  uv run python -m telemetry.adapters.db.migrate
MOWDECK_ENVIRONMENT=dev MOWDECK_DATABASE_URL=postgresql://telemetry:<password>@127.0.0.1:5432/telemetry \
  MOWDECK_OPENFGA_API_URL=http://127.0.0.1:8080 MOWDECK_OPENFGA_API_TOKEN=<key> \
  MOWDECK_AUTH_JWKS_URL=http://127.0.0.1/.well-known/jwks.json \
  uv run python -m telemetry.main
curl http://127.0.0.1:8000/readyz
```

Container image (build context is the repository root):

```bash
docker build -f services/telemetry/Dockerfile -t mowdeck/telemetry:dev .
```

The Compose block is in `compose.snippet.yml` (already pasted into `infra/compose/docker-compose.yml`), Kubernetes uses the umbrella chart `infra/helm/mowdeck`.

## Migrations

Migrations run as a separate step before the service starts (Compose: `telemetry-migrate`, Helm: a Job), never inside the service process. They must stay backward compatible for rolling updates.

```bash
# create a revision (the database URL comes from MOWDECK_DATABASE_URL)
uv run alembic -c services/telemetry/alembic.ini revision -m "add column"
```

A test compares the migrations with the table definitions against a real PostgreSQL (`test_telemetry_db.py`).

## Tests

```bash
uv run pytest services/telemetry --cov=telemetry
```

The PostgreSQL, NATS and OpenFGA tests run when `MOWDECK_TEST_DATABASE_URL`, `MOWDECK_TEST_NATS_URL` and `MOWDECK_TEST_OPENFGA_URL` (with `MOWDECK_TEST_OPENFGA_TOKEN`) are set (CI sets all of them); the module docstrings of `test_telemetry_db.py`, `test_telemetry_nats.py` and `test_telemetry_openfga.py` show the docker commands. Cross-account cases (a foreign owner gets 404 on every mower route) are in `test_telemetry_api.py`; the store contract runs against both the memory store and PostgreSQL.
