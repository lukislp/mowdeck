# ADR 0049: Manual driving over the Mammotion cloud

## Status

Accepted (2026-10-24). Amends [ADR 0038](0038-dual-link-per-mower-and-firmware-updates.md) (link roles: which link drives) and [ADR 0019](0019-mammotion-adapter-transports.md) (manual driving was Bluetooth only).

## Context

Until now a Mammotion mower could only be driven over Bluetooth: the BLE bridge (`ble_bridge`) or a cloud mower's bridge link (ADR 0038). The vendor's cloud remote drive ("FPV control", PyMammotion's `RemoteDriveSession`) had never been run against a mower, and the owner's firmware was below 1.30.31.19, which restores movement over Wi-Fi and 4G.

On 2026-10-24 the owner's LUBA 2 AWD X (firmware 1.30.31.19, PyMammotion 0.10.9, pinned) was driven over the cloud end to end with a local test script: `remote_drive_session(device_name)` without the video gate, `start()` (control token), the safety notice, `confirm()`, `drive(linear, angular)`, `drive(0, 0)`, `stop()` (a forced zero frame and the token release). The mower moved visibly. Measured and observed:

- The token is only granted to the mower's **owner account**. A shared account gets envelope code 50803 ("No permission to operate the device"); PyMammotion reports it as `token_unavailable` with that detail.
- The grant: `expire_in` 300 s, `timeout_exit` 180 s (the server ends an idle session after that, not after PyMammotion's 10 s default), `latency_threshold` 2000 ms, `fps_4g` 1.0. Acknowledgement round trip median about 244 ms (max about 430 ms), `measured_delay` 160 to 360 ms, one cloud send 200 to 470 ms.
- The wire units are the vendor app's stick transform, exactly MowDeck's `mapping.movement_wire` (15 % dead zone, x10 linear, x4.5 angular). Stick 0.3 (wire 150) moved the mower about 5.6 cm in 1.5 s by the cloud position reports (coarse, every few seconds). Wire values like 3 or 5 do nothing.
- **No reverse** over the cloud: the vendor app cannot, and PyMammotion sends a negative linear speed as 0.
- **The link keeps the mower moving.** After every acknowledgement PyMammotion re-sends the last non-zero speed every 150 ms by itself, and the server exits only after 180 s idle. Without an explicit zero frame the mower keeps driving.
- The vendor stops the mower and drops input when it is `APPROACH_FENCE_DISTANCE_M` (5 m) or more beyond its map, until the warning is acknowledged (`acknowledge_fence_warning`).
- The vendor app offers it only when `supports_wifi_movement()` (function code 002.002) and a map exist (`bol_hash` above 1), and while the 4G video quota lasts (MowDeck does not use the video gate).
- The same Mammotion account logged in by two clients at once makes both lose their MQTT session.

The owner's decision: driving over the cloud works "in principle the same as over Bluetooth": the joystick and drive view, the Home Assistant nudges, executing an AI assist proposal and spot mowing use it whenever the cloud is the drive link, blades included.

## Decision

### Adapter (`libs/mowdeck-control-mammotion`)

- **Port.** `MammotionPort.remote_drive_supported()` (PyMammotion's `supports_wifi_movement`) and `remote_drive()` (a `RemoteDriveLink` around PyMammotion's session, `ClientRemoteDrive`, `require_video` off). Events reach the adapter as `RemoteDriveInfo(kind, detail)`.
- **Session.** `CloudDriveSession` implements `DriveSession`: `begin()` requests the token, confirms the safety notice at once (starting a MowDeck drive session is the user's explicit action, ADR 0026) and sends, like the Bluetooth session, one `operate_on_device` with the blades off and `max_run_speed`. Frames use `movement_wire`. `update()` never blocks the controller: it stores the newest wire values and a sender task hands them to the vendor session (which keeps one frame in flight and sends the newest pending one after the ack).
- **Offered** (`capabilities.manual_drive`) on a cloud connection while the connection option `cloud_drive` is on (default), the mower supports the remote drive and has a map. `capabilities.drive_reverse` is false. Blades as over the bridge: the cloud connection options `allow_manual_blades`, `manual_blade_height_mm` and `manual_drive_speed_mps` (Fleet's cloud schema declares them, and the drive calibration keys, so a cloud-only mower stores its calibration).
- **Blades.** `operate_on_device` (`DrvMowCtrlByHand`) is a separate driver message next to the remote-drive frames (`DrvSessionCtrlReq`, which carry no blade field), so a frame cannot override it; it goes over the cloud transport like every command of a cloud connection. A failed switch-on refuses the frame ("the blades could not be switched on over the Mammotion cloud"), a switch-off is tried at every stop, end and vendor event and never raises. Whether the mower accepts `DrvMowCtrlByHand` over the cloud during a remote-drive session is not yet verified on hardware (test plan step 7).

### Safety layers

1. **Controller** (unchanged rules, one new): the dead-man timer of every frame's `ttl_ms` calls `stop()` and `close()`; blade rules; sensor stop. New: a frame with `linear < 0` is `rejected` without `capabilities.drive_reverse` (`"this drive link cannot drive backwards ..."`), never sent with the backwards part cut off.
2. **`FrameDeadlineCapable`** (new optional protocol in `mowdeck-control`): after every accepted frame the controller tells the session the frame's ttl (`expect_next_frame_within`). The cloud session's watchdog sends a zero frame and switches the blades off when that deadline passes without a newer frame, independent of the controller's timer. A frame without a deadline gets 1 s.
3. **Explicit zero on every end.** `stop()` sends `drive(0, 0)` and blades off; `close()` stops and releases the token (the vendor's `stop()` sends its own forced zero frame); every vendor end (refused token, another driver, slow network, token expiry, out of the map, idle exit, the app taking over over Bluetooth) marks the session ended, switches the blades off and makes the next frame fail with the reason. The connection's `disconnect` closes an open session; the emergency stop closes it **before** its own steps (`pause_execute_task`, `send_movement(0, 0)`, `operate_on_device` blades off and `max_run_speed` 0). PyMammotion's handle also stops the session when the client stops.
4. **Sensor stop.** Like the controller (rule 8): an error the adapter marks `drive_recoverable` (stuck, obstacle, wheel slip) admits frames, any other error stops the session. The Bluetooth session had refused every error, which blocked the recovery the rule allows; it now follows the same rule (one shared `_sensor_stop_reason`).

### Errors

| Situation | Error | Reason text (user facing) |
| --- | --- | --- |
| Token refused with 50803 | `CommandRejected` | "cloud driving needs the mower's owner account: the linked Mammotion account only has the mower shared (code 50803); link the account that owns the mower or use the Bluetooth bridge" |
| Other token refusal | `CommandRejected` | "the Mammotion cloud gave no drive token (code N)" |
| Another driver | `CommandRejected` | "someone else is driving the mower (the Mammotion app or another session)" |
| No cloud transport | `NotConnected` | "no usable Mammotion cloud connection for driving" |
| Slow network, token expiry, send failure, app over Bluetooth | `NotConnected` (a spot run ends `link_lost`) | the vendor reason |
| Idle exit, out of map, not ready | `CommandRejected` | the vendor reason |
| Option off / mower unsupported | `CommandRejected` | "manual driving over the cloud is switched off ..." / "this mower cannot be driven over the Mammotion cloud (firmware 1.30.31.19 or newer and a map are needed) ..." |

No account name or token is logged or shown (the masked holder of `occupied_by_other` is dropped).

### Fence warning

MowDeck's Realtime protocol has no in-session acknowledgement (the drive channel knows `drive_frame_ack` and `drive_ended`). Rather than a new message type, the warning ends the session: the vendor already stopped the mower, the adapter releases the token and the next frame fails with "the mower is 5 m or more beyond its mapped area (vendor fence warning) and stopped; start driving again within 5 minutes to acknowledge it and drive the mower back", which Realtime shows as the end reason. A session started within `FENCE_ACK_WINDOW_S` (300 s) of such an end counts as the acknowledgement: it acknowledges the next warning at once, so the mower can be driven back. A spot run stays inside the map's geofence, so it does not reach the warning.

### Which link drives (amends ADR 0038)

On a cloud mower with a bridge link: **the bridge when its proxy link is up, else the cloud** when it can drive. The choice is made when a session opens and never changes while it runs. With the proxy up a session waits for Bluetooth as before and refuses when it does not come up (no fallback then: the capabilities promised reverse). A Bluetooth session ends when Bluetooth drops (its frames never fall back to the cloud); the next one may use the cloud. The capabilities follow the link a new session would use: `manual_drive` (bridge or cloud), `drive_reverse` (bridge only), `manual_drive_blades` (the bridge link's option, else the cloud connection's). The web drive page no longer blocks a session while the bridge is down when `manual_drive` stays true.

### Consumers

- **Joystick and drive view**: unchanged; the pilot already cuts backwards deflection without `drive_reverse` and marks the lower half of the pad off.
- **Home Assistant nudges**: a single `manual_drive` frame with `ttl_ms` 500 opens a cloud session (a token request first, so the first nudge takes about a second longer), drives, and the dead-man timer and the watchdog stop it. "Back" is refused with the no-reverse reason; the integration should hide it without `drive_reverse`, as its README says already.
- **AI assist** (Vision): the context's `motion.reverse` tells the model whether it may plan backwards; a plan with a backwards step is not offered without reverse (`reverse_problem`, the dock fallback may be offered instead), and the executor refuses an approved plan with a backwards step when the link lost reverse since (nothing is driven). The fake provider plans forwards only then.
- **Spot runs and drive calibration**: allowed over the cloud with the same rules. The run's own safety covers the slower cloud: a pose older than `max_pose_age_s` (2.5 s) pauses the run with zero frames and `rtk_wait_s` (30 s) without a usable pose ends it `rtk_lost`. While a cloud session is open the refresh loop asks for an undebounced report whenever the last one is older than a second; a calibration cannot ask faster over the cloud (`CLOUD_MIN_DRIVE_POLL_S` 1 s, the server bans clients that ask too often). `multi_point` turns fall back to `arc` without reverse (ADR 0043). Whether the cloud delivers poses often enough for a smooth run is open (hardware test plan step 8); if not, the run pauses and ends `rtk_lost` instead of driving blind.

### Simulator and fakes

`FakeRemoteDrive` plays PyMammotion's session (refusal with a code, another driver, accepted frames with the vendor's reverse clamp, the forced zero of `stop`, fence warning, vendor ends); `FakeMammotionPort.cloud_drive` switches the support. The fake Mammotion cloud offers cloud driving on every mower; an account whose local part starts with `shared` (`shared-...@fake.mowdeck.test`) is refused with code 50803. The simulator already had `drive_reverse`.

## Consequences

- A cloud-only Mammotion mower can be driven, recovered by AI assist and run spot jobs without a bridge, forwards and turning only, with about 0.3 s more delay and a token request per session.
- The linked Mammotion account must be the owner account for driving. With the owner account in MowDeck, the vendor app on the phone logged in with the same account can drop MowDeck's MQTT session and the other way round; owner test scripts must not use MowDeck's account.
- Each nudge requests and releases a token. The 180 s server idle exit ends a long pause in a joystick session; the next frame says so and the user starts again.
- New contract surface: none in the API or events. `mowdeck-control` gains `FrameDeadlineCapable` and the controller's reverse rule; Fleet's cloud schema gains four options and the calibration keys.
- Open (hardware): blades over the cloud during a session, the report rate during a cloud session (spot runs), the turn direction of `angular` over the cloud, the fence warning on the real mower, the emergency stop latency over 4G.
