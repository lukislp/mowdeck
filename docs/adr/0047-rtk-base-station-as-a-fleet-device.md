# ADR 0047: The RTK base station as a Fleet device

## Status

Accepted (2026-10-23)

## Context

A LUBA 2 (also the owner's LUBA 2 AWD X 4G RTK) positions itself with RTK corrections from its own base station, received over LoRa radio or through the vendor cloud, or from a network RTK service (NRTK). Until now MowDeck knew the base station only as a device of a linked Mammotion account that "cannot be registered" (`is_mower` false, 422 `not_a_mower`, ADR 0034). The owner could neither see whether the mower still hears its base station, nor where the base stands, nor update its firmware, although all of that decides whether the mower can mow at all.

What the vendor exposes, as far as PyMammotion 0.10.9 and Mammotion-HA show it:

- **The base station is its own cloud device.** Mammotion-HA lists devices without a `Luba`/`Yuka` name and with the category `Tracker` as RTK devices; PyMammotion's `DeviceType.is_rtk` matches the names `RTK…`, `RBS03A0/1/2…`, `NB…` and the RTK product keys, and its device registry holds them as `RTKBaseStationDevice` (own IoT id, own MQTT properties: position in radians, Wi-Fi RSSI, LoRa version, firmware). Mammotion-HA polls such a device every five hours (`basestation_info` sent to the base, the vendor's version check) and offers its firmware update (`start_ota_upgrade` with the base's IoT id).
- **The mower describes its base in every report.** `report_data.rtk` carries the correction source (`mqtt_rtk_info.rtk_switch`: LoRa, internet, NRTK), the base position in degrees (`mqtt_rtk_info.latitude/longitude`; `0, 0` unknown, `-1, -1` no GGA), the LoRa link (`lora_info.lora_connection_status > 0`, the channel) and whether the base was moved (`score_info.base_moved`, `base_moving`); `report_data.basestation_info` the base's version (`rpt_basestation_info.ver_*`). A `basestation_info` request sent to the mower is answered with `base.to_app` (`ResponseBasestationInfoT`: satellites, solution, Wi-Fi RSSI, LoRa channel), which the next report overwrites in PyMammotion's model.
- **Base station commands go through the mower** (`basestation_info`, `reset_base_station`, `set_base_net_rtk_switch`, `read_and_set_rtk_pairing_code`).

The architecture rules stay: Fleet owns the registrations and never talks to the vendor (ADR 0034); the Device-Gateway is manufacturer free and opens one connection per mower; only `mowdeck-control-mammotion` talks to PyMammotion.

## Decision

The base station becomes its own Fleet record, linked to the mowers that use it. It gets no connection of its own: its state comes from the connection of a linked mower, and its firmware is checked and installed through that mower's cloud session with the base station's IoT id.

### Fleet

- A table `base_stations` (`id`, `account_id`, `name`, `source` `cloud` or `simulated`, `linked_account_id`, `device_name`, `iot_id` with an installation-wide unique index, `model`, timestamps) and the nullable column `mowers.base_station_id` (`ON DELETE SET NULL`), migration `0012`.
- Discovery gains `DiscoveredDevice.device_type` (`mower`, `rtk_base`, `other`; derived from `is_mower` for lists cached before), and the device list of a linked account shows a registered base station (`registered`, `base_station_id`) or one registered elsewhere, like a mower (ADR 0034, one physical device in one MowDeck account).
- API under `/api/v1/fleet/base-stations`: list, register (`{linked_account_id, device_name, name?}` from the cached device list, or `{source: "simulated"}`), get, rename, delete (the mowers lose the link), `POST /{id}/firmware/check` and `POST /{id}/firmware/update` (`{version, mower_id?}`); `PUT /api/v1/fleet/mowers/{id}/base-station` (`{base_station_id}` or `null`) links a mower. A cloud base station serves cloud mowers of the same linked account (its firmware is asked for with that account's session), the simulated one simulated mowers (409 `base-station-not-applicable` otherwise).
- Permissions without a new OpenFGA type: reading base stations and the firmware check need `can_view` on the account, every change and the firmware update `can_manage_mowers` on the account, linking a mower `can_manage_mowers` on the mower. A base station of another account is 404. Changes are audited (`base_station.registered`, `.updated`, `.removed`, `mower.base_station_set`, `mower.base_station_removed`) and re-publish the affected registrations as `mower.updated`.
- The registration carries the link: `MowerRegistration.base_station` (`BaseStationRef`: id, name, source, device name, IoT id; no secrets).

### Device-Gateway and control library

- The gateway passes the reference to the adapter as the connection option `base_station` and reopens the connection when the linked base station changes (not for a rename).
- `MowerState.base_station` (`BaseStationState`): the correction link (`lora`, `internet`, `network`), whether the mower receives the base (`link_up`, over LoRa), the LoRa channel, the base's satellites, solution and Wi-Fi signal (`details_at`), its position, `moved`, its firmware and `firmware_info`. Telemetry stores and serves it with the mower state; no new stream or table.
- Firmware reuses ADR 0038: `FirmwareCheckRequest.target` (`mower` or `base_station`) on the existing subject, a `BaseStationPort` protocol (`check_base_station_firmware`, `start_base_station_firmware_update`) with the capability `base_station_firmware_update`, and the command `base_station_firmware_update` (`version`). The controller's rule (`safety.base_station_firmware_update_problem`): only the offered version, nothing installing, and the relaying mower neither mows, is paused, returns, updates nor drives (every mower of the base loses its corrections while it restarts). Fleet records the command like any other (command record, plan feature `control`, single mower only) and additionally requires `can_manage_mowers` on the account and a linked base station. The gateway checks the base station's firmware with the mower's daily check.
- The simulator offers a simulated base station (LoRa while its RTK source is the base station, firmware offer and update), the Mammotion adapter the real one (below), the fake cloud a base station for the browser tests.

### Mammotion adapter

From every report: `rtk.mqtt_rtk_info`, `rtk.lora_info`, `rtk.score_info` and the version in `basestation_info`; from a `basestation_info` request to the mower (20 s after the connect, then hourly, every five minutes while an update runs) the details, kept by the adapter because the next report resets them; from the vendor's firmware service `get_device_ota_firmware([base_iot_id])` and `start_ota_upgrade(base_iot_id, version)` through the mower's cloud session, without touching the mower's own firmware state. A started update ends `idle` when the vendor or the mower's report shows the target version, `unknown` after 45 minutes.

### Options considered

- **The base station as a second cloud device in the gateway** (its own `RTKBaseStationDevice` handle, MQTT properties, polling like Mammotion-HA's `MammotionRTKCoordinator`): rejected for now. It needs a connection kind without a mower (the gateway, the controller and Telemetry are built around mowers), a second device handle on a shared session that was never verified with several handles (ADR 0034), and more cloud traffic, while every value the owner needs is already in the mower's reports. Kept as the follow-up when a base without a linked mower should be watched (the base's own Wi-Fi details, `lora_version`).
- **A `device_type` on the mower record** (`mowers` rows of type `rtk_base`): rejected. Every mower use case, the registry, the gateway sharding, the commands, Telemetry, the Scheduler and the web app would have to exclude base stations; a separate table with a link keeps them out.
- **A new OpenFGA type `base_station`** with `parent` site: rejected for now. A base station serves mowers of several sites; account-level relations express that, and the mower state (visible with the mower's own `can_view`) already shows a site-scoped user the base of their mower. A type can be added when base stations get sub-user grants of their own.
- **The firmware update as a request/reply instead of a command**: rejected; a command gives the audit record, the plan check, the status URL and the controller's safety rules for free.

### Out of scope

- Resetting the base station, switching its correction source and the LoRa pairing (`reset_base_station`, `set_base_net_rtk_switch`, `read_and_set_rtk_pairing_code`): they change how every mower positions itself and are not verified on hardware.
- The base station's own cloud device (see above), its LoRa version and Wi-Fi network details, the base of a mower connected only over Bluetooth (no base station link for `ble_bridge` mowers).
- Models other than the LUBA 2 family: Yuka, Spino and the RTK-less models (vision only) are not verified; a Yuka uses the same report fields in PyMammotion, but nothing here was tested with one.
- The web app: no base station UI yet (the device list keeps base stations out of the add-mower flow). The API and the state are ready for it.

## Consequences

- The owner sees in the mower's state whether it receives its base station and where the base stands, and can update the base's firmware from MowDeck with rules the vendor app does not enforce.
- Unverified against the real mower and base (the hardware owner script is the follow-up): that the LUBA 2 answers `basestation_info` (Mammotion-HA sends it to the base itself, PyMammotion's `mower_api.py` to the mower), the meaning of `ResponseBasestationInfoT.rtk_status` (read with PyMammotion's `RTKStatus` like its `BasestationInfo.fix_status`; Mammotion-HA shows it as `RTKPositionMode`), that `rpt_basestation_info.ver_*` is the base's version, that the base station's version check and OTA request work with the mower's account session (the base must be in the same Mammotion account or shared to it), and whether `link_up` should also be derived for corrections over the internet (it is null then).
- Fleet migration `0012` is additive; an older Fleet keeps working during the rollout. Base stations are registered after the upgrade from the device list (read the linked account again so the list carries `device_type`).
- A rename re-publishes the registrations but does not reconnect; linking or unlinking a base station reconnects the mower.
