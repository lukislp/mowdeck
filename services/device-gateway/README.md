# device-gateway

Holds the connections to mowers, executes commands and reports their state. It is the only MowDeck service that talks to mowers. It connects simulated mowers and, through the GPL-3.0 package [`mowdeck-control-mammotion`](../../libs/mowdeck-control-mammotion/README.md) installed in the image, real Mammotion mowers over the cloud (`cloud`) and over an ESPHome BLE proxy (`ble_bridge`); adapters plug in through the `mowdeck-control` registry ([ADR 0019](../../docs/adr/0019-mammotion-adapter-transports.md)).

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; layer rules in `docs/dev/service-template.md`. Design decisions: [ADR 0012](../../docs/adr/0012-mower-control-interface-and-safety-rules.md) (controller and safety rules) and [ADR 0013](../../docs/adr/0013-device-gateway-registry-and-sharding.md) (registry, sharding, concurrency).

## Purpose

- Keep a **volatile registry** of the mowers of this instance's shard: which mowers exist, their account, connection kind and options. Nothing is stored; the registry is rebuilt on every start.
- For each mower, create the connection through the `mowdeck-control` registry, wrap it in a `MowerController` (expiry, de-duplication, emergency stop priority, dead-man switch, blade rules) and reconnect with backoff when the link drops.
- Execute commands from `command.mower.<id>` and publish their results; publish telemetry and connection events, and the map and capabilities of every mower as snapshots.
- Answer live camera session requests for its mowers (credentials only; the video never passes through MowDeck).
- Log in to linked Mammotion accounts and list their devices for Fleet (`gateway.cloud.discover`), and keep **one cloud session per Mammotion account** that its mowers and the discovery share ([ADR 0034](../../docs/adr/0034-linked-cloud-accounts-and-device-discovery.md)).
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

Photo requests (ADR 0033): every instance subscribes to `gateway.mower.*.photo` (core NATS, no queue group); only the instance that holds the mower answers a `PhotoRequest` with a `PhotoReply` (base64 image, capped at `max_bytes`, at most 512 KiB) when the adapter implements `PhotoCapture` (the simulator), otherwise, when the capture worker is configured (`MOWDECK_CAPTURE_URL`, [ADR 0035](../../docs/adr/0035-server-side-camera-frames-and-automatic-analysis.md)) and the adapter offers a live camera (`CameraCapable`, the Mammotion cloud connection), with a frame of its camera: the session asks the adapter for a fresh camera session and posts it with the first camera's publisher uid (front left) to the worker (`HttpCameraSnapshotter`, `POST /api/v1/capture`, bearer `MOWDECK_CAPTURE_TOKEN`), one capture per mower at a time. Otherwise the reply is `unsupported`, `not_connected` (no stream token), `too_large` or `failed` (the worker's status in `detail`). A mower of another account answers `not_found`. `MOWDECK_PHOTO_TIMEOUT_SECONDS` (default 30) bounds the whole photo, `MOWDECK_CAPTURE_TIMEOUT_SECONDS` (default 20) the worker's wait for the first frame. While a drive session is open the telemetry heartbeat is `MOWDECK_TELEMETRY_DRIVE_HEARTBEAT_SECONDS` (default 1 s instead of 10 s), so AI assist's liveness watch (no state for 3 s aborts) also works for a mower that does not move.

## Events and subjects

Payload models live in `mowdeck_contracts.v1` (JSON Schemas in `libs/mowdeck-contracts/schemas/v1/`).

