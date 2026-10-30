# mowdeck-control-husqvarna

Husqvarna Automower adapter for `mowdeck-control`: a cloud provider plugin ([ADR 0066](../../docs/adr/0066-cloud-provider-plugins-and-more-vendors.md)) on top of [aioautomower](https://github.com/Thomas55555/aioautomower) 3.1.0 (MIT), the library of the Home Assistant core integration `husqvarna_automower`. It talks to the official **Automower Connect API** (REST plus an event websocket) and also covers Gardena smart mowers sold through the same Husqvarna portal.

> **Untested on real hardware, off by default.** The adapter was built and tested only against handwritten payloads shaped like the API's answers and a fake Husqvarna cloud. Operators enable it with `MOWDECK_CLOUD_PROVIDERS_ENABLE=husqvarna` (Compose: `GATEWAY_CLOUD_PROVIDERS_ENABLE=husqvarna`); the web app labels it "untested on real hardware".

Licence: Apache-2.0 (aioautomower and its dependencies are permissive).

## What the user provides

A Husqvarna **application key and application secret**. That needs a (free) **developer account on the user's side**: the user signs in to the [Husqvarna Developer Portal](https://developer.husqvarnagroup.cloud/) with their own Husqvarna account (the one of the Automower Connect app), creates an application (any name, any redirect URL such as `http://localhost`; MowDeck does not use it), connects it to the **Authentication API** and the **Automower Connect API**, and copies key and secret into MowDeck (Settings, Linked accounts). MowDeck needs no Husqvarna account of its own and never sees the Husqvarna password.

| Field | Kind | Stored |
| --- | --- | --- |
| `app_key` (identity) | text, letters, digits and dashes, 8 to 64 characters | plain, shown on the linked account |
| `app_secret` | secret | sealed by Fleet, never shown or logged |

MowDeck exchanges them for a bearer token with the OAuth2 client-credentials grant (`POST https://api.authentication.husqvarnagroup.dev/v1/oauth2/token`). The token sees the mowers of that Husqvarna account. A refused token request is `CloudLoginRefused` ("Husqvarna refused the application key or secret"); a 403 on the mower list means the application is not connected to the Automower Connect API and is refused the same way; network errors, 429 and server errors are `CloudUnavailable`. No message contains a credential, the token or a vendor payload.

## Rate limits

Husqvarna allows **1 request per second and 10,000 requests per month per application key** (HTTP 429 above that). The adapter keeps one session per application key for the discovery and every mower of the account (`AccountPool`, reference counted, 120 s linger) and lives on the websocket:

- one token request a day (tokens live 24 hours and are cached until 5 minutes before they expire, in memory only);
- `GET /mowers` once at the start, after the websocket was reopened (it is renewed every two hours and reopened after network errors) and every 30 minutes as a safety net (`HusqvarnaAccount.refresh_s`, never below aioautomower's 300 s); that is at most about 60 requests a day (1,800 a month) whatever the number of mowers;
- `POST /mowers/{id}/actions` per command;
- every REST request waits until a second has passed since the previous one of the same key (`RequestThrottle`), except the emergency stop;
- an empty websocket frame every 60 s as keepalive (no REST request); a closed websocket is reopened with a back-off from 60 s up to 16 minutes.

## Capabilities

| Capability | Value | Why |
| --- | --- | --- |
| `connection_kinds` | `cloud` | Automower Connect API |
| `model` | `system.model` | |
| `native_patterns` | none | the API has no pattern parameters (stripe angle, spacing) |
| `manual_drive`, `undock`, `custom_paths` | no | no such API |
| `camera` | `none` | |
| `light` | no | headlights are a mode (always on, evening only, ...), not a switch |
| `rtk_sources` | none | EPOS models use RTK internally, the API does not report it |
| `blade_height_range_mm` | none | the API's cutting height is a level from 1 to 9, not millimetres; reported as `raw.cutting_height_level` |
| `settings`, `maintenance`, `firmware_update` | none | |
| `start_plans` | `duration` (kind `now`), `work_area:<id>` per work area (kind `zone`, the work area's name; models with work areas), `schedule` (kind `schedule`) | the map has no areas, so the web app starts the mower through these ([ADR 0066](../../docs/adr/0066-cloud-provider-plugins-and-more-vendors.md) amendment); `duration` mows for the option `start_minutes` |

Map: the API has no geometry (work areas and stay-out zones are names only), so `get_map` returns a map without areas whose geo reference is the mower's first known GPS position (0/0 on models without GPS). Work area names are in `raw.work_areas`.

## State

| Husqvarna state | Husqvarna activity | MowDeck activity |
| --- | --- | --- |
| any, mower not connected to the cloud (`metadata.connected` false) | any | `offline` |
| `error`, `fatal_error`, `error_at_power_up` | any | `error` |
| `stopped` with an error code | any | `error` |
| `stopped` without an error code | any | `idle` |
| `wait_updating` | any | `updating` |
| `paused` | any | `paused` |
| others (`in_operation`, `restricted`, `off`, `wait_power_up`, ...) | `mowing`, `leaving` | `mowing` |
| | `going_home` | `returning` |
| | `charging` | `charging` (`charging` true) |
| | `parked_in_cs` | `docked` |
| | `stopped_in_garden` | `idle` |
| `off` | `unknown`, `not_applicable` | `idle` |
| others | `unknown`, `not_applicable` | `unknown` |

- Battery: `battery.batteryPercent`; position: the newest of the mower's GPS positions (models with `capabilities.position`), `position_source` `unknown` (the API gives no quality).
- Errors: with activity `error`, `error_code` is Husqvarna's code, `error_message` its English text (aioautomower's table) and `error_category` the neutral category from the error lexicon (see below); `drive_recoverable` is never set (no manual driving).
- Theft alarm ([ADR 0027](../../docs/adr/0027-health-history-and-theft-alarm.md)): an error with code 14 (Mower lifted), 15 (Lifted), 71 (Alarm! Mower lifted) or 131 (Lifted in link arm) sets `raw.lifted = true` and `raw.lift_code`; Notifications raises `mower_lifted` without knowing Husqvarna.
- Blades: not reported by the API; `raw.blade_on` is derived (true exactly while the state is `in_operation` and the activity `mowing`) and marked `raw.blade_on_source = "activity"`.
- Health counters for Telemetry: `raw.blade_used_s`, `raw.bat_cycles`, `raw.work_time_s`, `raw.mileage_m` from the mower's statistics.
- `observed_at` is the cloud's status time or the later time an event changed the mower's data.
- Further `raw` keys: `husqvarna_state`, `husqvarna_activity`, `husqvarna_mode`, `inactive_reason`, `restricted_reason`, `override`, `next_start`, `work_area_id`, `work_area_name`, `headlight_mode`, `remaining_charging_s`, `error_key`, `last_error_code` (a code outside an error state).

## Commands

The API accepts actions and the state confirms them later, so a sent command is `accepted`.

| MowDeck | Automower Connect |
| --- | --- |
| `StartMow` with `plan_ref` `schedule` | ResumeSchedule |
| `StartMow` with `plan_ref` `duration` or `duration:<minutes>` | Start for the minutes (default: option `start_minutes`, 60) |
| `StartMow` with `plan_ref` `work_area:<id>` or `work_area:<id>:<minutes>` | StartInWorkArea (models with work areas) |
| `StartMow` with areas or a spot plan | rejected: no map areas, no custom paths |
| `PauseMow` | Pause |
| `ResumeMow` | ResumeSchedule: the mower follows its schedule again (outside a schedule window it stays parked) |
| `ReturnToDock` | ParkUntilNextSchedule; with `end_job` ParkUntilFurtherNotice |
| `EmergencyStop` | Pause, at once (skips the throttle wait). **The API has no hard stop**: the mower stops blades and wheels as for a pause |
| everything else (blade height, speed, light, firmware, spot runs, manual driving) | rejected |

A start or resume while the mower reports an error raises `SensorStop` (a lift is never cleared by a command); a command for a mower that is not connected to the Husqvarna cloud is rejected (the emergency stop is sent anyway). Vendor answers: 403 and 400 are `rejected`, 429 is `failed` ("rate limit reached"), a refused token `failed` ("re-authenticate the linked account"), network and server errors `failed`.

Connection options (`ConnectionConfig.options`): the account's `app_key` and `app_secret` (merged by the gateway), `iot_id` (the Husqvarna mower id) or `device_name` (the mower's name; the serial number is added when two mowers of the account share a name), and `start_minutes` (1 to 10080). Other options of the `cloud` kind are ignored. `start_minutes` is the one mower option the descriptor declares (`mower_options`: integer, 1 to 10080, default 60), so Fleet stores it and the mower's settings page shows it; Fleet refuses any other option for a Husqvarna mower. The REST refresh interval is not a mower option: it belongs to the account session shared by all mowers of an application key (30 minutes).

## Known gaps

- Nothing tested on a real mower; the payload shapes follow aioautomower's models and the API documentation.
- Starting from the web app's map is not possible (no areas); starts go through the declared start plans (a duration, a work area, the schedule). Before the provider conformance kit ([ADR 0069](../../docs/adr/0069-provider-conformance-kit-and-package-template.md)) found it, none were declared and no client could start the mower.
- Not mapped: calendars and schedules, stay-out zones, work area settings, cutting height and headlight mode changes, error confirmation, messages, park for a duration.
- An authorization-code flow (a MowDeck application at Husqvarna, no developer account for the user) is an open decision of ADR 0066.

## Error lexicon

`lexicon.py` ([ADR 0067](../../docs/adr/0067-error-code-lexicon.md), registered as `husqvarna` in `mowdeck.error_lexicons`) explains every code of aioautomower's table (the names of the Automower Connect API): a neutral category (the same function fills `MowerState.error_category`), a severity (lift, tilt and the theft alarms 69 to 74 critical, defects errors, notices such as "Firmware has been updated" info), whether a person has to act, an English and a German title and a description and steps per category. The codes MowDeck acts on (trapped, outside, upside down, lifted, tilted, the theft alarms, battery empty, no power at the charging station, no accurate satellite position) have their own words with the likely cause. Lift and tilt codes (10, 14, 15, 33, 71, 72, 131) are `lift_tilt`, so Notifications raises the theft alarm by category; codes 14, 15, 71 and 131 also set `raw.lifted`.

The lexicon lists no connection kinds: Husqvarna mowers connect as `cloud` like the Mammotion cloud mowers, and a lookup by connection kind takes the first lexicon that lists it, so listing `cloud` here would hand Mammotion's codes to this lexicon. It is found by its vendor key (`GET /api/v1/telemetry/error-codes/husqvarna/15`) and, for the per-mower explanations (Telemetry's `/mowers/{id}/error`, the widget, the settings' error code list, the push texts), by the mower's cloud provider: the Device-Gateway stamps `cloud_provider` (`husqvarna`, equal to the vendor key) on every state, and the lookup prefers it over the connection kind ([ADR 0067](../../docs/adr/0067-error-code-lexicon.md#picking-the-lexicon-by-cloud-provider-added-2026-10-29)).

## Fake cloud

With `ProviderContext.fake` (gateway: `MOWDECK_FAKE_CLOUDS=true`, development and browser tests only) the plugin talks to `fake_cloud.FakeHusqvarnaCloud` through the same code path (token, throttle, aioautomower's session, websocket):

- every application key starting with `fake-husqvarna-` (e.g. `fake-husqvarna-app-key`) exists and takes the secret `fake-cloud-password`; anything else is refused like a wrong key;
- each fake application has two mowers: `Hugo` (Automower 430X NERA, online, parked, GPS, headlights, work areas `123456` Front lawn and `654321` Back lawn) and `Greta` (Automower 305, offline, no GPS);
- actions change `Hugo` and push websocket events; a park drives home and arrives after two seconds.

## Layout

| Module | Imports aioautomower | Role |
| --- | --- | --- |
| `plugin.py` | no | `SPEC` (entry point `mowdeck.cloud_providers`: `husqvarna`) |
| `descriptor.py` | no | credential fields and guidance |
| `transport.py` | no (aiohttp) | the HTTP and websocket port, `AiohttpTransport` |
| `auth.py` | yes | token cache, request throttle, aioautomower `AbstractAuth` |
| `account.py`, `pool.py` | yes | the shared session per application key |
| `mapping.py`, `options.py`, `connection.py`, `provider.py` | yes | translation, the `MowerConnection`, the plugin |
| `lexicon.py` | no (a copy of its code table, kept equal by a test) | the error lexicon (entry point `mowdeck.error_lexicons`: `husqvarna`) |
| `fake_cloud.py` | yes | the fake vendor (imported only with `ProviderContext.fake`) |

Tests (`tests/`, no network): descriptor and entry point, token cache and refusals, throttle, discovery from a recorded-shape `GET /mowers` payload parsed by aioautomower, the state mapping table, lift codes, command translation and vendor errors, the refresh schedule, websocket events, the error lexicon (every code, both languages, categories equal to the state's), the fake cloud end to end, and that no secret reaches a log, a message or a `repr`. `test_husqvarna_conformance.py` runs the provider conformance kit (`mowdeck-provider-testkit`, [ADR 0069](../../docs/adr/0069-provider-conformance-kit-and-package-template.md)) on the fake cloud, with aioautomower's code table as the codes the lexicon must explain.

```bash
uv run pytest libs/mowdeck-control-husqvarna
```
