# mowdeck-contracts

All API and event formats of MowDeck as Pydantic v2 models, plus their JSON Schemas. The package has no I/O and depends only on `pydantic`, so any service, script or client can use it. Licence: Apache-2.0.

## Contents

Everything lives in the versioned namespace `mowdeck_contracts.v1`.

| Module | Content |
| --- | --- |
| `envelopes` | `EventEnvelope[Payload]`, `CommandEnvelope`, `CommandResult`, `CommandPriority`, `CommandStatus` |
| `mower` | `MowerState` telemetry (with `heartbeat_at` and `connected`, the Device-Gateway's link heartbeat (ADR 0040), `links` (`{kind: LinkStatus}`), `firmware_info` (`FirmwareInfo` or null) and `spot_run` (`SpotRunStatus` or null), and the typed parts of module `mower_status`: `job`, `gnss`, `signal`, `self_check`, `error_category`), `GeoPosition`, enums (`MowerActivity` incl. `updating`, `RtkFixStatus`, `RtkSource`, `ConnectionKind`), commands `StartMow`, `PauseMow`, `ResumeMow`, `ReturnToDock` (`end_job` also ends the job), `EmergencyStop`, `SetBladeHeight`, `SetSpeed`, `SetLight`, `FirmwareUpdate` (`version`), `SpotMow` (`plan`, `blades` + `confirm_blades`, `speed_mps` at most 0.3, `spot`, `return_to_dock` (default true, ADR 0041)), `SpotStop` (also ends a drive calibration), `DriveCalibrate` (`step_s`), `SetDriveSpeed` (`speed_mps`), `ManualDrive` and the discriminated union `MowerCommand`; `MowerState.drive_calibration` (`DriveCalibrationStatus` or null) and `drive_speed_mps` |
| `mower_status` | Typed, optional parts of `MowerState` that used to live only in `raw` (every one null when the connection does not report it, every inner field nullable): `job` (`JobProgress`: `progress_percent` 0..100, `area_id` (a `MowerMap.areas[].id`, null when the mower works outside a known area), `elapsed_s`, `remaining_s` (seconds), `mowed_m2` (square metres)), null when no job runs; `gnss` (`GnssStatus`: `satellites`, `correction_age_s` (seconds, null without corrections), `rtk_status` (`GnssSolution`: `none`, `single`, `float`, `fixed`, `unknown`)); `signal` (`SignalStatus`: `wifi_dbm`, `mobile_dbm` (dBm, -150..-1), `network` (`NetworkKind`: `wifi`, `mobile`, `ble`, `none`, the mower's own uplink)); `self_check` (`SelfCheck`: `result` (`SelfCheckResult`: `ok`, `blocked`), `detail` (the blocking condition as a lower-case key such as `rain`, null with `ok`)); `error_category` (`MowerErrorCategory`: `trapped`, `obstacle`, `outside`, `wheel_slip`, `lift_tilt`, `collision`, `battery`, `blade_motor`, `rain`, `positioning`, `other`; only with activity `error`). Adapters keep the matching `raw` keys for older consumers |
| `calibration` | Drive calibration ([ADR 0042](../../docs/adr/0042-drive-calibration.md)): `DriveCalibration` (`full_speed_mps`, `full_yaw_rate_dps`, `dead_zone`, `turn_dead_zone`, `drive_speed_setting`, `calibrated_at`; `to_options()` / `from_options()` with the link option keys `DRIVE_CALIBRATION_OPTION_KEYS`, `full_speed_at(setting)` rescales proportionally to the vendor's speed setting), `DriveCalibrationStep`, `DriveCalibrationResult`, `DriveCalibrationStatus` (`preparing`, `measuring`, `stopping`, `done`, `aborted`, `failed`), `DriveSpeedSetting` (`MowerCapabilities.drive_speed_setting`), `calibration_of_links` |
| `fleet` | `MowerRegistration` (never with secrets: `credentials: "fleet"` marks withheld ones, `needs_credentials`, `repr` masks secret keys; `links` = the public options of additional links by kind, same rule, with `credentials_rev`), `MowerCredentialsRequest`, `MowerCredentialsReply` (`secret_options`, `link_secrets` by link kind), `MowerListRequest`, `MowerListReply`, `MowerConnectionEvent` and the event types `mower.registered`, `mower.updated`, `mower.removed`, `mower.connected`, `mower.disconnected` (Fleet and Device-Gateway) |
| `links` | Additional links of a mower (ADR 0038): the kind `ble_bridge` (`LINK_BLE_BRIDGE`, `LINK_KINDS`), `CREDENTIALS_REV_OPTION`, `LinkStatus` (`connected`, `since`, `rssi`, `esphome_version`, `compiled_at`, `project_version`, `outdated`, `outdated_reason`), and the bridge scan `BridgeScanRequest` (`mower_id`, `account_id`, `host`, `port`, `noise_psk` (secret, excluded from `repr`), `expected_name`, `timeout_s` at most 30) / `BridgeScanReply` (`proxy` (`ProxyInfo`), `devices` (`ScannedDevice`: `name`, `address`, `rssi`), `error` (`BridgeScanError`, `BridgeScanErrorCode`: `unreachable`, `auth_failed`, `timeout`, `busy`, `unsupported`, `not_found`, `forbidden`)) on `gateway.mower.<id>.bridge.scan` |
| `firmware` | Mower firmware (ADR 0038): `FirmwareInfo` (`installed`, `available`, `release_notes`, `update_state` (`idle`, `available`, `installing`, `failed`, `unknown`), `progress`, `failure_reason`, `checked_at`), `FirmwareCheckRequest` / `FirmwareCheckReply` (exactly one of `info` or `error` (`FirmwareCheckError`, `FirmwareCheckErrorCode`: `not_found`, `forbidden`, `unsupported`, `unavailable`)) on `gateway.mower.<id>.firmware.check` |
| `areas` | Area names: `area_display_name` / `area_display_names` (the display rule: Fleet's custom name, else the mower's own name unless empty or only a long number, else `Area N` / `Bereich N` in map order; `area_fallback_name`, `is_placeholder_name`), `clean_area_name` (trim, at most `AREA_NAME_MAX_LENGTH` = 60), the event `fleet.mower.area_names_changed` (`AreaNamesChanged`: `mower_id`, `account_id`, `names`) |
| `commands` | Fleet's command API: `CommandRequest`, `CommandAccepted` (202 for one mower), `CommandView` (the command record, with `origin` and `schedule_id` when a service issued it), `CommandState` (`pending`, `accepted`, then `executed`, `rejected`, `expired` or `failed`), `BulkCommandAccepted` with one `BulkCommandItem` per visible mower (`accepted`, `forbidden`, `not_published`) |
| `identity` | Identity's events: `AccountCreated`, `UserChanged`, `GrantsChanged` (the authorization cache invalidation signal), `ApiTokenRevoked`, `AccountKind` and the event types `identity.account.created`, `identity.user.created`, `identity.user.disabled`, `identity.user.deleted`, `identity.grants.changed`, `identity.api_token.revoked` |
| `realtime` | The WebSocket protocol of Realtime (import from `mowdeck_contracts.v1.realtime`): client messages `AuthMessage`, `SubscribeMessage`, `UnsubscribeMessage`, `PingMessage`, `DriveStartMessage`, `DriveFrameMessage`, `DriveStopMessage` (union `ClientMessage`, parser `RealtimeClientMessage`), server messages `ReadyMessage`, `SubscribedMessage` (with `denied`), `UnsubscribedMessage`, `StateMessage`, `CommandResultMessage`, `RevokedMessage`, `ErrorMessage`, `PongMessage`, `DriveStartedMessage`, `DriveFrameAckMessage`, `DriveEndedMessage`, `NotificationMessage` (`notification`: `RealtimeNotification` with `scope` `NotificationScope` `user` or `account`) and `VisionProposalMessage` (`vision_proposal`: `event` `VisionProposalEvent` `created` or `updated`, `proposal` `VisionProposalSummary`) (union `ServerMessage`, parser `RealtimeServerMessage`), `RealtimeCloseCode` (1001, 1009, 4400, 4401, 4429, 4430) |
| `device` | Last-value snapshots the Device-Gateway publishes per mower: `MowerMapSnapshot` (`mower_id`, `account_id`, `observed_at`, `map`) and `MowerCapabilitiesSnapshot` (`mower_id`, `account_id`, `observed_at`, `capabilities`), and `MowerPathSnapshot` (`path`: the mow path of the running job, `None` once it ended); Telemetry stores them and serves them to the web app |
| `mow_path` | The route a mower planned (`MowPath`: `path_hash` as text, `polylines` in local metres, `kinds` `mow`/`edge`/`transfer`/`other`, `points_total`, `progress {index, direction}` with `index` = points done over all polylines, `area_ids`, `planned_at`; at most 50 000 points) and the preview request: `PathPreviewRequest{area_ids, pattern}` (Fleet's body), `PathPreviewGatewayRequest` / `PathPreviewReply` (`path` or `error`: `not_found`, `forbidden`, `unsupported`, `rejected`, `unavailable`, `timeout`, `failed`) on `gateway.mower.<id>.path.preview` |
| `notifications` | The Notifications service: `NotificationSent` (payload of `notifications.sent`, one rendered notification for one user, for in-app feeds; optional `created_at`), `NotificationRaised` (payload of `notifications.raised`, `NOTIFICATION_RAISED`: every derived notification once per account, rendered in English, with `dedupe_key` and `required_relation`; Realtime forwards it to API tokens holding `NOTIFICATIONS_VIEW_PERMISSION` = `notifications.view`), `NotificationKind` (`mowing_finished`, `mower_stuck`, `battery_low`, `mower_offline`, `mower_online`, `job_started`, `test`, and from Task 16 `mower_lifted`, `mower_left_dock_unexpectedly`, `geofence_exit`, `blade_maintenance`), `NotificationSeverity` (`info`, `warning`, `critical`), `WebPushPayload` (the decrypted JSON a service worker receives) |
| `scheduler` | The Scheduler and its internal Fleet requests: `IssueCommandRequest` / `IssueCommandReply` (`fleet.commands.issue`: a command on behalf of a subject, `origin`, `schedule_id`; the reply has exactly one of `accepted` (`CommandAccepted`) or `error` (`IssueCommandError` with `IssueCommandErrorCode`: `not_found`, `forbidden`, `invalid`, `unavailable`)), `ResolveTargetRequest` / `ResolveTargetReply` with `TargetMower` (`fleet.targets.resolve`: the mowers of a mower or group target with the time zone and coordinates of their sites), the events `scheduler.run.recorded` (`ScheduleRunRecorded`, `ScheduleRunOutcome`: `started`, `skipped_rain`, `skipped_permission`, `skipped_window`, `skipped_mower_busy`, `skipped_manual`, `failed`) and `scheduler.schedule.disabled` (`ScheduleDisabled`) |
| `drive` | Manual driving (ADR 0026): `DriveStartRequest` / `DriveStartReply` (`fleet.drive.start`, exactly one of `started` (`DriveSessionStarted`) or `error` (`DriveError`, `DriveErrorCode`: `not_found`, `forbidden`, `busy`, `invalid`)), `DriveStopRequest` / `DriveStopReply` (`fleet.drive.stop`), `DriveSessionView` (Fleet's audit), `DriveEndReason` (`client_stop`, `socket_closed`, `revoked`, `token_expired`, `max_duration`, `mower_refused`, `mower_busy`, `replaced`, `expired`), `drive_correlation_id` / `drive_session_of` (frames carry `correlation_id` `drive:<session_id>`). The socket messages `DriveStartMessage`, `DriveFrameMessage`, `DriveStopMessage`, `DriveStartedMessage`, `DriveFrameAckMessage`, `DriveEndedMessage` and the `drive_*` error codes live in `realtime` |
| `cloud` | Linked vendor cloud accounts (ADR 0034): `CloudDiscoverRequest` (`provider` (`CloudProvider`: `mammotion`), `account_id`, `linked_account_id` or null, `email`, `password` (secret, excluded from `repr`)) and `CloudDiscoverReply` (exactly one of `devices` (`DiscoveredDevice`: `device_name`, `iot_id`, `product_key`, `model`, `online`, `is_mower`) or `error` (`CloudDiscoverError`, `CloudDiscoverErrorCode`: `login_failed`, `unavailable`, `unsupported`); `reused_session`) on `gateway.cloud.discover` |
| `camera` | Live camera (ADR 0032): `CameraSession` (`provider` `agora`, `fake` or `relay` (ADR 0045), `app_id`, `channel`, `token` (secret, excluded from `repr`), `uid`, `cameras` (`CameraInfo`: `id`, `name`, publisher `uid`), `encryption` (`CameraEncryption`: `mode`, `key`, base64 `salt`) or null, `expires_at`, `quota` (`CameraQuota`: `used_minutes`, `total_minutes`) or null; `redacted()` for logs and scripts), `CameraSessionRequest` / `CameraSessionReply` (`gateway.mower.<id>.camera.session`, exactly one of `session` or `error` (`CameraSessionError`, `CameraSessionErrorCode`: `not_found`, `unsupported`, `unavailable`, `forbidden`)), `CameraReleaseRequest` / `CameraReleaseReply` (`gateway.mower.<id>.camera.release`, ADR 0045: `mower_id`, `account_id`, `requested_by`, optional `channel`; the reply has `released` and an optional `error` (`CameraSessionError`), never `released` true together with an error; `released` false without an error means the connection cannot release), the plan feature key `CAMERA_FEATURE` (`camera`); for the relay `CameraSession.relay` (`RelayEndpoint`: `tiles` (`RelayTile`: `camera_id`, `whep_url`, `available`: whether the camera delivers, null while unknown), `ticket` (secret, excluded from `repr`, replaced by `redacted()`), `lease_id`, `heartbeat_seconds`), set exactly for provider `relay`, which carries no `token`, `app_id` or encryption; `CameraTransport` (`auto`, `relay`, `direct`) |
| `relay` | Camera relay (ADR 0045): `RelayAttachRequest` / `RelayAttachReply` (`relay.camera.attach`: `account_id`, `mower_id`, `camera_ids` (empty: every camera the mower offers), `requested_by`, `purpose` (`RelayPurpose`: `viewer`, `vision`); the reply has exactly one of a `relay` `session` or `error` (`RelayError`, `RelayErrorCode`: `unsupported_provider`, `unavailable`, `budget`, `forbidden`, `not_found`)), `RelayRenewRequest` / `RelayRenewReply` (`relay.camera.renew`, the heartbeat with a fresh ticket), `RelayDetachRequest` / `RelayDetachReply` (`relay.camera.detach`, idempotent `detached`), the events `CameraRelayStarted` (optional `trigger` and `started_by`), `CameraRelayStopped` (`RelayStopReason`, optional `trigger` and `peak_viewers`), `CameraRelayLeaseOpened`, `CameraRelayLeaseClosed` (`RelayLeaseCloseReason`) with their types `camera.relay.*` (`CAMERA_RELAY_EVENT_TYPES`), and the defaults `RELAY_GRACE_SECONDS` (15), `RELAY_HEARTBEAT_SECONDS` (15), `RELAY_LEASE_TTL_SECONDS` (45), `RELAY_TICKET_SECONDS` (60) and the Vision limits `VISION_*`; the frame tap's frame set (internal HTTP `POST /internal/v1/frames/set`): `VisionFrameSetRequest` (`account_id`, `mower_id`, `max_frames` 1 to `VISION_FRAME_SET_MAX_FRAMES` (2), `max_width`, `max_bytes` per frame, `timeout_s`, `requested_by` (a service), `on_behalf_of`, `demand`) and `VisionFrameSetReply` (`frames`: `VisionFrame` with `camera_id`, `camera_name`, `placement`, `side` (`CameraSide`: `left`, `right`), `image_base64`, `captured_at`, `received_at`, `width`, `height`; `missing`: `VisionFrameMissing` with `VisionFrameMissingReason` `silent`, `no_frame`, `too_large`; `skew_ms`; target `VISION_FRAME_SET_TARGET_SKEW_MS` (200)), `camera_side(placement)` |
| `health` | Telemetry's health history (ADR 0027): the event `telemetry.blade_hours.threshold` (`BladeHoursThreshold`) |
| `entitlements` | Plans and enforcement (ADR 0028): `EntitlementCheckRequest` / `EntitlementCheckReply` (`entitlements.check`) with the `EntitlementSnapshot`, `PlanChanged` (`entitlements.plan.changed`), `PlanStatus`, `PlanSource`, `PlanLimits`, `DenyReason`; `PlanSetRequest` / `PlanSetReply` (`entitlements.plan.set`, Billing sets a plan with `source=billing`, `PlanSetError`) |
| `billing` | Billing (ADR 0031): `SubscriptionChanged` (payload of `billing.subscription.changed`), `SubscriptionStatus` (Stripe's statuses) |
| `principal` | `Principal`, `PrincipalRef`, `PrincipalKind` (mirrors the JWT claims issued by Identity; `session_id` is the `sid` claim) |
| `module` | `ModuleDescriptor` with `PermissionDef` (optional `implied_by`: an account-wide key that follows other keys), `FeatureDef`, `EventDef`, `UiAreaDef` (optional `labels`: translated menu labels by language tag such as `de` or `en-GB`, `menu_label` stays the English fallback); served at `/.well-known/mowdeck-module` and aggregated for the web app by Identity's `GET /api/v1/identity/ui-areas` |
| `problem` | RFC 9457 `ProblemDetails` with the extensions `correlation_id` and `errors` |
| `spot` | Spot mowing (ADR 0030): `SpotMowPlan` (`plan_hash`, `pattern_chain`, `segments`, estimates, optional `native` `SpotNativeJob`) for `StartMow.spot_plan`, `SpotSegment` (`kind` `mow`, `turn` or `transfer`, `blades_on`, `speed_mps`, `path` as an encoded polyline of local metres with centimetre precision), `encode_polyline` / `decode_polyline`, `spot_plan_hash`; the hash must match the content and transfers must have the blades off; spot runs MowDeck drives itself (ADR 0039): `SpotCircle` (`center_wgs84`, `radius_m`) / `SpotPolygon` (`polygon_wgs84`) for `SpotMow.spot`, `SpotRunStatus` (`run_id`, `state`: `preparing`, `undocking`, `waiting_rtk`, `transit`, `spot_mowing`, `paused_rtk`, `returning`, `stopping`, `done`, `aborted`, `failed` (ADR 0041); `progress_percent`, `segment_index`, `segments_total`, `planned_points`, `driven_points`, `dry_run`, `reason`), `MAX_SPOT_MOW_SPEED_MPS` (0.3), `SPOT_REASON_STALLED` (`stalled`: no progress along the plan, ADR 0042); turn styles (ADR 0043): `TurnStyle` (`in_place`, `arc`, `multi_point`) for `SpotMow.turn_style` and `MowerState.turn_style`, `resolve_turn_style` (the command's, else the mower's, else `arc`; `multi_point` needs `MowerCapabilities.drive_reverse`), `SpotSegment.reverse` (driven backwards, blades off; adds `reverse` to the hash entry only when set) |
| `positions` | `PositionTrackRequest` / `PositionTrackReply` (`tracks.positions.query`: the position track of a mower for a time range as an encoded polyline of latitude and longitude, seven decimals) |
| `subjects` | NATS subject helpers and the JetStream stream definitions `COMMANDS`, `TELEMETRY`, `EVENTS`, `DEVICE`, `CAMERA` |

The JSON Schemas are committed in [`schemas/v1/`](schemas/v1), one file per model.

- `v1.vision` (ADR 0033): `PhotoRequest`/`PhotoReply` on `gateway.mower.<id>.photo` (`PhotoRequest.max_frames` 2 asks for both front cameras of the same moment; the reply then has `frames` (`PhotoFrame`: image, `media_type`, `captured_at`, `camera_id`, `camera_name`, `side`) and `frames_missing` instead of `image_base64`, all within `max_bytes`; the default 1 keeps the single image, ADR 0045) (core NATS, not below `device.`, which belongs to the `DEVICE` stream), the events `vision.proposal.created` (`VisionProposalCreated`) and `vision.proposal.updated` (`VisionProposalUpdated`, `VISION_PROPOSAL_UPDATED`: every later state change, `VisionProposalState`, `VISION_FINAL_STATES`), `VisionTask` and the plan feature keys `ai_recover` and `ai_recover_hosted` (siblings: plan keys are hierarchical). Also new in v1 for AI assist: `MowerState.drive_recoverable`, `DriveStartRequest.origin`/`DriveSessionView.origin` (`vision`), the drive end reasons `completed` and `aborted`, `CommandOrigin` `vision` and the notification kind `vision_proposal`.

## Rules the models enforce

- All timestamps are timezone-aware and normalised to UTC; naive timestamps are rejected.
- Models are immutable. Unknown fields are ignored (tolerant reader), so older consumers accept messages from newer producers.
- `CommandEnvelope.expires_at` must be after `issued_at`; consumers drop expired commands (`envelope.is_expired(now)`).
- `priority` is `emergency` for `EmergencyStop` and only for it. When omitted, it is derived from the command.
- `ManualDrive` is a dead-man sample: `linear` and `angular` in -1..1, `ttl_ms` in 100..2000 (default 1000).
- `StartMow` needs exactly one of `area_ids` (non-empty) or `plan_ref`.
- `SpotMow` drives at most 0.3 m/s and needs `confirm_blades` whenever `blades` is set; `FirmwareCheckReply` has exactly one of `info` or `error`.
- Event types, permission keys and similar identifiers are lower-case dotted keys such as `mower.registered` or `mower.control`.

Model-specific limits (for example the blade height range of one mower model) are enforced by the device adapter, not here.

## Subjects and streams

| Subject | Stream | Content |
| --- | --- | --- |
| `command.mower.<mower_id>` | `COMMANDS` | `CommandEnvelope` |
| `command.result.mower.<mower_id>` | `COMMANDS` | `CommandResult` |
| `telemetry.mower.<mower_id>` | `TELEMETRY` | `MowerState` |
| `event.<domain>.<name>` | `EVENTS` | `EventEnvelope`, e.g. `event.mower.registered` |
| `device.mower.<mower_id>.map`, `device.mower.<mower_id>.capabilities`, `device.mower.<mower_id>.path` | `DEVICE` | `MowerMapSnapshot`, `MowerCapabilitiesSnapshot`, `MowerPathSnapshot` (newest per subject) |
| `fleet.mowers.list` | none (core NATS request/reply) | `MowerListRequest`, answered with `MowerListReply` |
| `fleet.commands.issue` | none (core NATS request/reply, internal) | `IssueCommandRequest`, answered with `IssueCommandReply` |
| `fleet.targets.resolve` | none (core NATS request/reply, internal) | `ResolveTargetRequest`, answered with `ResolveTargetReply` |
| `fleet.drive.start`, `fleet.drive.stop` | none (core NATS request/reply, internal) | `DriveStartRequest` / `DriveStopRequest`, answered with `DriveStartReply` / `DriveStopReply` |
| `entitlements.check` | none (core NATS request/reply, internal) | `EntitlementCheckRequest`, answered with `EntitlementCheckReply` |
| `entitlements.plan.set` | none (core NATS request/reply, internal, Billing only) | `PlanSetRequest`, answered with `PlanSetReply` |
| `tracks.positions.query` | none (core NATS request/reply, internal; not below `telemetry.` because the `TELEMETRY` stream captures `telemetry.>`) | `PositionTrackRequest`, answered by Telemetry with `PositionTrackReply` |
| `gateway.mower.<mower_id>.camera.session` | none (core NATS request/reply, internal; outside `device.>` on purpose) | `CameraSessionRequest`, answered with `CameraSessionReply` by the Device-Gateway that holds the mower |
| `gateway.mower.<mower_id>.camera.release` | none (core NATS request/reply, internal) | `CameraReleaseRequest` from the camera relay, answered with `CameraReleaseReply` by the Device-Gateway that holds the mower (ADR 0045) |
| `gateway.mower.<mower_id>.path.preview` | none (core NATS request/reply, internal) | `PathPreviewGatewayRequest`, answered with `PathPreviewReply` by the Device-Gateway that holds the mower |
| `gateway.mower.<mower_id>.bridge.scan` | none (core NATS request/reply, internal) | `BridgeScanRequest`, answered with `BridgeScanReply` by the Device-Gateway that holds the mower (ADR 0038) |
| `gateway.mower.<mower_id>.firmware.check` | none (core NATS request/reply, internal) | `FirmwareCheckRequest`, answered with `FirmwareCheckReply` by the Device-Gateway that holds the mower (ADR 0038) |
| `fleet.mowers.credentials` (`FLEET_MOWERS_CREDENTIALS`) | none (core NATS request/reply, internal, queue `fleet`) | `MowerCredentialsRequest` from the Device-Gateway before a connect, answered with `MowerCredentialsReply` (`found`, `secret_options`, `link_secrets`, never in `repr`) by Fleet (ADR 0034) |
| `relay.camera.attach`, `relay.camera.renew`, `relay.camera.detach` (`RELAY_CAMERA_ATTACH`, `RELAY_CAMERA_RENEW`, `RELAY_CAMERA_DETACH`) | none (core NATS request/reply, internal, queue group `camera-relay`; not below `camera.` because the `CAMERA` stream captures `camera.>`) | `RelayAttachRequest`, `RelayRenewRequest`, `RelayDetachRequest` from Fleet or Vision, answered by camera-relay (ADR 0045) |
| `camera.mower.<mower_id>.relay.<kind>` | `CAMERA` | `EventEnvelope` with `CameraRelayStarted`, `CameraRelayStopped`, `CameraRelayLeaseOpened` or `CameraRelayLeaseClosed` (kinds `started`, `stopped`, `lease_opened`, `lease_closed`) |
| `gateway.cloud.discover` (`GATEWAY_CLOUD_DISCOVER`) | none (core NATS request/reply, internal, queue group `device-gateway`) | `CloudDiscoverRequest` from Fleet, answered with `CloudDiscoverReply` by any Device-Gateway instance (ADR 0034) |

```python
from uuid import uuid4
from mowdeck_contracts.v1.subjects import (
    command_subject,
    event_subject,
    event_wildcard,
    stream_for_subject,
)

mower_id = uuid4()
command_subject(mower_id)  # "command.mower.<id>"
event_subject("mower", "registered")  # "event.mower.registered"
event_wildcard("mower")  # "event.mower.>"
stream_for_subject("telemetry.mower.*")  # TELEMETRY stream spec
```

Device snapshots: `device_subject(mower_id, "map")` gives `device.mower.<id>.map` (or `"capabilities"`), `parse_device_subject(subject)` returns `(mower_id, kind)`, and `ALL_DEVICE_SNAPSHOTS` is the wildcard `device.mower.>`. Camera sessions: `camera_session_subject(mower_id)` gives `gateway.mower.<id>.camera.session`, `parse_camera_session_subject(subject)` the mower id, `ALL_CAMERA_SESSIONS` the gateway's wildcard `gateway.mower.*.camera.session`; no stream captures them. Camera releases (ADR 0045) likewise: `camera_release_subject(mower_id)` gives `gateway.mower.<id>.camera.release`, `parse_camera_release_subject`, `ALL_CAMERA_RELEASES`. Path previews: `path_preview_subject(mower_id)` gives `gateway.mower.<id>.path.preview`, `parse_path_preview_subject`, `ALL_PATH_PREVIEWS`. Bridge scans and firmware checks follow the same pattern: `bridge_scan_subject` / `parse_bridge_scan_subject` / `ALL_BRIDGE_SCANS` (`gateway.mower.<id>.bridge.scan`) and `firmware_check_subject` / `parse_firmware_check_subject` / `ALL_FIRMWARE_CHECKS` (`gateway.mower.<id>.firmware.check`). The snapshot kinds are `map`, `capabilities` and `path` (`DEVICE_SNAPSHOT_KINDS`); consumers of `device.mower.>` ignore the kinds they do not use.

Camera relay events: `camera_relay_event_subject(mower_id, "started")` gives `camera.mower.<id>.relay.started` (kinds in `CAMERA_RELAY_EVENT_KINDS`), `parse_camera_relay_event_subject(subject)` returns `(mower_id, kind)`, and `ALL_CAMERA_RELAY_EVENTS` is the wildcard `camera.mower.*.relay.*`.

The stream specs (`COMMANDS`, `TELEMETRY`, `EVENTS`, `DEVICE`, `CAMERA`) are plain data: retention, storage, maximum age, size limits and the duplicate window. `DEVICE` (`device.>`) is a last-value stream: one message per subject (`max_msgs_per_subject = 1`), kept 30 days, 256 MiB in total, duplicate window 120 s, so a consumer that starts late still finds the current map and capabilities of every mower. `CAMERA` (`camera.>`) keeps the camera relay's lifecycle events 30 days (64 MiB) for audit, metrics and the Vision daily limit. `mowdeck-service-kit` turns them into JetStream configurations.

## Examples

```python
from datetime import timedelta
from uuid import uuid4

from mowdeck_contracts.v1 import (
    CommandEnvelope,
    EmergencyStop,
    EventEnvelope,
    ManualDrive,
    Principal,
    PrincipalKind,
)

principal = Principal(
    subject=uuid4(), account_id=uuid4(), kind=PrincipalKind.USER, permissions=["mower.drive"]
)

command = CommandEnvelope.create(
    mower_id=uuid4(),
    account_id=principal.account_id,
    issued_by=principal.ref(),
    command=ManualDrive(linear=0.5, angular=0.0),
    ttl=timedelta(seconds=2),
)
assert command.priority == "normal"
assert not command.is_expired()

stop = CommandEnvelope.create(
    mower_id=command.mower_id,
    account_id=command.account_id,
    issued_by=principal.ref(),
    command=EmergencyStop(),
)
assert stop.priority == "emergency"

wire = stop.model_dump_json()
assert CommandEnvelope.model_validate_json(wire) == stop

event = EventEnvelope[dict[str, str]](
    type="mower.registered",
    producer="fleet",
    account_id=principal.account_id,
    payload={"mower_id": str(command.mower_id)},
)
```

Typed payloads: define a Pydantic model for the payload and use `EventEnvelope[MyPayload]`.

Mower registrations (Fleet to Device-Gateway):

```python
from mowdeck_contracts.v1 import MOWER_REGISTERED, ConnectionKind, EventEnvelope, MowerRegistration

registration = MowerRegistration(
    mower_id=uuid4(),
    account_id=principal.account_id,
    name="Front lawn",
    connection_kind=ConnectionKind.SIMULATED,
    connection_options={"time_scale": 30},  # adapter specific, never secrets
)
registered = EventEnvelope[MowerRegistration](
    type=MOWER_REGISTERED,
    producer="fleet",
    account_id=registration.account_id,
    payload=registration,
)
```

The Device-Gateway asks for the registrations of its shard with a core NATS request on `fleet.mowers.list` (`MowerListRequest(shard_index, shard_count)`, reply `MowerListReply(mowers=[...])`); a mower belongs to shard `i` of `n` when `mower_id.int % n == i`. See [ADR 0013](../../docs/adr/0013-device-gateway-registry-and-sharding.md).

## Versioning rules

- Within `v1` only backward compatible changes are allowed: new fields are optional (with a default), nothing is removed, renamed or narrowed.
- Events are never reinterpreted. A changed meaning or an incompatible payload gets a new event `version` (the `type` stays), and producers may publish both versions during a transition.
- An incompatible change to an envelope or model creates a new namespace `mowdeck_contracts.v2`; `v1` stays until no service uses it.
- The package follows Semantic Versioning; a new namespace is a minor release because `v1` keeps working.

See [ADR 0009](../../docs/adr/0009-command-and-event-envelopes.md).

## Regenerating the JSON Schemas

The test suite compares the committed schemas with a fresh export and fails when a model changed without regenerating them. After changing a model, run from the repository root:

```bash
uv run python -m mowdeck_contracts.schemas libs/mowdeck-contracts/schemas
```

This writes `schemas/v1/<Model>.json` (JSON Schema draft 2020-12, validation mode) and removes stale files. Commit the result together with the model change. The web app's TypeScript types are generated from these schemas (Realtime protocol) and from the services' OpenAPI documents: after a contract change also run `uv run poe openapi` and `pnpm codegen` (see `docs/dev/web-app.md`).

## Development

```bash
uv run pytest libs/mowdeck-contracts --cov=mowdeck_contracts
```
