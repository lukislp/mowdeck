# mowdeck-control-worx

Worx Landroid adapter for `mowdeck-control`, also for **Kress** and **LandXcape** mowers (the same Positec cloud behind three brand endpoints): a cloud provider plugin ([ADR 0066](../../docs/adr/0066-cloud-provider-plugins-and-more-vendors.md)) on top of [pyworxcloud](https://github.com/MTrab/pyworxcloud) 6.4.2, the library of the Home Assistant custom integration `landroid_cloud`. It talks to the brand's **unofficial** cloud API (REST for the login and the device list, MQTT over AWS IoT for the mowers' reports and the commands).

> **Untested on real hardware, off by default.** The adapter was built and tested only against handwritten payloads shaped like the cloud's answers (as pyworxcloud decodes them) and a fake cloud. Operators enable it with `MOWDECK_CLOUD_PROVIDERS_ENABLE=worx` (Compose: `GATEWAY_CLOUD_PROVIDERS_ENABLE=worx`); the web app labels it "untested on real hardware".

> **Unofficial API, account block risk.** The vendor does not document or support this interface; it can change or stop working at any time, and the vendor has temporarily blocked accounts that logged in or polled too often. The provider's link form says both (`ProviderDescriptor.warnings`), and the adapter logs in as rarely as it can (see "Rate limits and the login guard").

Licence: **GPL-3.0-only**. pyworxcloud's PyPI metadata says MIT, but the `LICENSE` file in its sdist is the GPL-3.0; the file wins, so this package is GPL-3.0 and isolated like the Mammotion adapter ([ADR 0011](../../docs/adr/0011-gpl-package-for-pymammotion-adapter.md)). No core service imports it; only the Device-Gateway image installs it.

## What the user provides

The **e-mail address and the password of the brand app's account** (Worx Landroid, Kress or LandXcape app) and the **brand**. No developer account. pyworxcloud logs in like the app (the brand app's OAuth client id, password grant). A second account the mower is shared with keeps the phone app signed in and limits what a block would affect.

| Field | Kind | Stored |
| --- | --- | --- |
| `brand` | choice: `worx`, `kress`, `landxcape` | plain, shown on the linked account |
| `email` (identity) | e-mail | plain, shown on the linked account |
| `password` | secret | sealed by Fleet, never shown or logged |

A refused login (HTTP 400, 401, 403) is `CloudLoginRefused` ("Worx Landroid refused the e-mail address or password"); HTTP 429 is `LoginRateLimited` (a `CloudUnavailable`); network and server errors are `CloudUnavailable`. No message holds a credential, a token or a vendor payload.

## Rate limits and the login guard

One session (one `WorxCloud`: one login, one MQTT link) per account serves the discovery and every mower of the account (`AccountPool`, reference counted, 120 s linger, so linking an account and registering its mowers needs one login). The session lives on **MQTT push**; MowDeck adds no polling of its own. pyworxcloud itself refreshes the device list over REST every 5 to 10 minutes (at most about 290 requests a day per account), renews its token half an hour before it expires and reconnects a dropped MQTT link on its own (at most every 15 minutes). A dropped MQTT link is therefore not a lost connection for the gateway: `is_connected` stays true while the session is held, so the gateway's reconnect loop (every 1 to 30 s) never turns into a login loop.

`LoginGuard` keeps logins rare when something goes wrong:

| After | The same credentials are not tried again for |
| --- | --- |
| a refused login | 15 minutes (a corrected password is tried at once) |
| HTTP 429 | 60 minutes, for every password of the account |
| a cloud or network error | 2 minutes |
| a successful login | 60 seconds before the next login of the account |

## Capabilities

What a mower gets depends on what it reports; nothing is declared that the cloud does not offer.

| Capability | Value | Why |
| --- | --- | --- |
| `connection_kinds` | `cloud` | |
| `model` | the product's name (`model.friendly_name`) | |
| `start_plans` | `now`, `zone:1` to `zone:4` (zones with a starting point in the rotation; only when there are at least two, option `offer_zones`), `edge` (models with the one-time schedule) | the cloud has no map geometry, so starts are plans, not areas (ADR 0066 amendment) |
| `blade_height_range_mm` | 30 to 60 on models with the cutting height module (`EA`); 5 mm steps | the range follows the brands' specifications and is not confirmed through the API |
| `settings` | `rain_delay` (0 to 1440 min) where the mower reports `cfg.rd` | ADR 0046 |
| `native_patterns`, `start_options` | none | no pattern parameters |
| `manual_drive`, `undock`, `custom_paths` | no | the cloud has no such commands |
| `camera`, `light` | none, no | Vision models' cameras and headlights are not reachable through the API |
| `rtk_sources` | none | RTK models report a position, not its quality |
| `maintenance`, `firmware_update` | none | not mapped (see known gaps) |

