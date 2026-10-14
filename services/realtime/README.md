# realtime

Live mower state and command results over WebSocket.

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; the layer rules and the definition of done are in `docs/dev/service-template.md`. Decision record: [ADR 0018](../../docs/adr/0018-realtime-websocket-protocol.md). Design: [Design: fleet, telemetry, realtime](../../docs/dev/design-fleet-telemetry-realtime.md), section 3 (corrections in 3.0).

## Purpose

- Push the newest `MowerState` and every `CommandResult` of the mowers a client subscribed to, over one WebSocket per client.
- Authenticate with Identity's access token in the **first message** and check every subscription with `can_view` in OpenFGA, again on `identity.grants.changed`, every 60 s and on every token refresh.
- Own no data: no database; a per-instance last-value cache of states (so a new subscription gets the current state at once) is the only memory. Scales with the number of open sockets and the telemetry rate (every instance reads the whole stream).

## Interfaces

All routes are mounted under `/api/v1` (OpenAPI at `/api/v1/openapi.json`). Traefik routes `/api/v1/realtime` to this service.

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| GET (upgrade) | `/api/v1/realtime/ws` | token in the first message | The WebSocket |
| GET | `/healthz` | none | Liveness |
| GET | `/readyz` | none | Readiness: the bus and both fan-out subscriptions; 503 while shutting down |
| GET | `/metrics` | none | Prometheus metrics |
| GET | `/.well-known/mowdeck-module` | none | Module descriptor |

Permission declared in the module descriptor: `realtime.view` ("Receive live state and command results of mowers"), an account key implied by `mower.view` (for the catalogue and the UI). The socket never reads it from the token; it checks `can_view` on every subscribed mower, so a viewer of one group receives exactly that group's mowers.

### Protocol

One JSON object per text frame, discriminated by `type`. Models and JSON Schemas: `mowdeck_contracts.v1.realtime` (`RealtimeClientMessage`, `RealtimeServerMessage` in `libs/mowdeck-contracts/schemas/v1/`). `request_id` is optional (1 to 64 of `A-Z a-z 0-9 . _ : -`) and echoed in the answer.

| Direction | `type` | Example | Notes |
| --- | --- | --- | --- |
| client | `auth` | `{"type": "auth", "token": "eyJ..."}` | first message within 5 s; later again with a fresh token (same subject and account) |
| client | `subscribe` | `{"type": "subscribe", "request_id": "s1", "mower_ids": ["9a5c..."]}` | at most 200 subscriptions per socket |
| client | `unsubscribe` | `{"type": "unsubscribe", "mower_ids": ["9a5c..."]}` | |
| client | `ping` | `{"type": "ping", "request_id": "p1"}` | application liveness (browsers cannot see protocol pings) |
| server | `ready` | `{"type": "ready", "connection_id": "...", "principal": {"subject": "...", "account_id": "...", "kind": "subuser", "display_name": "Anna"}, "token_expires_at": "2026-10-09T12:10:00Z", "server_time": "2026-10-09T12:00:00Z"}` | after every accepted `auth` |
| server | `subscribed` | `{"type": "subscribed", "request_id": "s1", "mower_ids": ["9a5c..."], "denied": ["7e01..."]}` | denied: unknown or not visible (indistinguishable) |
| server | `unsubscribed` | `{"type": "unsubscribed", "mower_ids": ["9a5c..."]}` | |
| server | `state` | `{"type": "state", "mower_id": "9a5c...", "state": {"activity": "mowing", "battery_percent": 98, "...": "..."}}` | newest state; after `subscribed` the cached one follows at once |
| server | `command_result` | `{"type": "command_result", "mower_id": "9a5c...", "result": {"command_id": "...", "status": "executed", "reason": "mowing front with stripes"}}` | every status transition |
| server | `revoked` | `{"type": "revoked", "mower_ids": ["9a5c..."], "reason": "access_revoked"}` | a re-check denied the mower; no further updates |
| server | `error` | `{"type": "error", "request_id": "s2", "code": "invalid_message", "message": "not a valid client message"}` | codes `invalid_message`, `unauthorized`, `limit_exceeded`, `internal`, and for driving `drive_not_allowed`, `drive_unsupported`, `drive_busy`, `drive_unavailable`, `drive_not_active` |
| server | `pong` | `{"type": "pong", "request_id": "p1"}` | |
| client | `drive_start` | `{"type": "drive_start", "request_id": "d1", "mower_id": "9a5c...", "blade_on_allowed": false}` | manual driving (ADR 0026): `can_drive`, `manual_drive` capability, Fleet session; one drive session per socket |
| client | `drive_frame` | `{"type": "drive_frame", "session_id": "...", "seq": 17, "linear": 0.6, "angular": -0.2, "blade_on": false}` | about 10 per second while the stick is held; above `MOWDECK_REALTIME_DRIVE_MAX_RATE_HZ` frames are dropped |
| client | `drive_stop` | `{"type": "drive_stop", "session_id": "..."}` | a zero frame stops the mower at once |
| server | `drive_started` | `{"type": "drive_started", "request_id": "d1", "session_id": "...", "mower_id": "9a5c...", "max_rate_hz": 10, "ttl_ms": 1000, "max_seconds": 600, "blade_on_allowed": false, "capabilities": {"manual_drive": true, "manual_drive_blades": true}}` | send frames now |
| server | `drive_frame_ack` | `{"type": "drive_frame_ack", "session_id": "...", "seq": 17}` | only for frames with `seq`, once published to the mower |
| server | `drive_ended` | `{"type": "drive_ended", "session_id": "...", "mower_id": "9a5c...", "reason": "client_stop", "detail": null}` | reasons `client_stop`, `socket_closed`, `revoked`, `token_expired`, `max_duration`, `mower_refused`, `mower_busy`, `replaced` |

