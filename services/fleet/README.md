# fleet

Sites, groups and mowers of an account, their place in the hierarchy, and the commands sent to them. Fleet is the only producer of mower registrations and commands: it writes the hierarchy tuples to OpenFGA, feeds the Device-Gateway's registry and checks every command before it reaches the bus.

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; layer rules in `docs/dev/service-template.md`. Design: [Design: fleet, telemetry, realtime](../../docs/dev/design-fleet-telemetry-realtime.md), section 1. Decision: [ADR 0016](../../docs/adr/0016-fleet-commands-and-authorization-per-command-kind.md) (relation per command kind, command records, bulk commands, default site, tuples in the transaction); tuple ownership in [ADR 0014](../../docs/adr/0014-authorization-model-and-tuple-ownership.md).

## Purpose

- **Sites** (a garden, a sports ground) with time zone, optional address and optional coordinates (`latitude`, `longitude`: the Scheduler's rain pause asks the weather there). Every account has one **hidden default site**, created when `identity.account.created` arrives and lazily when a mower is registered without a site.
- **Groups** of mowers inside a site or across the sites of the account.
- **Mowers** with connection kind and options, always in one site and in any number of groups; serial numbers are unique per account. Options are validated per kind (see below); secret values are never returned.
- **Area names**: a custom name per mower and map area id (`mower_area_names`, migration 0007; see [Area names](#area-names)). The mower keeps its own names; Fleet never writes to the mower.
- **Mowing defaults**: the pattern settings every client starts a mower with (`mowers.mow_defaults`, migration 0009; see [Mowing defaults](#mowing-defaults)). Nothing is sent to the mower.
- **Additional links** ([ADR 0038](../../docs/adr/0038-dual-link-per-mower-and-firmware-updates.md)): a Bluetooth bridge (an ESPHome Bluetooth proxy) next to a cloud mower's connection (`mower_links`, migration 0010; see [Links](#links)). Whether a connection kind may carry a link is a registry entry (`supports_links`), never a vendor name; the bridge key is encrypted like a linked account's password. Scans through the bridge and firmware checks are relayed to the Device-Gateway.
- **Linked Mammotion accounts** ([ADR 0034](../../docs/adr/0034-linked-cloud-accounts-and-device-discovery.md)): several per account, the password encrypted at rest, the device list read through the Device-Gateway (`gateway.cloud.discover`); cloud mowers are registered from them. One physical mower (its Mammotion IoT id) can be registered in one MowDeck account only, installation-wide.
- **Hierarchy tuples** in OpenFGA (`site#account`, `group#account`, `group#parent`, `mower#account`, `mower#parent`) through `TupleWriter(owner="fleet")`. Grants and shares are role assignments in Identity.
- **Registry of the Device-Gateway**: answers `fleet.mowers.list` per shard and publishes `mower.registered`, `mower.updated`, `mower.removed`.
- **Commands**: checked per command kind, recorded (who, what, when, status: the command audit), published, followed through the gateway's results; bulk commands per site or group; commands other services issue on behalf of a subject (`fleet.commands.issue`, the Scheduler) with the same checks and the record's `origin` and `schedule_id`.
- **Manual drive sessions** (joystick, [ADR 0026](../../docs/adr/0026-manual-drive-channel.md)): Realtime opens a session once per drive (`fleet.drive.start`, `can_drive` for the subject, one open session per mower) and closes it (`fleet.drive.stop`); the frames go from Realtime to the Device-Gateway and never pass Fleet. The sessions are the drive audit.
- **Audit log** (append-only) of every change to sites, groups, mowers and their links.

## Interfaces

All routes are mounted under `/api/v1` (OpenAPI at `/api/v1/openapi.json`); Traefik routes `/api/v1/fleet` to this service. The account is always the verified token's `acc`: nothing in a path, query or body can name another account, and every row is loaded with that account filter. "hide" means 403 when the caller sees the object but lacks the relation and 404 when it does not see it (objects of other accounts are always 404); "403" means the existence is no secret (the caller's own account).

| Method | Path | Required relation | Notes |
| --- | --- | --- | --- |
| GET | `/api/v1/fleet/sites` | `can_view` per site | `?include_default=true` shows the hidden default site |
| POST | `/api/v1/fleet/sites` | `can_manage_mowers` on the account (403) | `{name, timezone?, address?, latitude?, longitude?}`, 201; coordinates both or none (422) |
| GET | `/api/v1/fleet/sites/{site_id}` | `can_view` (hide) | |
| PATCH | `/api/v1/fleet/sites/{site_id}` | `can_manage_mowers` (hide) | the default site keeps its name (409); `latitude`/`longitude` change together, `null` clears them |
| DELETE | `/api/v1/fleet/sites/{site_id}` | `can_manage_mowers` (hide) | 409 for the default site or while mowers or groups use it |
| POST | `/api/v1/fleet/sites/{site_id}/commands` | `can_bulk_command` (hide; `can_emergency_stop` for an emergency stop), then per mower | 202 `BulkCommandAccepted` |
| GET | `/api/v1/fleet/groups` | `can_view` per group | `?site_id=` |
| POST | `/api/v1/fleet/groups` | `can_manage_mowers` on the parent site (hide), or on the account for a cross-site group (403) | `{name, site_id?}`, 201 |
| GET | `/api/v1/fleet/groups/{group_id}` | `can_view` (hide) | `mower_ids` lists only mowers the caller sees |
| PATCH | `/api/v1/fleet/groups/{group_id}` | `can_manage_mowers` (hide), and on the new site | `{name?, site_id?}`; `site_id: null` makes it cross-site |
| DELETE | `/api/v1/fleet/groups/{group_id}` | `can_manage_mowers` (hide) | 409 while it has mowers |
| POST | `/api/v1/fleet/groups/{group_id}/commands` | as for sites | 202 `BulkCommandAccepted` |
| GET | `/api/v1/fleet/connection-kinds` | any principal of an account | The connection kinds accepted here (`MOWDECK_ALLOWED_CONNECTION_KINDS`, in the order simulated, cloud, ble_bridge, others) with the option schema of each (see [Connection options](#connection-options)) |
| GET | `/api/v1/fleet/mowers` | `can_view` per mower | `?site_id=&group_id=` |
| POST | `/api/v1/fleet/mowers` | `can_manage_mowers` on the target site (hide; the account (403) for the default site) and on each group (hide) | `{name, connection_kind, connection_options?, model?, serial?, site_id?, group_ids?}`, 201 with `Location`; `cloud`: `{linked_account_id, device_name, name?, ...}` (see [Linked accounts](#linked-accounts)), 409 `mower-already-registered` |
| GET | `/api/v1/fleet/mowers/{mower_id}` | `can_view` (hide) | |
| PATCH | `/api/v1/fleet/mowers/{mower_id}` | `can_manage_mowers` (hide), and on a new site | name, model, serial, connection, site |
| GET | `/api/v1/fleet/mowers/{mower_id}/area-names` | `can_view` (hide) | `AreaNamesOut {names: {area_id: name}}`; the same names come in `MowerOut.area_names` |
| PUT | `/api/v1/fleet/mowers/{mower_id}/mow-defaults` | `can_control` (hide) | `{pattern: MowPattern \| null}`; unknown keys and out-of-range values 422; `null` drops the defaults; answers `MowerOut` with `mow_defaults` |
| PUT | `/api/v1/fleet/mowers/{mower_id}/area-names/{area_id}` | `can_control` (hide) | `{name: str \| null}`; trimmed, at most 60 characters (422), `null` or blank drops the custom name; answers all names; publishes `fleet.mower.area_names_changed` after a change |
| PUT | `/api/v1/fleet/mowers/{mower_id}/groups` | `can_manage_mowers` (hide) on the mower and on every added or removed group | `{group_ids}` |
| DELETE | `/api/v1/fleet/mowers/{mower_id}` | `can_manage_mowers` (hide) | 204; command records stay |
| POST | `/api/v1/fleet/mowers/{mower_id}/commands` | by kind (hide): `can_control` (start, pause, resume, dock, blade height, speed, light, spot stop), `can_drive` (manual drive, spot mow), `can_manage_mowers` (firmware update), `can_emergency_stop` = `can_view` (emergency stop) | `CommandRequest` in, 202 `CommandAccepted` with `Location` |
| GET | `/api/v1/fleet/mowers/{mower_id}/commands` | `can_view` (hide) | newest first; `?limit=&before=` (keyset on `issued_at`) |
| GET | `/api/v1/fleet/mowers/{mower_id}/drive-sessions` | `can_view` (hide) | the drive audit, newest first: who, start, expiry, end and reason, frames published and dropped; `?limit=&before=` (keyset on `started_at`) |
| POST | `/api/v1/fleet/mowers/{mower_id}/camera/session` | `can_view` (hide) and the plan feature `camera` | a short-lived live camera session (`CameraSession`, `Cache-Control: no-store`), see [Camera](#camera); 409 `camera-unsupported`, 503 when no gateway or vendor cloud answers |
| POST | `/api/v1/fleet/mowers/{mower_id}/links/ble_bridge/scan` | `can_manage_mowers` (hide) | `{host, port?, noise_psk?}` (`***` or omitted: the stored key): look for the mower through the bridge, relayed to the Device-Gateway; 200 `BridgeScanReply` (scan outcomes `unreachable`, `auth_failed`, `timeout`, `busy`, `already_connected` inside the reply), see [Links](#links); 409 `link-not-applicable` or `bridge-scan-unsupported`, 503 without a gateway |
| PUT | `/api/v1/fleet/mowers/{mower_id}/links/ble_bridge` | `can_manage_mowers` (hide) | `{host, port?, noise_psk, ble_address, device_name?, allow_manual_blades?, manual_blade_height_mm?}`: create or replace the Bluetooth bridge link; 422 per option `#/links/ble_bridge/<key>`, 409 `link-not-applicable`, 503 without a credential key; answers `LinkOut` (key `***`) and publishes `mower.updated` |
| GET, DELETE | `/api/v1/fleet/mowers/{mower_id}/links/ble_bridge` | GET `can_view`, DELETE `can_manage_mowers` (hide) | GET: `LinkOut` with the key masked and the cached `proxy_info` (404 without a link); DELETE: 204 and `mower.updated` without the link |
| POST | `/api/v1/fleet/mowers/{mower_id}/links/ble_bridge/check` | `can_manage_mowers` (hide) | scan again with the stored host, port and key ("re-check"); refreshes `proxy_info`; 404 without a link |
| POST | `/api/v1/fleet/mowers/{mower_id}/firmware/check` | `can_view` (hide) | ask the vendor for firmware updates now, relayed to the Device-Gateway; 200 `FirmwareInfo`; 409 `firmware-check-unsupported`, 503 offline or without a gateway |
| POST | `/api/v1/fleet/mowers/{mower_id}/path-preview` | `can_control` (hide) and the plan feature `control` | `PathPreviewRequest{area_ids, pattern}` (as for `start_mow`): the mower plans the route without starting, answer `MowPath` (`Cache-Control: no-store`), see [Path preview](#path-preview); 409 `path-preview-unsupported` or `path-preview-rejected`, 503, 504 `gateway-timeout` |
| GET | `/api/v1/fleet/linked-accounts` | `can_manage_mowers` on the account (403) | the linked Mammotion accounts (no passwords): e-mail, last sync and error, device and mower counts |
| POST | `/api/v1/fleet/linked-accounts` | as above | `{email, password}`: log in through the Device-Gateway, store with the device list; 201 with the devices; 409 linked already, 422 `linked-account-login-failed`, 503 without key, gateway or cloud |
| GET, DELETE | `/api/v1/fleet/linked-accounts/{id}` | as above (404 for other accounts) | delete: 204, 409 `linked-account-in-use` while mowers use it (each in `errors`, pointer `#/mowers/<id>`) |
| POST | `/api/v1/fleet/linked-accounts/{id}/sync` | as above | `{password?}`: read the devices again ("Konto neu einlesen"); a failure is kept in `last_sync_error`; a new password is stored and re-published to the account's mowers |
| GET | `/api/v1/fleet/linked-accounts/{id}/devices` | as above | the last device list: name, model, IoT id, online, `is_mower`, `registered` (+ `mower_id`), `registered_elsewhere` |
| POST | `/api/v1/fleet/linked-accounts/{id}/mowers` | as above for the link, and per mower what `POST /mowers` needs | `{mowers: [{device_name, name?, site_id?, group_ids?, serial?}]}` (1 to 50): one registration each; 201 `{created, failed}`; when none could be created the first problem |
| GET | `/api/v1/fleet/commands/{command_id}` | record of the account and `can_view` on its mower | 404 otherwise; `CommandView` (`origin`, `schedule_id` for issued commands) |
| GET | `/healthz`, `/readyz`, `/metrics`, `/.well-known/mowdeck-module` | none | Operations (kit); readiness covers the database, NATS and OpenFGA |

Bodies of the command routes are shared contracts (`mowdeck_contracts.v1.commands`: `CommandRequest`, `CommandAccepted`, `CommandView`, `BulkCommandAccepted`); the resource views stay in the OpenAPI document. Request bodies refuse unknown fields (422).

### Commands

1. The relation of the command kind is checked first (`mowdeck_authz.fastapi.authorize`); a refused command touches neither the database nor the bus.
2. The mower row is loaded with the account filter, the `CommandEnvelope` is built with `account_id` from the row and `issued_by` from the principal, the expiry from the settings (emergency stop and manual drive always short), and the record is committed with status `pending`.
3. The command is published on `command.mower.<id>` (msg id = command id). If the bus is unavailable the record becomes `failed` and the answer is 503 (no retry: a late start is worse than none).
4. Results on `command.result.mower.*` move the record forward only: `pending`, `accepted`, then `executed`, `rejected`, `expired` or `failed`. Redelivered, out-of-order and unknown results change nothing.

Issued commands (`fleet.commands.issue`, core NATS, queue `fleet`): another service sends an `IssueCommandRequest{on_behalf_of, account_id, mower_id, command, origin, schedule_id}`. Fleet turns `on_behalf_of` into a principal of `account_id` and runs exactly the checks of the HTTP endpoint for it: the relation of the command kind (`forbidden` when the subject sees the mower, `not_found` when it does not or the mower is in another account; service principals and unknown kinds are `invalid`); a refused command touches neither the database nor the bus. Accepted commands are recorded with `issued_by` = the subject and the new columns `origin` and `schedule_id`, then published like any other (`unavailable` when the bus does not take it; the record is `failed`). The answer is an `IssueCommandReply` with exactly one of `accepted` (`CommandAccepted`) or `error{code, detail}`. Every request is logged (`command issued on behalf of a subject`) and counted (`mowdeck_fleet_issued_commands_total{origin, outcome}`). Core NATS is internal: clients reach Fleet only through Traefik's HTTP routes ([ADR 0024](../../docs/adr/0024-scheduler-recurrence-rain-pause-and-command-issuing.md)).

Drive sessions (`fleet.drive.start` and `fleet.drive.stop`, core NATS, queue `fleet`, internal, from Realtime): a `DriveStartRequest{on_behalf_of, account_id, mower_id, blade_on_allowed, max_seconds, connection_id}` gets the checks of a `manual_drive` command for the subject (`can_drive`; `forbidden`, `not_found`, `invalid` as above) and `busy` while another session of the mower is open. A per-mower transaction lock serializes concurrent starts; an open session past its `expires_at` (Realtime never closed it) is closed with `expired` by the next start. The answer is a `DriveStartReply` with `started{session_id, account_id, started_at, expires_at}` or `error`. `DriveStopRequest{session_id, account_id, mower_id, reason, ended_at, frames, frames_dropped}` closes an open session of the account (`DriveStopReply{found}`; closing twice is harmless). Starts are counted (`mowdeck_fleet_drive_sessions_total{outcome}`); sessions are purged with the command retention.

Bulk commands: allowed mowers get their own command (shared `bulk_id`), visible mowers without the right are reported `forbidden`, mowers the caller cannot see are omitted. At most `MOWDECK_BULK_MAX_MOWERS` mowers (422 above).

Spot plans: a `start_mow` may carry a `spot_plan` computed by the Planner instead of `area_ids` ([ADR 0030](../../docs/adr/0030-mowing-patterns-and-planner.md)), and a `spot_mow` always carries its `plan` (a run MowDeck drives over the bridge, ADR 0039). Fleet accepts either for one mower only (422 in a bulk command; `spot_mow` is a single-mower kind, `fleet.domain.rules.SINGLE_MOWER_KINDS`), at most `MOWDECK_SPOT_PLAN_MAX_BYTES` as JSON (422), with the plan feature `spot_mowing`; the record keeps `plan_hash` (`CommandView.plan_hash`, column added in migration 0005) and a summary (hash, chain, segment count, estimates, native job) instead of the segments. The command itself carries the whole plan to the Device-Gateway.

Feature gates per kind are data (`fleet.domain.rules.command_features`): nothing for the emergency stop, `control` for every other kind (so also `firmware_update`), plus `camera` for `set_light` and `spot_mowing` for `spot_mow`; a `start_mow` with a spot plan adds `spot_mowing` in the use case.

### Area names

The mower's map (`MowerMap.areas[]` from the Device-Gateway, stored by Telemetry) carries the mower's own area names, often only a vendor hash. Fleet keeps a MowDeck name per mower and area id in `mower_area_names (mower_id, area_id, account_id, name, updated_at, updated_by)`, primary key `(mower_id, area_id)`, deleted with the mower (`ON DELETE CASCADE`; the use case deletes the rows explicitly too).

- `GET /mowers/{id}/area-names` (`can_view`, hidden otherwise) answers `{"names": {"<area_id>": "<name>"}}`; every `MowerOut` carries the same map as `area_names` (list, detail and the answers of the changing routes).
- `PUT /mowers/{id}/area-names/{area_id}` (`can_control`: 403 when the mower is visible, else 404) with `{"name": "Vorgarten"}` stores the name trimmed (inner whitespace collapsed; at most 60 characters, 422 above); `{"name": null}` or a blank name drops it. Any area id of 1 to 128 characters is accepted, because the client's map may be newer than any check here; a name for an area the map no longer has is never shown. An unchanged name changes nothing; a change appends the audit row `mower.area_renamed` (area, new and previous name) and publishes `fleet.mower.area_names_changed` with `AreaNamesChanged {mower_id, account_id, names}` (all names after the change) on `EVENTS`.
- Display rule, the same everywhere (`mowdeck_contracts.v1.area_display_name`, the web app's `areaLabel`, the Home Assistant coordinator in [`lukislp/mowdeck-hacs`](https://github.com/lukislp/mowdeck-hacs)): the custom name, else the mower's own name unless it is empty or only a long number (a hash), else "Area N" / "Bereich N" with N the 1-based position in the map's area order. The number comes from the map snapshot, so it is the same for everybody who sees that snapshot and changes only when the mower's map changes.

### Mowing defaults

A native start (`StartMow{area_ids, pattern}`) names its pattern itself; Fleet keeps per mower the settings every client starts with, so the web start sheet, Home Assistant and later the schedules pre-fill the same values. `mowers.mow_defaults` (JSONB, NULL until saved) holds `{"pattern": MowPattern}` with the contracts' `MowPattern` (`mowdeck_contracts.v1`): `name` (`stripes`, `diagonal`, `checkerboard`, `edge_only`, `random_angle`), `angle_deg` (0 to 180), `border_laps` (0 to 4, default 1) and the optional `spacing_m` (0.05 to 2.0; absent: the mower's own spacing).

- `PUT /mowers/{id}/mow-defaults` (`can_control`: 403 when the mower is visible, else 404) with `{"pattern": {"name": "diagonal", "angle_deg": 45, "border_laps": 1}}` stores the validated pattern (missing keys take the `MowPattern` defaults, unknown keys are refused); `{"pattern": null}` drops it. An unchanged value changes nothing; a change appends the audit row `mower.mow_defaults_changed` (new and previous value). No event: the value comes with every `MowerOut`.
- Every `MowerOut` carries `mow_defaults` (`{"pattern": {...}}` or `null`; clients then use stripes at 0 degrees with one border lap). Whether the mower can run the pattern is decided with its capabilities (`native_patterns`, `path_spacing_range_mm`), not here.
- `CommandView.pattern` repeats the pattern a `start_mow` on map areas was sent with (None for other kinds, spot plans and starts without one), so the command history can say "Mähen starten · Streifen 45° · 1 Randrunde".

### Linked accounts

[ADR 0034](../../docs/adr/0034-linked-cloud-accounts-and-device-discovery.md). A linked account (`linked_accounts`) is a Mammotion account of a MowDeck account: `email`, the password encrypted with AES-256-GCM under `MOWDECK_CREDENTIAL_ENCRYPTION_KEY` (random nonce, the linked account id as associated data, a key id byte; `MOWDECK_CREDENTIAL_ENCRYPTION_KEY_PREVIOUS` still decrypts during a rotation and the next successful re-read re-encrypts with the current key; never returned, never logged), the cached device list of the last successful read (`devices`), `last_synced_at` and `last_sync_error`. One link per e-mail and account (409). Fleet never talks to the Mammotion cloud: create and sync send a `CloudDiscoverRequest` on `gateway.cloud.discover` and wait `MOWDECK_CLOUD_DISCOVERY_TIMEOUT_SECONDS`; the Device-Gateway logs in (or answers from the session the account's connected mowers share) and replies with the devices or `login_failed` (422 `linked-account-login-failed`, field `#/password`) or `unavailable` (503). Without the key every linked account route answers 503. `last_sync_error` holds canned messages only; audit rows (`linked_account.created`, `.synced`, `.removed`) carry the linked account id, never the e-mail or the password.

Lockout protection (`fleet.application.throttle`): every create and re-read is a Mammotion login, so at most 5 failed logins per account and lower-case e-mail are allowed within 15 minutes; then 429 `urn:mowdeck:problem:too-many-requests` with `Retry-After`, without asking the vendor. The last wrong password (an HMAC with a per-process key) is refused again at once for 15 minutes, also without asking the vendor. A success clears both. The state is per replica (in memory). E-mail addresses are validated (one `@`, a dotted domain, no blanks or control characters) and compared with `lower()` like the unique index.

Registering a cloud mower: `POST /mowers` with `connection_kind: cloud`, `linked_account_id` and `device_name` (from the device list; unknown 422 `unknown_device`, a base station 422 `not_a_mower`); `name` defaults to the device name, `model` to the listed model; `connection_options` may only carry adapter extras (`refresh_seconds`, `session_cache_path`, timeouts; credentials or device keys are 422). Fleet stores `device_name` and `iot_id` as managed options (`managed: true` in `GET /connection-kinds`; a `PATCH` keeps them) and the columns `linked_account_id` and `iot_id`. `linked_account_id` is the only discriminator: every new cloud mower names a linked account. The bulk route registers several devices of one account, each with its own checks, tuples, audit row and event.

One physical mower, one account: the unique key is the IoT id (a device shared between two Mammotion accounts is still one mower), with an installation-wide unique index. A taken IoT id answers 409 `urn:mowdeck:problem:mower-already-registered`; for another account the detail never names it (`this mower is already registered in MowDeck`), in the own account it names the existing mower. The device list marks it `registered_elsewhere: true` (not addable) next to `registered: true` (this account). Deleting the mower frees the IoT id. Cloud mowers registered before linked accounts have no IoT id and are not covered.

Cloud mowers registered before linked accounts (own `account` with a `password` or `credential_ref`) keep working until they are deleted; their options are still validated with the old schema (`LEGACY_CLOUD_SCHEMA`), but a `PATCH` may not introduce a new plain password (422 `use_linked_account`) or change the IoT id (422), and another kind cannot be changed into `cloud` (422 `use_linked_account`).

**Secrets never travel in registrations.** `mower.registered`, `mower.updated`, `mower.removed` (JetStream `EVENTS`, kept 30 days, read by several services) and `fleet.mowers.list` never carry a `password`, a `noise_psk` or the vendor `account`. When Fleet withholds any of them, or the mower uses a linked account, the options say `credentials: "fleet"` (plus `linked_account_id` and `credentials_rev`, a fingerprint of the stored ciphertext that changes with a new password so the gateway reconnects). The Device-Gateway asks for them on `fleet.mowers.credentials` (`MowerCredentialsRequest{mower_id, account_id}` → `MowerCredentialsReply{found, secret_options}`) each time it opens the connection; Fleet answers for mowers of that account only (linked: e-mail and decrypted password; older mowers: their stored `account`, `password`, `noise_psk`) and logs only the mower id, the outcome and the key names.

### Links

[ADR 0038](../../docs/adr/0038-dual-link-per-mower-and-firmware-updates.md). A mower keeps one `connection_kind` and may carry additional *links* by kind, today `ble_bridge`: a Bluetooth bridge (an ESPHome Bluetooth proxy next to the mower) that adds the low-latency drive path to a cloud mower, while the cloud keeps the camera and everything else. The standalone `ble_bridge` connection kind stays for mowers without a cloud account.

- **Eligibility is a registry entry.** `fleet.domain.connections.SCHEMAS[kind].supports_links` names the link kinds a connection kind may carry (`cloud`: `{"ble_bridge"}`; the simulator and the standalone bridge: none). Fleet never decides by a kind's or a vendor's name: `require_link_supported` answers 409 `urn:mowdeck:problem:link-not-applicable` for any other kind. The options of a link are validated with the schema of the link kind itself (`SCHEMAS["ble_bridge"]`, the same rules as for the standalone kind: `host` and `ble_address` required, `port` 1 to 65535, a MAC pattern); a 422 names each option as `#/links/ble_bridge/<key>`.
- **Storage** (`mower_links`, migration `0010`): one row per mower and kind (`uq_mower_links_mower_kind`), gone with the mower (`ON DELETE CASCADE`; the use cases delete explicitly too, and drop links the new kind cannot carry when the connection kind changes, audited as `mower.link_removed`). `options` (JSONB) holds the public options (`host`, `port`, `ble_address`, `device_name`, `allow_manual_blades`, `manual_blade_height_mm`); `secrets_encrypted` (bytea) a JSON object `{"noise_psk": …}` sealed with the credential vault's AES-256-GCM cipher of the linked accounts (`MOWDECK_CREDENTIAL_ENCRYPTION_KEY`, the link id as associated data; without the key a `PUT` answers 503); `proxy_info` (JSONB) the `ProxyInfo` of the last scan or check (bridge name, ESPHome version, compile time, project and version, Bluetooth address, `outdated` with a reason), `NULL` until one answered.
- **Never the key.** Responses show `noise_psk` as `***` (sending `***` back keeps the stored key); audit rows (`mower.link_set` with the public options and `created`, `mower.link_removed`) and logs never carry it. The registration events and `fleet.mowers.list` carry `links: {"ble_bridge": {public options, credentials: "fleet", credentials_rev}}`; `credentials_rev` is a fingerprint of the sealed secrets, so the Device-Gateway reconnects the link only when the key changed (an unchanged key is not sealed again). The gateway gets the key with the connection secrets: `fleet.mowers.credentials` answers `link_secrets: {"ble_bridge": {"noise_psk": …}}` next to `secret_options`; a link whose secrets no longer decrypt (a lost key) is logged and left out, the connection itself still works.
- **Scan and check.** `POST /mowers/{id}/links/ble_bridge/scan` sends a `BridgeScanRequest{mower_id, account_id, host, port, noise_psk, expected_name, timeout_s: 30}` on `gateway.mower.<id>.bridge.scan` (core NATS, internal) and waits `MOWDECK_BRIDGE_SCAN_REQUEST_TIMEOUT_SECONDS` (35 s); `expected_name` is the link's or the cloud mower's `device_name`. The gateway that holds the mower connects to the bridge, reads its information and collects mower advertisements until the expected name shows up or 30 s pass. The reply goes back as a 200 `BridgeScanReply`: `proxy` as soon as the bridge answered, `devices` seen so far, and `error` for the outcomes the user must act on (`unreachable`, `auth_failed`, `timeout`, `busy`), so a timeout still shows the bridge and the other mowers seen; the relay outcomes are problems: `unsupported` 409 `urn:mowdeck:problem:bridge-scan-unsupported`, `not_found` (no gateway holds the mower) and no answer 503, `forbidden` 404. When the mower already has a link to the same bridge (host and port) its `proxy_info` is refreshed; `POST …/check` scans with the stored host, port and key and always refreshes it. Both answers carry `Cache-Control: no-store`.
- **Set and delete** publish `mower.updated` with the full registration (links included or, after a delete, without), so the Device-Gateway opens or closes the link; every `MowerOut` carries `links: {kind: LinkOut}` with the masked options, `proxy_info` and timestamps. The live link state (`connected`, RSSI, bridge firmware) is telemetry (`MowerState.links[kind]`), not Fleet.

### Firmware check

`POST /mowers/{id}/firmware/check` (`can_view`, hidden otherwise; ADR 0038) sends a `FirmwareCheckRequest{mower_id, account_id}` on `gateway.mower.<id>.firmware.check` (core NATS, internal) and waits `MOWDECK_FIRMWARE_CHECK_REQUEST_TIMEOUT_SECONDS` (30 s). The gateway that holds the mower asks the vendor through the mower's connection and answers a `FirmwareCheckReply` with the fresh `FirmwareInfo` (installed and available version, release notes, `update_state`, progress, `checked_at`), which also arrives with telemetry as `MowerState.firmware_info`; errors: `unsupported` (no cloud link) 409 `urn:mowdeck:problem:firmware-check-unsupported`, `unavailable` (offline, vendor silent) and `not_found` 503, `forbidden` 404, no answer 503. Nothing is stored or audited. Installing is the command `firmware_update {version}` (`can_manage_mowers`, plan feature `control`); the gateway applies the safety rules (docked with at least 70 % battery, no job or drive session, the offered version).

### Camera

`POST /mowers/{id}/camera/session` ([ADR 0032](../../docs/adr/0032-live-camera-over-agora.md)): after `can_view` on the mower (hide) and the plan feature `camera` (403 `feature-not-in-plan`), Fleet loads the mower with the account filter and sends a `CameraSessionRequest{mower_id, account_id, requested_by}` on `gateway.mower.<id>.camera.session` (core NATS, internal), waiting `MOWDECK_CAMERA_REQUEST_TIMEOUT_SECONDS`. The gateway that holds the mower answers a `CameraSessionReply` with the session (`provider` `agora` for Mammotion cloud mowers, `fake` for the simulator) or an error: `unsupported` becomes 409 `urn:mowdeck:problem:camera-unsupported`, `unavailable` and `not_found` (no gateway holds the mower right now) 503, `forbidden` 404; no answer is 503 `unavailable`. An issued session appends the audit row `mower.camera_opened` (actor, time, provider, expiry, number of cameras; never the token). The response is the session with its secret token and `Cache-Control: no-store`; Fleet stores nothing else.

### Path preview

`POST /mowers/{id}/path-preview` ("Pfad berechnen" in the web app's start sheet): after `can_control` on the mower (hide; planning is part of starting) and the plan feature `control` (403 `feature-not-in-plan`), Fleet loads the mower with the account filter and sends `PathPreviewGatewayRequest{mower_id, account_id, area_ids, pattern, requested_by}` on `gateway.mower.<id>.path.preview` (core NATS, internal), waiting `MOWDECK_PATH_PREVIEW_REQUEST_TIMEOUT_SECONDS` (85 s; the gateway's own limit is 75 s, the web app's 90 s). The gateway that holds the mower lets it plan the route without `start_job` and answers `PathPreviewReply`: the `MowPath` (polylines in the map's local metres, `kinds`, `points_total`, `path_hash`; no progress), or an error: `unsupported` is 409 `urn:mowdeck:problem:path-preview-unsupported`, `rejected` (a job runs, unknown areas, a pattern the mower cannot run) 409 `path-preview-rejected` with the reason as `detail`, `timeout` and no answer in time 504 `gateway-timeout`, `unavailable`, `not_found` (no gateway holds the mower) and `failed` 503, `forbidden` 404. Nothing is stored or audited (the mower only plans); the start afterwards sends the same areas and pattern and the mower plans the same route again. Planning takes a few seconds, over 4G up to about a minute.

Why `can_view`: watching changes nothing on the mower and the people who see its position and map may see its surroundings; an installation switches the camera off per plan (feature `camera`) or entirely (the web app's `MOWDECK_WEB_CAMERA_ENABLED` keeps the vendor video out of the CSP).

### Plans

Fleet enforces the account's plan with one call per enforcement point through the Entitlements client of the kit ([ADR 0028](../../docs/adr/0028-entitlements-plans-and-enforcement.md)):

| Action | Check | Refusal |
| --- | --- | --- |
| register a mower | limit `max_mowers` (current: the account's mowers) | 409 `urn:mowdeck:problem:plan-limit`, `errors`: `{"parameter": "max_mowers", "code": "limit", "detail": "4"}` and `{"parameter": "max_mowers", "code": "current", "detail": "4"}` |
| create a site | limit `max_sites` (the hidden default site counts) | 409 `plan-limit` |
| create a group | limit `max_groups` | 409 `plan-limit` |
| any command except `emergency_stop` (so also `firmware_update`) | feature `control` | 403 `urn:mowdeck:problem:feature-not-in-plan`; `fleet.commands.issue` answers `not_in_plan` |
| bulk command except `emergency_stop` | features `control` and `fleet.bulk_commands` | 403 `feature-not-in-plan` |
| `start_mow` with a `spot_plan` (a Planner route, [ADR 0030](../../docs/adr/0030-mowing-patterns-and-planner.md)) and `spot_mow` | features `control` and `spot_mowing` | 403 `feature-not-in-plan` |
| live camera session | feature `camera` | 403 `feature-not-in-plan` |
| path preview | feature `control` | 403 `feature-not-in-plan` |
| `set_light` (the mower's light, switched in the camera panel and by AI assist for dark photos) | features `control` and `camera` (also in a bulk command) | 403 `feature-not-in-plan` |

The emergency stop and viewing are never gated: an account in `grace` or `expired` can always stop its mowers, and no plan change ever sends a command. While billing is disabled (the default) every account has the plan `unlimited`. `MOWDECK_MAX_MOWERS_PER_ACCOUNT` stays as an installation ceiling below any plan.

### Module descriptor

Permissions `mower.view`, `mower.control`, `mower.drive`, `fleet.manage`, `fleet.bulk_command` (all with a relation in the authorization model, so grantable on account, site, group and mower; `fleet.bulk_command` not on a mower); plan features `camera` and `fleet.bulk_commands`; events `mower.registered`, `mower.updated`, `mower.removed`, `fleet.mower.area_names_changed`; UI area `/mowers`.
Permissions `mower.view`, `mower.control`, `mower.drive`, `fleet.manage`, `fleet.bulk_command` (all with a relation in the authorization model, so grantable on account, site, group and mower; `fleet.bulk_command` not on a mower); events `mower.registered`, `mower.updated`, `mower.removed`; UI areas `/mowers` and `/fleet` (the web app's fleet map, sites and groups), both for `mower.view`.

## Events and subjects

| Direction | Subject | Kind | Payload | Notes |
| --- | --- | --- | --- | --- |
| produced | `event.mower.registered`, `event.mower.updated`, `event.mower.removed` | JetStream `EVENTS` | `EventEnvelope[MowerRegistration]` | after the commit; `updated` when name, connection, site, groups or links change; `links` carry the public options of every additional link |
| produced | `event.fleet.mower.area_names_changed` | JetStream `EVENTS` | `EventEnvelope[AreaNamesChanged]` | after a committed change of a custom area name; all names of the mower |
| produced | `command.mower.<id>` | JetStream `COMMANDS` | `CommandEnvelope` | msg id = command id, header `Mowdeck-Priority` |
| reply | `fleet.mowers.list` | core NATS, queue `fleet` | `MowerListRequest` / `MowerListReply` | mowers with `mower_id.int % shard_count == shard_index`, all accounts (system path), without secrets |
| reply | `fleet.mowers.credentials` | core NATS, queue `fleet` (internal) | `MowerCredentialsRequest` / `MowerCredentialsReply` | the withheld secrets of one mower of the given account (`secret_options`) and of its links (`link_secrets`), for the Device-Gateway's connect (never logged) |
| reply | `fleet.commands.issue` | core NATS, queue `fleet` (internal) | `IssueCommandRequest` / `IssueCommandReply` | a command on behalf of a subject, with the checks of the HTTP endpoint (above) |
| reply | `fleet.drive.start` | core NATS, queue `fleet` (internal) | `DriveStartRequest` / `DriveStartReply` | opens a manual drive session (Realtime) |
| reply | `fleet.drive.stop` | core NATS, queue `fleet` (internal) | `DriveStopRequest` / `DriveStopReply` | closes it |
| reply | `fleet.targets.resolve` | core NATS, queue `fleet` (internal) | `ResolveTargetRequest` / `ResolveTargetReply` | the mowers of a mower or group target of one account with the time zone and coordinates of their sites and their saved mowing defaults (`mow_pattern`); `timezone` is the target's (a cross-site group: the default site) |
| request | `gateway.mower.<id>.camera.session` | core NATS (internal) | `CameraSessionRequest` / `CameraSessionReply` | a live camera session from the Device-Gateway that holds the mower |
| request | `gateway.cloud.discover` | core NATS (internal; any gateway instance answers) | `CloudDiscoverRequest` / `CloudDiscoverReply` | log in to a linked Mammotion account and list its devices (create and sync of linked accounts) |
| request | `gateway.mower.<id>.bridge.scan` | core NATS (internal) | `BridgeScanRequest` / `BridgeScanReply` | look for the mower through a Bluetooth bridge (scan and check of a link, ADR 0038) |
| request | `gateway.mower.<id>.firmware.check` | core NATS (internal) | `FirmwareCheckRequest` / `FirmwareCheckReply` | ask the vendor for firmware updates (ADR 0038) |
| consumed | `command.result.mower.*` | JetStream, durable and deliver group `fleet-command-results` | `CommandResult` | updates the records |
| consumed | `event.identity.account.created` | JetStream, durable and deliver group `fleet-accounts` | `EventEnvelope[AccountCreated]` | creates the default site (redelivered while OpenFGA is down) |
| consumed | `event.identity.grants.changed` | JetStream, every instance | `EventEnvelope[GrantsChanged]` | drops cached decisions (`mowdeck_authz.events`) |
| request | `entitlements.check` | core NATS (internal) | `EntitlementCheckRequest` / `EntitlementCheckReply` | plan limits and features (cached per account) |
| consumed | `event.entitlements.plan.changed` | JetStream, every instance | `EventEnvelope[PlanChanged]` | drops the cached plan snapshots of the Entitlements client |

`fleet.drive.start` also serves the Vision service (ADR 0033): it drives an approved AI assist maneuver on behalf of the approving user with `origin: vision`, which the `drive_sessions` row and `GET /mowers/{id}/drive-sessions` show (migration `0005`). `fleet.commands.issue` accepts the origin `vision` (the `return_to_dock` after a successful Find Dock).

## Consistency with OpenFGA

The database is the source of truth. Every change runs in one database transaction; right before the commit Fleet writes the tuple change. If OpenFGA cannot be written the transaction rolls back and the request answers **503**: nothing is half applied and no event is published. If the commit fails after the write, the tuple change is undone (best effort, logged). Events follow the commit, so the Device-Gateway only connects a mower whose tuples exist. This is Identity's write-through pattern instead of the design's outbox (ADR 0016).

## Settings

All settings of `mowdeck-service-kit` apply (`MOWDECK_*`, any value also from `MOWDECK_<NAME>_FILE`). OpenFGA: `MOWDECK_OPENFGA_*` of `mowdeck-authz`; token verification: `MOWDECK_AUTH_*` of `mowdeck-auth` (`MOWDECK_AUTH_JWKS_URL` unset means every request is 401). Service specific:

| Variable | Default | Description |
| --- | --- | --- |
| `MOWDECK_SERVICE_NAME` | `fleet` | Service name in logs, metrics and traces |
| `MOWDECK_DATABASE_URL` | required | PostgreSQL of the `fleet` database |
| `MOWDECK_COMMAND_TTL_SECONDS` | `30` | Default expiry of a command |
| `MOWDECK_COMMAND_TTL_MAX_SECONDS` | `300` | Upper bound for `ttl_seconds` in a request |
| `MOWDECK_EMERGENCY_STOP_TTL_SECONDS` | `10` | Expiry of an emergency stop |
| `MOWDECK_MANUAL_DRIVE_TTL_SECONDS` | `2` | Expiry of a manual drive frame |
| `MOWDECK_COMMAND_RETENTION_DAYS` | `90` | Command records older than this are deleted |
| `MOWDECK_COMMAND_RETENTION_INTERVAL_SECONDS` | `3600` | Interval of the retention job (advisory lock, one replica at a time) |
| `MOWDECK_MAX_MOWERS_PER_ACCOUNT` | `50` | Installation ceiling below any plan (409 above); the plan's `max_mowers` answers 409 `plan-limit` |
| `MOWDECK_ENTITLEMENTS_BILLING_ENABLED` | `false` | Kit setting: the installation switch of the plans; only decides while Entitlements does not answer (off: allowed, on: refused; ADR 0028) |
| `MOWDECK_ENTITLEMENTS_CACHE_SECONDS` | `60` | Kit setting: how long an account's plan snapshot is reused |
| `MOWDECK_ALLOWED_CONNECTION_KINDS` | `simulated` | Comma separated kinds accepted at registration (`simulated`, `cloud`, `ble_bridge`; Compose: `FLEET_ALLOWED_CONNECTION_KINDS`) |
| `MOWDECK_CONNECTION_OPTIONS_MAX_BYTES` | `4096` | Maximum JSON size of `connection_options` |
| `MOWDECK_BULK_MAX_MOWERS` | `200` | Maximum mowers per bulk command |
| `MOWDECK_SPOT_PLAN_MAX_BYTES` | `262144` | Largest spot plan of a `start_mow` as JSON (422 above) |
| `MOWDECK_DEFAULT_TIMEZONE` | `Europe/Berlin` | Time zone of new default sites |
| `MOWDECK_CAMERA_REQUEST_TIMEOUT_SECONDS` | `15` | How long Fleet waits for the Device-Gateway's camera session (503 after) |
| `MOWDECK_PATH_PREVIEW_REQUEST_TIMEOUT_SECONDS` | `85` | How long Fleet waits for a route the mower plans for a preview (504 after); above the gateway's `MOWDECK_PATH_PREVIEW_TIMEOUT_SECONDS` (75) and below the web app's 90 s |
| `MOWDECK_CREDENTIAL_ENCRYPTION_KEY` | unset | 256-bit key (base64 or hex; or `_FILE`) that encrypts the passwords of linked accounts. Unset: linked accounts answer 503 and old cloud mowers keep their inline credentials. Keep a backup: without it the stored passwords cannot be read (Compose: `FLEET_CREDENTIAL_ENCRYPTION_KEY`) |
| `MOWDECK_CREDENTIAL_ENCRYPTION_KEY_PREVIOUS` | unset | Earlier keys (comma separated) that still decrypt during a rotation (Compose: `FLEET_CREDENTIAL_ENCRYPTION_KEY_PREVIOUS`) |
| `MOWDECK_CLOUD_DISCOVERY_TIMEOUT_SECONDS` | `50` | How long Fleet waits for the Device-Gateway's login and device list (503 after; the gateway's own limit is 40 s) |
| `MOWDECK_BRIDGE_SCAN_REQUEST_TIMEOUT_SECONDS` | `35` | How long Fleet waits for the Device-Gateway's bridge scan (503 after); the scan itself looks for the mower for up to 30 s |
| `MOWDECK_FIRMWARE_CHECK_REQUEST_TIMEOUT_SECONDS` | `30` | How long Fleet waits for the Device-Gateway's firmware check (503 after) |

### Connection options

`connection_options` go unchanged to the Device-Gateway's adapter (`fleet.domain.rules.validate_connection_options`):

| Kind | Required | Optional | Secret keys allowed |
| --- | --- | --- | --- |
| `simulated` | none | `SimulationConfig` fields such as `time_scale` | none |
| `cloud` (linked account) | none from the client: `device_name` and `iot_id` are set by Fleet from the device list | `session_cache_path`, `refresh_seconds` (10 to 300, default 20: how often the mower is asked for a fresh report), timeouts | none (the linked account holds the credentials) |
| `cloud` (registered before linked accounts) | `account`, `device_name` or `iot_id`, `credential_ref` or a stored `password` (no new plain password, no new IoT id) | as above | `credential_ref`, `password` |
| `ble_bridge` | `host`, `ble_address` (MAC), `noise_psk` or `credential_ref` | `port` (6053), `device_name`, `allow_manual_blades`, timeouts | `noise_psk`, `credential_ref` |

The same `ble_bridge` schema validates the options of a Bluetooth bridge *link* of a cloud mower (`PUT /mowers/{id}/links/ble_bridge`, [Links](#links)); there the key is always `noise_psk`, stored encrypted. `GET /connection-kinds` does not list `supports_links` yet; the web app offers the bridge link for the kinds the registry names (`cloud`).

The rules are data: `fleet.domain.connections.SCHEMAS` describes each kind's options (`key`, `type` string, integer, number or boolean, `required`, `secret`, `group` for alternatives of which one is needed, `advanced`, `pattern` with a readable `format_hint`, `minimum`, `maximum`, `default`, `examples`, English `title` and `description`; `extra_options` when keys outside the schema are accepted, as for the simulator's `SimulationConfig`). `validate_connection_options` checks against it, and `GET /api/v1/fleet/connection-kinds` returns it for the allowed kinds (a kind without a schema is listed with no options and `extra_options: true`), so the web app generates its forms from it and a new adapter kind needs only a schema here. A 422 names every broken rule in `errors[]` with `pointer` `#/connection_options/<key>` and `code` `required`, `one_of` (on the first option of the group), `type`, `range` or `pattern`; a kind that is not enabled is `#/connection_kind` with `not_allowed`.

Any other key that looks like a secret (`pass`, `secret`, `token`, `key`, `credential`, `psk`, at any depth) is refused with 422. In production store a reference, `credential_ref` (`env:NAME` or `file:/run/secrets/name`), which the Device-Gateway resolves in its own environment; a plain `password` or `noise_psk` is accepted in Phase 2 with a warning in the log (TODO: a secret store in Phase 3, [ADR 0019](../../docs/adr/0019-mammotion-adapter-transports.md)). Responses never echo `password` or `noise_psk`: they show `***`, and a `PATCH` that sends `***` back keeps the stored value. The registration events and `fleet.mowers.list` never carry them; the Device-Gateway fetches them on `fleet.mowers.credentials` (see [Linked accounts](#linked-accounts)).

## Data model

Tables (Alembic revisions `0002` and the additive `0003`, `0004` and `0008`): `sites` (partial unique index: one default site per account; named sites unique per account ignoring case; nullable `latitude` and `longitude`, both or none, check constraint `ck_sites_coordinates`), `groups` (unique name per account), `mowers` (serial unique per account), `mower_groups`, `commands` (no foreign key: records outlive their mower; nullable `origin` and `schedule_id` for issued commands), `audit_log` (append-only: a trigger rejects `UPDATE` and `DELETE`), `drive_sessions` (`0004`: id, mower, account, subject, start, expiry, blade flag, connection, end, reason, frame counts; no foreign key, like commands; partial index on open sessions), `linked_accounts` (`0008`: id, account, provider, e-mail (unique per account and provider, ignoring case), `password_encrypted` (bytea), `devices` (JSONB), creator, timestamps, `last_synced_at`, `last_sync_error`); `mowers` got `linked_account_id` (foreign key to `linked_accounts`, `RESTRICT`) and `iot_id` (unique across all accounts: one physical mower in one MowDeck account) in `0008`. Every table carries `account_id`.

Migration numbering: `0008` follows `0007` (area names); `0009` (additive: nullable `mowers.mow_defaults` JSONB, the mowing defaults) follows `0008`; `0010` (additive: the table `mower_links`, [Links](#links)) follows `0009`.

## Layers

| Package | Content |
| --- | --- |
| `domain/` | entities (including `MowerLink`), rules (relation and plan features per command kind, expiry, status rank, shard rule, connection and link option validation, time zone), the option schemas per connection kind with `supports_links` (`connections.py`), errors (with field problems) |
| `application/` | use cases (`sites`, `groups`, `mowers`, `commands`, `drive`, `camera`, `linked_accounts` with the `CredentialVault`, `links` for the bridge links and the firmware check), unit of work with the in-transaction tuple write, hierarchy tuples, object checks (`access`), ports |
| `adapters/api/` | routes, schemas, dependencies (object gates, error mapping) |
| `adapters/bus/` | event and command publishers, subscriptions, the camera, cloud discovery, bridge scan and firmware check requests |
| `adapters/crypto.py` | AES-256-GCM of linked account passwords and link secrets |
| `adapters/db/` | tables, the PostgreSQL store, Alembic migrations |
| `adapters/memory.py` | the memory store (tests) |
| `main.py` | wiring (`build_app`), retention job, `main()` |

## Local start

With the Compose stack (builds the image, runs the migrations, waits for health):

```bash
cp infra/compose/.env.example infra/compose/.env
docker compose -f infra/compose/docker-compose.yml up -d --build --wait
curl -X POST http://localhost/api/v1/identity/register -H 'Content-Type: application/json' \
  -d '{"username": "lukas", "password": "correct horse battery", "account_name": "Home"}'
TOKEN=$(curl -s -X POST http://localhost/api/v1/identity/session -H 'Content-Type: application/json' \
  -d '{"username": "lukas", "password": "correct horse battery"}' | python -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
curl -X POST http://localhost/api/v1/fleet/mowers -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"name": "Front lawn", "connection_kind": "simulated"}'
curl http://localhost/api/v1/device-gateway/mowers -H "Authorization: Bearer $TOKEN"   # connected
curl -X POST http://localhost/api/v1/fleet/mowers/<mower_id>/commands -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"command": {"kind": "start_mow", "area_ids": ["front"]}}'
curl http://localhost/api/v1/fleet/commands/<command_id> -H "Authorization: Bearer $TOKEN"
```

The simulator's default map has the areas `front` and `back` (`libs/mowdeck-control/README.md`).

Container image (build context is the repository root):

```bash
docker build -f services/fleet/Dockerfile -t mowdeck/fleet:dev .
```

## Tests

```bash
uv run pytest services/fleet --cov=fleet
```

The tests use the memory store, the in-memory OpenFGA fake (it evaluates the packaged model), the in-memory bus and access tokens minted with `mowdeck_auth.testing` (`tests/fleet_support.py` has the harness and a fake Device-Gateway). PostgreSQL tests (migrations up and down, schema drift, the store contract, the advisory lock, a full flow) run with `MOWDECK_TEST_DATABASE_URL`; the OpenFGA integration test (the owner sees the mower, a foreign owner does not) with `MOWDECK_TEST_OPENFGA_URL`; the JetStream round trip with `MOWDECK_TEST_NATS_URL`:

```bash
docker run -d --name fleet-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=test public.ecr.aws/docker/library/postgres:17.11-alpine
docker run -d --name fleet-test-fga -p 18080:8080 ghcr.io/openfga/openfga:v1.22.0 run \
  --datastore-engine memory --authn-method preshared --authn-preshared-keys test-key
docker run -d --name fleet-test-nats -p 4222:4222 public.ecr.aws/docker/library/nats:2.15.0-alpine -js
MOWDECK_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres \
MOWDECK_TEST_OPENFGA_URL=http://127.0.0.1:18080 MOWDECK_TEST_OPENFGA_TOKEN=test-key \
MOWDECK_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest services/fleet
docker rm -f fleet-test-pg fleet-test-fga fleet-test-nats
```

## Upgrade notes

**Linked accounts (migration `0008`, [ADR 0034](../../docs/adr/0034-linked-cloud-accounts-and-device-discovery.md)).** Set `MOWDECK_CREDENTIAL_ENCRYPTION_KEY` (Compose: `FLEET_CREDENTIAL_ENCRYPTION_KEY` in `.env`; Helm: `secrets.FLEET_CREDENTIAL_ENCRYPTION_KEY`, or the keys `fleet-credential-encryption-key` and `fleet-credential-encryption-key-previous` of an `existingSecret`) and keep a backup of it; in production Fleet refuses the development value of `.env.example`. `fleet-migrate` adds the table and the columns. Cloud mowers registered before linked accounts keep working with their stored credentials until they are deleted (the Device-Gateway now fetches them on `fleet.mowers.credentials`); to use the device list, delete them and add them again from a linked account. Their passwords and the ESPHome keys of BLE bridges were part of every registration event until this version and may still be in the `EVENTS` stream (30 days) and in logs: change those passwords and keys after the upgrade, or purge the stream.

## Limits (Phase 1)

- No `link_status` column: the connection state is shown by the Device-Gateway (`GET /api/v1/device-gateway/mowers`) and later by Telemetry.
- Account deletion does not purge Fleet data yet (Identity publishes no deletion event in Phase 1).
- No reconcile job: if both a commit and the undo of its tuple change fail, OpenFGA can keep a tuple for a missing object (harmless, every handler loads the row with the account filter).
- Other services see hierarchy changes after at most their decision cache TTL (5 s).
- A failed event publish after a committed change is logged, not retried; the Device-Gateway catches up on its next start through `fleet.mowers.list`.
