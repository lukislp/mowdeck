# device-gateway

Holds the connections to mowers, executes commands and reports their state. It is the only MowDeck service that talks to mowers. It connects simulated mowers and, through the adapter packages installed in the image (today the GPL-3.0 package [`mowdeck-control-mammotion`](../../libs/mowdeck-control-mammotion/README.md)), real mowers over a vendor cloud (`cloud`) and over an ESPHome BLE proxy (`ble_bridge`). Adapters plug in only through entry points: connection factories through the `mowdeck-control` registry (`mowdeck.connections`, [ADR 0019](../../docs/adr/0019-mammotion-adapter-transports.md)) and vendor clouds as provider plugins (`mowdeck.cloud_providers`, [ADR 0066](../../docs/adr/0066-cloud-provider-plugins-and-more-vendors.md)); the service imports no vendor package.

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; layer rules in `docs/dev/service-template.md`. Design decisions: [ADR 0012](../../docs/adr/0012-mower-control-interface-and-safety-rules.md) (controller and safety rules) and [ADR 0013](../../docs/adr/0013-device-gateway-registry-and-sharding.md) (registry, sharding, concurrency).

## Purpose

- Keep a **volatile registry** of the mowers of this instance's shard: which mowers exist, their account, connection kind and options. Nothing is stored; the registry is rebuilt on every start.
- For each mower, create the connection through the `mowdeck-control` registry, wrap it in a `MowerController` (expiry, de-duplication, emergency stop priority, dead-man switch, blade rules) and reconnect with backoff when the link drops.
- Execute commands from `command.mower.<id>` and publish their results; publish telemetry and connection events, and the map and capabilities of every mower as snapshots.
- Answer live camera session requests for its mowers (credentials only; the video never passes through MowDeck) and camera release requests that tell a mower to stop publishing its stream (ADR 0045).
- List the enabled cloud providers with their credential fields for Fleet (`gateway.cloud.providers`), log in to linked vendor accounts through the provider plugins and list their devices (`gateway.cloud.discover`), and let each plugin keep **one cloud session per vendor account** that its mowers and the discovery share ([ADR 0034](../../docs/adr/0034-linked-cloud-accounts-and-device-discovery.md), [ADR 0066](../../docs/adr/0066-cloud-provider-plugins-and-more-vendors.md)).
- Scales with the number of connected mowers: one instance per shard.

## Interfaces

All routes are mounted under `/api/v1` (OpenAPI at `/api/v1/openapi.json`). Traefik routes `/api/v1/device-gateway` to this service.

| Method | Path | Permission | Description |
| --- | --- | --- | --- |
| GET | `/api/v1/device-gateway/mowers` | `device_gateway.view` (token level; implied by `mower.view` on the whole account) | Mowers held by this instance: connection status, connected since, last activity, activity, battery, commands in flight. Account principals see their account's mowers only; principals without an account (operators, services) see all |
| GET | `/healthz` | none | Liveness |
| GET | `/readyz` | none | Ready when the bus is connected and the command consumer is active (an empty registry is still ready); 503 while shutting down |
| GET | `/metrics` | none | Prometheus metrics |
| GET | `/.well-known/mowdeck-module` | none | Module descriptor: permission `device_gateway.view`, events `mower.connected`, `mower.disconnected` |

Principals come from Identity's access tokens: with `MOWDECK_AUTH_JWKS_URL` set (Compose: `http://identity:8000/.well-known/jwks.json`) the `mowdeck-auth` resolver verifies bearer tokens locally; without it every request is answered with 401. `device_gateway.view` is an account-wide key (no relation in the authorization model) checked from the token's `perms`; its descriptor declares it implied by `mower.view`, so Identity puts it into the token of everybody who may view the mowers of the whole account (owner, admin, Operator or Viewer at account level).

Photo requests (ADR 0033): every instance subscribes to `gateway.mower.*.photo` (core NATS, no queue group); only the instance that holds the mower answers a `PhotoRequest` with a `PhotoReply` (base64 image, capped at `max_bytes`, at most 512 KiB), one photo per mower at a time, from the first source that has one (`MowerSession.capture_photo`):

