# ADR 0038: Dual link per mower and firmware updates

## Status

Accepted (2026-10-15)

## Context

A cloud mower has everything but a usable manual drive path: the Mammotion cloud carries state, map, job commands, the camera and the vendor's firmware service, while joystick driving only works over Bluetooth (ADR 0019, ADR 0026). Until now a mower was registered with exactly one connection kind, so an owner had to choose between the camera (`cloud`) and driving (`ble_bridge`). Firmware updates were only possible in the vendor app, although the cloud exposes a version check and an OTA request.

The architecture rule stays: services and the web app know only `mowdeck-control` and the contracts; a manufacturer is an adapter package behind them.

## Decision

A mower keeps one connection kind and may carry additional **links** by kind (today `ble_bridge`, a Bluetooth bridge next to the mower: an ESPHome Bluetooth proxy, not manufacturer specific). Fleet stores the links with their secrets, the Device-Gateway opens them, the adapter decides what the extra link carries. Firmware is a neutral capability of a connection with the safety rules in the control library. The three parts:

## Fleet

Fleet owns the links (`mower_links` with AES-GCM encrypted secrets and the cached `proxy_info`), publishes their public options in `MowerRegistration.links` with the secrets withheld (`credentials: "fleet"`, `credentials_rev`) and hands the secrets to the Device-Gateway with the connection credentials (`MowerCredentialsReply.link_secrets`). Whether a mower may get a link kind comes from the connection-kind registry (`supports_links`), never from `kind == "cloud"`. The endpoints below `/api/v1/fleet/mowers/{id}/links/ble_bridge` (scan, set, get, delete, check) and `/firmware/check` relay to the gateway that holds the mower (`gateway.mower.<id>.bridge.scan`, `gateway.mower.<id>.firmware.check`); the command `firmware_update` needs `can_manage_mowers` and the feature `control`. Details live in the Fleet README and the Fleet part of this ADR's implementation.

## Device-Gateway and adapters

**Generic (`libs/mowdeck-control`, `libs/mowdeck-contracts`).** `MowerConnection` gains optional capabilities, each a protocol checked with `isinstance` and declared in the capabilities, like the camera and the path preview before: `capabilities.links_supported` (the extra link kinds a connection can open) with `LinkStatus` per kind in `MowerState.links` and `MowerState.connection` naming the link in use; `FirmwarePort` (`check_firmware() -> FirmwareInfo | None`, `start_firmware_update(version)`) with the flag `capabilities.firmware_update` and `FirmwareInfo` in `MowerState.firmware_info`; `BridgeScanCapable` (`scan_bridge(host, port, secret, expected_name, timeout_s)`) with the neutral reply (`ProxyInfo` of the bridge, `ScannedDevice` list, an error code). The safety rules of a firmware update are the controller's (`safety.firmware_update_problem`): only the offered version (a stale client is refused), the mower docked (activity `docked` or `charging`; it need not be charging) with at least 70 % battery (amended 2026-10-15, see below), no job, no open drive session, nothing already installing; while the mower reports `updating` every command but the emergency stop is rejected. The simulator implements all of it (a bridge link with a switch, a configurable scan answer, a firmware offer whose update progresses to done), so the unit tests, the Compose e2e and the web Playwright tests need no manufacturer package.

**Device-Gateway.** It stays manufacturer free: `MowerRegistration.links` reach the adapter as the connection option `links` (`{kind: options}`, the link secrets merged for that connect only, like the connection credentials of ADR 0034); a registration whose links changed reconnects. The responders for `gateway.mower.<id>.bridge.scan` and `gateway.mower.<id>.firmware.check` route through the connection's capabilities and answer `unsupported` when a connection lacks them; a scan is `busy` while another mower of the instance holds that bridge open or a scan through it runs (one native API client per proxy at a time; a connection answers the re-check of its own bridge from the live scanner). The session asks for firmware once after a connect (when the adapter's device information rounds are through) and daily, and compares the capabilities again when the links of a mower change, so manual driving appears and disappears with the bridge for Realtime's drive capabilities. Notifications derives `firmware_update_available` (once per version), `firmware_update_finished` and `firmware_update_failed` from the `firmware_info` transitions on the telemetry stream, as it derives every other kind.

**Mammotion adapter (`libs/mowdeck-control-mammotion`).** A cloud registration with a `ble_bridge` link gets a `MammotionDualConnection`: one PyMammotion client with two transports. A `BridgeWatcher` holds the ESPHome proxy, attaches the mower's BLE transport to the cloud device handle (`add_ble_to_device`, `connect_ble`) only while a link role needs it (see [Link roles](#link-roles)) and reconnects with its own backoff (5 s doubling to 60 s), independent of the cloud session. `is_connected` stays the cloud rule (a lost bridge interrupts nothing else, a dead cloud login reconnects as before), manual driving needs the bridge and a drive frame never falls back to the cloud. The bridge firmware (`esphome_version`, `project_version` of MowDeck's template, `outdated` with a reason) comes from ESPHome's `device_info`; the scan is `esphome.scan_proxy` with `APIConnectionError` mapped to `unreachable` and `InvalidEncryptionKeyAPIError` to `auth_failed`. The cloud connection is the `FirmwarePort`: `get_device_ota_firmware` and `start_ota_upgrade` through the port, a `FirmwareTracker` that follows the update through `otaProgress` and `toapp_upgrade_report` to `idle`, `failed` or `unknown` (45 min), `MODE_UPDATING` mapped to the activity `updating`. The owner script's `--bridge HOST,KEY` runs the dual link against the real mower.

