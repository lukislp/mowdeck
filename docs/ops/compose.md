# Operating the Compose stack

The stack lives in `infra/compose/` and runs the foundation infrastructure on one machine.

## Start, check, reset

```bash
cd infra/compose
cp .env.example .env            # set the passwords
docker compose up -d --build --wait   # build the MowDeck services, wait until all are healthy
docker compose ps
docker compose down -v          # reset, deletes all data
```

**Project name.** `docker-compose.yml` sets `name: mowdeck`, so every `docker compose` command in this guide addresses the project `mowdeck` without `-p`. `-p <name>` and `COMPOSE_PROJECT_NAME` take precedence over it: a stack started under another name has to get the same name on every command, otherwise Compose creates a second stack with its own containers and volumes next to it (and both claim the host ports). Either put `COMPOSE_PROJECT_NAME=<name>` in `.env` or pass `-p <name>` each time (`docker compose -p <name> exec -T postgres pg_dumpall -U postgres`). `docker compose ls` shows the running projects; `docker compose ps` in this directory must list the running containers before an update.

## Updating to a release

By default the stack builds the MowDeck images from the checkout (`mowdeck/<service>:dev`). To run a published release instead, the stack pulls the signed images of the release from GHCR: every MowDeck service uses `${MOWDECK_IMAGE_REGISTRY}/<service>:${MOWDECK_VERSION}` (defaults `mowdeck` and `dev`, the local build; `build:` stays, so `up --build` works as before). Set once in `.env`:

```bash
MOWDECK_IMAGE_REGISTRY=ghcr.io/lukislp/mowdeck   # a fork: ghcr.io/<owner>/mowdeck (and SOURCE_URL)
```

