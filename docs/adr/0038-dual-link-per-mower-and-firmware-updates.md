# ADR 0038: Dual link per mower (Bluetooth bridge next to the cloud) and firmware updates

## Status

Proposed (2026-10-14)

## Context

A mower connects to MowDeck through one `connection_kind`: the manufacturer's cloud (everything the vendor app can do, including the camera, over 4G from anywhere) or a Bluetooth bridge (an ESPHome Bluetooth proxy next to the mower: low latency, the only path on which the mower accepts joystick frames, no camera). Until now an installation had to choose one per mower, and the owner who wanted both the camera and manual driving had to register the same mower twice.

The forces:

- **Manual driving needs the bridge, everything else prefers the cloud.** The two paths are complementary, not alternatives: the bridge adds a drive channel to a cloud mower; it does not replace the cloud.
- **Secrets never travel in registrations** ([ADR 0034](0034-linked-cloud-accounts-and-device-discovery.md)): the bridge's API encryption key is a secret like a linked account's password and must be stored encrypted and handed to the Device-Gateway only on request.
- **Firmware updates are a vendor cloud function.** The vendor offers new versions through its cloud, and the mower installs them itself; MowDeck can only ask, show, and trigger, under safety rules (docked and charging, enough battery, no job, no drive session).
- **Manufacturer independence** (the architecture rules of this work): services and the web app know only the `mowdeck-control` interface and the contracts; a manufacturer is an adapter package behind it. Nothing in Fleet, the gateway's routing or the web may branch on a vendor name. A Bluetooth bridge is not manufacturer-specific either: it is an ESPHome proxy that forwards BLE.
- **Hexagonal layout and additive contracts**: the contracts gained `MowerRegistration.links`, `MowerCredentialsReply.link_secrets`, `LinkStatus`, `ProxyInfo`, `BridgeScanRequest/Reply`, `FirmwareInfo`, `FirmwareCheckRequest/Reply`, the command `firmware_update` and the activity `updating`, all additive; each service implements its own side behind its ports.

## Decision

**A mower keeps one connection and may carry additional *links* by kind, today `ble_bridge`.** Fleet stores the links and publishes them with the registration; the Device-Gateway opens them next to the connection and reports their state; the web app adds and checks them from the mower's settings. Firmware is checked through the connection and installed with a command. The sections below are owned by the service that implements them.

### Fleet