A browser client: open the socket, send `auth`, wait for `ready`, send `subscribe` with the ids from `GET /api/v1/fleet/mowers`, send `auth` again with the refreshed token before `token_expires_at`, reconnect with backoff on 1001, 1006, 4430 and after a 4401 caused by expiry (with a new token).

### Close codes

| Code | When |
| --- | --- |
| 1001 | The instance shuts down (going away); reconnect |
| 1009 | A frame larger than `MOWDECK_REALTIME_MAX_MESSAGE_BYTES` |
| 1011 | Unexpected server error |
| 4400 | A binary frame (text frames only) |
| 4401 | No `auth` within the timeout, the first message is not `auth`, an invalid token, a service principal or one without account, a token of another principal, or the token expired without refresh |
| 4429 | Connection limit of the user or account, or subscription limit of the socket (an `error` `limit_exceeded` comes first) |
| 4430 | Slow consumer: the send queue overflowed or a send took longer than the send timeout |

## Events and subjects

| Direction | Subject | Kind | Payload | Notes |
| --- | --- | --- | --- | --- |
| consumed | `telemetry.mower.*` | JetStream `TELEMETRY`, ephemeral push consumer, deliver new, no queue group | `MowerState` | every instance gets every state |
| consumed | `command.result.mower.*` | JetStream `COMMANDS`, ephemeral, deliver new, no queue group | `CommandResult` | |
| consumed | `event.identity.grants.changed` | JetStream `EVENTS`, ephemeral | `EventEnvelope[GrantsChanged]` | drops cached decisions of the subjects, then re-checks their sockets (subscriptions and drive sessions) |
| consumed | `device.mower.*.capabilities` | JetStream `DEVICE`, ephemeral, deliver last per subject | `MowerCapabilitiesSnapshot` | capability cache for `drive_start` (`manual_drive`, `manual_drive_blades`) |
| request | `fleet.drive.start`, `fleet.drive.stop` | core NATS request to Fleet | `DriveStartRequest`/`DriveStopRequest` | opens and closes the drive session record (ADR 0026) |
| published | `command.mower.<id>` | JetStream `COMMANDS` | `CommandEnvelope` with `ManualDrive` | one per accepted frame, `ttl_ms` and expiry `MOWDECK_REALTIME_DRIVE_TTL_MS`, `correlation_id` `drive:<session_id>`, plus a zero frame when the session ends while the last frame is still live |

### Manual driving

`drive_start` first checks the plan feature `manual_drive` through the Entitlements client of the kit ([ADR 0028](../../docs/adr/0028-entitlements-plans-and-enforcement.md)); without it the answer is `error` with `drive_not_allowed` and Fleet is not asked. The client caches the account's plan snapshot and drops it on `event.entitlements.plan.changed` (every instance); while billing is disabled every account has the plan `unlimited`.

