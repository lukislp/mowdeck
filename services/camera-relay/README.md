# camera-relay

Shared camera sessions per account and mower: leases, grace, tickets and lifecycle events ([ADR 0045](../../docs/adr/0045-camera-relay.md)).

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; the layer rules and the definition of done are in `docs/dev/service-template.md`.

## Status

Step 3 of ADR 0045: the session domain, tickets and the request responders. The media plane (MediaMTX, WHEP, the transcoder, the auth callback) and the source drivers follow in later steps. Until then the source port is `NoSourceDriver`, which answers every start with `unavailable` (`no camera source driver is installed yet ...`): an attach opens a lease, the start fails at once, the lease closes and the reply is the error. The service is off by default (Compose profile `relay`, Helm `services.cameraRelay.enabled: false`) and has no route through Traefik.

## Purpose

The relay holds at most one source session per `(account_id, mower_id)` and lets any number of leases of that account share it. The same mower under two accounts is two keys: sessions, random media paths, tickets, leases and the Vision minutes are never shared or listable across accounts. Nothing in the service names a manufacturer; a source is a `CameraProvider` (`fake`, `agora`, ...), reached through the `VendorSessions` port.

It scales with concurrent sessions and runs as a **single replica**: sessions and leases live in memory and are lost on a restart (clients get `not_found` on their next renew and attach again).

## Sessions and leases

```text
IDLE -> STARTING -> LIVE -> DRAINING -> STOPPING -> IDLE
            |         |         ^  |
            |         +---------+  +-- a new lease within the grace: back to LIVE, same source
            +-> FAILED (start timeout or source failure) -> backoff -> IDLE
```

| Rule | Value (source) |
| --- | --- |
| Active cameras | union of the open leases' cameras; a change is sent to the source (`UpdateCameras`) |
| Viewer heartbeat | renew every 15 s, closed 45 s after the last one (`RELAY_HEARTBEAT_SECONDS`, `RELAY_LEASE_TTL_SECONDS`) |
| Viewer without confirmed media | closed 30 s after attach (`expired`) or after the media was lost (`media_lost`), unless a reader confirms media (`UNBOUND_LEASE_SECONDS`) |
| Grace after the last lease | 15 s, then the source is left and released (`RELAY_GRACE_SECONDS`) |
| Start timeout | 30 s without media: `FAILED` (`START_TIMEOUT_SECONDS`) |
| Backoff after failures | 5 s doubling to 60 s; `unavailable` meanwhile; reset when a session goes live |
| Vision snapshot | holds the session at most 30 s, never extended (`VISION_SNAPSHOT_HOLD_SECONDS`) |
| Vision stream | at most 300 s after the last pull (renew) (`VISION_STREAM_LEASE_SECONDS`) |
| Vision limits (only without a present viewer) | refused or ended below 15 % battery unless charging, below 10 % of the video budget, after 20 Vision-only minutes per account, mower and UTC day (`budget`); never on an offline, sleeping or unknown mower (`unavailable`) |
| Viewers | never refused for a limit (the attach logs a warning); refused only on an offline or sleeping mower, during `STOPPING` or the backoff, or when 32 leases are open |
| Mower offline or asleep | a running session stops (`mower_offline`), every lease closes |

The domain (`domain/session.py`) is a pure functional core: every input (`attach`, `renew`, `detach`, `media_confirmed`, `media_lost`, `source_started`, `source_failed`, `stop_completed`, `mower_status_changed`, `tick`, `shutdown`) takes the current time and returns effects as data: `StartVendorSession`, `UpdateCameras`, `StopVendorSession` (with `release`), `IssueTicket`, `KickViewer`, `EmitEvent`. Deadlines are processed in time order, each at its own due time, so event times are exact however late the tick runs. The application layer (`application/service.py`) locks the key, feeds the input and carries out the effects through the ports.

## Tickets