### Link roles

Amended after the first owner runs: with Bluetooth attached all the time, PyMammotion sent every command and every fresh-state request over the bridge (a connected BLE transport always wins its transport choice). A docked mower at the edge of the bridge's range then answered only PyMammotion's own docked Bluetooth poll, every 5 minutes, so `observed_at` aged past the web app's 45 s and the mower showed as offline while both links were up; and the mower stopped advertising, so bridge scans from other entries saw nothing.

- **Cloud first.** The state, the data requests (reads, the map, the route), the fresh-state polling and every command except the drive channel go over the cloud, like the camera and the firmware service.
- **Role `drive`.** Manual driving and spot runs (both hold a drive session): the drive frames (`send_movement`, blades through `operate_on_device`) and the fast report polling of the open session go over Bluetooth. Opening a drive session connects Bluetooth on demand and refuses when it does not come up; a frame never falls back to the cloud.
- **Role `fast_position`.** While the mower is mowing or returning, Bluetooth also carries the continuous report stream, so positions arrive about once a second. It comes on top of the cloud polling, never instead of it, and can be switched off with the adapter's link option `fast_position` (default on).
- **Docked or idle.** No role is active: the bridge holds the proxy link and reports the link status and the mower's RSSI from its advertisements, but it does not connect Bluetooth and does not poll. The mower keeps advertising.

`MowerState.links.ble_bridge.connected` means the bridge holds the link (proxy up); `MowerState.connection` is `ble_bridge` only while Bluetooth carries traffic. `capabilities.manual_drive` follows the held link. The roles are an adapter rule; the generic interface needs no change for them (a second adapter decides its own routing).

## Web

The mower settings get a "Bluetooth bridge" panel for cloud mowers (scan through the bridge, pick the mower, save; afterwards the link state with RSSI, bridge firmware and an "outdated" badge; re-check and remove) and a "Firmware" panel (installed, available with release notes, state and progress, "check for updates", "install update" with the preconditions as hints and a confirmation). The joystick page says "Bluetooth bridge disconnected" instead of showing a dead joystick. The standalone `ble_bridge` kind stays for mowers without a cloud account. Details live in the web app guide and the Web part of this ADR's implementation.

## Manufacturer independence

Generic, and the same for every manufacturer: the contracts (`LinkStatus`, `ProxyInfo`, `ScannedDevice`, `FirmwareInfo`, the scan and check request/reply, the command `firmware_update`, the capability flags), the protocols `FirmwarePort` and `BridgeScanCapable` of `mowdeck-control`, the firmware safety rules and the `updating` lock-out in the controller, the simulator's implementation of all of it, the gateway's routing by capabilities, Fleet's storage of links keyed by kind, the Notifications rules and the web panels (they read `links`, `firmware_info` and the capabilities, never a vendor field). The Bluetooth bridge itself is an ESPHome Bluetooth proxy and works for any mower that speaks Bluetooth LE; its firmware fields describe the bridge, not the mower.

Adapter work, done once per manufacturer: how the extra link is attached to the vendor's client and preferred for the fast path, how the vendor's firmware service is called and how an update's progress and end are read from the vendor's reports. A second manufacturer's adapter would implement `FirmwarePort` with its vendor's version check and OTA request (setting `capabilities.firmware_update`), list `ble_bridge` in `capabilities.links_supported` and open the bridge next to its primary connection (reporting `MowerState.links` and the `connection` in use, publishing a state on every link change, ending drive sessions when the bridge drops), and implement `BridgeScanCapable` by reusing the ESPHome scan of the bridge package or its own. Nothing in the services or the web app changes for it.

## Consequences

Easier: one registration per mower with the camera and the joystick; firmware updates from MowDeck with rules a vendor app does not enforce; a bridge that tells the owner when it should be rebuilt. Harder: two transports on one client mean more states to reason about (the state's `connection` and `links` make them visible); the update tracker relies on vendor reports that were verified only partly (the hardware run of the owner script checks the dual link and the version check). Follow-ups: bridge firmware v2 (a MowDeck-built image installed over the air from the web app), spot mowing over the fast link (ADR 0039).

## Amendment (2026-10-15): docked with 70 % battery, charging not required

**Decision.** A firmware update needs the mower in its dock (activity `docked` or `charging`) with at least 70 % battery (`FIRMWARE_MIN_BATTERY_PERCENT`). The former requirement that `charging` is true is dropped, and the battery floor rises from 50 % to 70 %.

**Why.** The first owner run refused "Update installieren" with "must be charging" although the mower stood in its dock: a mower stops charging once its battery is full, so a docked, full mower reports `charging` false and could never be updated. What the rule wants to guarantee is enough energy for the 10 to 20 minutes of the update and a mower that does not drive away; the dock plus a battery floor say that directly. The floor rises to 70 % because the mower no longer has to be on charge while it installs.

The controller (`safety.firmware_update_problem`), the adapters and the simulator (which call it), the contract's `FirmwareUpdate` description and the web panel's hints follow the same rule.
