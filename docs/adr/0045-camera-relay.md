# ADR 0045: Camera relay

## Status

Proposed (2026-10-17). Amends [ADR 0032](0032-live-camera-over-agora.md) (the browser no longer joins the vendor channel by default), [ADR 0035](0035-server-side-camera-frames-and-automatic-analysis.md) (Vision gets frames from the relay instead of a headless browser per capture, in a later phase) and [ADR 0044](0044-drive-view-with-camera.md) (the drive view plays the relay; the freeze hold stays).

## Context

Since ADR 0032 every browser joins the mower's Agora channel itself. That has three costs that showed up on the owner's mower:

- **Browser differences.** Safari and the iOS app stop showing the picture after about two seconds while Chrome keeps playing; the vendor's media servers treat clients differently, and MowDeck controls none of it.
- **One viewer per session.** A camera session carries a single viewer uid. Two viewers of one mower are two vendor sessions, two 4G streams from the mower and, with a repeated uid, a kicked viewer. Vision's capture worker (ADR 0035) is a third client with its own headless Chromium per frame.
- **Secrets and privacy.** The vendor token and the viewer's IP address reach the vendor's cloud from every browser.

A spike (PR #32, run against the LUBA 2 AWD X on 2026-10-17) joined the channel with Agora's Linux server SDK as a receive-only audience member. Facts that shape this decision:

- The native join works with the vendor's app id, channel, token and viewer uid; frames arrive about 1.6 s after connect. Two cameras cost about 12 % of one core and 65 MB of memory.
- Each camera is H.265 Main, 640×480, about 10 fps, about 240 kbit/s, with a 4 s GOP. The publisher **ignores key-frame requests**. The capture timestamp is the sender's monotonic clock in 100 ms steps, not epoch time.
- The mower lists three cameras but publishes only two; a listed camera may never publish.
- Every session request gets a **new viewer uid**, so a server-side viewer never collides with a browser.
- **The mower keeps publishing after the last viewer leaves**, spending 4G video budget and battery until it is told to stop.
- The SDK's Python wrapper downloads the native SDK without a hash check, at install and again at import, and **turns off TLS certificate verification for the whole process**. The native SDK is proprietary and ships no licence file.

## Decision

**A new AGPL service `camera-relay` holds at most one source session per account and mower and serves it to any number of viewers of that account over WebRTC (WHEP), through the media server MediaMTX. Vendor sources are pluggable drivers; the Agora driver runs in a separate, optional `agora-bridge` process the operator builds.**

### Components

- **camera-relay** (AGPL-3.0, new): sessions keyed by `(account_id, mower_id)`, leases, tickets, the transcoder, the MediaMTX auth callback and lifecycle events. Two accounts with access to the same mower always get separate sessions, paths and tickets.
- **MediaMTX** (MIT, pinned image): data plane only. The relay publishes into it over RTSP inside the cluster; browsers read over WHEP. MediaMTX asks the relay for every read and publish (HTTP auth callback); its API is reachable only by the relay.
- **Source drivers**, keyed by `CameraProvider`: `fake` (a generated test pattern, for tests and the simulator), `agora` (through `agora-bridge`), later `rtsp` for mowers with a local stream. A provider without a driver is answered with `unsupported_provider`.
- **agora-bridge** (Apache-2.0 code, optional): joins the vendor channel with Agora's Linux server SDK and writes encoded frames to the relay over stdio, using the [camera source bridge protocol](../dev/camera-relay-bridge-protocol.md).
- **Fleet** stays the front door: `can_view` (OpenFGA, 404 semantics), the plan feature `camera`, the audit row; then it asks the relay for a lease. Its camera endpoint takes `transport` `auto`, `relay` or `direct` (`CameraTransport`).
- **Device-Gateway** keeps issuing vendor sessions (`gateway.mower.<id>.camera.session`) and gets a generic **release** that tells the mower to stop publishing; adapters implement it (Mammotion: the stream stop command).
- **Web app**: a WHEP player per camera tile, behind the existing `CameraStage` and its freeze detection.

### Sessions and leases

- A lease is `viewer` (a person) or `vision` (frames for Vision). `relay.camera.attach` opens one, `relay.camera.renew` is its heartbeat and returns a fresh ticket, `relay.camera.detach` ends it (core NATS request/reply, queue group `camera-relay`, outside every stream).
- **Presence is real use only:** a viewer lease counts while its media is confirmed (MediaMTX reports bytes sent to that reader growing; the relay reconciles with the MediaMTX API every 2 s) and its heartbeat arrives every 15 s. Without a heartbeat for 45 s the lease closes. A hidden tab, a left page or a closed app detaches at once; returning reconnects in 1 to 3 s.
- States: `IDLE → STARTING → LIVE → DRAINING (15 s after the last lease) → STOPPING → IDLE`, and `FAILED` with a backoff of 5 to 60 s. `STOPPING` leaves the source and then asks the gateway to release the stream, because the mower does not stop by itself.
- The relay **never wakes a mower**: an offline or sleeping mower is `unavailable`.
- Cameras are optional: a session goes live with the cameras that publish and never waits for a listed camera that does not.
- Vendor sessions last about an hour; the driver renews about five minutes before expiry and rejoins with a fresh session (new viewer uid) when a renew fails. The transcoder hides the rejoin; viewers see at most one GOP gap.

### Media

- The relay **always transcodes** to H.264 Constrained Baseline with a 1 s GOP (no B-frames, zero-latency tuning, `-fps_mode passthrough`), because browsers decode H.265 unevenly and the source's 4 s GOP cannot be shortened by a key-frame request. It keeps the last source key frame with its parameter sets so a restarted transcoder starts at once.
- The relay **never invents or repeats frames**: a frozen source is a frozen picture in the browser, so the drive view's freeze hold (ADR 0044) keeps working unchanged. A short source freeze does not tear down the WHEP session.
- One WHEP connection per camera tile. NACK and PLI are negotiated explicitly (Safari). Media leaves on a dedicated UDP/TCP port; signalling runs through the existing gateway, so a home network needs no TURN server.
- Target glass-to-glass latency below 0.5 s in the LAN, measured in the relay (the sender clock is not wall time).

### Contracts (`mowdeck-contracts`, Apache-2.0)

- `CameraProvider.relay`; `CameraSession.relay: RelayEndpoint | None` with `tiles` (`camera_id`, `whep_url`), `ticket` (secret, excluded from `repr` and replaced by `redacted()`), `lease_id` and `heartbeat_seconds`. Provider `relay` has the endpoint and no vendor secrets (`token` and `app_id` empty, no encryption), and `expires_at` is the ticket's expiry; every other provider has no endpoint.
- `CameraTransport` `auto|relay|direct`.
- `RelayAttachRequest` (`account_id`, `mower_id`, `camera_ids`, `requested_by`, `purpose` `viewer|vision`) and `RelayAttachReply`, `RelayRenewRequest`/`RelayRenewReply`, `RelayDetachRequest`/`RelayDetachReply`, with the error codes `unsupported_provider`, `unavailable`, `budget`, `forbidden`, `not_found`.
- Events `camera.relay.started`, `camera.relay.stopped`, `camera.relay.lease_opened`, `camera.relay.lease_closed` (`EventEnvelope`) on the new stream `CAMERA`, subjects `camera.mower.<id>.relay.<kind>`, kept 30 days for audit, metrics and the Vision daily limit. The request subjects live below `relay.` so no stream captures them (the lesson of ADR 0032).

### Transport choice

`auto` (the default) uses the relay and falls back to the direct vendor path of ADR 0032 **only** when the relay answers `unsupported_provider` (no bridge installed); it never switches while a drive session runs. `direct` is a manual per-browser option and keeps today's behaviour. `relay` shows the relay's errors and never falls back.

### Vision limits (later phase)

A vision lease without a viewer: a single frame holds the session at most 30 s; a frame stream lease lasts at most 5 minutes and is renewed by pulling frames; at most 20 minutes per mower and UTC day; refused (`budget`) below 10 % of the video budget or below 15 % battery unless the mower charges; never on an offline mower. A configured Vision holds nothing open by itself.

## Manufacturer independence

Generic, the same for every manufacturer: the relay, its sessions, leases, tickets and limits, MediaMTX, the transcoder, the contracts above, the events, the bridge protocol, the WHEP player and the freeze detection. Nothing in them reads a vendor field; a source is a `CameraProvider`, and Agora is a transport several vendors use, not a vendor. The simulator's `fake` provider drives every test without hardware or Agora.

Adapter work, once per manufacturer: the camera session (exists since ADR 0032) and the new release (stop publishing). A vendor with another transport adds a source driver (for example `rtsp`) or its own bridge that speaks the same protocol.

## Security

- Tickets are signed by the relay with Ed25519, live about 60 s and are bound to account, mower, relay instance, lease and the session's paths. A ticket of account A never opens anything of account B; tests with two accounts check that B can neither read A's stream, use A's ticket nor renew or detach A's lease.
- Paths are random per session (`r/<128-bit>/<camera>`), never the mower id, and no listing is reachable from outside.
- Vendor tokens and encryption keys stay on the server; they reach the bridge only on its stdin and are never logged. Browsers no longer talk to the vendor's media cloud on the relay path.
- The bridge makes no network calls of its own besides the SDK's (the wrapper disables TLS verification process-wide), runs without privileges and gets only the relay's pipes.

## Power and cost

One 4G stream per mower and account, however many people watch, and none once the 15 s grace has passed: the relay releases the stream so the mower stops publishing, which the browser path never did. Relay and bridge cost a few percent of a core per camera (hardware transcoding is a later option) and about 65 MB per bridge. The vendor's Agora minutes are used as before.

## Licensing

- camera-relay is AGPL-3.0 like every service; the contracts and the bridge code are Apache-2.0; MediaMTX is MIT.
- The Agora SDK is proprietary. It runs only in the separate `agora-bridge` process, which talks to the relay over a documented stdio protocol, so no AGPL or GPL code links it.
- **MowDeck never redistributes it.** No published image contains it. The operator builds the bridge image; the Dockerfile pins the wrapper version, downloads the native SDK itself and checks a pinned sha256, and places it so the import never downloads anything. Whoever builds it accepts Agora's terms.
- The bridge joins with the vendor's app id and vendor-issued tokens, as the browser does under ADR 0032; the documentation says so in neutral terms.

## Alternatives

- **Keep the browser path only** (ADR 0032): leaves the WebKit problem, one 4G stream and one vendor session per viewer, and no frames for Vision without a browser.
- **Headless Chromium as the source** (ADR 0035's worker, continuously): heavier per mower, the same browser-specific behaviour, and H.265 decoding without a GPU is doubtful. Kept as plan B behind the same bridge protocol, not needed after the spike.
- **Pass the source's H.265 through**: no transcoding cost, but several browsers cannot decode it and every new viewer waits up to 4 s for a key frame.
- **A bridge inside the relay process or the gateway**: links proprietary code into AGPL services and lets an SDK crash take them down.
- **A relay per mower shared by all accounts**: simpler, but an account could keep another account's stream open; rejected for account separation.

## Consequences

Easier: every browser and the iOS app play the same H.264 stream; any number of viewers per account cost one 4G stream; the stream ends 15 s after the last viewer; Vision gets frames without a browser; vendor tokens stay on the server.

Harder: two more moving parts (relay and MediaMTX) and an optional bridge the operator builds; a UDP/TCP media port to open in the LAN; the relay adds about 50 to 100 ms of latency against the direct path; transcoding costs CPU on the server.

Phased plan, one PR each:

1. ADR and contracts (this decision).
2. Release port in the adapters (simulator and Mammotion) and the gateway request. Implemented: `mowdeck_control.CameraSessionRelease` (`release_camera_session(channel)`, `release_camera_session_of`), the simulator records releases, the Mammotion cloud connection calls PyMammotion's `stop_stream`, and the Device-Gateway answers `gateway.mower.<id>.camera.release` (`CameraReleaseRequest` / `CameraReleaseReply`).
3. Relay domain: sessions, leases, grace, account separation, fully tested with two accounts. **Implemented** in `services/camera-relay` (Compose profile `relay`, Helm `services.cameraRelay.enabled`, both off by default): the session state machine as a pure core that returns effects, leases with heartbeat, a 30 s binding deadline for a viewer whose media is not yet confirmed and the Vision caps and daily minutes, a 30 s start timeout, Ed25519 tickets and the `relay.camera.*` responders. Until step 4 the source port has no driver, so every attach is answered with `unavailable`.
4. Media plane with the `fake` source: MediaMTX, auth callback, transcoder, gateway route, Compose and Helm.
5. Fleet: the camera endpoint through the relay, `transport`, renew and detach, audit.
6. Web and app: WHEP player, diagnostics, tab hiding detaches; e2e in Chromium, Firefox and WebKit.
7. Agora driver and bridge image (user-built, pinned and hashed), tests with the spike's recorded stream; CI needs no Agora.

Later: Vision through the relay (replacing the capture worker), hardware transcoding and monitoring, a "learn the garden" run that ties frames to RTK positions.

To check on hardware: latency on the drive page; Safari, the iOS app and Chrome for ten minutes each without stalls; that the release really ends the 4G stream; the media port reachable from a phone in the home Wi-Fi.