Map: no geometry (zones are starting points along the wire, not areas), so `get_map` returns a map without areas whose geo reference is the mower's first known GPS position (0/0 without GPS).

## State

| Worx | MowDeck activity |
| --- | --- |
| the cloud reports the mower offline (`online` false) | `offline` |
| an error code (`dat.le`) other than 0 (no error) and 5 (rain delay) | `error` |
| status trapped (9) or blade blocked (10) without an error code | `error` (category trapped or blade motor) |
| status idle (0) | `idle` |
| status home (1) | `docked`, `charging` while `dat.bt.c` is 1 |
| start sequence (2), leaving home (3), mowing (7, 8), digital fence escape (13), zoning (31), cutting edge (32), searching area (33), searching zone (103), border crossing (110), exploring lawn (111) | `mowing` |
| follow wire (4) | `returning` when the mower was returning before, else `mowing` |
| searching home (5, 104), searching wire (6), going home (30) | `returning` |
| pause (34) | `paused` |
| debug (11), remote control (12), unknown | `unknown` |

- Battery `dat.bt.p` (the last known value while a report carries none), charging `dat.bt.c == 1`, Wi-Fi signal `dat.rsi`, firmware `dat.fw`, rain sensor `dat.rain.s` (`rain_detected`), blade height `cfg.modules.EA.h`, the rain delay as the setting `rain_delay`.
- Position: the GPS module's `dat.modules.4G.gps.coo` (`position_source` `gnss`) or an RTK model's `dat.rtk.pos` (`unknown`, no quality reported).
- Errors: with activity `error`, `error_code` is the Worx code, `error_message` its English title and `error_category` the neutral category from the error lexicon.
- Theft alarm ([ADR 0027](../../docs/adr/0027-health-history-and-theft-alarm.md)): the codes lifted (2) and upside down (11) set `raw.lifted = true` and `raw.lift_code`; their category is `lift_tilt`, so Notifications raises `mower_lifted` without knowing Worx.
- Blades: not reported; `raw.blade_on` is derived (true in the statuses mowing, cutting edge and border crossing) and marked `raw.blade_on_source = "status"`.
- Health counters for Telemetry: `raw.blade_used_s`, `raw.work_time_s` (the cloud counts minutes), `raw.mileage_m`, `raw.bat_cycles`.
- Further `raw` keys: `vendor` (`worx`), `worx_status`, `worx_status_name`, `worx_protocol`, `zone` (1 to 4), `locked`, `rain_delay_remaining_min`, `last_error_code` (a code outside an error state, e.g. the rain delay).

## Commands

Commands go over MQTT and the mower confirms them through its next report, so a sent command is `accepted`.

| MowDeck | Worx (pyworxcloud) |
| --- | --- |
| `StartMow` with `plan_ref` `now` | `start` |
| `StartMow` with `plan_ref` `zone:<1-4>` | `setzone` (the next start begins at that zone), then `start` |
| `StartMow` with `plan_ref` `edge` | `edgecut` (one-time schedule along the boundary) |
| `StartMow` with areas or a spot plan, or an undeclared plan | rejected |
| `PauseMow` | `pause` |
| `ResumeMow` | `start` (continues the paused job) |
| `ReturnToDock` | `home`: the job ends, the next one follows the mower's own schedule. `dock_keeps_job` is not declared, so the app offers no "keep the job" choice and the controller refuses `end_job: false` |
| `EmergencyStop` | `pause` at once. **The cloud has no hard stop**: the mower stops its blades and wheels as for a pause |
| `SetBladeHeight` | `set_cutting_height` (models with the cutting height module, 30 to 60 mm in 5 mm steps) |
| `SetSetting` `rain_delay` | `raindelay` |
| everything else (speed, light, firmware, spot runs, manual driving) | rejected |

A start or resume while the mower reports an error raises `SensorStop` (a lift is never cleared by a command); a command for a mower the cloud reports offline is rejected (the emergency stop is sent anyway). An offline mower and a missing feature are `rejected`; no answer within the option `command_timeout_s`, a down MQTT link, 429 and cloud errors are `failed`.

