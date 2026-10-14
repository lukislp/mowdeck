# Spike 0001: BLE bridge and PyMammotion

Status: prepared, not executed (no hardware available yet).
Research date: 2026-10-08. Source revision read: PyMammotion `main` at commit `cdb19de` (version 0.10.9), Mammotion-HA `main` at commit `e81891c` (version 0.6.17).

## 1. Purpose and scope

This spike answers the open technical questions from the project plan (section "Risiken, Sicherheit, offene Fragen") before `mowdeck-control` and the Device-Gateway are built:

- Does PyMammotion deliver usable map geometry, manual driving, custom mow jobs, camera access, 4G and network RTK data?
- How high is the BLE command latency, locally and through an ESP32 bridge, and is it good enough for a joystick with a 1 s deadman?
- Which bridge firmware: ESPHome Bluetooth Proxy or a custom one?
- What should the HACS integration expose?
- How many mower connections can one process hold?

Out of scope: the payment provider question (Stripe vs. Paddle). It is not a hardware question and belongs in a separate ADR.

The document has two parts. Part A (sections 2 to 6) is the research result with sources. Part B (sections 7 to 10) is a prepared task the owner runs step by step when the hardware is available.

## 2. Facts about the owner's setup

| Item | Value |
|---|---|
| Mower | Mammotion LUBA 2 AWD X |
| Firmware | 1.30.29.26 (do not update during the spike) |
| Positioning | Mammotion network RTK over 4G, no own RTK base station |
| Accounts | Main account plus a second account; the mower is already shared with the second account |
| Home Assistant | The old community Mammotion integration still runs (double-access risk) |
| ESP32 | Not bought yet, see the [purchase and setup guide](../guides/esp32-bridge-purchase-and-setup.md) |
| Location | Germany |

