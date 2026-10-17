# camera-relay

Shared camera sessions per account and mower: leases, grace, tickets, lifecycle events and the media plane that serves them over WebRTC (WHEP) through MediaMTX ([ADR 0045](../../docs/adr/0045-camera-relay.md)).

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; the layer rules and the definition of done are in `docs/dev/service-template.md`.

## Status

Steps 3 to 5 of ADR 0045: the session domain, tickets, the request responders and the media plane with the simulator's `fake` source; Fleet's camera endpoint is the front door (`transport` `auto`, `relay`, `direct`). A viewer that attaches gets WHEP URLs and a ticket and plays real H.264 through MediaMTX. Sources without a driver yet (Agora, step 7) are answered with `unsupported_provider`, so Fleet's `auto` falls back to the direct path; the web player (step 6) follows. The service is off by default (Compose profile `relay`, Helm `services.cameraRelay.enabled: false`); the test stacks (`docker-compose.e2e.yml`) always run it.

Licence of the image: the relay is AGPL-3.0; it runs Debian's `ffmpeg` with libx264 (GPL), which is fine for this image (the relay only executes the binary). MediaMTX is a separate, MIT licensed container.

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

## Media plane

```text
browser --WHEP POST /media/whep/r/<rid>/<cam>/whep (Traefik, prefix stripped)--> MediaMTX :8889
        <==== ICE media on one UDP+TCP port (RELAY_ICE_PORT, default 8189) ====
MediaMTX --auth callback for every read, publish and API call--> camera-relay /internal/mediamtx/auth
camera-relay --RTSP publish (internal)--> MediaMTX r/<rid>/<cam>          (fake source: H.264)
vendor source --RTSP--> r/<rid>/<cam>/src --transcoder--> r/<rid>/<cam>   (H.265 -> H.264, step 7)
camera-relay --API every 2 s (readers, paths, kicks)--> MediaMTX :9997
```