Realtime adds no safety rule: the Device-Gateway's controller re-arms its dead-man timer with every frame and applies the blade rules, so the mower stops within `ttl_ms` when frames stop for any reason. A session ends on `drive_stop`, socket close, a re-check that finds `can_drive` revoked, token expiry, `MOWDECK_REALTIME_DRIVE_MAX_SECONDS`, a rejected or failed frame result (`mower_busy` while a job runs, else `mower_refused`) or a new `drive_start` on the same socket. Accepted frame results are not fanned out to subscribers (refusals are). The emergency stop stays on Fleet's command endpoint.

Nothing is produced. Malformed messages and messages whose subject names another mower are dropped (`DropMessage`).

### Fan-out and backpressure

The hub keeps `mower_id -> sessions` and hands each message, encoded once, to the subscribed sessions without awaiting a client. Per session a writer task sends: first the bounded queue (`MOWDECK_REALTIME_QUEUE_SIZE`, everything except states), then the state slots (one per mower; a newer state replaces an unsent older one, metric reason `coalesced`). A full queue or a send slower than `MOWDECK_REALTIME_SEND_TIMEOUT_SECONDS` closes the socket with 4430. A state is only handed to sessions of the state's own account.

### Heartbeat

Uvicorn's WebSocket protocol pings, every `MOWDECK_REALTIME_PING_SECONDS` (20) with the same pong timeout. They keep idle sockets open through Traefik (verified by the acceptance test with a 90 s idle socket) and close half-open connections. Clients may additionally send `ping` and expect `pong`.

## Settings

All settings of `mowdeck-service-kit` apply (`MOWDECK_*`, any value also from `MOWDECK_<NAME>_FILE`). OpenFGA: `MOWDECK_OPENFGA_*` of `mowdeck-authz`. Tokens: `MOWDECK_AUTH_*` of `mowdeck-auth` (without `MOWDECK_AUTH_JWKS_URL` every socket is closed with 4401).

| Variable | Default | Description |
| --- | --- | --- |
| `MOWDECK_SERVICE_NAME` | `realtime` | Service name in logs, metrics and traces |
| `MOWDECK_REALTIME_AUTH_TIMEOUT_SECONDS` | `5` | Time for the first `auth` message |
| `MOWDECK_REALTIME_RECHECK_SECONDS` | `60` | Periodic re-check of every subscription |
| `MOWDECK_REALTIME_MAX_CONNECTIONS_PER_ACCOUNT` | `50` | Open sockets per account and instance |
| `MOWDECK_REALTIME_MAX_CONNECTIONS_PER_USER` | `10` | Open sockets per user (or API token) and instance |
| `MOWDECK_REALTIME_MAX_SUBSCRIPTIONS` | `200` | Subscribed mowers per socket |
| `MOWDECK_REALTIME_QUEUE_SIZE` | `256` | Unsent non-state messages per socket before 4430 |
| `MOWDECK_REALTIME_SEND_TIMEOUT_SECONDS` | `10` | A single send slower than this closes with 4430 |
| `MOWDECK_REALTIME_PING_SECONDS` | `20` | Protocol ping interval and pong timeout (0 disables) |
| `MOWDECK_REALTIME_MAX_MESSAGE_BYTES` | `16384` | Largest client frame (uvicorn `ws_max_size`) |
| `MOWDECK_REALTIME_LAST_STATE_CACHE_SIZE` | `10000` | Mowers whose newest state is cached (0 disables) |
| `MOWDECK_REALTIME_DRIVE_MAX_SECONDS` | `600` | A manual drive session ends after this many seconds |
| `MOWDECK_REALTIME_DRIVE_MAX_RATE_HZ` | `10` | Joystick frames per second and session forwarded to the mower; more are dropped |
| `MOWDECK_REALTIME_DRIVE_TTL_MS` | `1000` | Dead-man timeout of every frame (`ManualDrive.ttl_ms`, 100 to 2000) |
| `MOWDECK_REALTIME_DRIVE_REQUEST_TIMEOUT_SECONDS` | `2` | Timeout of `fleet.drive.start` and `fleet.drive.stop` |
| `MOWDECK_REALTIME_ALLOWED_ORIGINS` | empty | Comma separated allowed `Origin` values; empty allows all (authentication is never a cookie) |

## Metrics