PyMammotion maps the "LUBA 2 AWD ... X" product to the internal type `LUBA_VP` (BLE and device name prefix `Luba-VP`), according to a comment in [`device_type.py`](https://github.com/mikey0000/PyMammotion/blob/main/pymammotion/utility/device_type.py). Unverified for this unit: step 0 records the real name.

## 3. Research findings

### 3.1 PyMammotion today

- **Version and cadence.** Latest release on PyPI is 0.10.9, published 2026-10-08. There were 20 releases between 0.9.0 (2026-09-22) and 0.10.9, and 483 releases overall ([PyPI](https://pypi.org/project/pymammotion/)). The maintainer (mikey0000, Michael Arthur) commits daily; the repo has a Discord and a large docs folder ([repo](https://github.com/mikey0000/PyMammotion)). The API changes fast, so pinning is mandatory.
- **Python version.** `requires-python = ">=3.13"` since 0.7.117 (2026-05-20); 3.12 has not been supported since 0.7.83 (2026-04-29) ([pyproject.toml](https://github.com/mikey0000/PyMammotion/blob/main/pyproject.toml), PyPI metadata). **This conflicts with the MowDeck constraint "Python 3.12"** (`requires-python = ">=3.12,<3.13"` in the root `pyproject.toml`). PyMammotion cannot be installed into the current workspace. See the recommendation in section 4.
- **License.** GPL-3.0 ([LICENSE](https://github.com/mikey0000/PyMammotion/blob/main/LICENSE)). ADR 0005 plans Apache-2.0 for `libs/`; see risk R3.
- **Dependencies.** bleak, bleak-retry-connector, aiohttp, betterproto2, aiomqtt, Alibaba Cloud IoT packages, shapely, numpy and more; the optional group `extras` adds `aioesphomeapi`, `bleak-esphome` and `bluetooth-adapters` ([pyproject.toml](https://github.com/mikey0000/PyMammotion/blob/main/pyproject.toml)).
- **Structure.** The old classes `MammotionBaseBLEDevice`, `MammotionCloudDevice` and `MammotionMixedDeviceManager` are gone (one stale example still imports them). The entry point is `pymammotion.client.MammotionClient`, one per process, with a `DeviceHandle` per account and device, a priority command queue, sagas for multi-frame transfers, and three transports: `AliyunMQTTTransport` (pre-2025 devices), `MQTTTransport` (Mammotion broker) and `BLETransport` (bleak) ([`__init__.py`](https://github.com/mikey0000/PyMammotion/blob/main/pymammotion/__init__.py), [docs/architecture.md](https://github.com/mikey0000/PyMammotion/blob/main/docs/architecture.md)).
- **Commands.** Commands are builder methods on `MammotionCommand` and are called by name: `client.send_command_with_args(name, "start_job")` or `client.send_command_and_wait(name, key, expected_field)`. Builders live in `mammotion/commands/messages/navigation.py` (`MctlNav`: `start_job`, `pause_execute_task`, `resume_execute_task`, `return_to_dock`, `cancel_job`, `generate_route_information`, map hash requests) and `driver.py` (`MctlDriver`: `send_movement`, `operate_on_device`, `set_blade_height`, `session_ctrl`) ([navigation.py](https://github.com/mikey0000/PyMammotion/blob/main/pymammotion/mammotion/commands/messages/navigation.py), [driver.py](https://github.com/mikey0000/PyMammotion/blob/main/pymammotion/mammotion/commands/messages/driver.py)). `Priority.USER` and `Priority.EMERGENCY` bypass the queue ([docs/decisions.md, D14](https://github.com/mikey0000/PyMammotion/blob/main/docs/decisions.md)).
- **Transport choice.** "A connected BLE always wins and a usable MQTT otherwise" (docstring of `send_command_with_args` in [client.py](https://github.com/mikey0000/PyMammotion/blob/main/pymammotion/client.py)). BLE can run without any cloud login via `add_ble_only_device(...)`.
- **ESPHome proxy is already supported.** `examples/dev_console.py --esphome-proxy HOST --ble-address MAC` routes the BLE connection through an ESPHome Bluetooth Proxy with `aioesphomeapi`, `bleak-esphome` and a minimal `habluetooth.BluetoothManager`, without Home Assistant. The rest of the stack (BLETransport, codec, poll loops) runs unchanged ([dev_console.py](https://github.com/mikey0000/PyMammotion/blob/main/examples/dev_console.py)). The example passes only an API `password`, not a Noise encryption key; the spike script in step 4 adds `noise_psk`, which `aioesphomeapi.APIClient` accepts (checked in aioesphomeapi 45.0.3, the version in PyMammotion's lock file).
- **Cloud send quota and Wi-Fi control.** PyMammotion's own 600-per-12-h send quota applies only to firmware below `1.30.25.1` ("Firmware version at which devices migrated to the Mammotion MQTT broker", [transport/cloud.py](https://github.com/mikey0000/PyMammotion/blob/main/pymammotion/transport/cloud.py)). A server 429 ban (12 h) still applies to everyone. Separately, `device_type.py` quotes a Mammotion release note that restores control over Wi-Fi for "LUBA 2, LUBA 2X, YUKA, LUBA mini Series, YUKA mini Series" from firmware **1.30.31.19**. The owner's 1.30.29.26 is below that threshold. I could not find the release note itself on Mammotion's support site, so the threshold is **unverified** beyond the PyMammotion source.

### 3.2 Open questions, what PyMammotion offers

| Plan question | Finding | Answer |
|---|---|---|
| Map geometry | `start_map_sync(name)` runs a saga that fills `device.map` (`HashList`): `area`, `obstacle`, `path`, `no_go_zone`, `virtual_wall`, `dump`, `svg`, `transfer_zone`, area names, plus `generated_geojson` ([hash_list.py](https://github.com/mikey0000/PyMammotion/blob/main/pymammotion/data/model/hash_list.py)). Points are `(x, y)` in metres in a device-local ENU frame around the RTK reference, rotated by the RTK yaw; PyMammotion converts them to WGS84 lon/lat ([generate_geojson.py](https://github.com/mikey0000/PyMammotion/blob/main/pymammotion/data/model/generate_geojson.py)). Dock position and rotation are in `device.location.dock`. | Yes. Unknown: what the reference origin is with network RTK and no base station (step 3 checks it). |
| Manual driving and blades | BLE: `send_movement(linear_speed, angular_speed)` plus helpers `move_forward/back/left/right(0.0..1.0)` with a 15 % dead zone. Blades: `operate_on_device(main_ctrl, cut_knife_ctrl, cut_knife_height, max_run_speed)`. Cloud: a separate `RemoteDriveSession` (`start_remote_drive`, `confirm_remote_drive`, `remote_drive`, `stop_remote_drive`) with a control token, one frame in flight, 150 ms pacing, idle timeout and fence events, gated by server function code 002.002 ([docs/remote_drive.md](https://github.com/mikey0000/PyMammotion/blob/main/docs/remote_drive.md)). The doc states: "none of it has been run against a mower". | Yes over BLE (used by Mammotion-HA). Cloud drive is unverified on hardware. Whether the mower stops by itself when BLE frames stop is unknown (step 6). |
| Mow job with own boundary | Jobs are built from existing zone hashes: `generate_route_information(GenerateRouteInformation(one_hashs=[...], ...))`, then `start_job`. No command was found that uploads an arbitrary polygon or waypoint list as a job. Related: map editing by recording (drive the border), `add_manual_element` (rectangle or ellipse safety zone with center, size, rotation), SVG pattern tiles placed inside an area (`send_svg`), and `start_no_area_work` (DropMow, X5 models only). | No for arbitrary boundaries via a known command. SVG tiles are a possible path for shapes (unverified). |
| Native patterns | `channel_mode` = `CuttingMode` (`single_grid`, `double_grid`, `segment_grid`, `no_grid`), `toward` (angle), `toward_mode` = `PathAngleSetting` (`relative_angle`, `absolute_angle`, `random_angle`), `toward_included_angle` (crossing angle for double grid), `channel_width` (path spacing), border laps 0 to 4, obstacle laps, `MowOrder` (`border_first`, `grid_first`), `auto_change_direction`, `ride_boundary_distance`, speed, blade height ([mowing_modes.py](https://github.com/mikey0000/PyMammotion/blob/main/pymammotion/data/model/mowing_modes.py), [device_config.py](https://github.com/mikey0000/PyMammotion/blob/main/pymammotion/data/model/device_config.py)). | Stripes at any angle, checkerboard, edge laps, alternating direction and random angle are native. Spiral, contour-parallel, cells and lawn art are not (lawn art maybe via SVG). |
| Camera stream | Video goes through Agora (cloud). `get_stream_subscription(name, iot_id)` returns an Agora `appid`, `channelName`, `token`, `uid`, camera list, optional AES-256-GCM2 key; `get_video_resource` returns the 4G video quota ([camera_stream.py](https://github.com/mikey0000/PyMammotion/blob/main/pymammotion/http/model/camera_stream.py)). Mammotion-HA joins the Agora channel server-side with `pyagorartc` and answers the browser's WebRTC offer; encrypted channels show no picture ([stream_session.py](https://github.com/mikey0000/Mammotion-HA/blob/main/custom_components/mammotion/stream_session.py)). | Cloud only. No local RTSP or WebRTC endpoint is known; unverified (step 7 port-scans the mower). |
| 4G and network RTK | Model fields exist: `report_data.connect` (`connect_type`, `used_net`, `mnet_rssi`, `wifi_rssi`, `ble_rssi`, `iot_con_status`), `report_data.dev.mnet_info` (SIM state, link type 2G..5G, RSSI, operator, IMEI, ICCID), `fpv_info.mobile_net_available` ([report_info.py](https://github.com/mikey0000/PyMammotion/blob/main/pymammotion/data/model/report_info.py)). Cloud control is independent of how the mower reaches the internet. | Yes in the model. Unknown whether the LUBA 2 AWD X fills these fields (step 8). |
| RTK fix and correction source | `report_data.rtk`: `status` (`RTKStatus` NONE/SINGLE/FIX/FLOAT), `pos_level`, satellites, correction age, packed signal quality, and `mqtt_rtk_info.rtk_switch` with `LORA` (base station over radio), `INTERNET` (base over internet) and `NRTK` (network RTK) ([docs/rtk_fields.md](https://github.com/mikey0000/PyMammotion/blob/main/docs/rtk_fields.md)). | Yes, the correction source is reported. Values on this unit to be confirmed (step 2). |
| BLE latency | Writes go to characteristic `ff01` with response (`write_gatt_char(..., True)`), notifications on `ff02` ([ble_message.py](https://github.com/mikey0000/PyMammotion/blob/main/pymammotion/bluetooth/ble_message.py)). No published measurements found. | Unknown (step 5). |
| Devices per process | One `MammotionClient` holds many handles keyed by `(account_id, device_id)`; BLE is one connection per mower. ESPHome proxy default is 3 connection slots, max 9, recommended at most 5 ([ESPHome docs](https://esphome.io/components/bluetooth_proxy/)). No load numbers found. | Unknown. The spike only measures the cost of one mower (CPU and memory per handle). |

### 3.3 Mammotion-HA integration

Mammotion-HA 0.6.17 pins `pymammotion==0.10.9` and `pyagorartc==0.2.3` and needs Home Assistant 2026.1.0 or newer ([manifest.json](https://github.com/mikey0000/Mammotion-HA/blob/main/custom_components/mammotion/manifest.json), [README](https://github.com/mikey0000/Mammotion-HA)). It is the best template for our HACS integration.

- **Platforms:** `lawn_mower`, `camera` (up to four WebRTC feeds), `sensor` (battery, progress, RTK, satellites, 4G RSSI, errors, blade hours), `binary_sensor` (charging, rain), `switch` (blades, rain detection, lights, remote drive), `number` (blade height, speed, path spacing, angle), `select` (`channel_mode`, border mode, laps, angle mode), `button` (cancel, release from dock, map sync, nudges), `device_tracker`, `event`, `update`.
- **Services** ([services.yaml](https://github.com/mikey0000/Mammotion-HA/blob/main/custom_components/mammotion/services.yaml)): `start_mow` (with areas and pattern settings), `cancel_job`, `modify_running_job`, `start_stop_blades`, `set_blade_height`, task CRUD (`create_task`, `edit_task`, `start_task`, ...), map tools (`get_map_data`, `get_geojson`, `get_mow_path_geojson`, `get_mow_progress_geojson`, map backup and restore), SVG tools, dump point setup.
- **Start sequence** used by `start_mow` ([lawn_mower.py](https://github.com/mikey0000/Mammotion-HA/blob/main/custom_components/mammotion/lawn_mower.py)): wait for fresh state, end a paused job or clear a breakpoint if needed, `generate_route_information` and wait for `bidire_reqconver_path`, then `start_job` and wait for `zone_start_precent_t`. The spike script uses the same order. (Superseded 2026-10-10: waiting for `zone_start_precent_t` re-sends `start_job` on every attempt without that reply, which restarted a paused LUBA 2; the adapter now sends `start_job` once and confirms by the state, see the adapter README.)
- **Double access:** the README asks for "A second account with your mower/s shared to it" because the main account logs the phone app out. PyMammotion detects Aliyun "bind_reply 2152" (account lock held by the app), flags `account_in_use` and retries; for the Mammotion broker it notes "no such contention" ([architecture.md, 4.4](https://github.com/mikey0000/PyMammotion/blob/main/docs/architecture.md), [cloud.py](https://github.com/mikey0000/PyMammotion/blob/main/pymammotion/transport/cloud.py)). There is no coordination between two independent programs using the same account or the same BLE link. An ESPHome-based BLE setup in the wiki even adds a switch to release the mower "to the Mammotion app" ([ESP32 guide](https://github.com/mikey0000/Mammotion-HA/wiki/ESP32-guide)), which suggests one BLE central at a time (unverified).

### 3.4 ESP32 bridge options

| Criterion | (a) ESPHome Bluetooth Proxy | (b) Custom firmware, GATT over outbound WebSocket/TLS | (c) Existing projects |
|---|---|---|---|
| Works with PyMammotion today | Yes, via `bleak-esphome` (shown in `dev_console.py`) | Needs a new bleak backend in `mowdeck-control` modelled on bleak-esphome | `zitafilms/mammotion-rc`: custom TCP proxy plus PyMammotion web UI with joystick, wire protocol not documented, connection inbound on LAN ([repo](https://github.com/zitafilms/mammotion-rc)); `esp32-ble2mqtt`: GATT over MQTT ([repo](https://github.com/shmuelzon/esp32-ble2mqtt)) |
| Latency | One Wi-Fi hop plus BLE write with response; no published numbers (unknown) | Same physics; framing is ours | Unknown |
| Connection direction | Inbound: client connects to the ESP on TCP 6053, Noise encryption ([bleak-esphome architecture](https://bleak-esphome.readthedocs.io/en/latest/architecture.html), [ESPHome API](https://esphome.io/components/api/)). Outbound possible with the ESPHome `wireguard` component (single peer, needs SNTP, keepalive behind NAT) ([ESPHome WireGuard](https://esphome.io/components/wireguard/)) | Outbound TLS with device secret, as the plan requires | (c1) inbound, (c2) outbound MQTT, TLS depends on broker |
| Reconnect | Mature (used by thousands of HA installs), `bleak-retry-connector` on the client | Must be built | Unknown |
| OTA | Built in (`ota: platform: esphome`) | Must be built (ESP-IDF OTA, signed images) | Varies |
| Effort | Hours | Weeks (firmware, protocol, bleak backend, provisioning) | Days to evaluate, licence and fit unclear |

**Not recommended:** ESPHome `ble_client` pointed at the mower, as in the Mammotion-HA wiki. It makes the ESP itself a GATT client and probably blocks the proxied connection (unverified).

### 3.5 Hardware on the mower

- The LUBA 2 AWD manual (V1.0, 2024-01) shows on the rear view: "16. SIM card tray, 17. USB port for troubleshooting and debugging, 19. Charging port"; mower rating IPX6 ([manual PDF](https://robotlawnmowers.com.au/wp-content/uploads/2024/02/Luba_2_AWD_Series_User_Manual_V1.0_EN.pdf)). **Unverified:** whether the port is USB-C, whether it supplies 5 V, how much current, whether it is powered while the mower sleeps, and whether the X variant has the same layout. The `mammotion-rc` project says "plug the HC33 into the mower's USB-C port and power the mower on" without naming the model.
- Board choice, shops and prices are in the [guide](../guides/esp32-bridge-purchase-and-setup.md).

## 4. Recommendation

1. **Connection path.** Phase 1 (MVP): BLE through an ESP32 running the **ESPHome Bluetooth Proxy**, with the Device-Gateway on the home LAN (or reaching the ESP through ESPHome WireGuard). Cloud MQTT stays a second adapter behind `MowerConnection`; it is needed anyway for the camera and is worth testing in this spike because the firmware is past the quota cut-off.
2. **Bridge firmware.** Phase 1: ESPHome Bluetooth Proxy, unchanged. Phase 2: decide after the spike. Prefer ESPHome plus WireGuard if latency and stability are good, because it meets "outbound, works behind any router" without custom firmware. Build the custom MowDeck firmware (outbound WebSocket over TLS, device secret, signed OTA, plus a bleak backend in `mowdeck-control`) only if the spike shows that ESPHome cannot meet the joystick or reconnect targets, or WireGuard is impractical for cloud hosting.
3. **PyMammotion version.** Pin `pymammotion==0.10.9` exactly (same pin as Mammotion-HA today). Pin the proxy helpers to the versions in PyMammotion's lock file: `aioesphomeapi==45.0.3`, `bleak-esphome==3.7.5`, `habluetooth==6.1.0`, `bluetooth-adapters==2.1.1`. Upgrade deliberately, one release at a time, after the owner's spike scripts pass again.
4. **Python version.** `mowdeck-control` needs Python 3.13. Proposal for an ADR: move the workspace to Python 3.13 (Home Assistant also runs on 3.13 or newer). Until then the spike runs in a separate uv project outside the repo.
5. **Firmware-update policy.** Stay on 1.30.29.26 for the whole spike so results are comparable. Afterwards, consider 1.30.31.19 or newer (Wi-Fi control restored, per PyMammotion's reading of the release note) only when a PyMammotion release mentions it and Mammotion-HA issues show no regressions for at least two weeks. Re-run steps 2, 5 and 6 after any update.

## 5. Risks

| ID | Risk | Mitigation |
|---|---|---|
| R1 | Account ban: the PyMammotion README warns that unofficial API use "may result in a permanent ban of your Mammotion account". One ioBroker user reports being blocked for about 24 h after a few requests ([forum](https://forum.iobroker.net/topic/84044/test-mammotion-adapter-f%C3%BCr-mammotion-luba-yuka/73), anecdotal) | Use only the second account, cache tokens, avoid login loops, prefer BLE |
| R2 | Python 3.13 required, workspace is 3.12 | ADR to move to 3.13 |
| R3 | GPL-3.0 PyMammotion vs. Apache-2.0 plan for `mowdeck-control` (ADR 0005): a distributed library that imports PyMammotion is in practice bound by the GPL | Keep the PyMammotion adapter in a separate GPL package (for example `mowdeck-control-mammotion`) and keep the `MowerConnection` interface Apache-2.0; get a licence review |
| R4 | Very fast PyMammotion release pace, API churn, packaging slips (0.10.9 misses the `packaging` dependency) | Exact pin, explicit extra dependencies, spike scripts as regression checks |
| R5 | Bridge on the mower loses home Wi-Fi in parts of the garden | Record Wi-Fi RSSI along the mowing area (step 4); fall back to cloud adapter |
| R6 | Single BLE central: phone app, HA proxy and MowDeck fight over the link (unverified) | Disable the HA integration and keep the phone away during BLE tests |
| R7 | Mower may keep driving when BLE frames stop | Step 6 measures it; deadman must send an explicit stop and the gateway must stop on bridge loss |
| R8 | USB port on the mower unsuitable (no power, sleeps, wrong connector) | Verify before relying on it; fallback is a small power bank in the enclosure |
| R9 | Camera only through Agora cloud, encrypted channels unsupported by `pyagorartc` | Keep the camera behind the cloud adapter; Phase 5 decision |

## 6. What this decides

| Plan question | Decided by | Impact on `mowdeck-control` |
|---|---|---|
| Map geometry usable? | Step 3 | `MowerConnection.get_map()` returns local ENU metres plus a WGS84 transform; Planner works in metres |
| Manual drive and custom boundaries? | Steps 4 and 6, research 3.2 | `DriveSession` as its own object (like PyMammotion D15), not a command; custom shapes need either native zones or closed-loop driving (high risk) |
| Arbitrary paths or only native patterns? | Research 3.2, step 4 | Planner output gets two targets: "native job" (zone hashes plus pattern parameters) and "own route" (later, only if closed-loop driving proves safe) |
| Camera path, locally reachable? | Step 7 | Camera is a cloud-only capability; adapter exposes `capabilities()` |
| BLE latency enough for joystick? | Steps 5 and 6 | Frame interval, deadman timeout and whether joystick is allowed over the bridge |
| HACS entities and services | Research 3.3 | Mirror Mammotion-HA entity set against our public API |
| 4G and net RTK, correction source | Steps 2 and 8 | Telemetry schema: `rtk_status`, `pos_level`, `correction_source`, `net_type`, `mnet_rssi` |
| Bridge firmware | Steps 4 to 6 | Transport adapter: `bleak-esphome` now, maybe own bleak backend later |
| Connections per gateway instance | Step 5 resource figures | First sharding estimate; real load test later |

## 7. Prepared spike task: prerequisites

- [ ] Second Mammotion account can log in to the Mammotion app and sees the LUBA as shared.
- [ ] Find out which account the old Home Assistant integration uses. During the spike, **disable** that integration entry (Settings, Devices and services, Mammotion, three dots, Disable), and do not let Home Assistant adopt the new ESPHome device.
- [ ] Phone app closed and phone Bluetooth off near the mower during BLE steps.
- [ ] Mower firmware is 1.30.29.26 and automatic updates are off in the app.
- [ ] Laptop with Bluetooth LE (Windows 11 or Linux with BlueZ), within 5 m of the mower for local BLE steps.
- [ ] ESP32 bridge flashed as in the guide (needed from step 4 on).
- [ ] Safety for steps 4 and 6: lawn clear of people, children and pets, someone at the mower's red STOP button, blades never switched on by the scripts.
- [ ] A notebook or the results table (section 10) at hand.

## 8. Environment setup

The spike runs outside the MowDeck repo because PyMammotion needs Python 3.13.

Windows (PowerShell):

```powershell
winget install --id=astral-sh.uv -e
uv python install 3.13
mkdir C:\dev\mowdeck-spike-0001; cd C:\dev\mowdeck-spike-0001
uv init --bare --python 3.13
uv python pin 3.13
uv add "pymammotion==0.10.9" packaging psutil "aioesphomeapi==45.0.3" "bleak-esphome==3.7.5" "habluetooth==6.1.0" "bluetooth-adapters==2.1.1"
uv tool install "esphome==2026.9.1"
$env:MAMMOTION_EMAIL = "second-account@example.com"
$env:MAMMOTION_PASSWORD = "..."
$env:ESPHOME_NOISE_PSK = "..."   # the api encryption key from the ESPHome YAML
```

Linux (bash):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.13
mkdir -p ~/mowdeck-spike-0001 && cd ~/mowdeck-spike-0001
uv init --bare --python 3.13
uv python pin 3.13
uv add "pymammotion==0.10.9" packaging psutil "aioesphomeapi==45.0.3" "bleak-esphome==3.7.5" "habluetooth==6.1.0" "bluetooth-adapters==2.1.1"
uv tool install "esphome==2026.9.1"
export MAMMOTION_EMAIL="second-account@example.com" MAMMOTION_PASSWORD="..." ESPHOME_NOISE_PSK="..."
```

`packaging` is listed explicitly because the 0.10.9 wheel imports it (`transport/cloud.py`) without declaring it: on a clean install `import pymammotion` fails with `ModuleNotFoundError: No module named 'packaging'` (reproduced on 2026-10-08). `psutil` is only for step 5. All imports and builder names used below were checked against the installed 0.10.9 package. Run every script with `uv run python <script> ...`. Output goes to `./out/` (JSON, CSV, `spike.log` with full DEBUG). `out/token_cache.json` contains live credentials: never commit it.

Create `common.py` once; all step scripts import it.

```python
"""Shared helpers for MowDeck spike 0001 (PyMammotion 0.10.9)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from pymammotion.client import MammotionClient
from pymammotion.data.model.device import MowingDevice
from pymammotion.transport.base import TransportType

OUT = Path(os.environ.get("SPIKE_OUT", "out"))
OUT.mkdir(exist_ok=True)
TOKEN_CACHE = OUT / "token_cache.json"
log = logging.getLogger("spike")


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    logfile = logging.FileHandler(OUT / "spike.log", encoding="utf-8")
    logfile.setLevel(logging.DEBUG)
    for handler in (console, logfile):
        handler.setFormatter(fmt)
        root.addHandler(handler)


def jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return jsonable(value.to_dict())
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def write_json(name: str, data: Any) -> None:
    path = OUT / name
    path.write_text(json.dumps(jsonable(data), indent=2, default=str), encoding="utf-8")
    log.info("wrote %s", path)


def transport_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--via", choices=["cloud", "ble", "proxy"], default="cloud")
    parser.add_argument("--name", help="device name, e.g. Luba-VPxxxxxx (required for ble/proxy)")
    parser.add_argument("--address", help="mower BLE MAC (required for ble/proxy)")
    parser.add_argument("--proxy", default="mowdeck-bridge.local", help="ESPHome proxy host")
    return parser


async def connect_cloud() -> MammotionClient:
    email, password = os.environ["MAMMOTION_EMAIL"], os.environ["MAMMOTION_PASSWORD"]
    client = MammotionClient("mowdeck-spike-0001")

    async def save_cache() -> None:
        if cache := client.to_cache():
            TOKEN_CACHE.write_text(json.dumps(jsonable(cache)), encoding="utf-8")

    client.on_credentials_updated = save_cache
    restored = False
    if TOKEN_CACHE.exists():
        try:
            cached = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
            await client.restore_credentials(email, password, cached)
            restored = True
            log.info("session restored from token cache")
        except Exception:
            log.warning("token cache restore failed, doing a password login", exc_info=True)
    if not restored:
        await client.login_and_initiate_cloud(email, password)
    await save_cache()
    await asyncio.sleep(3)  # let MQTT subscribe and first reports arrive
    return client


async def _register_ble(client: MammotionClient, name: str, **ble: Any) -> None:
    handle = await client.add_ble_only_device(
        device_id=name, device_name=name, initial_device=MowingDevice(name=name), **ble
    )
    await handle.start()
    transport = handle.get_transport(TransportType.BLE)
    if transport is None:
        raise RuntimeError("handle has no BLE transport")
    t0 = time.perf_counter()
    await transport.connect()
    log.info("BLE connected in %.2f s", time.perf_counter() - t0)


async def connect_ble_proxy(client: MammotionClient, name: str, address: str, host: str) -> None:
    from aioesphomeapi import APIClient
    from bleak_esphome import connect_scanner
    from bluetooth_adapters import get_adapters
    from habluetooth import BluetoothManager, set_manager

    class _NoOpManager(BluetoothManager):
        def _discover_service_info(self, service_info: object) -> None:
            return

    manager = _NoOpManager(get_adapters(), slot_manager=None)
    set_manager(manager)
    await manager.async_setup()
    api = APIClient(
        address=host, port=6053, password=None, noise_psk=os.environ["ESPHOME_NOISE_PSK"]
    )
    await api.connect(login=True)
    info = await api.device_info()
    log.info(
        "proxy %s connected, bt features %s",
        info.name,
        info.bluetooth_proxy_feature_flags_compat(api.api_version),
    )
    scanner = connect_scanner(api, info, available=True).scanner
    await scanner.async_setup()
    manager.async_register_scanner(scanner)
    loop = asyncio.get_running_loop()
    deadline, ble_device = loop.time() + 30, None
    while ble_device is None and loop.time() < deadline:
        for dev, _adv in scanner.discovered_devices_and_advertisement_data.values():
            if dev.address.upper() == address.upper():
                ble_device = dev
        await asyncio.sleep(0.5)
    if ble_device is None:
        raise RuntimeError(f"{address} not seen by proxy {host} within 30 s")
    await _register_ble(client, name, ble_device=ble_device, self_managed_scanning=False)


async def open_session(args: argparse.Namespace) -> tuple[MammotionClient, str]:
    if args.via == "cloud":
        client = await connect_cloud()
        names = [h.device_name for h in client.device_registry.all_devices]
        name = args.name or next((n for n in names if n.startswith("Luba")), None)
        if name is None:
            raise SystemExit(f"no Luba on this account, devices: {names}")
        return client, name
    if not (args.name and args.address):
        raise SystemExit("--name and --address are required for --via ble/proxy (see step 0)")
    client = MammotionClient("mowdeck-spike-0001")
    if args.via == "ble":
        await _register_ble(client, args.name, ble_address=args.address.upper())
    else:
        await connect_ble_proxy(client, args.name, args.address, args.proxy)
    await asyncio.sleep(2)
    return client, args.name


def transport_label(client: MammotionClient, name: str) -> str:
    handle = client.mower(name)
    if handle is None:
        return "none"
    for kind in (TransportType.BLE, TransportType.CLOUD_MAMMOTION, TransportType.CLOUD_ALIYUN):
        if handle.is_transport_connected(kind):
            return kind.value
    return "none"


async def wait_saga_idle(client: MammotionClient, name: str, timeout: float = 300.0) -> None:
    handle = client.mower(name)
    loop = asyncio.get_running_loop()
    start_by, deadline = loop.time() + 3, loop.time() + timeout
    while loop.time() < start_by and not handle.queue.is_saga_active:
        await asyncio.sleep(0.05)
    while handle.queue.is_saga_active:
        if loop.time() > deadline:
            raise TimeoutError("saga did not finish")
        await asyncio.sleep(0.5)
```

The device name matters: PyMammotion derives model-specific behaviour from the name prefix, so always pass the real advertised name for BLE.

## 9. Steps

Save each script as its own file next to `common.py`.

### Step 0: find the mower over BLE (`s00_scan.py`)

```python
import asyncio
from bleak import BleakScanner


async def main() -> None:
    found = await BleakScanner.discover(timeout=15.0, return_adv=True)
    for address, (dev, adv) in found.items():
        name = adv.local_name or dev.name or ""
        if name.startswith(("Luba", "Yuka")):
            print(f"{address}  {name}  rssi={adv.rssi}  services={adv.service_uuids}")


asyncio.run(main())
```

Run: `uv run python s00_scan.py` with the mower switched on.
Expected: one line like `XX:XX:XX:XX:XX:XX  Luba-VP...  rssi=-60` with service `0000ffff-...`.
Record: name, MAC, RSSI at 1 m and at 5 m. Pass: mower found. Fail: not found (check that no phone or HA proxy is connected to it).

### Step 1: cloud login and device list (`s01_login.py`)

```python
import argparse, asyncio
from common import connect_cloud, setup_logging, transport_label, write_json


async def main() -> None:
    setup_logging()
    client = await connect_cloud()
    rows = []
    for h in client.device_registry.all_devices:
        rows.append(
            {
                "name": h.device_name,
                "iot_id": h.iot_id,
                "account": h.account_id,
                "firmware": h.firmware_version,
                "transport": transport_label(client, h.device_name),
            }
        )
        print(rows[-1])
    write_json("s01_devices.json", rows)
    await client.stop()


asyncio.run(main())
```

Expected: the LUBA appears with its `iot_id` and `transport` `cloud_mammotion` (or `cloud_aliyun`). Record: device name, transport type, firmware string, whether the phone app (main account) stays logged in. Pass: device listed and main account unaffected. Fail: login error, 429, or main app logged out.

### Step 2: status, battery, RTK fix (`s02_status.py`)

```python
import argparse, asyncio
from pymammotion.data.model.enums import RTKStatus
from pymammotion.utility.constant.device_enums import WorkMode
from common import open_session, setup_logging, transport_args, transport_label, write_json


async def main() -> None:
    args = transport_args(argparse.ArgumentParser()).parse_args()
    setup_logging()
    client, name = await open_session(args)
    samples = []
    for _ in range(30):  # 60 s
        dev = client.get_device_by_name(name)
        rd, rtk = dev.report_data, dev.report_data.rtk
        s = {
            "transport": transport_label(client, name),
            "battery": rd.dev.battery_val,
            "charge_state": rd.dev.charge_state,
            "mode": WorkMode(rd.dev.sys_status).name,
            "rtk_status": RTKStatus.from_value(rtk.status).name,
            "pos_level": rtk.positioning_mode.name,
            "sats": rtk.gps_stars,
            "l2_sats": rtk.l2_stars,
            "corr_age_s": rtk.age,
            "correction_source": rtk.mqtt_rtk_info.switch_mode.name,
            "pos_deg": (dev.location.device.latitude, dev.location.device.longitude),
        }
        samples.append(s)
        print(s)
        await asyncio.sleep(2)
    write_json(f"s02_status_{args.via}.json", samples)
    await client.stop()


asyncio.run(main())
```

Run with `--via cloud`, then `--via ble --name <name> --address <mac>`.
Expected: battery 0 to 100, a mode such as `MODE_READY` or `MODE_CHARGING`, `rtk_status` `FIX` outdoors, `correction_source` `NRTK`.
Record: values per transport, whether values update between samples. Pass: battery and mode plausible on both transports, RTK fields non-zero. Fail: fields stay at defaults (0) after 60 s.

### Step 3: map geometry to JSON (`s03_map.py`)

```python
import argparse, asyncio
from pymammotion.data.model.generate_geojson import apply_area_geojson
from common import open_session, setup_logging, transport_args, wait_saga_idle, write_json


async def main() -> None:
    args = transport_args(argparse.ArgumentParser()).parse_args()
    setup_logging()
    client, name = await open_session(args)
    await client.start_map_sync(name)
    await wait_saga_idle(client, name, timeout=300)
    dev = client.get_device_by_name(name)
    m = dev.map
    write_json(
        "s03_map_raw.json",
        {
            "area": m.area,
            "obstacle": m.obstacle,
            "path": m.path,
            "no_go_zone": m.no_go_zone,
            "virtual_wall": m.virtual_wall,
            "dump": m.dump,
            "svg": m.svg,
            "area_name": m.area_name,
        },
    )
    write_json(
        "s03_frames.json",
        {
            "rtk_origin_rad": dev.location.RTK,
            "dock": dev.location.dock,
            "device_deg": dev.location.device,
            "rtk_info": dev.report_data.rtk.mqtt_rtk_info,
        },
    )
    if not m.generated_geojson:
        apply_area_geojson(m, dev.location.RTK, dev.location.dock)
    write_json("s03_map.geojson", m.generated_geojson)
    print({k: len(getattr(m, k)) for k in ("area", "obstacle", "path", "no_go_zone", "svg")})
    await client.stop()


asyncio.run(main())
```

Expected: counts matching the zones, obstacles and channels in the app; a GeoJSON file.
Record: counts, sync duration per transport, whether `rtk_origin_rad` is non-zero with network RTK, and whether the GeoJSON lines up with an aerial photo (drag the file into geojson.io). Pass: all zones present and position error under about 1 m visually. Fail: empty map or origin at 0/0.

### Step 4: start, pause, resume, return to dock (`s04_commands.py`)

Mow job on the smallest zone. Take its hash from `s03_map_raw.json` (keys of `area`).

```python
import argparse, asyncio, time
from pymammotion.data.model.device_config import OperationSettings, build_route_information
from pymammotion.messaging.command_queue import Priority
from pymammotion.utility.constant.device_enums import WorkMode
from common import open_session, setup_logging, transport_args, transport_label, write_json


async def main() -> None:
    p = transport_args(argparse.ArgumentParser())
    p.add_argument("--zone", type=int, required=True)
    p.add_argument("--blade-height", type=int, default=60)
    args = p.parse_args()
    setup_logging()
    client, name = await open_session(args)
    log = []

    async def step(label, coro, mode=None):
        t0 = time.perf_counter()
        await coro
        sent = time.perf_counter() - t0
        if mode is not None:
            await client.wait_for(name, lambda d: d.report_data.dev.sys_status == mode, timeout=90)
        log.append(
            {
                "step": label,
                "send_s": round(sent, 3),
                "until_state_s": round(time.perf_counter() - t0, 3),
                "transport": transport_label(client, name),
            }
        )
        print(log[-1])

    dev = client.get_device_by_name(name)
    settings = OperationSettings(
        areas=[args.zone], blade_height=args.blade_height, channel_mode=0, toward=0
    )
    route = build_route_information(name, dev, settings)
    input("Area clear, someone at STOP? Press Enter to plan and start ")
    await step(
        "plan",
        client.send_command_and_wait(
            name,
            "generate_route_information",
            "bidire_reqconver_path",
            generate_route_information=route,
            priority=Priority.USER,
        ),
    )
    await step(
        "start",
        client.send_command_and_wait(
            name, "start_job", "zone_start_precent_t", priority=Priority.USER
        ),
        WorkMode.MODE_WORKING,
    )
    await asyncio.sleep(30)
    await step(
        "pause",
        client.send_command_with_args(name, "pause_execute_task", priority=Priority.USER),
        WorkMode.MODE_PAUSE,
    )
    await asyncio.sleep(10)
    await step(
        "resume",
        client.send_command_with_args(name, "resume_execute_task", priority=Priority.USER),
        WorkMode.MODE_WORKING,
    )
    await asyncio.sleep(10)
    await step(
        "dock",
        client.send_command_with_args(name, "return_to_dock", priority=Priority.USER),
        WorkMode.MODE_RETURNING,
    )
    write_json(f"s04_commands_{args.via}.json", log)
    await client.stop()


asyncio.run(main())
```

Run once with `--via ble ... --zone <hash>`, once with `--via cloud --zone <hash>`; optionally with `--via proxy`, walking along with the laptop is then not needed.
Expected: the mower plans, leaves the dock, mows, pauses, resumes and drives home.
Record: `send_s` and `until_state_s` per step and transport, any error. Pass: all five steps reach their state within 90 s. Fail: timeout or wrong state (note the error text from `spike.log`).

### Step 5: command latency (`s05_latency.py`)

Measures a request/response round trip (`get_device_version_info` answered by `toapp_dev_fw_info`) and the duration of a zero-speed movement write, which is what a joystick frame costs. The mower must be idle; zero speed must not move it (abort with STOP if it does).

```python
import argparse, asyncio, csv, os, statistics, time
import psutil
from pymammotion.messaging.command_queue import Priority
from common import OUT, open_session, setup_logging, transport_args, transport_label


async def main() -> None:
    p = transport_args(argparse.ArgumentParser())
    p.add_argument("-n", type=int, default=100)
    args = p.parse_args()
    setup_logging()
    client, name = await open_session(args)
    rows = []
    for i in range(args.n):
        for kind in ("rtt", "move0"):
            t0 = time.perf_counter()
            ok = True
            try:
                if kind == "rtt":
                    await client.send_command_and_wait(
                        name,
                        "get_device_version_info",
                        "toapp_dev_fw_info",
                        send_timeout=3.0,
                        priority=Priority.USER,
                    )
                else:
                    await client.send_command_with_args(
                        name,
                        "send_movement",
                        linear_speed=0,
                        angular_speed=0,
                        priority=Priority.USER,
                    )
            except Exception as exc:
                ok = False
                print(i, kind, "error", exc)
            rows.append(
                {
                    "i": i,
                    "kind": kind,
                    "ms": round((time.perf_counter() - t0) * 1000, 1),
                    "ok": ok,
                    "transport": transport_label(client, name),
                }
            )
        await asyncio.sleep(0.2)
    with open(OUT / f"s05_latency_{args.via}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    for kind in ("rtt", "move0"):
        ms = sorted(r["ms"] for r in rows if r["kind"] == kind and r["ok"])
        if ms:
            print(
                kind,
                "n",
                len(ms),
                "p50",
                statistics.median(ms),
                "p95",
                ms[int(len(ms) * 0.95) - 1],
                "max",
                ms[-1],
            )
    proc = psutil.Process(os.getpid())
    print("rss_mb", round(proc.memory_info().rss / 1e6, 1), "cpu_s", sum(proc.cpu_times()[:2]))
    await client.stop()


asyncio.run(main())
```

Run in this order: `--via ble` (laptop at 2 m), `--via proxy` (ESP32 next to the mower, laptop indoors), `--via proxy` with the ESP32 mounted on the mower at the far end of the lawn, `--via cloud`.
Record: p50, p95, max and error count per run, plus the ESP32 Wi-Fi RSSI sensor value from `esphome logs`, and the process `rss_mb`. Pass for joystick over the bridge: `move0` p95 below 250 ms, no single value above 1000 ms, no errors in 100 frames. Fail otherwise. These thresholds are a proposal, derived from the 1 s deadman and the app's 150 ms pacing.

### Step 6: manual drive with deadman (`s06_drive.py`)

BLE only (`--via ble` or `--via proxy`). Blades stay off. Put the mower on open lawn, not in the dock.

```python
import argparse, asyncio, csv, time
from pymammotion.messaging.command_queue import Priority
from common import OUT, open_session, setup_logging, transport_args

FRAME_S, DEADMAN_S = 0.15, 1.0
STOP = ("send_movement", {"linear_speed": 0, "angular_speed": 0})


async def main() -> None:
    args = transport_args(argparse.ArgumentParser()).parse_args()
    setup_logging()
    client, name = await open_session(args)
    rows, t_start = [], time.perf_counter()
    stick = {"cmd": STOP, "t": 0.0}  # last stick input and its time
    sender_on = True  # False simulates a dead bridge

    async def send(cmd, kw, label):
        t0 = time.perf_counter()
        await client.send_command_with_args(name, cmd, priority=Priority.USER, **kw)
        loc = client.get_device_by_name(name).location.device
        rows.append(
            {
                "t": round(t0 - t_start, 3),
                "label": label,
                "cmd": cmd,
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "lat": loc.latitude,
                "lon": loc.longitude,
            }
        )

    async def sender():  # the deadman loop, as the gateway would run it
        stopped = True
        while True:
            if sender_on:
                if time.perf_counter() - stick["t"] <= DEADMAN_S and stick["cmd"] is not STOP:
                    await send(*stick["cmd"], "drive")
                    stopped = False
                elif not stopped:
                    await send(*STOP, "deadman_stop")
                    stopped = True
            await asyncio.sleep(FRAME_S)

    async def hold(cmd, kw, seconds):  # simulated stick held, input every 100 ms
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            stick.update(cmd=(cmd, kw), t=time.perf_counter())
            await asyncio.sleep(0.1)

    task = asyncio.create_task(sender())
    input("Lawn clear, blades off, hand on STOP? Enter for A: forward 2 s, then release ")
    await hold("move_forward", {"linear": 0.35}, 2.0)
    stick["cmd"] = STOP
    await send(*STOP, "release")
    input(
        "Enter for B: left turn 2 s, then input stops without release (deadman must stop it after 1 s) "
    )
    await hold("move_left", {"angular": 0.35}, 2.0)
    await asyncio.sleep(2.0)
    input(
        "Enter for C (dead bridge): forward 2 s, then NO frames for 5 s. Watch if it keeps rolling "
    )
    await hold("move_forward", {"linear": 0.35}, 2.0)
    sender_on = False
    await asyncio.sleep(5.0)
    stick["cmd"] = STOP
    sender_on = True
    await send(*STOP, "safety_stop")
    task.cancel()
    with open(OUT / f"s06_drive_{args.via}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    await client.stop()


asyncio.run(main())
```

Expected: A stops promptly on release; in B the script's deadman sends a stop about 1 s after the last input (`deadman_stop` row). C is the key observation: does the mower stop by itself when no frames arrive at all, and after how long? The script sends a safety stop after 5 s.
Record: perceived stop delay after release (stopwatch or video), C: "stops after x s" or "keeps rolling until explicit stop", max `ms` per frame. Pass: A and B smooth, A stops within 0.5 s of release, B stops within 1.5 s of the last input. C has no pass or fail; it decides whether the deadman can rely on a device-side timeout (it must not if the mower keeps rolling).

### Step 7: camera stream (`s07_camera.py`)

```python
import asyncio
from common import connect_cloud, setup_logging, write_json


async def main() -> None:
    setup_logging()
    client = await connect_cloud()
    handle = next(h for h in client.device_registry.all_devices if h.device_name.startswith("Luba"))
    resp = await client.get_stream_subscription(handle.device_name, handle.iot_id)
    data = resp.data if resp is not None else None
    video = await client.mammotion_http.get_video_resource(handle.iot_id)
    summary = {
        "code": getattr(resp, "code", None),
        "has_token": bool(data and data.token),
        "channel": bool(data and data.channelName),
        "open_encrypt": data.openEncrypt if data else None,
        "cameras": len(data.cameras) if data else 0,
        "available_time": data.availableTime if data else None,
        "video_quota": video.data if video else None,
    }
    print(summary)
    write_json("s07_camera.json", summary)
    await asyncio.sleep(5)
    await client.stop_stream(handle.device_name)
    await client.stop()


asyncio.run(main())
```

Then check local reachability: find the mower's IP in the router (it is on Wi-Fi only when Wi-Fi is configured) and run `nmap -p- -T4 <mower-ip>` (install nmap first).
Record: `has_token`, `open_encrypt`, number of cameras, quota, open TCP ports. Pass: token received. "Locally reachable" is yes only if a port such as 554 (RTSP) is open and serves video; otherwise record "cloud only".

### Step 8: 4G and network RTK fields (`s08_net.py`)

```python
import argparse, asyncio
from common import open_session, setup_logging, transport_args, write_json


async def main() -> None:
    args = transport_args(argparse.ArgumentParser()).parse_args()
    setup_logging()
    client, name = await open_session(args)
    await client.refresh_status(name)
    await asyncio.sleep(10)
    dev, handle = client.get_device_by_name(name), client.mower(name)
    rd, rtk = dev.report_data, dev.report_data.rtk
    mnet = rd.dev.mnet_info
    out = {
        "connect": rd.connect,
        "sim": mnet.sim,
        "link_type": mnet.link_type,
        "mnet_rssi": mnet.rssi,
        "operator": mnet.operator,
        "inet_connected": mnet.inet.connect,
        "fpv_info": rd.dev.fpv_info,
        "rtk_switch": rtk.mqtt_rtk_info.rtk_switch,
        "nrtk_net_mode": rtk.mqtt_rtk_info.nrtk_net_mode,
        "nrtk_map_convert_status": rtk.mqtt_rtk_info.nrtk_map_convert_status,
        "dis_status": rtk.get_dis_status(),
        "score": rtk.score_info,
        "supports_wifi_movement": handle.supports_wifi_movement(),
    }
    print(out)
    write_json(f"s08_net_{args.via}.json", out)  # IMEI/ICCID are not written
    await client.stop()


asyncio.run(main())
```

Run with `--via cloud`, then again after switching the mower's home Wi-Fi off in the app (mower on 4G only), if the app allows it.
Record: SIM state, link type, RSSI, `used_net`, `rtk_switch`, whether cloud commands from step 4 still work on 4G only. Pass: fields populated and `rtk_switch` reports network RTK.

## 9a. Running the spike with the adapter

Since Task 13a the MowDeck adapter itself can run the cloud steps, from the repository (Python 3.13 workspace, no separate spike project needed): `libs/mowdeck-control-mammotion/scripts/try_real_mower.py` connects through `mowdeck-control-mammotion` and the `MowerController` exactly as the Device-Gateway does, and appends every step to `out/try_real_mower.jsonl` for the results table below.

| Spike step | Script call |
| --- | --- |
| 1 Login, 2 Status, 3 Map | `uv run python libs/mowdeck-control-mammotion/scripts/try_real_mower.py` (capabilities, state with battery, activity, RTK status and source, firmware, map summary) |
| 4 Commands | `... --start-area "<area name>"`, then `... --dock`; `... --estop` checks the emergency stop sequence |
| 2 to 4 over the proxy | `... --esphome <host>,<noise_psk>,<MAC>` once the ESP32 is flashed |
| 7 Camera | `... --camera` (the stream token as a redacted session and the camera list), then the web app, see "Camera: running it" below |

Environment: `MAMMOTION_ACCOUNT`, `MAMMOTION_PASSWORD` (the second account) and `MAMMOTION_DEVICE_NAME`; note that the stand-alone scripts above use `MAMMOTION_EMAIL` instead. `--dry-run` exercises the same path on a fake mower without credentials. Details and the safety notes: "Testing with a real mower" in `libs/mowdeck-control-mammotion/README.md`. The latency and manual drive steps (5 and 6) still use the stand-alone scripts; the camera step (7) can use either.

### Camera: running it

Since Task 25 MowDeck shows the camera itself ([ADR 0032](../adr/0032-live-camera-over-agora.md)): the adapter fetches the Agora stream token through PyMammotion and the browser joins the channel directly with the Agora Web SDK, so encrypted channels can work (the step 7 script and Mammotion-HA join on the server, where encryption shows no picture).

1. `uv run python libs/mowdeck-control-mammotion/scripts/try_real_mower.py --camera` prints the session with the token, key and salt redacted and the cameras with their publisher uids. Record in the results table: `encryption` set or not, the cameras, `expires_at` minus now (the token lifetime) and the `quota` numbers next to the 4G allowance the app shows.
2. Start the stack with `WEB_CAMERA_ENABLED=true` in `infra/compose/.env`, register the mower with the `cloud` connection, open the web app over HTTPS (Tailscale) or `http://localhost`, open the mower and press "Live camera". Note whether the picture appears, the time to the first frame, whether the camera picker switches, and whether the session renews (the panel counts renewals) when the token's lifetime is short.

Details: "Live camera" in [the web app guide](../dev/web-app.md#live-camera) and in `libs/mowdeck-control-mammotion/README.md`.

Correction found while building the adapter: in `connect_ble_proxy` (section 8), `scanner.async_setup()` of habluetooth 6.1.0 is synchronous and returns a teardown callback; call it without `await` (the adapter's `esphome.py` does).

## 10. Results template

| Step | Transport | Key values | Pass/Fail | Notes |
|---|---|---|---|---|
| 0 Scan | local BLE | name, MAC, RSSI 1 m / 5 m | | |
| 1 Login | cloud | transport type, firmware, main app still logged in? | | |
| 2 Status | cloud / BLE / proxy | battery, mode, RTK status, correction source | | |
| 3 Map | cloud / BLE | zones, obstacles, paths, sync time, origin non-zero, alignment | | |
| 4 Commands | BLE / cloud / proxy | send_s and until_state_s per command | | |
| 5 Latency | BLE | rtt p50/p95/max, move0 p50/p95/max, errors, rss_mb | | |
| 5 Latency | proxy near | same, ESP Wi-Fi RSSI | | |
| 5 Latency | proxy on mower, far end | same, ESP Wi-Fi RSSI | | |
| 5 Latency | cloud | same | | |
| 6 Drive A/B | BLE / proxy | stop delay after release | | |
| 6 Drive C | BLE / proxy | self-stop after x s or keeps rolling | | |
| 7 Camera | cloud | token, encrypt flag, cameras, quota, open ports | | |
| 8 Net/RTK | cloud | SIM, link type, RSSI, rtk_switch, 4G-only control | | |
| Bridge power | hardware | USB port type, voltage, powered while sleeping? | | |

After the run, add the filled table to this document under a new section "Results", answer the checklist in the plan, and write the ADRs that follow from section 6 (bridge firmware, Python 3.13, licence split).