- **Data model.** `mower_links` (migration `0010`): `id`, `mower_id` (foreign key, `ON DELETE CASCADE`), `account_id`, `kind`, `options` (JSONB: the public options `host`, `port`, `ble_address`, `device_name`, `allow_manual_blades`, `manual_blade_height_mm`), `secrets_encrypted` (bytea: a JSON object `{"noise_psk": …}` sealed with the credential vault's AES-256-GCM cipher of ADR 0034, the link id as associated data; extensible to further secrets such as Wi-Fi or OTA passwords), `proxy_info` (JSONB: the `ProxyInfo` of the last scan, `NULL` until one answered), `created_by`, timestamps; unique `(mower_id, kind)`. Domain entity `MowerLink`, `Table[MowerLink]` on the transaction, in the PostgreSQL and the memory store.
- **Eligibility by the connection-kind registry, not by name.** `KindSchema` (the registry `fleet.domain.connections.SCHEMAS` that also drives `GET /connection-kinds`) gains `supports_links: frozenset[str]`; the `cloud` schema declares `{"ble_bridge"}`, the simulator and the standalone bridge kind declare nothing. `require_link_supported(connection_kind, link_kind)` answers 409 `link-not-applicable` otherwise. A new connection kind that can carry a bridge needs one registry entry and no code elsewhere. The options of a link are validated with the schema of the link kind itself (`SCHEMAS["ble_bridge"]`, `option_problems`), so the bridge options have one definition for the standalone kind and for the link; a 422 names each option as `#/links/ble_bridge/<key>`.
- **API** (`/api/v1/fleet/mowers/{id}`, `can_manage_mowers` unless noted): `POST …/links/ble_bridge/scan {host, port, noise_psk | "***"}` relays a `BridgeScanRequest` to the gateway that holds the mower (core NATS `gateway.mower.<id>.bridge.scan`, Fleet waits 35 s, the scan itself up to 30 s) and answers the gateway's `BridgeScanReply` as a 200: the scan outcomes the user must act on (`unreachable`, `auth_failed`, `timeout`, `busy`) stay inside the reply together with the proxy information and the devices seen, so a timeout still shows the bridge and the other mowers; the relay outcomes become problems (`unsupported` 409 `bridge-scan-unsupported`, `not_found` 503, `forbidden` 404, no answer 503). `PUT …/links/ble_bridge` validates, encrypts, upserts, audits `mower.link_set` (never the key) and publishes `mower.updated` with the full registration; `GET` answers the options with the key masked (`***`, which a `PUT` may send back to keep it) and the cached `proxy_info`; `DELETE` removes the link, audits `mower.link_removed` and publishes `mower.updated` without it; `POST …/links/ble_bridge/check` scans again with the stored host, port and key and refreshes `proxy_info`. `POST …/firmware/check` (`can_view`) relays a `FirmwareCheckRequest` and answers the fresh `FirmwareInfo` (`unsupported` 409 `firmware-check-unsupported`, `unavailable` and `not_found` 503, `forbidden` 404). Every `MowerOut` carries `links: {kind: LinkOut}`.
- **Registration and secrets.** `public_registration` adds `links: {kind: public options + credentials: "fleet" + credentials_rev}`; `credentials_rev` is a fingerprint of the sealed secrets, so the gateway reconnects the link only when the key changed (an unchanged key is not sealed again: AES-GCM would otherwise change the ciphertext on every save). The credentials reply (`fleet.mowers.credentials`) adds `link_secrets: {kind: {noise_psk: …}}`. Links are deleted with the mower and dropped (audited) when the connection kind changes to one that cannot carry them.
- **Command gates.** Relations: `firmware_update` needs `can_manage_mowers` (it changes the device like its settings), `spot_mow` `can_drive`, `spot_stop` `can_control`. Plan features (`command_features` in `fleet.domain.rules`): `control` for every kind but the emergency stop, so also for `firmware_update`; `spot_mowing` as well for `spot_mow` and for a `start_mow` with a spot plan. A `spot_mow` carries a Planner route for one mower: refused in bulk commands (422), limited to `MOWDECK_SPOT_PLAN_MAX_BYTES`, and recorded with its `plan_hash` and a summary instead of the route, exactly like `start_mow.spot_plan`.

### Device-Gateway and adapters

(To be written by the Device-Gateway implementation: the dual connection, the bridge watcher and link state, the scan responder, the firmware check and update with its safety rules, the simulator's fake links and firmware.)

### Web

(To be written by the web implementation: the Bluetooth bridge panel and sheet in the mower settings, the firmware panel, the drive page gate.)

### Manufacturer independence

What is generic and what is adapter work:

- **Generic (Fleet, contracts, `mowdeck-control`):** the notion of a link kind and its options schema; the storage, encryption and publication of links; the eligibility registry per connection kind; the relay of scans and firmware checks over neutral request/reply contracts (`BridgeScanRequest/Reply` with `ProxyInfo` and `ScannedDevice`, `FirmwareCheckRequest/Reply` with `FirmwareInfo`); the command gates; the safety rules of a firmware update in the control library's base; the `LinkStatus` the gateway reports. Fleet never imports an adapter package and never asks "is this a Mammotion mower": it asks the registry whether the kind supports the link, and the gateway whether the connection's capabilities (`links_supported`, `firmware_update`) allow the request.
- **Adapter work (`mowdeck-control-mammotion` today):** attaching the bridge's BLE transport to the vendor client, reading the vendor's firmware state and starting its OTA, mapping the vendor's update progress to `FirmwareInfo`. The ESPHome scan implementation sits in the adapter's `esphome.py` because the adapter owns the Bluetooth transport, but its result is the neutral `ProxyInfo` and `ScannedDevice`.
- **A second manufacturer's adapter** would implement, in its own package: `capabilities.links_supported` (which link kinds it can attach, if any) and the attach/detach of a bridge transport; `check_firmware()` and `start_firmware_update(version)` against its own cloud; nothing in Fleet, Telemetry, Notifications, Realtime or the web app changes. If its mowers connect through a new connection kind, Fleet needs one `KindSchema` with `supports_links`.
- **Naming** stays neutral in contracts, API and UI: "Bluetooth bridge", "firmware", "link". Where a vendor detail is unavoidable (the key is ESPHome's `api.encryption.key`), it is in the help text of the field, not in a model name.

## Consequences

- One mower, one registration, two paths: the camera and the vendor's functions over the cloud, manual driving (and later spot mowing) over the bridge. The standalone `ble_bridge` connection kind stays for mowers without a cloud account.
- The bridge key is stored and handled like a linked account's password; the gateway sees it only when it opens the link. Installations without `MOWDECK_CREDENTIAL_ENCRYPTION_KEY` cannot add links (503), as they cannot link accounts.
- A link is a registry decision: adding a link kind to a connection kind is a data change in Fleet; adding a new link kind needs a schema and, in the gateway, an adapter that can open it.
- Fleet tolerates a gateway without the scan or firmware responders (503 "the device gateway did not answer"); the gateway tolerates registrations without `links`. The two can be deployed in either order.
- Follow-ups: the bridge firmware itself (an OTA of the ESPHome template through MowDeck) stays documented as a later step; the gateway and web sections of this ADR are filled by their implementations.