Each update, from `infra/compose` of the checkout (a stack under another [project name](#start-check-reset) adds its `-p <name>` to every `docker compose` command):

```bash
git fetch --tags && git checkout --detach v<x>      # the Compose files of that release
docker compose exec -T postgres pg_dumpall -U postgres > ~/mowdeck-before-<x>.sql   # backup
./verify-release.sh <x>                             # writes docker-compose.release.yml
sed -i 's/^MOWDECK_VERSION=.*/MOWDECK_VERSION=<x>/' .env
docker compose -f docker-compose.yml -f docker-compose.release.yml pull
docker compose -f docker-compose.yml -f docker-compose.release.yml up -d --no-build --wait
```

`verify-release.sh` reads the image digests from the notes of the GitHub release `v<x>` (with `gh` when it is installed, else the public GitHub API with `curl`; no token), checks that each registry tag `<x>` points to that digest, verifies each digest's Sigstore signature with `cosign` (bound to `https://github.com/<repo>/.github/workflows/release.yml@refs/heads/main` and the issuer `https://token.actions.githubusercontent.com`; cosign from `PATH`, else the pinned cosign container) and fails closed: if one image fails, it writes nothing and exits 1. On success it writes `docker-compose.release.yml` (ignored by git), which sets every service to `<registry>/<service>:<x>@sha256:<digest>`, so `up` runs exactly the verified images even if a tag is moved later. `--print` writes the override to stdout, `--out FILE` elsewhere, `--no-pin` only verifies. The repository comes from `SOURCE_URL` (or `MOWDECK_RELEASE_REPO=<owner>/<repo>`) and must belong to the registry's owner. Instead of a full checkout, fetching `docker-compose.yml`, `verify-release.sh`, `init/` and `mediamtx/` of the tag is enough.

agora-bridge (profile `agora`) is never published: it keeps its local image `mowdeck-local/agora-bridge:local`; build it once with `docker compose --profile agora build agora-bridge` before an `up --no-build`. vision-capture (profile `capture`, retired) is not published either.

**Rollback.** Check out the earlier tag, run `./verify-release.sh <earlier>` (it rewrites the override), set `MOWDECK_VERSION=<earlier>` and run the same `pull` and `up` commands. Migrations are backward compatible, so the older images start against the newer schema ([Rolling back](release.md#rolling-back)); a rollback across a migration that is not needs the backup (`docker compose exec -T postgres psql -U postgres -d postgres < ~/mowdeck-before-<x>.sql` into an empty `postgres-data` volume).

## Services and ports

| Service | Image | Host port |
| --- | --- | --- |
| traefik | `public.ecr.aws/docker/library/traefik:v3.7.14` | 80, 443 |
| nats | `public.ecr.aws/docker/library/nats:2.15.0-alpine` | 127.0.0.1:4222 (client), 127.0.0.1:8222 (monitoring, `/healthz`) |
| postgres | `public.ecr.aws/docker/library/postgres:17.11-alpine` | 127.0.0.1:5432 |
| openfga-migrate | `ghcr.io/openfga/openfga:v1.22.0` | one-shot, no port |
| openfga | `ghcr.io/openfga/openfga:v1.22.0` | 127.0.0.1:8080 (HTTP), 127.0.0.1:8081 (gRPC) |
| authz-bootstrap | `mowdeck/authz-bootstrap:dev`, built from `infra/openfga/bootstrap` | one-shot, no port |
| whoami (profile `demo`) | `ghcr.io/traefik/whoami:v1.10.3` | via Traefik at `/api/v1/whoami` |
| identity-migrate | `mowdeck/identity:dev`, built from the repository | one-shot, no port |
| identity | `mowdeck/identity:dev`, built from the repository | via Traefik at `/api/v1/identity` and `/.well-known/jwks.json` |
| fleet-migrate | `mowdeck/fleet:dev`, built from the repository | one-shot, no port |
| fleet | `mowdeck/fleet:dev`, built from the repository | via Traefik at `/api/v1/fleet` |
| telemetry-migrate | `mowdeck/telemetry:dev`, built from the repository | one-shot, no port |
| telemetry | `mowdeck/telemetry:dev`, built from the repository | via Traefik at `/api/v1/telemetry` |
| device-gateway | `mowdeck/device-gateway:dev`, built from the repository | via Traefik at `/api/v1/device-gateway` |
| realtime | `mowdeck/realtime:dev`, built from the repository | via Traefik at `/api/v1/realtime` (WebSocket `/api/v1/realtime/ws`) |
| notifications-migrate | `mowdeck/notifications:dev`, built from the repository | one-shot, no port |
| notifications | `mowdeck/notifications:dev`, built from the repository | via Traefik at `/api/v1/notifications` |
| scheduler-migrate | `mowdeck/scheduler:dev`, built from the repository | one-shot, no port |
| scheduler | `mowdeck/scheduler:dev`, built from the repository | via Traefik at `/api/v1/scheduler` |
| vision-migrate | `mowdeck/vision:dev`, built from the repository | one-shot, no port |
| vision | `mowdeck/vision:dev`, built from the repository | via Traefik at `/api/v1/vision` |
| camera-relay (profile `relay`) | `mowdeck/camera-relay:dev`, built from the repository (with ffmpeg) | none (internal: NATS requests `relay.camera.*` and MediaMTX's auth callback; [ADR 0045](../adr/0045-camera-relay.md)) |
| agora-bridge (profile `agora`, with `relay`) | `mowdeck-local/agora-bridge:local`, built locally from `integrations/agora-bridge` (contains Agora's SDK, never pulled or published) | none (listens on camera-relay's loopback `127.0.0.1:7700`, shares its network namespace; [Agora source](#agora-source-profile-agora)) |
| mediamtx (profile `relay`) | `bluenviron/mediamtx:1.21.2` (MIT) | via Traefik at `/media/whep/r/` (WHEP only, `/media/whep` stripped); `RELAY_ICE_BIND:RELAY_ICE_PORT` UDP and TCP (ICE media, default 8189) |
| web | `mowdeck/web:dev`, built from `apps/web/Dockerfile` | via Traefik at `/` (the web app; router priority 1, behind every `/api/v1` route) |

PostgreSQL creates the databases `identity`, `fleet`, `telemetry`, `scheduler`, `notifications`, `entitlements`, `billing`, `vision`, `openfga`, each with a role of the same name.

The complete operating notes are in the repository file `infra/compose/README.md`.

## Image sources

All third-party images are pinned under "Image sources" in `.env.example` and chosen in `.env` by the same variables: `TRAEFIK_IMAGE`, `NATS_IMAGE`, `POSTGRES_IMAGE`, `OPENFGA_IMAGE`, `WHOAMI_IMAGE` for the infrastructure and `PYTHON_IMAGE`, `UV_IMAGE`, `NODE_IMAGE`, `NGINX_IMAGE`, `BUSYBOX_IMAGE`, `FGA_CLI_IMAGE`, `JQ_IMAGE` for the base images of the MowDeck builds (plus `NTFY_IMAGE`, `STRIPE_MOCK_IMAGE` and `PLAYWRIGHT_IMAGE`, which only CI runs). The defaults are public registries that need no login (ECR Public, `ghcr.io`, Docker Hub for the OpenFGA CLI, ntfy and stripe-mock, `mcr.microsoft.com` for Playwright).

To use another registry at home (a pull-through cache or a mirror on the NAS), copy the images there with the same tags and set the variables in `.env`, for example `POSTGRES_IMAGE=registry.example.org/postgres:17.11-alpine`, or all of them at once with `MOWDECK_IMAGE_MIRROR=registry.example.org/mowdeck sh infra/mirror-env.sh >> infra/compose/.env`; a later line in `.env` overrides an earlier one. Log in to that registry first if it needs credentials.

CI pulls the same pinned images through the repository's GHCR mirror `ghcr.io/<owner>/mowdeck/mirror/<alias>:<tag>`, which the workflow `mirror-images.yml` fills from the same list, because anonymous pulls from the public registries are rate limited on the shared CI runners; the stack at home is not affected. Details: `infra/compose/README.md`, "Image sources".

## Authorization bootstrap

`authz-bootstrap` creates the OpenFGA store `mowdeck` if it is missing and writes `infra/openfga/model.fga` only when it differs from the latest model in the store, then exits. It is idempotent and runs on every `up`; its log shows `created store` and `wrote model` on the first run and `found store` and `model unchanged` afterwards (`docker compose logs authz-bootstrap`). Services find the store by name and pin the latest model when they start, so no store or model id needs to be configured. After a model change, run `docker compose up -d --build --wait` and restart the services that use authorization so they pin the new model. See [ADR 0014](../adr/0014-authorization-model-and-tuple-ownership.md).

## Identity

Identity issues the access tokens (EdDSA, 10 minutes) and publishes its public keys at `/.well-known/jwks.json`; the other services verify tokens against `http://identity:8000/.well-known/jwks.json` inside the network. `identity-migrate` runs the database migrations first. Set `IDENTITY_KEY_ENCRYPTION_KEY` in `.env` (the value in `.env.example` is a marked development default: generate your own and keep a backup, it encrypts the signing keys). `IDENTITY_REGISTRATION=open` lets anybody register; `closed` accepts only the first account. Login, registration and token exchange are rate limited by Traefik (router `identity-auth`: 10 per minute per client IP, burst 20) and by Identity itself; refresh and logout (`/api/v1/identity/session/...`) have their own, looser limit (router `identity-session`: 60 per minute, burst 30), so several devices of a household behind one address stay logged in. A refresh token presented again within `MOWDECK_IDENTITY_REFRESH_REUSE_GRACE_SECONDS` (10) after a lost response does not end the session.

Operator commands run in the container:

```bash
docker compose exec identity python -m identity.cli list-keys
docker compose exec identity python -m identity.cli rotate-keys          # announce a next key
docker compose exec identity python -m identity.cli reconcile            # repair OpenFGA from the database
docker compose exec -e MOWDECK_IDENTITY_CLI_PASSWORD=... identity python -m identity.cli create-user anna --account-name "Anna home"
```

Details: `services/identity/README.md` and [ADR 0015](../adr/0015-tokens-sessions-and-key-rotation.md).

## Fleet

Fleet owns sites, groups and mowers, writes their place in the hierarchy to OpenFGA and sends every command to the Device-Gateway after checking the right the command kind needs (`can_control`, `can_drive`, or `can_emergency_stop` for an emergency stop). `fleet-migrate` runs the database migrations first. Register a simulated mower with `POST /api/v1/fleet/mowers` (`{"name": "Front lawn", "connection_kind": "simulated"}`), start it with `POST /api/v1/fleet/mowers/<id>/commands` (`{"command": {"kind": "start_mow", "area_ids": ["front"]}}`) and follow the command at `GET /api/v1/fleet/commands/<command_id>`. `FLEET_ALLOWED_CONNECTION_KINDS` in `.env` lists the accepted connection kinds (the example enables `simulated,cloud,ble_bridge`; the web app offers exactly these, from `GET /api/v1/fleet/connection-kinds`). Cloud mowers come from a linked Mammotion account ([ADR 0034](../adr/0034-linked-cloud-accounts-and-device-discovery.md)): link it once in the web app (add mower, or Settings, Linked accounts), then pick its mowers; the password is stored encrypted with `FLEET_CREDENTIAL_ENCRYPTION_KEY` (the example value is a marked development key: generate your own and keep a backup). `GATEWAY_FAKE_MAMMOTION_CLOUD=true` (development only) replaces the Mammotion cloud with a fake one (`*@fake.mowdeck.test`, password `fake-cloud-password`). Details: `services/fleet/README.md` and [ADR 0016](../adr/0016-fleet-commands-and-authorization-per-command-kind.md).

## Telemetry

Telemetry stores what the Device-Gateway publishes: the latest state of every mower, a history in which unchanged heartbeats are kept only once per `TELEMETRY_HEARTBEAT_HISTORY_SECONDS`, the position track and the timeline of command results. Read it with `GET /api/v1/telemetry/mowers/<id>/state`, `.../history?step=5s`, `.../positions` and `.../commands`, or the states of all visible mowers with `GET /api/v1/telemetry/accounts/me/mowers/states`; a mower you cannot see answers 404 like an unknown one, and a visible mower that has not reported yet answers 404 "no telemetry yet". The mower's map and capabilities, as the Device-Gateway last read them, are at `.../map` (with an ETag) and `.../capabilities` (404 "no map yet" or "no capabilities yet" before the first report). A retention job (history 30 days, positions 90, command timeline 30; `TELEMETRY_RETENTION_*_DAYS` in `.env`) deletes older rows, on one replica at a time. Details: `services/telemetry/README.md` and [ADR 0017](../adr/0017-telemetry-storage-and-retention.md).

## Realtime

Realtime pushes the live state and the command results of the mowers a client may view over a WebSocket at `ws://<host>/api/v1/realtime/ws`. The client sends its access token in the first message (`{"type": "auth", "token": "..."}`), then subscribes to mower ids; every id is checked in OpenFGA and checked again when Identity changes grants, every minute and on every token refresh. Traefik needs no extra configuration for the upgrade; protocol pings every `REALTIME_PING_SECONDS` (20) keep idle sockets open. Limits per instance: `REALTIME_MAX_CONNECTIONS_PER_ACCOUNT` (50) and `REALTIME_MAX_CONNECTIONS_PER_USER` (10). `REALTIME_ALLOWED_ORIGINS` (default `http://localhost`) lists the origins allowed to open a socket: add the address you open the web app with when it is not `http://localhost`, including the LAN address a phone on your network uses (for example `REALTIME_ALLOWED_ORIGINS=http://localhost,http://192.168.1.20`). A refused socket closes before the app can say why; realtime logs it at info level with the origin (`websocket refused: origin not allowed`), so `docker compose logs realtime` shows which address to add. Restarting the service closes every socket with 1001; clients reconnect. Realtime is ready when NATS and its two subscriptions are active; an OpenFGA outage keeps open sockets running but denies new subscriptions. Details: `services/realtime/README.md` and [ADR 0018](../adr/0018-realtime-websocket-protocol.md).

## Web app

The service `web` serves the web app (a static SvelteKit PWA in an unprivileged, read-only nginx, port 8080 inside the network) at `/`: open `http://localhost`, register and add a simulated mower. It runs on the same origin as the API, so no CORS is needed. nginx sends a strict Content Security Policy and the other security headers; `/healthz` is its healthcheck, and Traefik waits for it. After a change to the app, `docker compose up -d --build web`; for development use the Vite dev server instead (`pnpm dev`, see `apps/web/README.md`).

**Map background.** By default the map draws a metric grid in the mower's own frame and sends nothing to third parties. `WEB_MAP_TILES` (empty by default) sets a raster tile URL template with `{z}`, `{x}`, `{y}` (and optionally `{s}`), `WEB_MAP_ATTRIBUTION` the attribution text the provider requires (filled in automatically for `openstreetmap.org` URLs), `WEB_MAP_MAX_ZOOM` the highest zoom it delivers (default 19). The `web` container renders them into `/config.json` and adds the tile host to the CSP `img-src` at start (`docker compose up -d web` applies a change); an invalid template is logged and ignored. **Privacy:** with tiles, every map view tells the tile server which tiles are shown, that is where the garden is, together with the viewer's IP address. Respect the provider's usage policy (the public OpenStreetMap tile servers are not meant for apps: https://operations.osmfoundation.org/policies/tiles/); an API key in the template is visible to every user of the app.

**Live camera.** `WEB_CAMERA_ENABLED=false` by default. With `true` the browser may join the live video of cloud mowers on Agora's servers: the `web` container adds `*.agora.io` and `*.sd-rtn.com` (on any port) to the CSP `connect-src` (and `blob:` to `media-src` and `worker-src`) and sets `cameraEnabled` in `/config.json` at start ([ADR 0032](../adr/0032-live-camera-over-agora.md)). The live video needs HTTPS (or `localhost`). **Privacy:** the video of the garden and the viewer's IP address go to Agora and Mammotion; over 4G the stream counts against the mower's video allowance. The simulated mower's test pattern works without the switch.

**Source code link.** The sign-in page and Settings, About show the build's version and link its source code, and every service names the same address in the `X-Source-Code` response header (AGPL-3.0 section 13). `SOURCE_URL` (default the official repository) sets the repository for every service and the web app; a fork that runs modified code must set its own. Locally built images show `dev` and link the repository root; see [Source code offer](source-code.md).

**HTTPS.** Installing the PWA reliably and Web Push (Task 12) need HTTPS. At home the intended path is Tailscale with its HTTPS certificates (for example `https://mowdeck.<tailnet>.ts.net`) or a reverse proxy with a certificate in front of Traefik. Behind HTTPS set `IDENTITY_REFRESH_COOKIE_SECURE=true` (the refresh cookie gets the `Secure` flag) and `REALTIME_ALLOWED_ORIGINS` to the HTTPS origin, then `docker compose up -d`. Behind your own reverse proxy also set `GATEWAY_TRUSTED_PROXIES` to the proxy's address (comma separated IPs or CIDRs, empty by default): Traefik then keeps the proxy's `X-Forwarded-For` and `X-Forwarded-Proto`, so Identity's login throttling and audit log see each user's real IP instead of the proxy's. Without it Traefik discards incoming `X-Forwarded-*` headers, which also keeps clients from faking their address. The proxy needs WebSocket upgrade and a long read timeout, and port 80 should only be reachable from the proxy and the LAN; the checklist is in [infra/compose/README.md](https://github.com/lukislp/mowdeck/blob/main/infra/compose/README.md#behind-your-own-reverse-proxy).

## Notifications

Notifications turns the mower states into notifications (mowing finished, stuck, battery low, offline and back online, optionally job started) and delivers them to the users who subscribed in their preferences (`PUT /api/v1/notifications/preferences`), after checking again that they may view the mower. Channels: Web Push to the PWA (`POST /api/v1/notifications/push-subscriptions`), ntfy (`PUT /api/v1/notifications/ntfy`, servers from `NOTIFICATIONS_NTFY_ALLOWED_SERVERS` only) and the service log (`log`, enabled in `.env.example`). `POST /api/v1/notifications/test` sends a test notification; `GET /api/v1/notifications/inbox` lists the newest 200. Without `NOTIFICATIONS_VAPID_PRIVATE_KEY` the service creates a development VAPID key pair in the `notifications-data` volume on first start and logs the public key; production generates a pair once (`docker compose run --rm notifications python -m notifications.cli generate-vapid-keys`) and sets `NOTIFICATIONS_VAPID_PRIVATE_KEY`, `NOTIFICATIONS_VAPID_PUBLIC_KEY` and `NOTIFICATIONS_VAPID_SUBJECT`. Details: `services/notifications/README.md` and [ADR 0022](../adr/0022-notifications-rules-and-delivery.md).

## Scheduler

The Scheduler fires mowing schedules (weekly or every N days, for a mower or a group) in the site's time zone and starts the mowers through Fleet on behalf of the schedule's owner, whose `can_control` is checked again at every firing. The rain pause uses Open-Meteo (internet access from the container; data CC BY 4.0) and the site's coordinates (`latitude`, `longitude` on `PATCH /api/v1/fleet/sites/{id}`), else the mower's map origin or last position; `SCHEDULER_WEATHER_PROVIDER=static` turns it into a fixed answer for offline installations and tests (`SCHEDULER_STATIC_WEATHER=dry|rain|unknown`). Run records: `GET /api/v1/scheduler/schedules/{id}/runs`; Fleet's command records show `origin=scheduler`. Details: `services/scheduler/README.md` and [ADR 0024](../adr/0024-scheduler-recurrence-rain-pause-and-command-issuing.md).

## Acceptance test

The test suites create users, accounts and mowers, so they run against a throwaway second stack, never the one you use: `pnpm e2e:stack up` starts the Compose project `mowdeck-e2e` from the same `docker-compose.yml` plus `docker-compose.e2e.yml`, with its own volumes, network, image tag and host ports (web at `http://localhost:8480`, everything bound to 127.0.0.1), configured only from `.env.example` and `tests/e2e-web/stack.env`; `pnpm e2e:stack down` deletes it again. Both suites refuse a stack without the e2e marker that the override adds (`MOWDECK_E2E_ALLOW_ANY_STACK=1` overrides that on purpose). Details: [tests/e2e-web/README.md](https://github.com/lukislp/mowdeck/blob/main/tests/e2e-web/README.md).

`pnpm e2e:stack acceptance` runs the Phase 1 acceptance test (`tests/e2e`, `uv run poe e2e`) against the test stack through Traefik, about two minutes including a 90 s idle WebSocket. CI runs it in the job `e2e` and uploads the Compose logs when it fails.

The Phase 2 gate for the web app is a Playwright suite (`tests/e2e-web`): `pnpm install`, then `pnpm e2e:stack test` (or `pnpm e2e:stack run`: up, test, down). CI runs it in the job `e2e-web` and uploads the Playwright report and the Compose logs when it fails.

## Device-Gateway

The Device-Gateway holds the mower connections. It loads the mowers from Fleet when it starts and follows Fleet's `mower.registered`, `mower.updated` and `mower.removed` events; the stack seeds no mower. `MOWDECK_SIM_TIME_SCALE` (30 in `.env.example`) speeds up the simulation. `GET /api/v1/device-gateway/mowers` shows the connection status of the account's mowers. After every connect the gateway publishes the mower's capabilities and map to the stream `DEVICE` (only when they changed) and reads the map again after a job ends; Telemetry stores them for the web app.

The service runs as non-root with a read-only root file system (tmpfs at `/tmp`); its healthcheck uses `/readyz`. Details: `services/device-gateway/README.md`.

## Camera relay (profile `relay`)

`COMPOSE_PROFILES=relay` starts `camera-relay` and its media server MediaMTX ([ADR 0045](../adr/0045-camera-relay.md); the details are in `services/camera-relay/README.md`). The relay asks the Device-Gateway for the mower's camera session, publishes every camera as H.264 into MediaMTX over RTSP (inside the Compose network) and browsers play it over WebRTC (WHEP):

- **Signalling** runs through Traefik on the MowDeck origin: `/media/whep/r/<session>/<camera>/whep` is routed to MediaMTX's WebRTC server with `/media/whep` stripped. Nothing else of MediaMTX is routed: its API (`:9997`) and RTSP (`:8554`) are reachable inside the Compose network only, metrics, playback and pprof are off, and every read, publish and API call is decided by the relay (a viewer needs a relay ticket of a live lease).
- **Media** leaves on one ICE port for UDP and TCP, `RELAY_ICE_PORT` (default 8189), published on `RELAY_ICE_BIND` (default all interfaces). The container listens on the same port it is published on, because ICE candidates carry the listener port. Set `RELAY_ICE_HOSTS` to the address(es) clients reach the host under (the LAN IP, comma separated); there is no STUN and no TURN.
- **Firewall:** UDP and TCP `RELAY_ICE_PORT` must be reachable from the LAN clients (phones in the Wi-Fi). Without UDP, browsers fall back to TCP on the same port.
- The relay holds at most one source per account and mower and stops it 15 s after the last viewer, then asks the gateway to release the stream. The simulator's `fake` camera is played as a generated test pattern; Agora mowers (Mammotion) need the optional agora-bridge (next section). Sources without an available driver are answered with `unsupported_provider`, and Fleet's camera endpoint (`transport` `auto`, the default) then hands out the direct vendor session as before. Without the profile `auto` takes the direct path as well (no relay answers).

The test stacks (`docker-compose.e2e.yml`, `pnpm e2e:stack`) always run the relay, with its test hook `POST /internal/v1/test/freeze` (pauses the fake source) and the ICE port on `127.0.0.1:<MOWDECK_E2E_PORT>+9`.

## Agora source (profile `agora`)

A Mammotion mower's camera comes from Agora's media cloud. The relay plays it once `agora-bridge` runs next to it ([ADR 0045](../adr/0045-camera-relay.md) step 7, details in `integrations/agora-bridge/README.md`): the bridge joins the mower's channel with Agora's Linux server SDK as a receive-only member and hands the H.265 frames to the relay, which transcodes them to H.264 for every viewer. One 4G stream per mower and account however many people watch, ended 15 s after the last viewer, and the vendor token and the viewers' IP addresses stay on the server.

**Licence first.** The image contains Agora's proprietary native SDK. MowDeck never publishes it: the stack builds it on your server (`pull_policy: build`), downloading the SDK from Agora with a pinned SHA-256. **Building it means accepting Agora's SDK licence terms.** Never push the image to a public registry. x86_64 hosts only.

Enable it on a home server (the stack in `infra/compose`, an existing `.env`):

1. Make sure the relay works: `RELAY_ICE_HOSTS` is the server's LAN IP and UDP and TCP `RELAY_ICE_PORT` (8189) are open in its firewall (previous section).
2. Add both profiles to `.env`: `COMPOSE_PROFILES=relay,agora` (keep the profiles you already use, for example `COMPOSE_PROFILES=billing,relay,agora`).
3. Build and start: `docker compose build agora-bridge && docker compose up -d`. The build prints `verified ... (33340805 bytes, sha256 66f8a30d...)` for the SDK and `{"ok": true, ... "tls_verification": true}` for the offline import check; it fails on any other download.
4. Check it: `docker compose ps agora-bridge` is `healthy`, and `docker compose logs agora-bridge` shows `listening on 127.0.0.1:7700`. Open a Mammotion mower's camera in the web app with the camera option "Übertragungsweg" (under "Erweitert") set to "Relais", which shows the relay's errors and never falls back to the direct path: the picture plays; the relay's log has `agora channel joined` and `muxer started` per camera. Set the option back to "Automatisch" afterwards.
5. Updates: `git pull`, then `docker compose build camera-relay agora-bridge && docker compose up -d` (a bridge update is rare; the SDK pin changes only with a reviewed pull request).

Nothing else changes: the relay always looks for the bridge on its own loopback (`MOWDECK_RELAY_AGORA_BRIDGE_URL=tcp://127.0.0.1:7700`), and while nothing listens there (profile off, image missing, container restarting) Agora mowers are answered with `unsupported_provider`, so `auto` uses the direct path. To switch it off again, remove `agora` from `COMPOSE_PROFILES` and remove the container: `docker compose --profile agora rm -sf agora-bridge`. The bridge has no port and no network of its own: it shares `camera-relay`'s network namespace (`network_mode: service:camera-relay`), so the vendor token travels only over that loopback; Compose restarts it with the relay.

## Notes

- Never commit `.env`; secrets never go into images.
- Init scripts run only on an empty data volume.
- Traefik v3.6.1 or newer is needed with Docker 29.

## AI assist (Vision)

The `vision` service ([ADR 0033](../adr/0033-vision-assisted-recovery-with-approval.md)) asks a vision model how to free a stuck mower (or bring it near the dock); a user with `can_drive` approves every maneuver in the web app, and the service drives it with blades off through Fleet's drive sessions. Two ways to provide a model:

- **Every user brings a provider** (Settings, AI provider in the web app): model string, optional base URL, API key. Keys are stored encrypted with `VISION_SECRETS_KEY` (32 bytes, `openssl rand -base64 32`); without it users can only configure keyless providers (Ollama, open endpoints). The plan feature is `ai_recover` (every plan while billing is disabled).
- **The installation provides one** for users without their own: `VISION_DEFAULT_ENABLED=true`, `VISION_DEFAULT_MODEL`, `VISION_DEFAULT_API_BASE`, `VISION_DEFAULT_API_KEY` (plan feature `ai_recover_hosted`). Without `VISION_DEFAULT_API_KEY` LiteLLM reads the family's usual variable (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `MISTRAL_API_KEY`), which you can add to the `vision` service's environment.

Model strings (LiteLLM):

| Family | Model string | Key | Base URL |
| --- | --- | --- | --- |
| Anthropic | `anthropic/claude-sonnet-5-5` (default), `anthropic/claude-opus-5-5`, `anthropic/claude-haiku-5-5` | `ANTHROPIC_API_KEY` | no |
| OpenAI | `openai/gpt-5-mini`, `openai/gpt-4.1-mini` | `OPENAI_API_KEY` | no |
| Google Gemini | `gemini/gemini-2.5-flash`, `gemini/gemini-2.5-pro` | `GEMINI_API_KEY` | no |
| Mistral | `mistral/pixtral-large-latest` | `MISTRAL_API_KEY` | no |
| Ollama (local) | `ollama/llava`, `ollama/qwen2.5vl` | none | `http://<host>:11434` |
| OpenAI-compatible (vLLM, LM Studio, LocalAI, a proxy) | `openai/<model name of the endpoint>` | optional | `http://<host>:<port>/v1` |
| Fake (offline demo, development) | `fake/recover`, `fake/dock`, `fake/decline`, `fake/people`, `fake/low_confidence`, `fake/fail` | none | no |

**Local models.** Run Ollama next to the stack (`docker run -d --name ollama --network mowdeck -v ollama:/root/.ollama ollama/ollama`, then `docker exec ollama ollama pull llava`) and use `ollama/llava` with the base URL `http://ollama:11434`; images then never leave the installation. Small local models answer less reliably: answers that do not match the schema are retried once and otherwise fail, low confidence is never proposed.

**Costs.** One analysis sends one or two images (about 1,000 to 1,600 input tokens each for Claude) and the scene context (about 1,000 tokens) and gets a few hundred output tokens back (plus thinking tokens on current Claude models). With `anthropic/claude-sonnet-5-5` ($2 input and $10 output per million tokens) that is about 1 to 4 US cents per analysis; every request is recorded with its tokens and LiteLLM's estimate (`provider_requests`). Caps: `VISION_USER_DAILY_REQUESTS` per user and day, `VISION_HOSTED_ACCOUNT_DAILY_REQUESTS` per account and day on the installation model.

**Privacy.** With a cloud model the snapshots and the map excerpt around the mower go to that provider. Images are deleted with the proposal after `VISION_PROPOSAL_TTL_SECONDS` (default 300) unless `VISION_KEEP_IMAGES_DAYS` is set; they are never logged.

**Offline demo with the simulator.** Set `VISION_DEFAULT_ENABLED=true` and `VISION_DEFAULT_MODEL=fake/recover`, register a simulated mower with the connection option `{"stuck_after_seconds": 20}` (development only: the mower gets stuck after 20 s of mowing), start mowing, and use AI assist on the mower page when it reports the error. `VISION_ALLOW_FAKE_PROVIDER=true` also offers the fake to users on the settings page (e2e stack).

**A second stack next to this one.** `MOWDECK_NETWORK` names the Docker network (default `mowdeck`). Every Traefik with the Docker provider sees the labels of all containers on the host, so a second stack (own project name with `docker compose -p`, own network, no host ports besides its gateway) must not carry the same router labels: remove them in an override file (`labels: !reset []`) and route with a Traefik file provider, or pin each Traefik to its own project.

### Photos of a real camera and automatic analyses

A Mammotion mower has no photo command, so without an open browser Vision has no picture of it. The camera relay provides it ([ADR 0045](../adr/0045-camera-relay.md), step 8): with the relay running (profile `relay`, for Mammotion also `agora`, see above), set `GATEWAY_RELAY_URL=http://camera-relay:8000` and a shared `RELAY_FRAMES_TOKEN` (for example `openssl rand -hex 24`) in `infra/compose/.env`, then `docker compose up -d camera-relay device-gateway`. Each photo holds a Vision lease for that one request: while someone watches the camera it is a frame of that stream (no second vendor session); otherwise the relay starts it: AI assist and the automatic analysis are error analyses, which may wake a sleeping mower and are not bound by the Vision budget (refused only when the mower has no connection); routine Vision demand keeps every limit (never wakes a sleeping mower, not below 15 % battery unless charging or below 10 % of the video budget, at most 20 Vision minutes per mower and day) and releases it 15 s after the photo. Vision always gets both front cameras of the same moment (the relay's frame set, analysed as a stereo pair); a camera that does not deliver is left out and the analysis goes on with the other. The relay's log shows `vision frame set served` per photo (with the cameras, any `missing` camera and the `skew_ms` between the two frames). Update the camera relay and the Device-Gateway together: a gateway of this release still gets a single frame from an older relay, and an older gateway keeps asking for single frames.

**Deprecated: the capture worker** ([ADR 0035](../adr/0035-server-side-camera-frames-and-automatic-analysis.md)). Until this release the photo came from `vision-capture` (profile `capture`, `GATEWAY_CAPTURE_URL=http://vision-capture:8000`, `CAPTURE_TOKEN`; a headless Chromium that joins the camera itself, about 0.5 GB). It stays for one release: with both configured, the gateway asks the worker only when the relay cannot serve the mower at all (not reachable, frame tap off, no driver for the camera), never after a refusal of the relay. To drop it once the relay path works on your mower:

1. Check a photo through the relay: start AI assist on the mower page without the camera panel open (or wait for an automatic analysis); the relay's log has `vision frame set served` for the mower, and `docker compose logs device-gateway` shows `camera frame captured` with `source=RelaySnapshotter`.
2. Remove `capture` from `COMPOSE_PROFILES` and empty `GATEWAY_CAPTURE_URL` (and `CAPTURE_TOKEN`) in `infra/compose/.env`.
3. `docker compose --profile capture rm -sf vision-capture`, then `docker compose up -d device-gateway`; optionally `docker image rm mowdeck/vision-capture:dev`.

Users switch on "Automatische KI-Analyse bei Fehlern" on the AI provider page; when a mower gets stuck, blocked, leaves its area or its wheels slip, Vision takes a photo, asks the user's model and sends "KI-Vorschlag für <Mäher>" as a notification. Limits: `VISION_AUTO_COOLDOWN_MINUTES` (10) per mower, `VISION_AUTO_DAILY_PER_MOWER` (20); `VISION_AUTO_ENABLED=false` switches it off. Nothing moves without an approval.
