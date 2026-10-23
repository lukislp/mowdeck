# Home Assistant integration

The Home Assistant (HACS) integration `mowdeck` lives in its own repository, [`lukislp/mowdeck-hacs`](https://github.com/lukislp/mowdeck-hacs). It was developed in this monorepo under `integrations/home-assistant` and moved out; the first entry of its `CHANGELOG.md` names the last monorepo commit. Decision record: [ADR 0025](../adr/0025-home-assistant-integration-as-api-client.md).

## Installation

HACS: add `https://github.com/lukislp/mowdeck-hacs` as a custom repository of type Integration (once that repository is public), install **MowDeck** and restart Home Assistant. Without HACS, copy `custom_components/mowdeck` into `<config>/custom_components/`. The repository README describes the API token (Settings, API tokens), the configuration, the entities (including the camera light switch and the area select with MowDeck's area names), the actions and troubleshooting.

## Contract

The integration is a pure client of the public MowDeck API and shares no code with this repository. The OpenAPI documents generated here (guarded by the contract drift tests) are therefore its contract: a change of the public API that is not backward compatible (ADR 0009) needs a matching release of the integration. Development, tests and releases of the integration are described in its own repository.

## Nudge and remote drive

Mammotion-HA offers four emergency nudge buttons and a cloud "remote drive" session. MowDeck covers both with its existing manual driving (`ManualDrive`, ADR 0026) and needs no API of its own for them. The integration maps them like this.

### Nudge buttons

| Mammotion-HA | MowDeck |
| --- | --- |
| `emergency_nudge_forward` (`move_forward(0.4)`) | `POST /api/v1/fleet/mowers/{id}/commands` with `{"command": {"kind": "manual_drive", "linear": 0.4, "angular": 0, "ttl_ms": 500}}` |
| `emergency_nudge_back` (`move_back(0.4)`) | the same with `"linear": -0.4` |
| `emergency_nudge_left` (`move_left(0.4)`) | the same with `"linear": 0, "angular": 0.4` (left is positive) |
| `emergency_nudge_right` (`move_right(0.4)`) | the same with `"linear": 0, "angular": -0.4` |

Mammotion-HA sends one movement frame per press, with the blades untouched. Over its cloud session it sends the frame and then a zero frame 0.5 s later. In MowDeck a single `manual_drive` frame does the same:

- The controller opens a drive session and sends the frame.
- After `ttl_ms` the dead-man switch stops the wheels and closes the session.
- The stick scale is the same: MowDeck's `linear` 0.4 gives the wire values of PyMammotion's `move_forward(0.4)` (`mapping.movement_wire`).

ManualDrive's safety limits apply to a nudge unchanged:

- `ttl_ms` must lie within 100 to 2000 ms. Fleet drops a frame that waits longer than 2 s for delivery.
- The blades stay off: `blade_on` defaults to false, and blades need `manual_drive_blades`.
- A nudge needs the mower's `can_drive` relation and `MowerCapabilities.manual_drive`.
- It is refused during a sensor stop (lift, tilt, collision). It is accepted during a drive-recoverable error (stuck, wheel slip), which is the case the buttons are for.
- It is refused while a start or a return waits for its confirmation, and while a spot run, a drive calibration or a firmware update runs.
- A nudge for a mower that is mowing or returning is rejected (`pause or end the job before driving manually`). Pause the mower first.

Reverse is offered only when `MowerCapabilities.drive_reverse` is true. Mammotion-HA allows no reverse over its cloud session either.

### Remote drive

Mammotion-HA's cloud remote drive works in four steps:

1. Its `remote_drive` switch requests an FPV control token.
2. Its `confirm_remote_drive` button confirms the vendor's safety notice.
3. Its `acknowledge_remote_drive_fence` button resumes after the mower stopped at its geofence.
4. Frames go over the vendor cloud, forward and turns only.

In MowDeck the session is the drive session of the realtime socket: `drive_start`, then `drive_frame` messages, then `drive_stop` (ADR 0026). It needs `can_drive`, the plan feature `manual_drive` and `MowerCapabilities.manual_drive`. Starting it is the user's explicit action, so there is no separate confirm step. The emergency stop ends it at any time.

For Mammotion mowers MowDeck drives over the Bluetooth bridge (ADR 0038), which can also reverse. The vendor's cloud remote-drive transport (PyMammotion's `RemoteDriveSession`, firmware 1.30.31.19 or newer) is not implemented in the adapter. Over the cloud alone, `manual_drive` is therefore false and the nudge and drive controls should stay hidden. Its fence acknowledgement has no counterpart, because MowDeck's manual driving does not use the vendor fence.