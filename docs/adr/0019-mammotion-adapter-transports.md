# ADR 0019: Mammotion adapter transports, manual drive and secrets

## Status

Accepted (2026-10-09). Builds on ADR 0011 (GPL package) and ADR 0012 (control interface). Amends the "options are never secrets" rule of ADR 0013 for the Mammotion adapters in Phase 2.

## Context

Phase 2 needs real mower control. [Spike 0001](../spikes/0001-ble-bridge-and-pymammotion.md) showed that PyMammotion 0.10.9 reaches a LUBA over three transports: the Mammotion cloud (`MQTTTransport`, `AliyunMQTTTransport` for pre-2025 devices), local BLE (`BLETransport` over bleak) and BLE through an ESPHome Bluetooth Proxy (`bleak-esphome`). Facts that shape the decision:

- No hardware is available yet: the ESP32 bridge (XIAO ESP32-S3 with the ESPHome proxy) comes later, the mower (LUBA 2 AWD X, firmware 1.30.29.26, network RTK over 4G) is reachable over the cloud today, and a second Mammotion account with the mower shared to it exists.
- Manual driving: BLE `send_movement` is what Mammotion-HA uses; PyMammotion's cloud `RemoteDriveSession` "has never been run against a mower" and is gated by a server function code; control over Wi-Fi returns only from firmware 1.30.31.19.
- The cloud needs account credentials; the BLE proxy needs the ESPHome API key. Fleet stores connection options and publishes them to the Device-Gateway; until now they must not contain secrets.
- PyMammotion releases almost daily and its API churns (spike risk R4); the 0.10.9 wheel misses its `packaging` dependency.

## Decision

1. **Cloud first.** `libs/mowdeck-control-mammotion` registers `ConnectionKind.CLOUD` (`MammotionCloudConnection`) as the first real adapter, because it works without hardware. It logs in with the account from the options, selects the mower by `device_name` or `iot_id` and uses PyMammotion's MQTT transport. A transient MQTT loss does not end the connection (PyMammotion reconnects, commands raise `NotConnected`); only a dead login does, so the Device-Gateway does not log in again on every hiccup (ban risk R1). The PyMammotion credential cache can be kept on a private volume (`session_cache_path`).
2. **BLE through the ESPHome proxy.** `ConnectionKind.BLE_BRIDGE` (`MammotionBleProxyConnection`, extra `esphome`) connects to the stock ESPHome Bluetooth Proxy (`host`, `port` 6053, `noise_psk`, `ble_address`) with a minimal `habluetooth` manager and a `bleak-esphome` scanner, and registers the mower with `add_ble_only_device` (no cloud login). A lost proxy or BLE link ends the connection, and the Device-Gateway's backoff reconnects.
3. **Manual driving only over BLE.** The BLE connection offers a `DriveSession` on `send_movement`; blades while driving stay off unless the option `allow_manual_blades` is set. Over the cloud `manual_drive` is false until `RemoteDriveSession` is verified on hardware. `ConnectionKind.WIFI` is not implemented.
4. **One PyMammotion surface.** Only `port.py` imports `pymammotion`; it defines the small `MammotionPort` protocol the rest of the adapter uses, so tests inject a fake built from the real PyMammotion data classes and an upgrade has one place to review.
5. **Secrets.** Production registrations store a reference, `credential_ref` (`env:NAME` or `file:/path`), which the Device-Gateway resolves in its own environment (Docker or Kubernetes secrets). Phase 2 still accepts a plain `password` (cloud) and `noise_psk` (BLE); Fleet allows exactly these keys per kind, logs a warning for plain secrets and never returns their values (`***` in every response; sending `***` back keeps the stored value). TODO for Phase 3: a secret store and rejecting plain secrets.
6. **Pin policy.** `pymammotion==0.10.9` exactly, plus `packaging` declared by us. The extra `esphome` pins `aioesphomeapi==45.0.3`, `bleak-esphome==3.7.5`, `habluetooth==6.1.0` and `bluetooth-adapters==2.1.1` (PyMammotion's lock file). Upgrades are deliberate, one PyMammotion release at a time, after the adapter tests and the owner script (`scripts/try_real_mower.py`) pass again.

Alternatives considered:

- *BLE first, cloud later.* Rejected for now: without the ESP32 nothing could be tested end to end; the cloud path is also needed for the camera later.
- *Manual driving over the cloud with `RemoteDriveSession`.* Rejected until verified: a joystick path that was never run against a mower must not be offered next to a dead-man switch.
- *A separate Device-Gateway secret store now.* Deferred: it is a platform feature (Phase 3); references already keep secrets out of Fleet's database where the deployment allows it.
- *Range pins (`pymammotion>=0.10,<0.11`).* Rejected: the API changes within patch releases.

## Consequences

- The Device-Gateway image includes the adapter and PyMammotion's dependency tree (numpy, shapely, bleak and more), which roughly doubles its size (265 MB to 466 MB).
- Fleet must enable the kinds (`MOWDECK_ALLOWED_CONNECTION_KINDS=simulated,cloud,ble_bridge`) before real mowers can be registered.
- Until the secret store exists, connection options with plain secrets travel over the internal bus to the Device-Gateway; deployments should prefer `credential_ref`.
- Every cloud mower holds its own PyMammotion client and login; sharing one client per account is a later optimisation.
- The owner's spike run (step 1 to 4 with the script) confirms or corrects the unverified mappings: work modes and RTK fields of the LUBA 2 AWD X, the map reference with network RTK, the dock convention, the StartMow pattern mapping and the emergency stop sequence.

## Amendment (2026-10-24): manual driving over the cloud

Point 3 is superseded by [ADR 0049](0049-manual-driving-over-the-mammotion-cloud.md): `RemoteDriveSession` was verified on the owner's LUBA 2 AWD X (firmware 1.30.31.19), and the cloud connection offers manual driving (forwards and turns, no reverse) when the mower supports it, with the owner account.