- **Offer.** Before the first lease of an idle key the relay asks the Device-Gateway for the camera session (`gateway.mower.<id>.camera.session`, service principal, the lease's account) and reads its provider and cameras (`VendorSessions.offer`). **A provider without a source driver is answered with `unsupported_provider`** before any lease, event or start exists and **without releasing** that vendor session: a client in `auto` (Fleet) falls back to the direct path, and that viewer needs the mower's stream. No backoff follows; the next attach asks again. An attach without `camera_ids` gets every offered camera; named cameras the mower does not offer are left out (none left: `unavailable`, and the offered vendor session is released, since nothing starts). Replies carry the lease's cameras as `CameraInfo` (`cameras`).
- **Start.** The first lease of a key starts the source with the offered session (fetched again when it is older than 30 s, `OFFER_REUSE_SECONDS`) through the driver of its `CameraProvider`. `fake` starts one ffmpeg per camera: `testsrc2` 640x480 at 10 fps with an overlay (mower id short, camera id, wall clock in ms), H.264 Constrained Baseline, no B-frames, 1 s GOP, `-fps_mode passthrough`. It is paced by the `realtime` filter instead of `-re`, so a paused source resumes at 10 fps instead of catching up in a burst. A provider that lost its driver between offer and start fails the session without a release. The session goes `LIVE` when the reconcile loop sees a camera's path ready.
- **Transcoder** (generic, for source drivers that deliver to `.../src`): `ffmpeg -fflags nobuffer -flags low_delay -i rtsp://.../src -an -c:v libx264 -preset ultrafast -tune zerolatency -profile:v baseline -bf 0 -g 10 -fps_mode passthrough -f rtsp .../r/<rid>/<cam>`. It never repeats a frame, and a stalled input produces no frame: a frozen source is a frozen picture.
- **Who may do what** (MediaMTX `authMethod: http`): a WHEP read of `r/<rid>/<cam>` only with a relay ticket (bearer token) that is valid, unexpired, names that path and belongs to a live lease of a running session; the reader's MediaMTX session id is bound to the lease. Publishing, RTSP reads (the transcoder) and the API only with the internal publisher credential (`MOWDECK_RELAY_PUBLISHER_USER` and `_SECRET`, a random secret per process by default). Metrics, playback and pprof are off and denied; every other path is refused (there is no `all_others`). No ticket or password is logged.
- **Reconcile loop** (every 2 s, `/v3/webrtcsessions/list` and `/v3/paths/list`): a bound reader whose `bytesSent` grows confirms its lease's media (`media_confirmed`); one that stalls for three polls or disappears is `media_lost` (the lease may reconnect within its binding deadline); an unbound reader is kicked. A closed lease kicks its reader (`/v3/webrtcsessions/kick/<id>`).
- **Stop.** 15 s after the last lease the source is stopped and the gateway is asked to release the stream (`gateway.mower.<id>.camera.release` with the session's channel).
- **Freeze hook** (tests only, `MOWDECK_RELAY_TEST_HOOKS=true`, refused with `MOWDECK_ENVIRONMENT=prod`): `POST /internal/v1/test/freeze` with `{"frozen": true|false, "relay_id": optional}` pauses (SIGSTOP) or resumes the fake sources. A freeze longer than MediaMTX's `readTimeout` (10 s) ends the publisher.

### Ports and firewall

| Port | Purpose | Reachable from |
| --- | --- | --- |
| camera-relay 8000 | `/internal/mediamtx/auth`, health, metrics, the test hook | the internal network only (no Traefik route) |
| MediaMTX 8889 | WHEP signalling | Traefik only: `PathPrefix(/media/whep/r/)`, `/media/whep` stripped |
| MediaMTX 9997 | control API | the internal network only, every call authorized by the relay |
| MediaMTX 8554 | RTSP of the pipeline | the internal network only |
| MediaMTX `RELAY_ICE_PORT` (8189) | ICE media, **UDP and TCP** | **LAN clients**: open it in the host firewall; the container port equals the host port |

`RELAY_ICE_HOSTS` lists the addresses clients reach the host under (the LAN IP); MediaMTX advertises only these (no interface addresses, no STUN, no TURN). The configuration is `infra/compose/mediamtx/mediamtx.yml` (the Helm chart has a copy).

MediaMTX returns the WHEP session URL (`Location`) as `/r/<rid>/<cam>/whep/<secret>`: a client prefixes it with `/media/whep` for its `PATCH` and `DELETE`.

## Interfaces

No public HTTP routes.

| Method | Path | Permission | Description |
| --- | --- | --- | --- |
| GET | `/healthz` | none | Liveness |
| GET | `/readyz` | none | Readiness (bus and the request subscriptions); 503 while shutting down |
| GET | `/metrics` | none | Prometheus metrics |
| GET | `/.well-known/mowdeck-module` | none | Module descriptor (the four relay events, no permissions) |
| POST | `/internal/mediamtx/auth` | MediaMTX (internal network) | Auth callback: `200` allowed, `401` denied |
| POST | `/internal/v1/test/freeze` | only with `MOWDECK_RELAY_TEST_HOOKS=true` | Pause or resume the fake sources |

## Events and requests

| Direction | Subject | Payload |
| --- | --- | --- |
| answered (core NATS, queue `camera-relay`) | `relay.camera.attach` | `RelayAttachRequest` / `RelayAttachReply` (empty `camera_ids`: every offered camera; no driver for the provider: `unsupported_provider`, nothing released) |
| answered | `relay.camera.renew` | `RelayRenewRequest` / `RelayRenewReply` (another account: `forbidden`; unknown or another mower: `not_found`) |
| answered | `relay.camera.detach` | `RelayDetachRequest` / `RelayDetachReply` (idempotent; another account: `forbidden`) |
| requested (core NATS) | `gateway.mower.<id>.camera.session` | `CameraSessionRequest` / `CameraSessionReply` (service principal, the lease's account) |
| requested | `gateway.mower.<id>.camera.release` | `CameraReleaseRequest` / `CameraReleaseReply` (after the grace) |
| produced (stream `CAMERA`) | `camera.mower.<id>.relay.started` | `EventEnvelope[CameraRelayStarted]` (`trigger`: the purpose of the starting lease, `started_by`) |
| produced | `camera.mower.<id>.relay.stopped` | `EventEnvelope[CameraRelayStopped]` (reason, `started_at`, `vision_seconds`, `trigger`, `peak_viewers`) |
| produced | `camera.mower.<id>.relay.lease_opened` | `EventEnvelope[CameraRelayLeaseOpened]` |
| produced | `camera.mower.<id>.relay.lease_closed` | `EventEnvelope[CameraRelayLeaseClosed]` (reason) |

The sender (Fleet, later Vision) has authorized the principal; Fleet turns `started` and `stopped` into its audit rows `mower.camera_stream_started` and `mower.camera_stream_stopped`; a request that does not validate is dropped without a reply. Media confirmations and source readiness come from the reconcile loop; mower states (`mower_status_changed`) are a use case without a subscription yet.

## Settings

All settings of `mowdeck-service-kit` apply (`MOWDECK_*`, any value also from `MOWDECK_<NAME>_FILE`). Service specific settings and changed defaults:

| Variable | Default | Description |
| --- | --- | --- |
| `MOWDECK_SERVICE_NAME` | `camera-relay` | Service name in logs, metrics and traces |
| `MOWDECK_HTTP_PORT` | `8000` | HTTP port |
| `MOWDECK_RELAY_WHEP_BASE_URL` | `/media/whep` | Prefix of the WHEP URLs in replies: `<prefix>/r/<relay_id>/<camera>/whep` (Traefik strips `/media/whep`) |
| `MOWDECK_RELAY_TICKET_SIGNING_KEY` | unset | Ed25519 key of the tickets: PEM (PKCS#8) or the base64 encoded 32-byte seed, usually as `_FILE`. Unset: a new key per process (enough for the single replica) |
| `MOWDECK_RELAY_TICK_SECONDS` | `1` | How often deadlines are processed (bounds only the delay; event times are exact) |
| `MOWDECK_RELAY_MAX_LEASES_PER_SESSION` | `32` | Open leases per account and mower |
| `MOWDECK_RELAY_MEDIAMTX_API_URL` | unset | MediaMTX's control API, e.g. `http://mediamtx:9997`. Unset: no media plane, every attach is `unavailable` |
| `MOWDECK_RELAY_MEDIAMTX_RTSP_URL` | `rtsp://mediamtx:8554` | MediaMTX's RTSP server for the pipeline (no credentials) |
| `MOWDECK_RELAY_PUBLISHER_USER` | `relay` | User of the relay's pipeline towards MediaMTX (publish, internal reads, API) |
| `MOWDECK_RELAY_PUBLISHER_SECRET` | unset | Its password, usually as `_FILE`. Unset: a random secret per process |
| `MOWDECK_RELAY_RECONCILE_SECONDS` | `2` | Interval of the reconcile loop |
| `MOWDECK_RELAY_MEDIA_STALL_POLLS` | `3` | Polls without growing `bytesSent` before a reader counts as lost |
| `MOWDECK_RELAY_GATEWAY_TIMEOUT_SECONDS` | `10` | Timeout of the gateway's camera session and release requests |
| `MOWDECK_RELAY_SERVICE_SUBJECT` | a fixed UUID | Subject of the relay's service principal in gateway requests |
| `MOWDECK_RELAY_FFMPEG_PATH` | `ffmpeg` | ffmpeg of the pipeline |
| `MOWDECK_RELAY_FAKE_FONT` | DejaVu Sans Mono | Font of the fake source's overlay |
| `MOWDECK_RELAY_TEST_HOOKS` | `false` | Serve the freeze hook (test stacks; refused with `MOWDECK_ENVIRONMENT=prod`) |

The timings and limits are the ADR 0045 defaults from `mowdeck_contracts.v1.relay` and are not configurable.

## Layers

| Package | Content | Rule |
| --- | --- | --- |
| `domain/` | `RelaySession` (state machine), leases, policy and limits, effects, ticket scope | pure Python, no I/O, no framework imports |
| `application/` | `RelayService` (use cases), `MediaAuthorizer` (the auth callback's decisions) and ports (`VendorSessions`, `MediaPlane`, `EventPublisher`, `TicketSigner`, `RelayRepository`, `Clock`) | depends on `domain` and ports only |
| `adapters/bus/` | request responders, the event publisher | translate messages to use cases |
| `adapters/media/` | the MediaMTX API client and `MediaMtxPlane`, the supervised ffmpeg process, the fake source and the transcoder, the reconcile loop | the media plane |
| `adapters/http/` | the auth callback and the test hook routes | internal HTTP |
| `adapters/` | `GatewayVendorSessions` (gateway session and release, a driver per provider), in-memory repository, Ed25519 tickets, `NoSourceDriver` and `NoMediaPlane` | implement the ports |
| `main.py` | wiring (`build_app`), the tick task, `main()` | the only place that knows every layer |

## Local start

```bash
uv sync
docker compose -f infra/compose/docker-compose.yml up -d --wait nats
MOWDECK_ENVIRONMENT=dev uv run python -m camera_relay.main
curl http://127.0.0.1:8000/readyz
```

In the Compose stack: `docker compose --profile relay up -d --build camera-relay mediamtx` (set `RELAY_ICE_HOSTS` to the host's LAN IP first).

Container image (build context is the repository root):

```bash
docker build -f services/camera-relay/Dockerfile -t mowdeck/camera-relay:dev .
```

## Tests

```bash
uv run pytest services/camera-relay --cov=camera_relay
```

The domain tests run the state machine with a fake time (reference counting, the exact grace, re-attach within the grace, binding and heartbeat deadlines, Vision caps and limits, the backoff, an offline mower, events); the service tests cover the replies, the effects and account separation with the same mower under two accounts; the ticket tests cover scope, expiry, tampering and foreign keys; the media unit tests cover the auth callback's decisions, the reconcile loop, kicks, the gateway port, the ffmpeg command lines, the supervisor and the freeze hook's gating.

The media plane's integration tests (`tests/media`) run the relay image against a real MediaMTX and NATS in a Compose project of their own, with aiortc as the WHEP client (a test dependency only): H.264 Constrained Baseline 640x480 at about 10 fps; 401 without a ticket or with a foreign or tampered one; NACK, PLI and transport-cc in the answer; a frozen source sends no frames and resumes without a burst; detach kicks the viewer; after the 15 s grace the source stops and the stream is released; the transcoder replays the spike's recorded H.265 stream without a duplicated frame. CI runs them in the job `camera-relay-media`:

```bash
docker build -f services/camera-relay/Dockerfile -t mowdeck/camera-relay:dev .
MOWDECK_TEST_RELAY_MEDIA=1 uv run pytest services/camera-relay/tests/media
```

Measured on a desktop CPU: the fake source takes about 6 to 7 % of one core and 33 MiB per camera, the transcoder (H.265 decode and H.264 encode, 640x480 at 10 fps) about 8 to 9 % of one core and 57 MiB.