A ticket is `v1.<payload>.<signature>` (base64url), signed with Ed25519 and bound to the account, the mower, the session's random 128-bit path token, the lease and the lease's media paths `r/<relay_id>/<camera>`; it lives 60 s (`RELAY_TICKET_SECONDS`). `Ed25519TicketSigner.verify` checks signature, format and expiry; `TicketClaims.allows(account_id=..., path=..., now=...)` checks a requested path (used by the media server's auth callback in the next step). Paths never contain the mower id.

## Interfaces

No public HTTP routes.

| Method | Path | Permission | Description |
| --- | --- | --- | --- |
| GET | `/healthz` | none | Liveness |
| GET | `/readyz` | none | Readiness (bus and the request subscriptions); 503 while shutting down |
| GET | `/metrics` | none | Prometheus metrics |
| GET | `/.well-known/mowdeck-module` | none | Module descriptor (the four relay events, no permissions) |

## Events and requests

| Direction | Subject | Payload |
| --- | --- | --- |
| answered (core NATS, queue `camera-relay`) | `relay.camera.attach` | `RelayAttachRequest` / `RelayAttachReply` |
| answered | `relay.camera.renew` | `RelayRenewRequest` / `RelayRenewReply` (another account: `forbidden`; unknown or another mower: `not_found`) |
| answered | `relay.camera.detach` | `RelayDetachRequest` / `RelayDetachReply` (idempotent; another account: `forbidden`) |
| produced (stream `CAMERA`) | `camera.mower.<id>.relay.started` | `EventEnvelope[CameraRelayStarted]` |
| produced | `camera.mower.<id>.relay.stopped` | `EventEnvelope[CameraRelayStopped]` (reason, `started_at`, `vision_seconds`) |
| produced | `camera.mower.<id>.relay.lease_opened` | `EventEnvelope[CameraRelayLeaseOpened]` |
| produced | `camera.mower.<id>.relay.lease_closed` | `EventEnvelope[CameraRelayLeaseClosed]` (reason) |

The sender (Fleet, later Vision) has authorized the principal; a request that does not validate is dropped without a reply. Mower states (`mower_status_changed`), media confirmations from the media server and source reports are use cases without a subscription yet; later steps wire them.

## Settings

All settings of `mowdeck-service-kit` apply (`MOWDECK_*`, any value also from `MOWDECK_<NAME>_FILE`). Service specific settings and changed defaults:

| Variable | Default | Description |
| --- | --- | --- |
| `MOWDECK_SERVICE_NAME` | `camera-relay` | Service name in logs, metrics and traces |
| `MOWDECK_HTTP_PORT` | `8000` | HTTP port |
| `MOWDECK_RELAY_WHEP_BASE_URL` | `/whep` | Prefix of the WHEP URLs in replies: `<prefix>/r/<relay_id>/<camera>/whep` (the route comes with the media plane) |
| `MOWDECK_RELAY_TICKET_SIGNING_KEY` | unset | Ed25519 key of the tickets: PEM (PKCS#8) or the base64 encoded 32-byte seed, usually as `_FILE`. Unset: a new key per process (enough for the single replica) |
| `MOWDECK_RELAY_TICK_SECONDS` | `1` | How often deadlines are processed (bounds only the delay; event times are exact) |
| `MOWDECK_RELAY_MAX_LEASES_PER_SESSION` | `32` | Open leases per account and mower |

The timings and limits are the ADR 0045 defaults from `mowdeck_contracts.v1.relay` and are not configurable.

## Layers

| Package | Content | Rule |
| --- | --- | --- |
| `domain/` | `RelaySession` (state machine), leases, policy and limits, effects, ticket scope | pure Python, no I/O, no framework imports |
| `application/` | `RelayService` (use cases) and ports (`VendorSessions`, `MediaPlane`, `EventPublisher`, `TicketSigner`, `RelayRepository`, `Clock`) | depends on `domain` and ports only |
| `adapters/bus/` | request responders, the event publisher | translate messages to use cases |
| `adapters/` | in-memory repository, Ed25519 tickets, `NoSourceDriver` and `NoMediaPlane` | implement the ports |
| `main.py` | wiring (`build_app`), the tick task, `main()` | the only place that knows every layer |

## Local start

```bash
uv sync
docker compose -f infra/compose/docker-compose.yml up -d --wait nats
MOWDECK_ENVIRONMENT=dev uv run python -m camera_relay.main
curl http://127.0.0.1:8000/readyz
```

In the Compose stack: `docker compose --profile relay up -d --build camera-relay`.

Container image (build context is the repository root):

```bash
docker build -f services/camera-relay/Dockerfile -t mowdeck/camera-relay:dev .
```

## Tests

```bash
uv run pytest services/camera-relay --cov=camera_relay
```

The domain tests run the state machine with a fake time (reference counting, the exact grace, re-attach within the grace, binding and heartbeat deadlines, Vision caps and limits, the backoff, an offline mower, events); the service tests cover the replies, the effects and account separation with the same mower under two accounts; the ticket tests cover scope, expiry, tampering and foreign keys.