| Direction | Subject | Kind | Payload | Notes |
| --- | --- | --- | --- | --- |
| consumed | `command.mower.*` | JetStream `COMMANDS`, durable `device-gateway-<shard>` | `CommandEnvelope` | every instance sees every command and acknowledges commands of other shards without acting |
| produced | `command.result.mower.<id>` | JetStream `COMMANDS` | `CommandResult` | msg id `<command_id>:<status>`, so a result is stored once |
| produced | `telemetry.mower.<id>` | JetStream `TELEMETRY` | `MowerState` | on every significant change (anything except `observed_at` and `raw`) and as a heartbeat every `MOWDECK_TELEMETRY_HEARTBEAT_SECONDS` |
| produced | `device.mower.<id>.capabilities`, `device.mower.<id>.map` | JetStream `DEVICE` (newest per subject) | `MowerCapabilitiesSnapshot`, `MowerMapSnapshot` | after every connect, and the map again after a job ends; only when the content changed. Msg id `<mower_id>:capabilities|map:<sha256 prefix>` |
| produced | `event.mower.connected`, `event.mower.disconnected` | JetStream `EVENTS` | `EventEnvelope[MowerConnectionEvent]` | `mower_id`, `account_id`, `connection_kind`, `at`, `reason` (disconnect only) |
| consumed | `event.mower.registered`, `event.mower.updated`, `event.mower.removed` | JetStream `EVENTS`, ephemeral per instance | `EventEnvelope[MowerRegistration]` | from Fleet; add, update (reconnect if the link changed) or close a mower |
| reply | `gateway.mower.<id>.camera.session` | core NATS request/reply, every instance (no queue group) | `CameraSessionRequest` / `CameraSessionReply` | only the instance whose shard holds the mower answers; see [Camera sessions](#camera-sessions) |
| reply | `gateway.cloud.discover` | core NATS request/reply, queue group `device-gateway` (any instance answers) | `CloudDiscoverRequest` / `CloudDiscoverReply` | see [Cloud accounts](#cloud-accounts) |
| request | `fleet.mowers.credentials` | core NATS request/reply | `MowerCredentialsRequest` / `MowerCredentialsReply` | before every connect of a mower whose registration says `credentials: "fleet"`; see [Credentials](#credentials) |
| request | `fleet.mowers.list` | core NATS request/reply | `MowerListRequest` / `MowerListReply` | on start; retried with backoff (1 s doubling up to `MOWDECK_FLEET_RETRY_MAX_SECONDS`) while Fleet does not answer |

### Map and capabilities snapshots

After every (re)connect the mower's session reads `capabilities()` and the map from the controller and publishes each as a snapshot (`mowdeck_contracts.v1.device`) when its content fingerprint (SHA-256 of the JSON) differs from the one it published last; the fingerprint prefix is also the msg id, so JetStream de-duplicates repeats. When the activity changes from mowing, paused or returning to docked, charging or idle, a job has ended and zones may have changed in the vendor app, so the map is read again (with refresh) in the background. A failed read or publish is logged as a warning and never drops the connection: the snapshots serve clients, the mower works without them. Telemetry stores the snapshots and serves them at `/api/v1/telemetry/mowers/{id}/map` and `.../capabilities`.

### Command handling

1. Invalid envelopes and envelopes whose mower differs from the subject are dropped (terminated, not redelivered).
2. Commands for mowers of another shard are acknowledged and ignored.
3. Unknown mower: `rejected` (`unknown mower`). Account of the envelope differs from the registered owner: `rejected` with a warning log; the command never reaches the controller (tenancy defence in depth).
4. Otherwise the command runs in its own task on the mower's `MowerController`, and the result is published. Mower not connected (reconnecting): `failed` (`mower is not connected`).

Commands are acknowledged once admitted. The controller serializes the ordinary commands of one mower and lets an emergency stop overtake them; commands of different mowers never wait for each other.

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

### Credentials

Registrations from Fleet carry no secrets (ADR 0034): Fleet withholds `password`, `noise_psk` and the vendor `account` and marks the registration with `credentials: "fleet"`. Before every connect of such a mower (the first one and every reconnect, so also after a dead cloud login) the session asks Fleet on `fleet.mowers.credentials` and merges the answer into the options of that connection only; the session keeps the registration without secrets, nothing is logged. When Fleet does not answer or holds nothing for the mower, the connect fails and the usual backoff retries.

### Cloud accounts

[ADR 0034](../../docs/adr/0034-linked-cloud-accounts-and-device-discovery.md). The cloud connections of all mowers with the same Mammotion credentials share one logged-in PyMammotion client from the `CloudSessionPool` of `mowdeck-control-mammotion` (`adapters/control/cloud_accounts.py`): the first mower logs in, the others borrow the session (no second login, which would supersede the first at the Mammotion cloud), and each keeps its own connection, controller, state, refresh loop, map, camera and device information rounds. Closing a mower releases its share; the session is stopped two minutes after the last release and replaced when its login died (`reauth_required`). `RegistryConnectionProvider` builds `cloud` connections with the pooled factory; the other kinds still come from the `mowdeck-control` registry.

`gateway.cloud.discover`: Fleet sends a `CloudDiscoverRequest{provider, account_id, linked_account_id?, email, password}` when a user links or re-reads a Mammotion account. The gateway borrows the session for these credentials (logging in only when none is open) and answers the devices PyMammotion registered at login (`DiscoveredDevice`: name, IoT id, product key, model, online flag, `is_mower`: false for RTK base stations and pool cleaners) or an error: `login_failed` (the cloud refused the credentials), `unavailable` (no answer within `MOWDECK_CLOUD_DISCOVERY_TIMEOUT_SECONDS`, including the wait for one of the `MOWDECK_CLOUD_DISCOVERY_CONCURRENCY` login slots, or another failure; only the exception type is reported) or `unsupported`. Invalid requests are dropped without an answer. The password is never logged; the log line names the MowDeck account and the linked account. The session pool keys sessions by an HMAC of the address and the password (a random key per process) and logs only a short tag of the address; a login with a new password stops the old password's sessions as soon as they are unused. Each address gets its own PyMammotion credential cache in `MOWDECK_CLOUD_SESSION_CACHE_DIR` (unset: no cache); the per-mower `session_cache_path` option is not used for shared sessions.

**Several shards:** linked accounts are unsupported with `MOWDECK_SHARD_COUNT` above 1 (mowers of one account may land on different instances with separate sessions that log each other out); the gateway logs a warning once when it gets such a mower. Answers are counted in `mowdeck_device_gateway_cloud_discoveries_total{outcome}` (`listed` or the error code). A device added in the Mammotion app appears after the session is opened again.

`MOWDECK_FAKE_MAMMOTION_CLOUD=true` (development and the browser tests only; refused with `MOWDECK_ENVIRONMENT=prod`) replaces the Mammotion cloud with the fake one of the adapter package: accounts `*@fake.mowdeck.test` with the password `fake-cloud-password`, the mowers `Luba-FAKE0001` (online) and `Luba-FAKE0002` (offline) and the base station `RTK-FAKE0001`; registered cloud mowers connect to a `FakeMammotionPort` through the same pool.

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
| `MOWDECK_PHOTO_TIMEOUT_SECONDS` | `30` | Longest time for a photo (`gateway.mower.<id>.photo`), a camera frame of the capture worker included; keep it below Vision's `MOWDECK_VISION_PHOTO_TIMEOUT_SECONDS` |
| `MOWDECK_CAPTURE_URL` | unset | Base URL of the capture worker (`http://vision-capture:8000`, ADR 0035); unset: camera-capable adapters without an own photo answer `unsupported` |
| `MOWDECK_CAPTURE_TOKEN` (`_FILE`) | unset | Shared secret sent to the worker |
| `MOWDECK_CAPTURE_TIMEOUT_SECONDS` | `20` | How long the worker may wait for the first decoded frame |
| `MOWDECK_CAPTURE_MAX_WIDTH` | `1280` | Wider frames are scaled down by the worker |
| `MOWDECK_FAKE_MAMMOTION_CLOUD` | `false` | **Development only.** The fake Mammotion cloud for discoveries and cloud mowers (Compose: `GATEWAY_FAKE_MAMMOTION_CLOUD`) |
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
| `domain/rules.py` | `Shard`, `ConnectionStatus`, `command_rejection`, `is_significant_change`, `backoff` (pure) |
| `application/` | `DeviceGateway` (registry, admission, Fleet sync, shutdown), `MowerSession` (link supervision, telemetry, commands), `CloudDiscoveryService`, ports |
| `adapters/bus/` | command consumer, registry events, camera session responder (`camera.py`), cloud discovery responder (`cloud.py`), Fleet request, publisher |
| `adapters/control/` | `RegistryConnectionProvider`: connections from the `mowdeck-control` registry (simulated, `ble_bridge` from `mowdeck-control-mammotion`) and `cloud` from the shared session pool; `cloud_accounts.py`: pool, cloud factory and discovery (real or fake); simulator clock |
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

The tests use the in-memory bus and simulated mowers (`tests/dg_support.py` has the helpers). The JetStream flow test runs when `MOWDECK_TEST_NATS_URL` is set:

```bash
docker run -d --name mowdeck-test-nats -p 4222:4222 public.ecr.aws/docker/library/nats:2.15.0-alpine -js
MOWDECK_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest services/device-gateway
docker rm -f mowdeck-test-nats
```