| Metric | Type | Labels |
| --- | --- | --- |
| `mowdeck_realtime_connections` | gauge | |
| `mowdeck_realtime_subscriptions` | gauge | |
| `mowdeck_realtime_messages_sent_total` | counter | `type` |
| `mowdeck_realtime_messages_dropped_total` | counter | `reason`: `coalesced`, `slow_consumer`, `account_mismatch`, `invalid` |
| `mowdeck_realtime_closed_total` | counter | `code` (`none`: the client left) |
| `mowdeck_realtime_drive_sessions` | gauge | |
| `mowdeck_realtime_drive_frames_total` | counter | `outcome`: `published`, `rate_limited`, `failed` |

Plus the kit's HTTP metrics and `mowdeck_authz_*`.

## Scaling and operations

- **Replicas** need no sticky sessions and no shared state: every instance receives every message. Limits are per instance (a user may open `limit x replicas` sockets).
- **Cost**: every instance decodes the whole telemetry stream. Next step when that matters: one core subscription per mower with local subscribers.
- **OpenFGA outage**: open sockets keep their updates (re-checks are skipped while `ping()` fails), new subscriptions are denied. Readiness does not include OpenFGA on purpose.
- **NATS**: the ephemeral consumers do not survive a NATS restart that outlasts their inactivity threshold; restart the service then (follow-up: recreate them in the kit).
- **Shutdown**: `RealtimeServer` closes every socket with 1001 before uvicorn would close them with 1012, then the lifespan shutdown unsubscribes. Keep `stop_grace_period` above `MOWDECK_SHUTDOWN_GRACE_SECONDS`.
- **Traefik** passes the upgrade through without extra configuration (`PathPrefix(/api/v1/realtime)`, port 8000). `Origin` reaches the service unchanged; the protocol pings keep idle sockets well below Traefik's timeouts.

## Layers

| Package | Content |
| --- | --- |
| `domain/` | value objects (`Authenticated`, `CloseRequest`), rules (acceptable principal, same principal on refresh, close reason length), errors |
| `application/` | `Hub` (sessions, limits, mower index, last-value cache, fan-out, re-checks, shutdown), `Session` (protocol, expiry timer, reader and writer), `Outbox` (state slots and bounded queue), ports |
| `adapters/api/` | the WebSocket route and the Starlette socket adapter |
| `adapters/bus/` | the four subscriptions and the drive backend (`fleet.drive.*` requests, frame publishing) |
| `adapters/` | `auth.py` (`mowdeck-auth` verifier), `authz.py` (`check_many(can_view)`), `metrics.py` (Prometheus), `server.py` (`RealtimeServer`) |
| `main.py` | wiring (`build_app`), uvicorn options, `main()` |

## Local start

```bash
uv sync
cp infra/compose/.env.example infra/compose/.env
docker compose -f infra/compose/docker-compose.yml up -d --build --wait
```

Then, with a token from `POST /api/v1/identity/session`, any WebSocket client works, for example:

```bash
uv run python -m websockets ws://localhost/api/v1/realtime/ws
> {"type": "auth", "token": "<access token>"}
> {"type": "subscribe", "mower_ids": ["<mower id>"]}
```

Container image (build context is the repository root):

```bash
docker build -f services/realtime/Dockerfile -t mowdeck/realtime:dev .
```

The Compose block is in `compose.snippet.yml` (pasted into `infra/compose/docker-compose.yml`), Kubernetes uses the umbrella chart `infra/helm/mowdeck`.

## Tests

```bash
uv run pytest services/realtime --cov=realtime
```

`test_realtime_protocol.py` serves the app with uvicorn in-process (the production server class) and talks real WebSockets with the `websockets` client: authentication, subscriptions with allowed and denied ids, delivery, cross-account isolation, revocation by event and by the periodic re-check, token expiry and refresh, limits, overflow, oversize and binary frames, origin check and shutdown. `test_realtime_session.py` tests backpressure on a fake slow socket (coalescing, send timeout, queue overflow). `test_realtime_nats.py` runs two instances on one NATS with `MOWDECK_TEST_NATS_URL` (CI sets it):

```bash
docker run -d --name realtime-test-nats -p 4222:4222 public.ecr.aws/docker/library/nats:2.15.0-alpine -js
MOWDECK_TEST_NATS_URL=nats://127.0.0.1:4222 uv run pytest services/realtime -k nats
docker rm -f realtime-test-nats
```

The Phase 1 acceptance test (`tests/e2e`) covers Realtime through Traefik against the full stack.
