# camera-relay

Shared camera sessions per account and mower: leases, grace, tickets, lifecycle events and the media plane that serves them over WebRTC (WHEP) through MediaMTX ([ADR 0045](../../docs/adr/0045-camera-relay.md)).

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; the layer rules and the definition of done are in `docs/dev/service-template.md`.

## Status

Steps 3 to 8 of ADR 0045 (accepted): the session domain, tickets, the request responders and the media plane with the simulator's `fake` source and the `agora` source through the optional agora-bridge (`integrations/agora-bridge`); Fleet's camera endpoint is the front door (`transport` `auto`, `relay`, `direct`) and the web app plays WHEP; Vision gets its frames from the [frame tap](#frame-tap-vision) through the Device-Gateway. A viewer that attaches gets WHEP URLs and a ticket and plays real H.264 through MediaMTX. A provider without an available driver (Agora while no bridge is reachable) is answered with `unsupported_provider`, so Fleet's `auto` falls back to the direct path. The service is off by default (Compose profile `relay`, Helm `services.cameraRelay.enabled: false`); the test stacks (`docker-compose.e2e.yml`) always run it.

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
| Vision limits (only without a present viewer) | refused or ended below 15 % battery unless charging, below 10 % of the video budget, after 20 Vision-only minutes per account, mower and UTC day (`budget`); never on an offline, sleeping or unknown mower (`unavailable`); routine Vision-only use ends when the mower falls asleep. Only for `routine` demand: an `error_analysis` (a mower with a problem) may wake a sleeping mower and ignores these limits, refused only without a connection (owner decision 2026-10-18) |
| Viewers | never refused for a limit (the attach logs a warning); a viewer may wake a sleeping mower like the vendor app (owner decision 2026-10-18); refused only on an offline mower (no connection), during `STOPPING` or the backoff, or when 32 leases are open |
| Mower offline (no connection) | a running session stops (`mower_offline`), every lease closes; asleep alone stops nothing but routine Vision-only use |

The domain (`domain/session.py`) is a pure functional core: every input (`attach`, `renew`, `detach`, `media_confirmed`, `media_lost`, `source_started`, `source_failed`, `stop_completed`, `mower_status_changed`, `tick`, `shutdown`) takes the current time and returns effects as data: `StartVendorSession`, `UpdateCameras`, `StopVendorSession` (with `release`), `IssueTicket`, `KickViewer`, `EmitEvent`. Deadlines are processed in time order, each at its own due time, so event times are exact however late the tick runs. The application layer (`application/service.py`) locks the key, feeds the input and carries out the effects through the ports.

## Tickets

A ticket is `v1.<payload>.<signature>` (base64url), signed with Ed25519 and bound to the account, the mower, the session's random 128-bit path token, the lease and the lease's media paths `r/<relay_id>/<camera>`; it lives 60 s (`RELAY_TICKET_SECONDS`). `Ed25519TicketSigner.verify` checks signature, format and expiry; `TicketClaims.allows(account_id=..., path=..., now=...)` checks a requested path (used by the media server's auth callback in the next step). Paths never contain the mower id.

## Media plane

```text
browser --WHEP POST /media/whep/r/<rid>/<cam>/whep (Traefik, prefix stripped)--> MediaMTX :8889
        <==== ICE media on one UDP+TCP port (RELAY_ICE_PORT, default 8189) ====
MediaMTX --auth callback for every read, publish and API call--> camera-relay /internal/mediamtx/auth
camera-relay --RTSP publish (internal)--> MediaMTX r/<rid>/<cam>          (fake source: H.264)
agora-bridge --tcp://127.0.0.1:7700--> camera-relay --MPEG-TS--> muxer --RTSP--> r/<rid>/<cam>/src
                                            r/<rid>/<cam>/src --transcoder--> r/<rid>/<cam>   (H.265 -> H.264)
camera-relay --API every 2 s (readers, paths, kicks)--> MediaMTX :9997
```

- **Offer.** Before the first lease of an idle key the relay asks the Device-Gateway for the camera session (`gateway.mower.<id>.camera.session`, service principal, the lease's account) and reads its provider and cameras (`VendorSessions.offer`). **A provider without a source driver is answered with `unsupported_provider`** before any lease, event or start exists and **without releasing** that vendor session: a client in `auto` (Fleet) falls back to the direct path, and that viewer needs the mower's stream. No backoff follows; the next attach asks again. An attach without `camera_ids` gets every offered camera; named cameras the mower does not offer are left out (none left: `unavailable`, and the offered vendor session is released, since nothing starts). Replies carry the lease's cameras as `CameraInfo` (`cameras`).
- **Start.** The first lease of a key starts the source with the offered session (fetched again when it is older than 30 s, `OFFER_REUSE_SECONDS`) through the driver of its `CameraProvider`. `fake` starts one ffmpeg per camera: `testsrc2` 640x480 at 10 fps with an overlay (mower id short, camera id, wall clock in ms), H.264 Constrained Baseline, no B-frames, 1 s GOP, `-fps_mode passthrough`. It is paced by the `realtime` filter instead of `-re`, so a paused source resumes at 10 fps instead of catching up in a burst. A provider that lost its driver between offer and start fails the session without a release. The session goes `LIVE` when the reconcile loop sees a camera's path ready.
- **Transcoder** (generic, for source drivers that deliver to `.../src`): `ffmpeg -probesize 1000000 -analyzeduration 1000000 -fpsprobesize 0 -fflags nobuffer -flags low_delay -i rtsp://.../src -an -c:v libx264 -preset ultrafast -tune zerolatency -profile:v baseline -bf 0 -g 10 -fps_mode passthrough -f rtsp .../r/<rid>/<cam>`. It never repeats a frame, and a stalled input produces no frame: a frozen source is a frozen picture. The short probe only skips ffmpeg's frame-rate estimate (codec and size come from the parameter sets), which the passthrough output never uses.
- **Start behaviour of a camera** (agora source): a camera's `.../src` exists only some time after its first key frame (the LUBA sends one every 4 s and ignores key-frame requests; then the muxer's probe and the RTSP handshake). The transcoder therefore starts only once MediaMTX reports `.../src` ready (control API, polled every 0.5 s); before that a read would get 404, and waiting is not a failure. A source not ready within `MOWDECK_RELAY_SOURCE_READY_SECONDS` (20 s: several GOPs plus the probe) is logged as late (`camera source not ready in time; still waiting`) and still awaited. A transcoder that exits is started again, once `.../src` is ready, after 1 s doubling to 30 s, for as long as the camera is leased. One camera's pipeline never ends the session: the session goes `LIVE` with the first ready camera, a camera that never publishes (a listed uid without a publisher) blocks nothing, and the session fails only by the domain's start timeout (30 s without any ready camera), by a bridge that ends, or when a camera's muxer cannot be started at all (no ffmpeg) and no other camera delivers. A transcoder whose source went away (its muxer stopped or started anew) attaches again at the shortest delay once the new `.../src` is ready.
- **Recovery of a camera** (agora source; live incident 2026-10-18, a LUBA mowing over 4G whose link stalled again and again): a muxer that exits by itself (e.g. `Broken pipe` after MediaMTX dropped a stalled publisher) is started again at the next key frame for as long as the camera is leased: the first restart at once, then after 1 s doubling to 30 s (from the start again after a muxer ran 30 s). A camera that sends nothing for 20 s gets its muxer stopped cleanly (stdin closed, then killed after 5 s at the latest), so no ffmpeg waits on a publish that is gone; its next key frame starts a new one. The bridge's `publisher` `unpublished` stops that camera's muxer at once, `published` clears its backoff; the transcoder attaches again once the new `.../src` is ready. Events of the other connection during a rejoin change nothing. A camera counts as delivering (for the rule above) only while it sent a frame within the last 20 s. The session stays `LIVE` meanwhile: viewers get 404 on their WHEP offers until the camera is back and keep offering (the web player for 20 s, then with a new lease), so a viewer plays again within a few seconds of the camera's next key frame.
- **Stall and `readTimeout`.** MediaMTX's `readTimeout` is 30 s (`infra/compose/mediamtx/mediamtx.yml`, the Helm copy is identical): short 4G stalls (10 to 20 s) no longer drop the publishers, the relay's own stall watch (20 s) ends a longer one cleanly before MediaMTX would. The trade-off: a publisher the relay lost track of keeps its path up to 30 s, and a stalled source is a standing picture for up to 20 s before its viewers are disconnected. Nothing is repeated or invented while a source stalls (`-fps_mode passthrough`, presentation times from the bridge's receive clock): the viewers' freeze detection shows it honestly ("Bild steht"), and the drive view holds the mower.
- **Which cameras deliver.** The reconcile loop reports the first ready viewer paths as `source_started` and every camera that becomes ready later once (`cameras_published`). Attach and renew replies mark each tile with `available`: `true` once its stream appeared, `false` when it has not appeared 15 s after the session went live (a camera the vendor lists but the mower never publishes, derived from the publishers, never from the model), `null` while not known yet. A camera found silent in a session is remembered per account and mower (in memory, for 24 h) and reported `false` from the start of the next sessions; once it publishes it is `true` at once and forgotten. The web app offers only cameras known to deliver (`true`, a tile that plays, or the camera the viewer chose); "both" is the stereo pair, as before.
- **Who may do what** (MediaMTX `authMethod: http`): a WHEP read of `r/<rid>/<cam>` only with a relay ticket (bearer token) that is valid, unexpired, names that path and belongs to a live lease of a running session; the reader's MediaMTX session id is bound to the lease. Publishing, RTSP reads (the transcoder) and the API only with the internal publisher credential (`MOWDECK_RELAY_PUBLISHER_USER` and `_SECRET`, a random secret per process by default). Metrics, playback and pprof are off and denied; every other path is refused (there is no `all_others`). No ticket or password is logged.
- **Reconcile loop** (every 2 s, `/v3/webrtcsessions/list` and `/v3/paths/list`): a bound reader whose `bytesSent` grows confirms its lease's media (`media_confirmed`, with the camera of its path); one that stalls for three polls or disappears is `media_lost` (the lease may reconnect within its binding deadline); an unbound reader is kicked. A lease holds one reader per camera (a viewer with both front cameras side by side has two); a new reader of the same camera replaces and kicks the old one (a reconnect), and the lease stays present while any of its readers receives. A closed lease kicks its readers (`/v3/webrtcsessions/kick/<id>`).
- **Stop.** 15 s after the last lease the source is stopped and the gateway is asked to release the stream (`gateway.mower.<id>.camera.release` with the session's channel).
- **Agora source** (`adapters/media/bridge_source.py`, `MOWDECK_RELAY_AGORA_BRIDGE_URL`): the optional agora-bridge container shares the relay's network namespace and listens on its loopback; one TCP connection is one bridge process (one channel join) speaking the [bridge protocol](../../docs/dev/camera-relay-bridge-protocol.md). `offer` probes the bridge (a connect that starts nothing): not reachable, the provider is `unsupported_provider` (nothing released). `start` sends `join` with the vendor session (the only place its token and key travel; never logged, never in an argument or the environment) and `subscribe` with the uids of the leased cameras (a listed camera that never publishes, like the LUBA's rear one, blocks nothing). Each camera's access units, from its first key frame on, go into an MPEG transport stream (`adapters/media/mpegts.py`: presentation times from the bridge's `recv_mono_ns`, an access unit delimiter after every frame so ffmpeg's parser adds no frame of delay) piped into an ffmpeg that publishes `r/<rid>/<cam>/src` as H.265 (`-c:v copy`); the transcoder (started once `.../src` is ready, see the start behaviour above) makes the H.264 viewer path. A muxer that falls behind drops delta frames until the next key frame; one that exits restarts at the next key frame with a backoff, however often (see the recovery above). The muxer probes at most 32 KB or 0.5 s and estimates no frame rate (`-analyzeduration 500000 -fpsprobesize 0`; `0` would mean ffmpeg's default of 7 s for MPEG-TS). Renewal five minutes before the vendor session expires: a fresh session from the gateway, `renew` on the same channel, otherwise (another channel, `renew_failed`, no answer in 10 s, or the SDK's `token_will_expire`) a rejoin make-before-break: a second connection joins, each camera switches to it at its first key frame (at the latest after 12 s), then the old one gets `leave`. The active connection ending without `leave` (the server's `exit` record, a closed connection, a protocol error) fails the source (`FAILED`, backoff). Stopping sends `leave`, stops the muxers and transcoders, then the release runs.
- **Freeze hook** (tests only, `MOWDECK_RELAY_TEST_HOOKS=true`, refused with `MOWDECK_ENVIRONMENT=prod`): `POST /internal/v1/test/freeze` with `{"frozen": true|false, "relay_id": optional}` pauses (SIGSTOP) or resumes the fake sources. A freeze longer than MediaMTX's `readTimeout` (30 s) ends the publisher.

### Frame tap (Vision)

Vision's photos come from here (ADR 0045 step 8): the Device-Gateway's `RelaySnapshotter` calls the frame tap for mowers without an own photo function. Internal HTTP on port 8000 (never routed by Traefik), `Authorization: Bearer <MOWDECK_RELAY_FRAMES_TOKEN>`; without a configured token the tap is off (`503 frames-disabled`).

- `POST /internal/v1/frames/snapshot` with `{account_id, mower_id, camera_id?, max_width (1280), max_bytes (512 KiB), timeout_s (20, at most 30), requested_by?, on_behalf_of?}` answers one `image/jpeg` with `X-Frame-Captured-At` and `X-Frame-Received-At` (ISO 8601, the relay's wall clock; there is no capture clock yet, so both are the time the relay decoded the frame), `X-Frame-Width` and `X-Frame-Height`.
- `GET /internal/v1/frames/stream?account_id&mower_id&camera_id&fps (1, at most 5)&max_width` answers `multipart/x-mixed-replace; boundary=frame`: one JPEG part per frame with `Content-Length`, `X-Frame-Index` and the two time headers. No consumer yet (the "learn the garden" run).
- **One lease per request.** Each request opens a `vision` lease owned by the requesting service principal (`requested_by`, kind `service`; default a fixed frame tap principal; `on_behalf_of` is only logged) for exactly its lifetime: a snapshot lease (`VisionMode.SNAPSHOT`) is capped at 30 s by the domain and detached when the frame is there, the request times out or the caller goes away (the detach runs as its own task and is awaited shielded); a stream lease (`VisionMode.STREAM`) is renewed every 5 s while the client pulls frames and ends with the connection, after 300 s whatever happens, or when the lease is closed (a limit, the mower went offline).
- **Reuse and limits** are the session domain's: a running session is used as it is (no second vendor session while a viewer watches); without a present viewer the lease is admitted only within the Vision limits (table above). Without `camera_id` one camera is used, the camera most likely to deliver (one already publishing, else a front camera, never one known to be silent such as a listed rear camera that never publishes), so a vision-only session publishes one camera. Like the transcoder, a request waits until its camera's path is ready (the camera is reported available) before it reads a frame; a camera that turns out silent (not published within the 15 s camera settle time) fails a request that named it, otherwise the next candidate is tried.
- **Frames** come from the session's H.264 path `r/<rid>/<cam>` over RTSP with the internal credential (`adapters/media/frames.py`): a snapshot is ffmpeg's first decoded frame (after a key frame; the path is tried again until it is ready), scaled down to `max_width` (never up), encoded with Pillow at JPEG quality 0.8 and lowered to 0.6 and 0.4 while above `max_bytes` (the capture worker's steps); a stream is ffmpeg's MJPEG output with a `select` filter at the requested rate (`-fps_mode vfr`: frames are dropped, never repeated; a frozen source sends no parts), split at the JPEG end markers.
- **Errors** are problem documents: `401 unauthorized`, `404 not-found` (the account does not own the mower: the gateway refused the vendor session), `409 camera-budget`, `503 camera-unavailable` (offline, asleep, unknown state, the source failed or did not start), `503 camera-relay-unsupported` (no driver for the mower's camera provider), `503 frames-busy` (more than `MOWDECK_RELAY_FRAMES_MAX_CONCURRENCY` requests, after 5 s), `504 frame-timeout`, `502 frame-failed` (no frame within `max_bytes` at the lowest quality).
- **Mower states.** The limits need the mower's state and the relay never asks a mower: it reads `telemetry.mower.*` (stream `TELEMETRY`, an ephemeral consumer from the newest state of every mower on, `adapters/bus/telemetry.py`). Online: the gateway's link (`connected`) with a heartbeat at most 120 s old; asleep: activity `offline`; battery and charging (also activity `charging`). The video budget comes from the vendor session the relay is offered (`CameraSession.quota`) and is kept across states. Without a connection everyone is refused and a running session stops (`mower_offline`); a sleeping mower is woken by a viewer or an `error_analysis`, only `routine` Vision is refused (and ended) while it sleeps. `docked` and `charging` are never offline. The request's `demand` (`routine` default, `error_analysis`; query parameter `demand` on the stream) is the lease's `VisionDemand`.

### Ports and firewall

| Port | Purpose | Reachable from |
| --- | --- | --- |
| camera-relay 8000 | `/internal/mediamtx/auth`, the frame tap, health, metrics, the test hook | the internal network only (no Traefik route) |
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
| POST | `/internal/v1/frames/snapshot` | bearer `MOWDECK_RELAY_FRAMES_TOKEN` (the Device-Gateway) | One JPEG of a mower's camera under a `vision` lease ([frame tap](#frame-tap-vision)) |
| GET | `/internal/v1/frames/stream` | bearer `MOWDECK_RELAY_FRAMES_TOKEN` | JPEG frames as `multipart/x-mixed-replace`, at most 5 fps and 300 s |
| POST | `/internal/v1/test/freeze` | only with `MOWDECK_RELAY_TEST_HOOKS=true` | Pause or resume the fake sources |

## Events and requests

| Direction | Subject | Payload |
| --- | --- | --- |
| answered (core NATS, queue `camera-relay`) | `relay.camera.attach` | `RelayAttachRequest` / `RelayAttachReply` (empty `camera_ids`: every offered camera; no driver for the provider: `unsupported_provider`, nothing released) |
| answered | `relay.camera.renew` | `RelayRenewRequest` / `RelayRenewReply` (another account or another principal than the lease's opener: `forbidden`; unknown or another mower: `not_found`) |
| answered | `relay.camera.detach` | `RelayDetachRequest` / `RelayDetachReply` (idempotent for the opener; another account or another principal: `forbidden`, the lease stays open) |
| consumed (stream `TELEMETRY`, ephemeral, newest per mower first) | `telemetry.mower.<id>` | `MowerState` (online, asleep, battery, charging for the limits) |
| requested (core NATS) | `gateway.mower.<id>.camera.session` | `CameraSessionRequest` / `CameraSessionReply` (service principal, the lease's account) |
| requested | `gateway.mower.<id>.camera.release` | `CameraReleaseRequest` / `CameraReleaseReply` (after the grace) |
| produced (stream `CAMERA`) | `camera.mower.<id>.relay.started` | `EventEnvelope[CameraRelayStarted]` (`trigger`: the purpose of the starting lease, `started_by`) |
| produced | `camera.mower.<id>.relay.stopped` | `EventEnvelope[CameraRelayStopped]` (reason, `started_at`, `vision_seconds`, `trigger`, `peak_viewers`) |
| produced | `camera.mower.<id>.relay.lease_opened` | `EventEnvelope[CameraRelayLeaseOpened]` |
| produced | `camera.mower.<id>.relay.lease_closed` | `EventEnvelope[CameraRelayLeaseClosed]` (reason) |

The sender (Fleet; the Device-Gateway for Vision's frames over HTTP) has authorized the principal; Fleet turns `started` and `stopped` into its audit rows `mower.camera_stream_started` and `mower.camera_stream_stopped` (`trigger` `vision` for a session a Vision lease started); a request that does not validate is dropped without a reply. Media confirmations and source readiness come from the reconcile loop, mower states from the telemetry subscription.

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
| `MOWDECK_RELAY_SOURCE_READY_SECONDS` | `20` | How long a camera's `.../src` may take after its first key frame before it is logged as late (it is still awaited; its transcoder starts only once it is ready) |
| `MOWDECK_RELAY_GATEWAY_TIMEOUT_SECONDS` | `10` | Timeout of the gateway's camera session and release requests |
| `MOWDECK_RELAY_SERVICE_SUBJECT` | a fixed UUID | Subject of the relay's service principal in gateway requests |
| `MOWDECK_RELAY_FFMPEG_PATH` | `ffmpeg` | ffmpeg of the pipeline |
| `MOWDECK_RELAY_FAKE_FONT` | DejaVu Sans Mono | Font of the fake source's overlay |
| `MOWDECK_RELAY_AGORA_BRIDGE_URL` | unset | The agora-bridge server, `tcp://host:port` (Compose and Helm: `tcp://127.0.0.1:7700`, the relay's loopback). Unset or empty: no `agora` driver. Set but not reachable: `agora` mowers are `unsupported_provider` |
| `MOWDECK_RELAY_FRAMES_TOKEN` | unset | Shared secret of the frame tap (bearer token of the Device-Gateway), usually as `_FILE`. Unset or empty: the frame tap is off (`503 frames-disabled`) |
| `MOWDECK_RELAY_FRAMES_MAX_CONCURRENCY` | `4` | Frame requests (snapshots and streams) at the same time; one more waits 5 s, then `503 frames-busy` |
| `MOWDECK_RELAY_TEST_HOOKS` | `false` | Serve the freeze hook (test stacks; refused with `MOWDECK_ENVIRONMENT=prod`) |

The timings and limits are the ADR 0045 defaults from `mowdeck_contracts.v1.relay` and are not configurable.

## Layers

| Package | Content | Rule |
| --- | --- | --- |
| `domain/` | `RelaySession` (state machine), leases, policy and limits, effects, ticket scope | pure Python, no I/O, no framework imports |
| `application/` | `RelayService` (use cases), `FrameTap` (Vision's frames under `vision` leases), `MediaAuthorizer` (the auth callback's decisions) and ports (`VendorSessions`, `MediaPlane`, `EventPublisher`, `TicketSigner`, `RelayRepository`, `Clock`, `FrameGrabber`) | depends on `domain` and ports only |
| `adapters/bus/` | request responders, the event publisher, the mower states from telemetry | translate messages to use cases |
| `adapters/media/` | the MediaMTX API client and `MediaMtxPlane`, the supervised ffmpeg process, the fake source and the transcoder, the agora bridge source (`bridge_source.py`, `bridge_protocol.py`, `mpegts.py`), the reconcile loop, the ffmpeg frame grabber (`frames.py`) | the media plane |
| `adapters/http/` | the auth callback, the frame tap and the test hook routes | internal HTTP |
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

The frame tap tests run `FrameTap` with a vendor that goes live and a fake grabber (a snapshot holds a `vision` snapshot lease of the requesting service for exactly the request; a viewer's running session is reused without a second vendor session or Vision minutes; battery, video budget from the offer (the offered session released), daily minutes, offline, asleep and unknown states refused; a timeout, a source that never goes live and a cancelled request leave no lease; account B never reads a path of account A's session; streams renew, end with the client, after 300 s and when the lease is closed) and its HTTP routes with the in-memory bus (token, problem types, multipart parts with their headers, the telemetry subscription and the budget kept across states), plus the ffmpeg command lines, the JPEG quality steps and the MJPEG splitter. The domain tests run the state machine with a fake time (reference counting, the exact grace, re-attach within the grace, binding and heartbeat deadlines, Vision caps and limits, the backoff, an offline mower, events); the service tests cover the replies, the effects and account separation with the same mower under two accounts; the ticket tests cover scope, expiry, tampering and foreign keys; the media unit tests cover the auth callback's decisions, the reconcile loop, kicks, the gateway port, the ffmpeg command lines, the supervisor and the freeze hook's gating; the bridge source tests run the agora driver against a fake bridge server (the join and subscribe lines, frames to one muxer per camera from the first key frame, presentation times from the receive clock, back-pressure, a muxer restart, restarts at key frames with a backoff however often a muxer exits, a silent camera's muxer stopped without a zombie, `unpublished` stopping and `published` restarting a camera with its transcoder attached again, a muxer that cannot start failing the source only when the other cameras are silent, renewal timing and `renew` on the same channel, rejoin make-before-break on `renew_failed`, another channel or `token_will_expire`, a crash or a broken record failing the source, `leave` on stop, `unsupported_provider` without a reachable bridge and no release, and no token, key or salt in logs, arguments or the environment) and the MPEG-TS writer (PSI with valid CRCs, the lead delimiter, PES timestamps).

The media plane's integration tests (`tests/media`) run the relay image against a real MediaMTX and NATS in a Compose project of their own, with aiortc as the WHEP client (a test dependency only): H.264 Constrained Baseline 640x480 at about 10 fps; 401 without a ticket or with a foreign or tampered one; NACK, PLI and transport-cc in the answer; a frozen source sends no frames and resumes without a burst; detach kicks the viewer; after the 15 s grace the source stops and the stream is released; the transcoder replays the spike's recorded H.265 stream without a duplicated frame; an `agora` mower plays through the agora-bridge server (run from `integrations/agora-bridge/src`, with `tests/media/fake_bridge.py` replaying `luba-uid1.h265` through the protocol instead of the SDK) as H.264 Constrained Baseline at about 10 fps with H.265 on `.../src`, a link that stalls again and again (12 s stalls), a camera that leaves for good and one that is unpublished and published again recover (a new WHEP reader gets H.264 again: the live incident of 2026-10-18), a crashing bridge fails the session into the backoff, and with the bridge stopped an `agora` mower is `unsupported_provider` and nothing is released; the frame tap (`test_camera_relay_frames_media.py`, mower states published on the test NATS) answers a 640x480 JPEG within its size cap for the fake source and for the recorded LUBA stream through the agora-bridge server, scales and lowers the quality for a small cap (and refuses one that cannot be met), refuses a low battery (409) and an offline mower (503) without asking the gateway, streams parts at the requested rate (2 fps) and ends the lease with the client, and every vision-only session is released after the 15 s grace. No Agora SDK and no network join are involved. CI runs them in the job `camera-relay-media`:

```bash
docker build -f services/camera-relay/Dockerfile -t mowdeck/camera-relay:dev .
MOWDECK_TEST_RELAY_MEDIA=1 uv run pytest services/camera-relay/tests/media
```

Measured on a desktop CPU: the fake source takes about 6 to 7 % of one core and 33 MiB per camera, the transcoder (H.265 decode and H.264 encode, 640x480 at 10 fps) about 8 to 9 % of one core and 57 MiB.