1. the adapter's own photo (`PhotoCapture`, the simulator's garden), unless it raises `PhotoUnavailable` (the simulator's option `photos: false`);
2. the camera relay ([ADR 0045](../../docs/adr/0045-camera-relay.md) step 8), when `MOWDECK_RELAY_URL` and `MOWDECK_RELAY_FRAMES_TOKEN` are set and the mower has a camera (capability `camera` not `none`): `RelaySnapshotter` posts the mower's account and id **from this connection** (never from the request) with the gateway's service principal to the relay's frame tap (`POST /internal/v1/frames/snapshot`). The relay holds a `vision` lease for that request only: it takes the frame from the session a viewer holds (no second vendor session) or starts one within the Vision limits, and releases it 15 s later.

The capture worker of [ADR 0035](../../docs/adr/0035-server-side-camera-frames-and-automatic-analysis.md) (`MOWDECK_CAPTURE_URL`, `MOWDECK_CAPTURE_TOKEN`) is removed; both variables are ignored.

Both front cameras (`PhotoRequest.max_frames` 2, what Vision asks for): the reply carries `frames` instead of `image_base64`, one `PhotoFrame` per camera of the same moment, left first, with `side`, `camera_id`, `camera_name` and `captured_at`, and `frames_missing` (the cameras that gave none), all frames together within `max_bytes` (`MowerSession.capture_frames`). The relay is asked for its frame set (`RelaySnapshotter.frame_set`, `POST /internal/v1/frames/set`, half of `max_bytes` per frame): one frame of the left and the right front camera captured closest together (target 200 ms); a silent or late camera is left out and named, never failing the photo. The camera the app shows plays no part. A relay without that route (an older release: 404 without a problem document, or 405) is asked for its single snapshot instead. The adapter's own photo gives its single frame as a set of one. A frame beyond the cap is dropped and named in `frames_missing` (`too_large` when not even the first fits). Without `max_frames` the reply is the single image as before.

A relay that cannot serve the mower at all (`SnapshotUnavailable`: not reachable, frame tap off, no driver for the camera's provider) and any other answer of the relay (a Vision limit `camera-budget`, an offline mower, no frame in time) fail the photo with the relay's reason. Otherwise the reply is `unsupported` (no camera, no source configured), `not_connected` (no stream token), `too_large` or `failed` (the source's status and problem type in `detail`). A mower of another account answers `not_found`. `MOWDECK_PHOTO_TIMEOUT_SECONDS` (default 30) bounds the whole photo, `MOWDECK_CAPTURE_TIMEOUT_SECONDS` (default 20) the relay's frame. While a drive session is open the telemetry heartbeat is `MOWDECK_TELEMETRY_DRIVE_HEARTBEAT_SECONDS` (default 1 s instead of 10 s), so AI assist's liveness watch (no state for 3 s aborts) also works for a mower that does not move.

## Link heartbeat

Clients judge online and offline by the gateway's link, not by the age of the values ([ADR 0040](../../docs/adr/0040-link-heartbeat-for-online-status.md)). Every state the session publishes carries `heartbeat_at` (when the gateway last knew the connection alive) and `connected`; the session sets both, adapters never do. While the connection says `is_connected`, the latest state is republished every `MOWDECK_TELEMETRY_HEARTBEAT_SECONDS` (default 10 s, 1 s with an open drive session) even when the adapter reported nothing new: a fresh `heartbeat_at`, the unchanged `observed_at` (the time of the mower's own report). A heartbeat that falls due while reports arrive that are not significant carries the newest of them. When the link ends (lost, failed, or the gateway releases the mower) the heartbeats stop and the last state goes out once more with `connected: false` and the last `heartbeat_at`, before the connection is closed and `mower.disconnected` follows. Nothing depends on the manufacturer: liveness is the generic `MowerConnection.is_connected`, and the simulator runs the same code.

## Events and subjects

Payload models live in `mowdeck_contracts.v1` (JSON Schemas in `libs/mowdeck-contracts/schemas/v1/`).

| Direction | Subject | Kind | Payload | Notes |
| --- | --- | --- | --- | --- |
| consumed | `command.mower.*` | JetStream `COMMANDS`, durable `device-gateway-<shard>` | `CommandEnvelope` | every instance sees every command and acknowledges commands of other shards without acting |
| produced | `command.result.mower.<id>` | JetStream `COMMANDS` | `CommandResult` | msg id `<command_id>:<status>`, so a result is stored once |
| produced | `telemetry.mower.<id>` | JetStream `TELEMETRY` | `MowerState` | on every significant change (anything except `observed_at`, `raw`, `heartbeat_at` and `connected`) and as the link heartbeat every `MOWDECK_TELEMETRY_HEARTBEAT_SECONDS` while the connection is alive; once more with `connected: false` when the link ended (see [Link heartbeat](#link-heartbeat)) |
| produced | `device.mower.<id>.capabilities`, `device.mower.<id>.map` | JetStream `DEVICE` (newest per subject) | `MowerCapabilitiesSnapshot`, `MowerMapSnapshot` | after every connect, the map again after a job ends, the capabilities again when a link of the mower changed (manual driving follows the Bluetooth bridge); only when the content changed. Msg id `<mower_id>:capabilities|map:<sha256 prefix of the snapshot>` (it covers `observed_at`, so a value that flips back within the stream's duplicate window is not dropped) |
| produced | `device.mower.<id>.path` | JetStream `DEVICE` (newest per subject) | `MowerPathSnapshot` | the mow path of the running job (adapters with `MowPathSource`): at connect (also `path: null`, so a stale value goes), at once for a new route or a cleared one (the job ended), progress-only changes at most every `MOWDECK_PATH_PROGRESS_SECONDS`. Msg id `<mower_id>:path:<sha256 prefix of the snapshot>`; see [Mow path](#mow-path) |
| produced | `event.mower.connected`, `event.mower.disconnected` | JetStream `EVENTS` | `EventEnvelope[MowerConnectionEvent]` | `mower_id`, `account_id`, `connection_kind`, `at`, `reason` (disconnect only) |
| consumed | `event.mower.registered`, `event.mower.updated`, `event.mower.removed` | JetStream `EVENTS`, ephemeral per instance | `EventEnvelope[MowerRegistration]` | from Fleet; add, update (reconnect if the link changed) or close a mower: removal, a link change and a replacement close the old connection exactly once (`connection for mower closed` in the log) |
| reply | `gateway.mower.<id>.camera.session` | core NATS request/reply, every instance (no queue group) | `CameraSessionRequest` / `CameraSessionReply` | only the instance whose shard holds the mower answers; see [Camera sessions](#camera-sessions) |
| reply | `gateway.mower.<id>.camera.release` | core NATS request/reply, every instance (no queue group) | `CameraReleaseRequest` / `CameraReleaseReply` | only the instance whose shard holds the mower answers; see [Camera release](#camera-release) |
| reply | `gateway.mower.<id>.path.preview` | core NATS request/reply, every instance (no queue group) | `PathPreviewGatewayRequest` / `PathPreviewReply` | only the instance whose shard holds the mower answers, each request in its own task; see [Mow path](#mow-path) |
| reply | `gateway.mower.<id>.bridge.scan` | core NATS request/reply, every instance (no queue group) | `BridgeScanRequest` / `BridgeScanReply` | only the instance whose shard holds the mower answers, each request in its own task; see [Bluetooth bridges and firmware](#bluetooth-bridges-and-firmware) |
| reply | `gateway.mower.<id>.firmware.check` | core NATS request/reply, every instance (no queue group) | `FirmwareCheckRequest` / `FirmwareCheckReply` | only the instance whose shard holds the mower answers; see [Bluetooth bridges and firmware](#bluetooth-bridges-and-firmware) |
| reply | `gateway.cloud.discover` | core NATS request/reply, queue group `device-gateway` (any instance answers) | `CloudDiscoverRequest` / `CloudDiscoverReply` | see [Cloud accounts](#cloud-accounts) |
| reply | `gateway.errors.explain`, `gateway.errors.lexicons` | core NATS request/reply, queue group `device-gateway` (any instance answers) | `ErrorExplainRequest` / `ErrorExplainReply`, `ErrorLexiconListRequest` / `ErrorLexiconListReply` | see [Error lexicons](#error-lexicons) |
| request | `fleet.mowers.credentials` | core NATS request/reply | `MowerCredentialsRequest` / `MowerCredentialsReply` | before every connect of a mower whose registration (or one of its links) says `credentials: "fleet"`; see [Credentials](#credentials) |
| request | `fleet.mowers.list` | core NATS request/reply | `MowerListRequest` / `MowerListReply` | on start; retried with backoff (1 s doubling up to `MOWDECK_FLEET_RETRY_MAX_SECONDS`) while Fleet does not answer |

### Error lexicons

Vendor error codes in plain words ([ADR 0067](../../docs/adr/0067-error-code-lexicon.md)). The adapter packages installed in this image register their lexicons through the entry point group `mowdeck.error_lexicons` (`mowdeck-control`): today `simulated` (from `mowdeck-control`) and `mammotion` (663 codes from PyMammotion's table, 26 with curated texts in English and German). The gateway holds no table of its own; it answers `gateway.errors.explain` (up to 20 codes, picked by `vendor` or by the mower's `connection`, in `locale` with English as the fallback; an unknown code gets the generic advice of its `category`) and `gateway.errors.lexicons` (the summaries) from any instance. The lexicons are built at the first request, in a thread. Telemetry serves them to clients, Notifications puts them into the push texts.

### Map and capabilities snapshots

After every (re)connect the mower's session reads `capabilities()` and the map from the controller and publishes each as a snapshot (`mowdeck_contracts.v1.device`) when its content fingerprint (SHA-256 of the JSON) differs from the one it published last; the fingerprint prefix is also the msg id, so JetStream de-duplicates repeats. When the activity changes from mowing, paused or returning to docked, charging or idle, a job has ended and zones may have changed in the vendor app, so the map is read again (with refresh) in the background. A failed read or publish is logged as a warning and never drops the connection: the snapshots serve clients, the mower works without them. Telemetry stores the snapshots and serves them at `/api/v1/telemetry/mowers/{id}/map` and `.../capabilities`.

### Mow path

The route a mower planned for its job (`MowPath` in `mowdeck_contracts.v1.mow_path`: polylines in the map's local metres, `kinds`, `points_total`, the vendor's `path_hash` as text, `progress {index, direction}`). On every state (and heartbeat) the session reads `MowerController.current_path()` (no traffic: the adapter holds the route, see the adapter READMEs) and publishes a `MowerPathSnapshot` when it is due: the first check of a connection always (also without a path, so a value left on the stream outlives no job), a new route or a cleared path (`path: null`, the job ended) at once, a change of the progress only at most every `MOWDECK_PATH_PROGRESS_SECONDS` (default 5 s), so a mowing mower publishes the few kilobytes of its route about every 5 s and not with every report. A failed read or publish is logged and never drops the connection. Telemetry stores the newest one and serves it at `/api/v1/telemetry/mowers/{id}/path`.

Path previews: Fleet sends `PathPreviewGatewayRequest{mower_id, account_id, area_ids, pattern, requested_by}` on `gateway.mower.<id>.path.preview`; the gateway lets the mower plan the start's route without starting (`MowerController.preview_path`, adapters with `PathPreviewCapable`) and answers `PathPreviewReply` with the `path` or an error, bounded by `MOWDECK_PATH_PREVIEW_TIMEOUT_SECONDS` (default 75 s, below Fleet's own wait):

| Case | Answer |
| --- | --- |
| unknown mower in this shard / the request's account does not own the mower | `not_found` / `forbidden` (logged) |
| the connection cannot plan without starting (no `PathPreviewCapable`) | `unsupported` |
| a job runs, a start awaits its confirmation, a drive session is open, unknown areas, a pattern the mower cannot run | `rejected` (the reason in `detail`) |
| mower not connected | `unavailable` |
| no route in time (the adapter's or the gateway's limit) | `timeout` |
| the mower answered without a route, any other failure | `failed` |

### Command handling

1. Invalid envelopes and envelopes whose mower differs from the subject are dropped (terminated, not redelivered).
2. Commands for mowers of another shard are acknowledged and ignored.
3. Unknown mower: `rejected` (`unknown mower`). Account of the envelope differs from the registered owner: `rejected` with a warning log; the command never reaches the controller (tenancy defence in depth).
4. Otherwise the command runs in its own task on the mower's `MowerController`, and the result is published. Mower not connected (reconnecting): `failed` (`mower is not connected`).

Commands are acknowledged once admitted. The controller serializes the ordinary commands of one mower and lets an emergency stop overtake them; commands of different mowers never wait for each other.

### Spot runs

[ADR 0039](../../docs/adr/0039-spot-mowing-by-mowdeck-driven-paths.md). `spot_mow` is a MowDeck job, not the mower's: the controller of `mowdeck-control` checks the start conditions and starts a `SpotMowRun` that drives the inline plan with joystick frames (5 per second, `ttl_ms` 1000, `correlation_id = spot:<command_id>`) through its own command path, so the dead-man switch, the blade rules and the sensor stop apply. The session publishes the controller's `accepted` result at once (or `rejected`/`failed` when a check fails), then waits for the run's end in a follow-up task and publishes the final result: `executed` when the plan was driven, `failed` otherwise, with the run's summary as the reason (`spot run aborted: rtk_lost (...); 12/40 points, 30 % coverage, 95 s, dry run`). Both rows reach Fleet and Telemetry's command timeline (msg ids `<command>:accepted` and `<command>:<status>`).

- **State.** `MowerState.spot_run` (`SpotRunStatus`: state `preparing`, `undocking`, `waiting_rtk`, `transit`, `spot_mowing`, `paused_rtk`, `returning`, `stopping`, `done`, `aborted`, `failed`, progress, segment, points, `dry_run`, reason) is merged into every published state by the controller; a status change publishes a state at once. `MowerActivity` stays what the mower reports.
- **Path.** While the run drives, `controller.current_path()` returns its plan with the progress, so it is published as the ordinary `device.mower.<id>.path` snapshot (progress-only changes at most every `MOWDECK_PATH_PROGRESS_SECONDS`) and the web app draws it with the mow-path layer; the run's end publishes the cleared path at once.
- **From the dock** ([ADR 0041](../../docs/adr/0041-spot-runs-from-the-dock-undock-transit-return.md)). A docked mower on a connection with `capabilities.undock` may get `spot_mow`: the run undocks, plans the transit on the map and drives it with the blades off, mows and sends the mower home (`SpotMow.return_to_dock`). The controller checks the route before it answers: without one the command is `rejected` with `no_route: ...`, before anything moves. The path snapshot carries the transit as the first `transfer` polyline.
- **Rule 12.** While a run is active every command except `emergency_stop`, `spot_stop` and `set_light` is rejected (`spot run active; stop it first`); `spot_stop` ends the run (zero frame, blades off, session closed) and is answered `executed`, without a run `rejected`.
- **Drive calibration** ([ADR 0042](../../docs/adr/0042-drive-calibration.md)). `drive_calibrate` runs like a spot run: `accepted` at once, then `executed` with the fit (`calibration done: full speed 0.120 m/s, full yaw rate 3.5 deg/s, dead zone 0.15 (turns 0.20)`) or `failed` with the reason; the status, the measured steps and the result are in `MowerState.drive_calibration`, and rule 12 applies while it runs (`spot_stop` ends it). The stored calibration (the link options `drive_full_speed_mps`, `drive_full_yaw_rate_dps`, `drive_dead_zone`, `drive_turn_dead_zone`, `drive_speed_setting`, `calibrated_at`, written by Fleet) is taken off the options before the connection is created, so adapters never see it, and handed to the controller, which applies it to every spot run. A registration update that changes only these keys does not reconnect the mower.
- **Maintenance commands** ([ADR 0048](../../docs/adr/0048-maintenance-commands.md)). `restart_mower`, `sync_map`, `relocate_dock`, `leave_dock` and `resync_rtk` run through the controller like every command (capability `maintenance`, rule 14 and `maintenance_problem`, rejected with the reason). After a `sync_map`, `resync_rtk` or `relocate_dock` that went out (`executed` or `accepted`) the session compares the snapshots at once and publishes the map when it changed, instead of waiting for the periodic recheck. A `restart_mower` makes the mower drop off for a minute or two; the session reconnects as after any lost link.
- **Drive link and signal.** A spot run or calibration does not start while an additional link reports the mower's signal below `LINK_MIN_RSSI_DBM` (-90 dBm in `mowdeck_contracts.v1.links`, `rejected` with the measured value; an unknown signal does not block), read generically from `MowerState.links`. When the drive link does not come up or drops during the run (the adapter raises `NotConnected`, or a frame is refused while the state reports a link down) the run ends `link_lost`, not `frame_rejected`.
- **Link loss and shutdown.** A lost link or a released mower closes the controller, which ends the run with `link_lost` and publishes its final result; the mower's own dead-man stops it within the frame's ttl. `/readyz` draining does not wait for a run (it may last up to 20 minutes); closing the controller ends it.

### Camera sessions

`gateway.mower.<id>.camera.session` ([ADR 0032](../../docs/adr/0032-live-camera-over-agora.md)): Fleet sends a `CameraSessionRequest{mower_id, account_id, requested_by}` after its own `can_view` and plan checks. Every instance subscribes with `gateway.mower.*.camera.session`; an instance stays silent for mowers of other shards, so Fleet gets one answer or a timeout. Requests whose subject and body name different mowers are dropped. The owner answers a `CameraSessionReply` with exactly one of `session` or `error`:

| Case | Answer |
| --- | --- |
| unknown mower in this shard | `not_found` |
| the request's account does not own the mower (as for commands) | `forbidden` (logged as a warning) |
| mower not connected (reconnecting), no stream token from the vendor cloud, adapter error | `unavailable` |
| the connection has no camera (`mowdeck_control.camera_session_of` gives `None`: the BLE bridge, a Luba 1, a simulator with `camera: false`) | `unsupported` |
| otherwise | `session`: from `get_camera_session()` of the connection (`agora` for Mammotion cloud mowers, `fake` for the simulator) |

The session holds a short-lived token: it is sent, never logged. Answered requests are counted in `mowdeck_device_gateway_camera_sessions_total{outcome}` (`issued` or the error code). The subject lives below `gateway.` because the stream `DEVICE` captures `device.>`, and a request on a stream subject would also be stored and acknowledged by JetStream.

### Camera release

`gateway.mower.<id>.camera.release` ([ADR 0045](../../docs/adr/0045-camera-relay.md)): the camera relay (from phase 3 on; nothing calls it yet) sends a `CameraReleaseRequest{mower_id, account_id, requested_by, channel}` after it left a source session, because a mower keeps publishing its camera stream after the last viewer left (4G volume and battery). `channel` is the `CameraSession.channel` when known; adapters that stop per mower ignore it. Subscriptions, shards and dropped mismatched subjects work as for camera sessions; the subject lives outside every stream. The owner answers a `CameraReleaseReply`:

| Case | Answer |
| --- | --- |
| unknown mower in this shard | `error` `not_found` |
| the request's account does not own the mower (as for commands) | `error` `forbidden` (logged as a warning) |
| mower not connected, the stop could not be sent, adapter error | `error` `unavailable`; the mower's session stays open |
| the connection has no release port (`mowdeck_control.release_camera_session_of` gives `False`: the BLE bridge, adapters without it) | `released: false` without an error: nothing to stop, nothing sent |
| otherwise | `released: true`: `release_camera_session(channel)` of the connection sent the stop (Mammotion: PyMammotion's `stop_stream`; the simulator records it) |

The release is idempotent: a second request, or one without a running stream, sends a harmless stop again. Answered requests are counted in `mowdeck_device_gateway_camera_releases_total{outcome}` (`released`, `unsupported` or the error code).

### Credentials

Registrations from Fleet carry no secrets (ADR 0034): Fleet withholds `password`, `noise_psk` and the vendor `account` and marks the registration with `credentials: "fleet"`. Before every connect of such a mower (the first one and every reconnect, so also after a dead cloud login) the session asks Fleet on `fleet.mowers.credentials` and merges the answer into the options of that connection only; the session keeps the registration without secrets, nothing is logged. When Fleet does not answer or holds nothing for the mower, the connect fails and the usual backoff retries. The additional links of a registration (`MowerRegistration.links`, ADR 0038) follow the same rule: their withheld secrets arrive as `link_secrets` by kind and are merged into the link for that connect; the `credentials` markers are dropped.

### Bluetooth bridges and firmware

[ADR 0038](../../docs/adr/0038-dual-link-per-mower-and-firmware-updates.md). The gateway knows nothing about a manufacturer here: it hands `MowerRegistration.links` to the adapter as the connection option `links` (`{kind: options}`, secrets merged) and routes everything else through the capabilities of the connection.

- **Links in the state.** An adapter that opened an additional link reports it in `MowerState.links` (`ble_bridge`: connected, since, RSSI, bridge firmware with `outdated`) and the `connection` in use. When the links change, the session compares the capabilities again at once and republishes them when they differ (manual driving comes and goes with the bridge), so Realtime's drive capabilities follow.
- **Bridge scans.** `gateway.mower.<id>.bridge.scan`: Fleet sends a `BridgeScanRequest{mower_id, account_id, host, port, noise_psk, expected_name, timeout_s}` before it stores a bridge link (and for a re-check). The owner of the mower answers a `BridgeScanReply` with `proxy` (what the bridge says about itself), `devices` (the mower advertisements it saw) or an `error`: `not_found`, `forbidden` (the account rule of commands), `unsupported` (the connection cannot open bridge links, `capabilities.links_supported`, or the mower is not connected), `busy` (another mower of this instance holds that bridge open, or a scan through it runs; one scan per bridge at a time), `timeout` (the bridge answered, the mower did not advertise; `proxy` is set), `already_connected` (the expected mower was not seen, or the bridge is held, and another mower entry of this instance is connected over Bluetooth through that bridge, so the mower does not advertise: `held_by` names the entry, the detail adds its id; decided from the sessions' own state, `connection` `ble_bridge` and the bridge host of the registration, never by manufacturer) and the adapter's `unreachable` and `auth_failed`. A connection that holds the bridge itself answers its re-check from its live scanner. The key is used, never logged; answers are counted in `mowdeck_device_gateway_bridge_scans_total{outcome}`.
- **Firmware.** `gateway.mower.<id>.firmware.check`: the owner asks the manufacturer through the connection's `FirmwarePort` (`capabilities.firmware_update`, else `unsupported`; `unavailable` while the mower is not connected or the vendor did not answer), publishes the state that carries `firmware_info` and answers the `FirmwareCheckReply`. Without a request the session checks once `MOWDECK_FIRMWARE_CHECK_INITIAL_SECONDS` after a connect (when the adapter's device information rounds are through) and then every `MOWDECK_FIRMWARE_CHECK_INTERVAL_SECONDS`. The update itself is the command `firmware_update {version}` through the normal command path: the controller of `mowdeck-control` applies the safety rules (the offered version only, docked with at least 70 % battery (charging not required), no job, no drive session, nothing already installing) and refuses every command but the emergency stop while the mower reports `updating`. Notifications derives `firmware_update_available`, `firmware_update_finished` and `firmware_update_failed` from the telemetry.
- **Simulator.** A simulated mower with a `ble_bridge` link gets a simulated bridge (`connection` `ble_bridge`, a switch in tests), answers scans with a configurable device list and offers a firmware update when `firmware_available` is set in its options; the fake Mammotion cloud does the same for its mowers.
- **RTK base station** ([ADR 0047](../../docs/adr/0047-rtk-base-station-as-a-fleet-device.md)). `MowerRegistration.base_station` reaches the adapter as the connection option `base_station` (a `BaseStationRef`); linking or unlinking one reopens the connection, a rename does not. The adapter reports the base in `MowerState.base_station`. `FirmwareCheckRequest.target` `base_station` routes the check through the connection's `BaseStationPort` (`capabilities.base_station_firmware_update`, else `unsupported`; a mower without a linked base station is `unsupported` too), and the periodic firmware check asks for the base station as well. The command `base_station_firmware_update` runs through the controller like every command.

### Cloud accounts

[ADR 0034](../../docs/adr/0034-linked-cloud-accounts-and-device-discovery.md), [ADR 0066](../../docs/adr/0066-cloud-provider-plugins-and-more-vendors.md). At start the gateway loads the cloud provider plugins of the installed adapter packages (entry point group `mowdeck.cloud_providers`, `adapters/control/providers.py`): those tested on real hardware are on by default, `MOWDECK_CLOUD_PROVIDERS_ENABLE` adds others, `MOWDECK_CLOUD_PROVIDERS_DISABLE` switches any off. A `cloud` registration goes to the plugin named by its option `cloud_provider` (Fleet sets it from the linked account); one without it (registered before ADR 0066) goes to the provider that claims those (Mammotion), and one naming a disabled provider is refused, never handed to another vendor. Each plugin shares sessions per vendor account as it sees fit; the Mammotion plugin keeps the `CloudSessionPool` of ADR 0034: the first mower logs in, the others borrow the session (no second login, which would supersede the first at the Mammotion cloud), and each keeps its own connection, controller, state, refresh loop, map, camera and device information rounds. Closing a mower releases its share; the session is stopped two minutes after the last release and replaced when its login died (`reauth_required`). The other kinds still come from the `mowdeck-control` registry.

`gateway.cloud.providers`: Fleet asks for the enabled providers (`CloudProvidersRequest`) and gets their `ProviderDescriptor`s (id, name, credential fields, guidance, `tested_on_hardware`). `gateway.cloud.discover`: Fleet sends a `CloudDiscoverRequest{provider, account_id, linked_account_id?, credentials}` (the deprecated `email` and `password` of ADR 0034 are mapped onto the provider's identity and only secret field) when a user links or re-reads an account. The gateway checks the credentials against the provider's fields and asks the plugin, which borrows the session for these credentials (logging in only when none is open) and answers the account's devices (`DiscoveredDevice`: name, device id, product key, model, online flag, `is_mower`, `device_type`) or an error: `login_failed` (the cloud refused the credentials), `unavailable` (no answer within `MOWDECK_CLOUD_DISCOVERY_TIMEOUT_SECONDS`, including the wait for one of the `MOWDECK_CLOUD_DISCOVERY_CONCURRENCY` login slots, or another failure; only the exception type is reported) or `unsupported` (the provider is not installed or not enabled). Invalid requests are dropped without an answer. Credentials are never logged; the log line names the provider, the MowDeck account and the linked account. Session caches live below `MOWDECK_CLOUD_SESSION_CACHE_DIR` (unset: no cache); the per-mower `session_cache_path` option is not used for shared sessions.

**Several shards:** linked accounts are unsupported with `MOWDECK_SHARD_COUNT` above 1 (mowers of one account may land on different instances with separate sessions that log each other out); the gateway logs a warning once when it gets such a mower. Answers are counted in `mowdeck_device_gateway_cloud_discoveries_total{outcome}` (`listed` or the error code). A device added in the vendor's app appears after the session is opened again.

`MOWDECK_FAKE_CLOUDS=true` (development and the browser tests only; refused with `MOWDECK_ENVIRONMENT=prod`; the former name `MOWDECK_FAKE_MAMMOTION_CLOUD` still works) makes every enabled provider answer from the fake cloud of its adapter package (`ProviderContext.fake`). The Mammotion fake: accounts `*@fake.mowdeck.test` with the password `fake-cloud-password`, the mowers `Luba-FAKE0001` (online) and `Luba-FAKE0002` (offline) and the base station `RTK-FAKE0001`; registered cloud mowers connect to a `FakeMammotionPort` through the same pool. The fake accounts of the other providers are in their package READMEs.

## Settings

All settings of `mowdeck-service-kit` apply (`MOWDECK_*`, any value also from `MOWDECK_<NAME>_FILE`). Service specific settings and changed defaults:

| Variable | Default | Description |
| --- | --- | --- |
| `MOWDECK_SERVICE_NAME` | `device-gateway` | Service name in logs, metrics and traces |
| `MOWDECK_HTTP_PORT` | `8000` | HTTP port |
| `MOWDECK_SHARD_INDEX` | `0` | Shard of this instance |
| `MOWDECK_SHARD_COUNT` | `1` | Number of instances (shards) |
| `MOWDECK_BOOTSTRAP_MOWERS` | `[]` | **Development only.** JSON list of `MowerRegistration` that seeds the registry without Fleet (running the service alone); the Compose stack does not set it |
| `MOWDECK_TELEMETRY_HEARTBEAT_SECONDS` | `10` | Publish the latest state at least this often |
| `MOWDECK_SNAPSHOT_RECHECK_SECONDS` | `30` | While states arrive, compare capabilities (reloaded) and the map (rebuilt by adapters with `MapRebuildable`, never re-read from the mower) again this often and republish only what changed (model, limits, dock and RTK reference arrive after the connect) |
| `MOWDECK_SIM_TIME_SCALE` | `1.0` | Simulated seconds per real second (unless a registration sets `time_scale`) |
| `MOWDECK_SIM_TICK_SECONDS` | `1.0` | Real seconds between simulation steps |
| `MOWDECK_FLEET_REQUEST_TIMEOUT_SECONDS` | `2.0` | Timeout of `fleet.mowers.list` |
| `MOWDECK_FLEET_RETRY_MAX_SECONDS` | `30.0` | Maximum backoff between Fleet requests |
| `MOWDECK_RECONNECT_INITIAL_SECONDS` | `1.0` | First reconnect delay |
| `MOWDECK_RECONNECT_MAX_SECONDS` | `30.0` | Maximum reconnect delay |
| `MOWDECK_CONNECTION_CHECK_SECONDS` | `2.0` | How often an established link is checked |
| `MOWDECK_CLOUD_DISCOVERY_TIMEOUT_SECONDS` | `40` | Longest login and device listing for `gateway.cloud.discover` |
| `MOWDECK_CLOUD_DISCOVERY_CONCURRENCY` | `2` | Vendor logins of discoveries that may run at once on this instance |
| `MOWDECK_CLOUD_SESSION_CACHE_DIR` | unset | Directory for the credential caches of the shared cloud sessions, one file per account (private, writable volume) |
| `MOWDECK_PHOTO_TIMEOUT_SECONDS` | `30` | Longest time for a photo (`gateway.mower.<id>.photo`), a camera frame of the relay included; keep it below Vision's `MOWDECK_VISION_PHOTO_TIMEOUT_SECONDS` |
| `MOWDECK_PATH_PREVIEW_TIMEOUT_SECONDS` | `75` | Longest wait for a route the mower plans for a preview (`gateway.mower.<id>.path.preview`); keep it below Fleet's `MOWDECK_PATH_PREVIEW_REQUEST_TIMEOUT_SECONDS` |
| `MOWDECK_PATH_PROGRESS_SECONDS` | `5` | A mow path snapshot whose only change is the progress is republished at most this often |
| `MOWDECK_FIRMWARE_CHECK_INITIAL_SECONDS` | `120` | Wait after a connect before the manufacturer is asked for firmware updates (connections with `capabilities.firmware_update`); `0` switches the periodic check off |
| `MOWDECK_FIRMWARE_CHECK_INTERVAL_SECONDS` | `86400` | Then ask again this often while the connection lasts |
| `MOWDECK_RELAY_URL` | unset | Base URL of the camera relay (`http://camera-relay:8000`, ADR 0045): photos of camera-capable adapters without an own photo come from its frame tap |
| `MOWDECK_RELAY_FRAMES_TOKEN` (`_FILE`) | unset | Shared secret of the relay's frame tap; unset: no relay photos (a warning when the URL is set) |
| `MOWDECK_CAPTURE_TIMEOUT_SECONDS` | `20` | How long the relay may take for one frame (at most 30 s); without the relay, camera-capable adapters without an own photo answer `unsupported` |
| `MOWDECK_CAPTURE_MAX_WIDTH` | `1280` | Wider frames are scaled down by the relay |
| `MOWDECK_CLOUD_PROVIDERS_ENABLE` | empty | Comma separated cloud provider ids to enable besides the default ones (those tested on real hardware), e.g. `husqvarna` (Compose: `GATEWAY_CLOUD_PROVIDERS_ENABLE`) |
| `MOWDECK_CLOUD_PROVIDERS_DISABLE` | empty | Comma separated cloud provider ids to switch off; wins over the defaults and the enable list (Compose: `GATEWAY_CLOUD_PROVIDERS_DISABLE`) |
| `MOWDECK_FAKE_CLOUDS` | `false` | **Development only.** The fake clouds of the enabled providers for discoveries and cloud mowers (Compose: `GATEWAY_FAKE_CLOUDS`; the former name `MOWDECK_FAKE_MAMMOTION_CLOUD` still works) |
| `MOWDECK_AUTH_JWKS_URL` | unset | Identity's JWKS; other `MOWDECK_AUTH_*` settings in `libs/mowdeck-auth/README.md` |

Example bootstrap value (for running the gateway without Fleet):

```json
[{"mower_id": "00000000-0000-4000-8000-00000000000a", "account_id": "00000000-0000-4000-8000-00000000000b",
  "name": "Simulated mower", "connection_kind": "simulated", "connection_options": {}}]
```

`connection_options` go to the adapter factory; for the simulator they override `SimulationConfig` fields (`time_scale`, `battery_percent`, ...). The Mammotion options are listed in the [adapter README](../../libs/mowdeck-control-mammotion/README.md#connection-options). Cloud mowers arrive without credentials; the gateway fetches them from Fleet before connecting (see [Credentials](#credentials)). Older cloud mowers and BLE bridges may carry references (`credential_ref`: `env:NAME` or `file:/run/secrets/name`), resolved in this service's environment, so give the container the referenced variables or secret files; for the credential cache (`session_cache_path`) mount a private writable volume (the root file system is read-only). A plain `noise_psk` is still accepted.

## Sharding

A mower belongs to the instance with `mower_id.int % MOWDECK_SHARD_COUNT == MOWDECK_SHARD_INDEX`. Each instance has its own durable consumer `device-gateway-<index>`, receives every command and ignores (acknowledges) those of other shards. Kubernetes runs the gateway as a StatefulSet and takes the index from the pod index label (`apps.kubernetes.io/pod-index`) and the count from the replicas (`infra/helm/mowdeck/templates/device-gateway.yaml`).

Limitation (fine for Phase 1): changing the shard count moves mowers between instances, and there is no hand-over; affected mowers reconnect on their new owner, and commands sent during the switch may be answered `rejected` (`unknown mower`) or `failed`.

## Shutdown

`SIGTERM` turns `/readyz` to 503, stops the command consumer (new deliveries are negatively acknowledged and redelivered after the restart), waits until every admitted command has published its result, closes every controller (ending drive sessions, publishing `mower.disconnected`) and then drains the bus. Keep `MOWDECK_SHUTDOWN_GRACE_SECONDS` above the longest command.

## Layers

| Package | Content |
| --- | --- |
| `domain/rules.py` | `Shard`, `ConnectionStatus`, `command_rejection`, the request rejections, `bridge_in_use`, `links_changed`, `is_significant_change`, `backoff` (pure) |
| `application/` | `DeviceGateway` (registry, admission, bridge scans, firmware checks, Fleet sync, shutdown), `MowerSession` (link supervision, telemetry, periodic firmware checks, commands), `CloudDiscoveryService`, ports |
| `adapters/bus/` | command consumer, registry events, camera session and release responders (`camera.py`), bridge scan responder (`bridge.py`), firmware check responder (`firmware.py`), cloud discovery responder (`cloud.py`), Fleet request, publisher |
| `adapters/control/` | `RegistryConnectionProvider`: connections from the `mowdeck-control` registry (simulated, `ble_bridge` from an adapter package) and `cloud` from the provider plugin of the registration; `providers.py`: loads the enabled cloud provider plugins (real or fake) and implements the discovery port on them; simulator clock |
| `adapters/api/` | `GET /api/v1/device-gateway/mowers` |
| `main.py` | wiring (`build_app`) and `main()` |

## Local start

With the Compose stack (builds the image; mowers come from Fleet):

```bash
cp infra/compose/.env.example infra/compose/.env
docker compose -f infra/compose/docker-compose.yml up -d --build --wait
```

Register a simulated mower through Fleet (`POST /api/v1/fleet/mowers`, see `services/fleet/README.md`); the gateway connects it within a second and `GET /api/v1/device-gateway/mowers` shows it.

Without Docker for the service itself:

```bash
docker compose -f infra/compose/docker-compose.yml up -d --wait nats
MOWDECK_ENVIRONMENT=dev MOWDECK_SIM_TIME_SCALE=30 \
MOWDECK_BOOTSTRAP_MOWERS='[{"mower_id": "00000000-0000-4000-8000-00000000000a", "account_id": "00000000-0000-4000-8000-00000000000b", "name": "Sim", "connection_kind": "simulated"}]' \
  uv run python -m device_gateway.main
```

Container image (build context is the repository root):

```bash
docker build -f services/device-gateway/Dockerfile -t mowdeck/device-gateway:dev .
```

### Sending commands (`scripts/send_command.py`)

Publishes a command directly on the bus (bypassing Fleet and its authorization check; development only) and prints the results and the telemetry for `--seconds`. Against the Compose stack pass the `--mower-id` and `--account-id` of a mower registered through Fleet; its results do not update a Fleet command record:

```bash
uv run python services/device-gateway/scripts/send_command.py start --areas front back --seconds 10
uv run python services/device-gateway/scripts/send_command.py pause
uv run python services/device-gateway/scripts/send_command.py dock
uv run python services/device-gateway/scripts/send_command.py stop        # emergency stop
```

Options: `--nats-url` (default `MOWDECK_NATS_URL` or `nats://127.0.0.1:4222`), `--mower-id` and `--account-id` (default: the bootstrap mower), `--ttl` (command expiry). Output:

```text
sent start command=e259672a-... to mower 00000000-0000-4000-8000-00000000000a
result command=e259672a-... status=executed (mowing front with stripes)
telemetry activity=mowing battery=99% blade=True x=11.239 y=0.2 progress=3%
```

## Tests

```bash
uv run pytest services/device-gateway --cov=device_gateway
```

The tests use the in-memory bus and simulated mowers (`tests/dg_support.py` has the helpers). `test_dg_relay_frames.py` covers the photo order (the adapter's own photo first, then the relay; a cloud mower's photo from the relay without a vendor session of the gateway; an unavailable relay and a refusal both fail with the relay's reason; one photo per mower at a time; account and mower from the connection; a mower without a camera or without a relay is `unsupported`; the removed worker's settings are ignored) and `RelaySnapshotter` against a mocked frame tap; `test_dg_frame_sets.py` the frame sets (both front cameras to Vision left first with their times and half the cap each, a silent camera named, frames beyond the cap dropped, single frame requests unchanged, the adapter's photo and a source without sets as sets of one, an unavailable relay and a refusal, a foreign account, `frame_set` over HTTP with its body, the fallback to one frame on an older relay and the mapped answers). The JetStream flow test runs when `MOWDECK_TEST_NATS_URL` is set:

```bash
docker run -d --name mowdeck-test-nats -p 4222:4222 public.ecr.aws/docker/library/nats:2.15.0-alpine -js
MOWDECK_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest services/device-gateway
docker rm -f mowdeck-test-nats
```
