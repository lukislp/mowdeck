# Home Assistant integration

The Home Assistant (HACS) integration `mowdeck` lives in its own repository, [`lukislp/mowdeck-hacs`](https://github.com/lukislp/mowdeck-hacs). It was developed in this monorepo under `integrations/home-assistant` and moved out; the first entry of its `CHANGELOG.md` names the last monorepo commit. Decision record: [ADR 0025](../adr/0025-home-assistant-integration-as-api-client.md).

## Installation

HACS: add `https://github.com/lukislp/mowdeck-hacs` as a custom repository of type Integration (once that repository is public), install **MowDeck** and restart Home Assistant. Without HACS, copy `custom_components/mowdeck` into `<config>/custom_components/`. The repository README describes the API token (Settings, API tokens; see [API token](#api-token) below), the configuration, the entities (including the camera light switch and the area select with MowDeck's area names), the actions and troubleshooting.

## API token

The integration authenticates with an API token of the account. Create it in the web app under **Settings, API tokens** (`/settings#api-tokens`) and pick the rights:

| Preset | Permissions | In Home Assistant |
| --- | --- | --- |
| Nur ansehen | `mower.view` | read only: states, map, cameras, health, notifications (an account-wide `mower.view` implies `notifications.view`) |
| Home Assistant – Grundfunktionen (default) | `mower.view`, `mower.control` | also start, pause, dock, end job, blade height, mowing speed, light, **Run now** |
| Home Assistant – vollständig | adds `mower.drive`, `fleet.manage`, `schedule.edit` | everything the integration offers (mowdeck-hacs 0.17 and newer) |
| Eigene Auswahl | any keys an API token may hold | as chosen |

What the three extra keys unlock:

- `mower.drive`: the nudge buttons, `mowdeck.spot_mow` (with `mower.control`), **Start AI analysis** and the Vision notifications. It moves the mower directly.
- `fleet.manage`: the mower's own settings, the maintenance buttons (**Blade changed**, the blade change reminder), installing mower firmware and the RTK base station's firmware. It changes devices and can update firmware.
- `schedule.edit`: the schedule switches and **Skip next run**.

The web app marks `mower.drive` and `fleet.manage` as powerful rights. Basics stays the default: choose full only when Home Assistant should do all of the above. Every token from the web app is granted on the whole account. A grant on a site, group or single mower is possible through the API (`POST /api/v1/identity/accounts/{account_id}/api-tokens`, one entry in `grants` per level).

The integration asks which of these the token holds (`POST /api/v1/identity/me/permissions`) and creates only the entities whose action it may run. A token keeps the rights it was created with: to add rights, create a new token, enter it with **Reconfigure** on the integration and revoke the old one. The permission table of the integration is in the [mowdeck-hacs README](https://github.com/lukislp/mowdeck-hacs#create-an-api-token-in-mowdeck). Keep it and the presets in `apps/web/src/lib/settings/token-permissions.ts` in step: when the integration needs a new key, add it to the full preset.

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

Reverse is offered only when `MowerCapabilities.drive_reverse` is true. Mammotion-HA allows no reverse over its cloud session either, and neither does MowDeck's cloud drive ([ADR 0049](../adr/0049-manual-driving-over-the-mammotion-cloud.md)): a backwards nudge is rejected with "this drive link cannot drive backwards ...", so hide the back button while `drive_reverse` is false. Over the cloud the first nudge of a session also requests the vendor's control token (about a second); the dead-man time of the frame and the adapter's watchdog send the zero frame.

### Remote drive

Mammotion-HA's cloud remote drive works in four steps:

1. Its `remote_drive` switch requests an FPV control token.
2. Its `confirm_remote_drive` button confirms the vendor's safety notice.
3. Its `acknowledge_remote_drive_fence` button resumes after the mower stopped at its geofence.
4. Frames go over the vendor cloud, forward and turns only.

In MowDeck the session is the drive session of the realtime socket: `drive_start`, then `drive_frame` messages, then `drive_stop` (ADR 0026). It needs `can_drive`, the plan feature `manual_drive` and `MowerCapabilities.manual_drive`. Starting it is the user's explicit action, so there is no separate confirm step. The emergency stop ends it at any time.

For Mammotion mowers MowDeck drives over the Bluetooth bridge when it is connected (ADR 0038, with reverse), else over the vendor's cloud remote drive (PyMammotion's `RemoteDriveSession`, firmware 1.30.31.19 or newer, forwards and turns only, [ADR 0049](../adr/0049-manual-driving-over-the-mammotion-cloud.md)). `manual_drive` is true with either; `drive_reverse` only with the bridge. Cloud driving needs the Mammotion account that owns the mower: with a shared account every drive (joystick, nudge, AI assist, spot run) is refused with "cloud driving needs the mower's owner account ...". The vendor's fence warning (the mower 5 m or more beyond its map) ends the MowDeck session with that reason; starting a new session within 5 minutes is the acknowledgement, so no separate button is needed.