Connection options (`ConnectionConfig.options`): the account's `brand`, `email` and `password` (merged by the gateway), `iot_id` (the serial number) or `device_name` (the mower's name; the serial number is added when two mowers share a name), and the two mower options the descriptor declares, so Fleet stores them and the mower's settings page shows them:

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `offer_zones` | boolean | true | list the mower's zones as start choices |
| `command_timeout_s` | number, 5 to 120 (advanced) | 30 | how long a command waits for the mower's answer |

## Error lexicon

`lexicon.py` ([ADR 0067](../../docs/adr/0067-error-code-lexicon.md), registered as `worx` in `mowdeck.error_lexicons`) explains every error code of pyworxcloud's table (1 to 21, 100 to 116) in English and German: a neutral category (the same function fills `MowerState.error_category`), a severity (lifted and upside down critical, the rain delay info, defects such as a missing or reversed boundary wire errors, the rest warnings), whether a person has to act, a title and a description and steps per category; trapped, lifted, upside down, outside the wire, rain delay, battery low, charging error and charging station not found have their own words with the likely cause. **Every entry is `verified: false`**: the vendor publishes no code table, and pyworxcloud's names come from reverse engineering the app. The excessive slope (104) is `other`, not `lift_tilt`, so it never raises the theft alarm.

The lexicon lists no connection kinds: Worx mowers connect as `cloud` like other vendors' cloud mowers, so it is found by its vendor key (`GET /api/v1/telemetry/error-codes/worx/2`); `raw.vendor` (`worx`) is in every state.

## Fake cloud

With `ProviderContext.fake` (gateway: `MOWDECK_FAKE_CLOUDS=true`, development and browser tests only) the plugin opens its sessions at `fake_cloud.FakeWorxCloud`, which implements the same session port as the real one on product items shaped like the cloud's; the snapshot reader, the mapping, the pool and the connection run unchanged:

- every e-mail address at `fake.mowdeck.test` exists at every brand and takes the password `fake-cloud-password`; anything else is refused; an address starting with `limited` is answered with HTTP 429;
- each fake account has two mowers: `Wanda` (online, docked, GPS module, cutting height module at 45 mm, zones 1 and 2, edge cut, rain delay 60 min) and `Kurt` (offline, no modules);
- commands change `Wanda` and push a report; home drives home and arrives after two seconds.

## Layout

| Module | Imports pyworxcloud | Role |
| --- | --- | --- |
| `plugin.py` | no | `SPEC` (entry point `mowdeck.cloud_providers`: `worx`) |
| `descriptor.py` | no | credential fields, guidance, warnings, mower options |
| `snapshot.py` | no | `WorxMower`: one product item and its last report, read from plain JSON |
| `session.py` | no | the session port, actions and command errors |
| `pool.py` | no | the shared session per account and the login guard |
| `mapping.py`, `options.py`, `connection.py`, `provider.py` | no | translation, the `MowerConnection`, the plugin |
| `pyworx_session.py` | yes | the real session on `WorxCloud` (imported when a real session opens) |
| `lexicon.py` | no (a copy of pyworxcloud's tables, kept equal by a test) | the error lexicon (entry point `mowdeck.error_lexicons`: `worx`) |
| `fake_cloud.py` | no | the fake vendor (imported only with `ProviderContext.fake`) |

Tests (`tests/`, no network): descriptor and entry points, that the plugin imports without pyworxcloud, the snapshot reader on recorded-shape product items (a wire mower, a GPS mower, a Vision-style RTK mower, an offline mower), the state table, lift codes, capabilities and start plans, command translation and vendor errors, the login guard and the session pool, the real session against a stand-in `WorxCloud` (exception mapping, events from another thread), the error lexicon (every code, both languages, categories equal to the state's, the copies equal to pyworxcloud's tables), the fake cloud end to end through the `MowerController`, and that no secret reaches a log, a message or a `repr`. `test_worx_conformance.py` runs the provider conformance kit (`mowdeck-provider-testkit`, [ADR 0069](../../docs/adr/0069-provider-conformance-kit-and-package-template.md)) on the fake cloud, with pyworxcloud's error table as the codes the lexicon must explain.

```bash
uv run pytest libs/mowdeck-control-worx
```

## Known gaps

- Nothing tested on a real mower; the payload shapes follow pyworxcloud 6.4.2's decoder.
- Not mapped: schedules, the party mode (schedule off), the lock, off-limits and ACS modules, the torque, zone training, restart, firmware updates, the Vision models' auto schedule and border settings.
- Vision models report RTK zones by id (`cfg.rtk.zs`); the zone start plans cover only the four starting points of wire-guided models.
- The Scheduler's schedules start mowers by map area, which a Worx mower has none of; the mower's own schedule (set in the app) stays in charge.
