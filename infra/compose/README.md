# Compose stack (self-hosted mode)

Runs the foundation infrastructure of MowDeck on one machine. The same images run in Kubernetes with the Helm chart `infra/helm/mowdeck` ([docs/ops/helm.md](../../docs/ops/helm.md)).

## Start

```bash
cd infra/compose
cp .env.example .env          # PowerShell: Copy-Item .env.example .env
# edit the passwords in .env
docker compose up -d --wait   # waits until every service is healthy
```

Then open the web app at `http://localhost`.

The project name is `mowdeck` (`name:` in `docker-compose.yml`). A stack started with another `-p <name>` needs that name on every later command, or `COMPOSE_PROJECT_NAME=<name>` in `.env`; without it Compose starts a second stack ([Project name](../../docs/ops/compose.md#start-check-reset)).

This builds the MowDeck images from the checkout. To run a signed release from GHCR instead, set `MOWDECK_IMAGE_REGISTRY=ghcr.io/lukislp/mowdeck` and `MOWDECK_VERSION=<version>` in `.env`, run `./verify-release.sh <version>` and start with `docker compose -f docker-compose.yml -f docker-compose.release.yml up -d --no-build --wait` ([Updating to a release](../../docs/ops/compose.md#updating-to-a-release)).

Optional routing check (a `whoami` container behind Traefik at `/api/v1/whoami`):

```bash
docker compose --profile whoami up -d --wait
curl http://localhost/api/v1/whoami
```

A public demo with simulated mowers (one-click "Try the demo", reset every few hours) is a separate stack with the override `docker-compose.demo.yml`; see [docs/ops/demo.md](../../docs/ops/demo.md).

## What runs

| Service | Image (pinned) | Host port | Purpose |
| --- | --- | --- | --- |
| traefik | `public.ecr.aws/docker/library/traefik:v3.7.14` | 80, 443 | Gateway; docker provider, dashboard disabled, `/api/v1/<service>` routing by labels |
| nats | `public.ecr.aws/docker/library/nats:2.15.0-alpine` | 127.0.0.1:4222, 127.0.0.1:8222 | Event bus with JetStream; monitoring and `/healthz` on 8222 |
| postgres | `public.ecr.aws/docker/library/postgres:17.11-alpine` | 127.0.0.1:5432 | One database and one role per service (see `init/01-databases.sql`) |
| openfga-migrate | `ghcr.io/openfga/openfga:v1.22.0` | none | One-shot schema migration for OpenFGA |
| openfga | `ghcr.io/openfga/openfga:v1.22.0` | 127.0.0.1:8080 (HTTP), 127.0.0.1:8081 (gRPC) | Authorization; playground off, pre-shared key auth |
| authz-bootstrap | `mowdeck/authz-bootstrap:dev` (built from `infra/openfga/bootstrap`: OpenFGA CLI `v0.8.1`, busybox, jq) | none | One-shot: creates the store `mowdeck` and writes the authorization model when it changed |
| whoami (profile `whoami`) | `ghcr.io/traefik/whoami:v1.10.3` | via Traefik | Placeholder for routing checks |
| identity-migrate | `mowdeck/identity:dev` | none | One-shot: Alembic migrations of the `identity` database |
| identity | `mowdeck/identity:dev` (built from `services/identity/Dockerfile`) | via Traefik at `/api/v1/identity` and `/.well-known/jwks.json` | Accounts, users, roles, sessions, API tokens; issues the access tokens |
| fleet-migrate | `mowdeck/fleet:dev` | none | One-shot: Alembic migrations of the `fleet` database |
| fleet | `mowdeck/fleet:dev` (built from `services/fleet/Dockerfile`) | via Traefik at `/api/v1/fleet` | Sites, groups, mowers, the Device-Gateway's registry, commands and command records |
| telemetry-migrate | `mowdeck/telemetry:dev` | none | One-shot: Alembic migrations of the `telemetry` database |
| telemetry | `mowdeck/telemetry:dev` (built from `services/telemetry/Dockerfile`) | via Traefik at `/api/v1/telemetry` | Latest state, history, position track and command timeline of every mower |
| device-gateway | `mowdeck/device-gateway:dev` (built from `services/device-gateway/Dockerfile`) | via Traefik at `/api/v1/device-gateway` | Mower connections (simulator, Mammotion cloud, Bluetooth bridge), commands, telemetry |
| realtime | `mowdeck/realtime:dev` (built from `services/realtime/Dockerfile`) | via Traefik at `/api/v1/realtime` (WebSocket `/api/v1/realtime/ws`) | Live states and command results of the mowers a client may view |
| notifications-migrate | `mowdeck/notifications:dev` | none | One-shot: Alembic migrations of the `notifications` database |
| notifications | `mowdeck/notifications:dev` (built from `services/notifications/Dockerfile`) | via Traefik at `/api/v1/notifications` | Notifications from the mower states: rules, inbox, Web Push, ntfy and log delivery |
| scheduler-migrate | `mowdeck/scheduler:dev` | none | One-shot: Alembic migrations of the `scheduler` database |
| scheduler | `mowdeck/scheduler:dev` (built from `services/scheduler/Dockerfile`) | via Traefik at `/api/v1/scheduler` | Mowing schedules with rain pause and mowing angle rotation; starts mowers through Fleet |
| entitlements-migrate | `mowdeck/entitlements:dev` | none | One-shot: Alembic migrations of the `entitlements` database |
| entitlements | `mowdeck/entitlements:dev` (built from `services/entitlements/Dockerfile`) | via Traefik at `/api/v1/entitlements` | Plans and limits per account (`unlimited` unless billing is on) |
| planner-migrate | `mowdeck/planner:dev` | none | One-shot: Alembic migrations of the `planner` database |
| planner | `mowdeck/planner:dev` (built from `services/planner/Dockerfile`) | via Traefik at `/api/v1/planner` | Spot mowing: patterns, previews, plans, coverage gaps |
| vision-migrate | `mowdeck/vision:dev` | none | One-shot: Alembic migrations of the `vision` database |
| vision | `mowdeck/vision:dev` (built from `services/vision/Dockerfile`) | via Traefik at `/api/v1/vision` | AI assist: recovery proposals from camera photos, nothing moves without an approval |
| billing-migrate, billing (profile `billing`) | `mowdeck/billing:dev` (built from `services/billing/Dockerfile`) | via Traefik at `/api/v1/billing` | Subscriptions and payments (Stripe); off unless the profile is enabled, see [Planner, plans, billing and AI assist](#planner-plans-billing-and-ai-assist) |
| camera-relay (profile `relay`) | `mowdeck/camera-relay:dev` (built from `services/camera-relay/Dockerfile`, with ffmpeg) | none | Receives each mower's stream once and serves it to every viewer through MediaMTX; frame tap for Vision ([ADR 0045](../../docs/adr/0045-camera-relay.md)) |
| mediamtx (profile `relay`) | `bluenviron/mediamtx:1.21.2` | via Traefik at `/media/whep/r/`; `RELAY_ICE_BIND:RELAY_ICE_PORT` UDP and TCP (default `0.0.0.0:8189`) | WebRTC (WHEP) media server of the camera relay; uid 65534, all capabilities dropped |
| agora-bridge (profile `agora`, with `relay`) | `mowdeck-local/agora-bridge:local`, always built locally from `integrations/agora-bridge` (contains Agora's proprietary SDK; never pulled or published) | none (camera-relay's loopback `127.0.0.1:7700`) | Agora source of the camera relay ([integrations/agora-bridge](../../integrations/agora-bridge/README.md)) |
| matter-server (profile `matter`) | `mowdeck/matter-server:dev` (built from `services/matter-server/Dockerfile`, Node 24, matter.js) | **host network**: UDP 5353 (mDNS), one UDP port per mower from `MATTER_PORT_BASE` (5540), admin API `MATTER_HTTP_PORT` (8790) via Traefik at `/api/v1/matter` | Every mower as a Matter device for Apple Home, Google Home and Home Assistant ([Matter server](../../docs/ops/matter.md), [ADR 0063](../../docs/adr/0063-matter-server.md)); firewall the ports to the LAN and the admin port to Docker's bridges |
| web | `mowdeck/web:dev` (built from `apps/web/Dockerfile`) | via Traefik at `/` | The web app (static SvelteKit PWA in an unprivileged nginx) |

Model and model tests: `infra/openfga/README.md`.

## Image sources

Every third-party image the stack runs or builds from is pinned in one place, the `*_IMAGE` lines under the heading "Image sources" of `.env.example`, and selected by the same variable in `.env`: `TRAEFIK_IMAGE`, `NATS_IMAGE`, `POSTGRES_IMAGE`, `OPENFGA_IMAGE`, `WHOAMI_IMAGE` for the infrastructure, `PYTHON_IMAGE`, `UV_IMAGE`, `NODE_IMAGE`, `NGINX_IMAGE`, `BUSYBOX_IMAGE`, `FGA_CLI_IMAGE`, `JQ_IMAGE` for the base images, passed to the Dockerfiles as build arguments, and `NTFY_IMAGE` and `STRIPE_MOCK_IMAGE` for the two images only the CI integration tests run (the stack never starts them; they are in the list so that the mirror below and the checks have one list). The defaults are public registries without a login: the official Docker images from ECR Public (`public.ecr.aws/docker/library/...`, Docker's mirror of Docker Hub), OpenFGA, whoami, jq and uv from `ghcr.io`, and the OpenFGA CLI, ntfy and stripe-mock from Docker Hub. A repository check (`tools/repo-checks/tests/test_images.py`) keeps the Dockerfile and Compose defaults, the Helm chart, the CI workflows and `infra/mirror-env.sh` equal to that list. The base images are pinned by digest as well, as `image:tag@sha256:<index digest>` (the OpenSSF Scorecard's Pinned-Dependencies check, enforced by the same test for every `ARG *_IMAGE` default): a bump changes tag and digest together, the digest being the `Digest` line of `docker buildx imagetools inspect <image:tag>`. The mirror copies such a line at exactly that digest under its tag, and `infra/mirror-env.sh` keeps the digest in the references it prints, so a pull from the mirror is checked against it.

**Other registries for a home installation.** To pull from a registry of your own (a pull-through cache, a Harbor or a mirror on your NAS), copy the images there with the same tags and set the variables in `.env`, one by one or all at once:

```bash
echo 'POSTGRES_IMAGE=registry.example.org/postgres:17.11-alpine' >> infra/compose/.env   # one image
MOWDECK_IMAGE_MIRROR=registry.example.org/mowdeck sh infra/mirror-env.sh >> infra/compose/.env   # all of them
docker login registry.example.org        # if the registry needs a login
docker compose up -d --build --wait
```

`infra/mirror-env.sh` prints one `NAME_IMAGE=<prefix>/<alias>:<tag>` line per image of the list; the alias is the variable name without `_IMAGE` in lower case with `-` for `_` (`postgres`, `fga-cli`, `stripe-mock`), the prefix is its argument or `MOWDECK_IMAGE_MIRROR`. A later line in `.env` overrides an earlier one, and a variable exported in the shell overrides `.env`. Remove the lines again to go back to the public defaults.

**The CI mirror (decision of 2026-10-14).** CI does not pull from the public registries: GitHub-hosted runners share their egress addresses, so the anonymous rate limits of ECR Public and Docker Hub are shared with every other workflow behind the same address, and a build fails with `429 Too Many Requests` whenever that quota is spent. The workflow `.github/workflows/mirror-images.yml` copies every image of the list with `crane copy` (all architectures, digests unchanged) to the repository's own package namespace `ghcr.io/<owner>/mowdeck/mirror/<alias>:<tag>`; it runs when `.env.example`, `infra/mirror-env.sh` or the workflow changes on `main` or in a pull request of this repository, and by hand (`gh workflow run mirror-images.yml`), skips images whose digest already matches and retries with backoff on rate limits. `ci.yml`, `helm-kind.yml` and `release.yml` log in to GHCR with `GITHUB_TOKEN` (`packages: read`), export the mirror references with `infra/mirror-env.sh` (the workflows set `MOWDECK_IMAGE_MIRROR`) and use them for the postgres service container, the `docker run` commands, the build arguments and the `.env` of the Compose jobs. The stack at home keeps pulling the public images: the mirror is a CI concern, and `.env.example` stays as it is unless `MOWDECK_IMAGE_MIRROR` or single `*_IMAGE` variables say otherwise ([ADR 0029](../../docs/adr/0029-helm-chart-and-release-pipeline.md), amendment). A pull request that adds or bumps an image fills the mirror itself; when its CI started pulling before the copy finished, re-run the failed jobs.

**The mirror packages.** The first copy creates the package `mowdeck/mirror/<alias>` under the owner, and the workflow's `GITHUB_TOKEN` gives this repository's workflows access to it. The copies carry no label or annotation of their own (one would change the digest, and the mirror keeps the source digests), so GHCR cannot connect them to the repository by itself: connect each package once by hand so that it appears on the repository's Packages page and inherits the repository's permissions (GitHub, the owner's profile, Packages, `mowdeck/mirror/<alias>`, "Connect repository" in the sidebar or Package settings, "Manage Actions access"). The visibility is set in the same place; the images are public upstream, so a public package gives nothing away, and a private one can be pulled outside CI only with `docker login ghcr.io` and a token that has `read:packages`. If a package with one of these names already exists under the owner without this repository's write access, grant it or remove the package before the first run; otherwise the push is refused.

## Authorization bootstrap

`authz-bootstrap` runs after OpenFGA is healthy and exits. It is idempotent, so it runs on every `up`:

1. It looks for the store named `OPENFGA_STORE_NAME` (default `mowdeck`; with several of that name the oldest wins) and creates it if missing.
2. It renders the latest model of that store and `infra/openfga/model.fga` with the same CLI transformer and writes the model only when they differ. Rewriting an unchanged model would only create a new model id and make running services and new ones disagree about "latest".

Typical output, first and second run:

```text
authz-bootstrap: created store mowdeck (01M4ETX89CTD4SGA6DPRT42J0E)
authz-bootstrap: wrote model 01M4ETX8EHR001DM13HZ5S5J1Y
authz-bootstrap: found store mowdeck (01M4ETX89CTD4SGA6DPRT42J0E)
authz-bootstrap: model unchanged (01M4ETX8EHR001DM13HZ5S5J1Y)
```

The ids are only logged. Services using `mowdeck-authz` resolve the store **by name** and pin the latest model at startup, so nothing has to be copied into their configuration; `MOWDECK_OPENFGA_STORE_ID` and `MOWDECK_OPENFGA_MODEL_ID` pin both when wanted. Every service that uses authorization (Identity, Fleet, Telemetry, Realtime, Notifications, Scheduler, Entitlements, Planner, Vision, and Billing with its profile) depends on the job with `service_completed_successfully`, which also lets `up --wait` accept the exited job; Traefik depends on a healthy Identity, Fleet, Telemetry, Realtime, Notifications, Scheduler, Entitlements, Planner, Vision and web app. After changing `model.fga`, rebuild the job: `docker compose up -d --build --wait`.

| Variable | `.env.example` | Meaning |
| --- | --- | --- |
| `OPENFGA_API_URL` | `http://openfga:8080` | OpenFGA URL for the job and the services |
| `OPENFGA_API_TOKEN` | unset (falls back to `OPENFGA_AUTHN_PRESHARED_KEYS`) | Key the clients use; set it when several pre-shared keys are configured |
| `OPENFGA_STORE_NAME` | `mowdeck` | Store name |

## MowDeck services

Services are built from the repository root (`build.context: ../..`); `docker compose up -d --build --wait` rebuilds them. They run as non-root with `read_only: true` and a tmpfs at `/tmp`, and their healthcheck calls `/readyz`.

### Identity

`identity-migrate` runs the Alembic migrations and exits; `identity` starts after it, after `authz-bootstrap` and with NATS and OpenFGA healthy. On first start Identity creates its signing key. Traefik routes `/api/v1/identity` and `/.well-known/jwks.json`. Three routers with priority 100 and 101 add rate limits per client IP:

| Router | Rule | Middleware | Limit |
| --- | --- | --- | --- |
| `identity-auth` | `Path(/api/v1/identity/session)` (login), `/register`, `/token`, `/passkeys/session`, `/app-login/session`, the GitHub and Apple callbacks (`/oauth/<provider>/callback`) and link starts (`/me/external-identities/<provider>/link`) | `identity-ratelimit` | 10 per minute, burst 20 |
| `identity-session` | `PathPrefix(/api/v1/identity/session/)` (refresh, logout) | `identity-session-ratelimit` | 60 per minute, burst 30 |
| `identity-passkey-challenge` | `/passkeys/challenge`, `/app-login/start`, `/oauth/github/start`, `/oauth/apple/start` (they check nothing; every sign-in page asks for a challenge) | `identity-session-ratelimit` | 60 per minute, burst 30 |

Refresh and logout need a valid refresh cookie anyway; their looser limit keeps several devices of a household behind one address (Tailscale, a proxy) logged in. Services inside the network verify tokens against `http://identity:8000/.well-known/jwks.json` (`MOWDECK_AUTH_JWKS_URL`).

| Variable | `.env.example` | Meaning |
| --- | --- | --- |
| `IDENTITY_KEY_ENCRYPTION_KEY` | a marked development value | **Required.** 256-bit key (base64) that encrypts the signing keys at rest; generate your own for any real installation and keep a backup |
| `IDENTITY_REGISTRATION` | `open` | `open`, or `closed` (only the first account; more owners with the CLI) |
| `IDENTITY_ARGON2_PROFILE` | `rfc9106_low` | `owasp_min` for small hosts |
| `IDENTITY_REFRESH_COOKIE_SECURE` | `false` | `true` behind HTTPS (see [HTTPS](#https)) |
| `IDENTITY_PUBLIC_URL` | empty (passkeys off) | The address users open MowDeck at, for example `https://mowdeck.example.org`; passkeys are bound to its host name (HTTPS and a host name, not an IP; plain HTTP only for `http://localhost`), and the GitHub and Apple callbacks are built from it ([Passkeys](../../docs/ops/passkeys.md)) |
| `IDENTITY_PASSKEY_RP_ID` | empty | Optional parent domain of that host as the passkeys' relying party id |
| `IDENTITY_GITHUB_CLIENT_ID`, `IDENTITY_GITHUB_CLIENT_SECRET_FILE` | empty (GitHub login off) | Sign in with GitHub ([ADR 0051](../../docs/adr/0051-sign-in-with-github.md), [operations](../../docs/ops/github-login.md)); the secret is a file in `secrets/github/` (git-ignored, mounted at `/run/secrets/github`, readable by uid 10001) |
| `IDENTITY_APPLE_SERVICES_ID`, `_TEAM_ID`, `_KEY_ID`, `_PRIVATE_KEY_FILE`, `IDENTITY_APPLE_PKCE` | empty, `true` | Sign in with Apple ([ADR 0052](../../docs/adr/0052-sign-in-with-apple.md), [operations](../../docs/ops/apple-login.md)); the `.p8` key is a file in `secrets/apple/` (git-ignored, mounted at `/run/secrets/apple`) |
| `IDENTITY_EXTRA_MODULE_URLS` | empty | Descriptors of optional modules Identity lists besides the built-in services (comma separated); with the profile `matter`: `http://host.docker.internal:8790` |

Try it:

```bash
curl -X POST http://localhost/api/v1/identity/register -H 'Content-Type: application/json' \
  -d '{"username": "lukas", "password": "correct horse battery", "account_name": "Home"}'
curl -X POST http://localhost/api/v1/identity/session -H 'Content-Type: application/json' \
  -d '{"username": "lukas", "password": "correct horse battery"}'
curl http://localhost/api/v1/device-gateway/mowers -H "Authorization: Bearer <access_token>"
docker compose exec identity python -m identity.cli list-keys
```

### Fleet

`fleet-migrate` runs the Alembic migrations of the `fleet` database and exits; `fleet` starts after it, after `authz-bootstrap` and with NATS and OpenFGA healthy. It verifies access tokens against Identity's JWKS, writes the hierarchy tuples to OpenFGA, answers the Device-Gateway's `fleet.mowers.list` request and publishes `event.mower.registered|updated|removed`. Identity reads its module descriptor (`MOWDECK_IDENTITY_MODULE_URLS`).

| Variable | `.env.example` | Meaning |
| --- | --- | --- |
| `FLEET_ALLOWED_CONNECTION_KINDS` | `simulated` | Connection kinds accepted at registration |
| `FLEET_CREDENTIAL_ENCRYPTION_KEY` | a marked development value | 256-bit key (base64) that encrypts the passwords of linked Mammotion accounts ([ADR 0034](../../docs/adr/0034-linked-cloud-accounts-and-device-discovery.md)). Empty: Fleet starts, but linking a Mammotion account answers 503, so no cloud mower can be added. Fleet refuses the development value with `MOWDECK_ENVIRONMENT=prod`; generate your own and keep a backup |
| `FLEET_CREDENTIAL_ENCRYPTION_KEY_PREVIOUS` | empty | Key rotation: earlier keys (comma separated) that still decrypt; the next re-read of a linked account re-encrypts with the current key |
| `FLEET_PATH_PREVIEW_REQUEST_TIMEOUT_SECONDS` | `85` | How long Fleet waits for a path preview (504 after); keep `GATEWAY_PATH_PREVIEW_TIMEOUT_SECONDS` (75) < this < the web app's 90 s |
| `GATEWAY_CLOUD_PROVIDERS_ENABLE` | empty | Cloud providers to enable besides the default ones (those tested on real hardware), comma separated provider ids such as `husqvarna` ([ADR 0066](../../docs/adr/0066-cloud-provider-plugins-and-more-vendors.md)); disabled providers are not offered in the web app |
| `GATEWAY_CLOUD_PROVIDERS_DISABLE` | empty | Cloud providers to switch off (wins over the defaults and the enable list) |
| `GATEWAY_FAKE_CLOUDS` | `false` | **Development only.** The Device-Gateway answers linked account logins and connects cloud mowers through the fake cloud of every enabled provider (for example `*@fake.mowdeck.test`, password `fake-cloud-password`); the browser tests switch it on. The former name `GATEWAY_FAKE_MAMMOTION_CLOUD` still works |

Register a simulated mower and start it (`<token>` from the login above; the simulator's default map has the areas `front` and `back`):

```bash
curl -X POST http://localhost/api/v1/fleet/mowers -H "Authorization: Bearer <token>"   -H 'Content-Type: application/json' -d '{"name": "Front lawn", "connection_kind": "simulated"}'
curl -X POST http://localhost/api/v1/fleet/mowers/<mower_id>/commands -H "Authorization: Bearer <token>"   -H 'Content-Type: application/json' -d '{"command": {"kind": "start_mow", "area_ids": ["front"]}}'
curl http://localhost/api/v1/fleet/commands/<command_id> -H "Authorization: Bearer <token>"
```

### Telemetry

`telemetry-migrate` runs the Alembic migrations of the `telemetry` database and exits; `telemetry` starts after it, after `authz-bootstrap` and with NATS and OpenFGA healthy. It consumes the Device-Gateway's `telemetry.mower.*`, `command.result.mower.*` and map and capabilities snapshots (`device.mower.>`, stream `DEVICE`) and Fleet's `mower.*` events (and seeds its mower directory from `fleet.mowers.list`), keeps the latest state, a de-duplicated history, the position track, the command timeline and the latest map and capabilities in PostgreSQL, and serves them under `/api/v1/telemetry`. Identity reads its module descriptor (`MOWDECK_IDENTITY_MODULE_URLS`), so `telemetry.view` appears in the permission catalogue.

| Variable | `.env.example` | Meaning |
| --- | --- | --- |
| `TELEMETRY_HEARTBEAT_HISTORY_SECONDS` | `300` | An unchanged state is stored in the history at most this often |
| `TELEMETRY_RETENTION_HISTORY_DAYS` | `30` | History older than this is deleted |
| `TELEMETRY_RETENTION_POSITIONS_DAYS` | `730` | Position track older than this is deleted (`0` keeps it forever) |
| `TELEMETRY_POSITIONS_DETAIL_DAYS` | `90` | Positions younger than this keep every row; older ones are thinned |
| `TELEMETRY_MAINTENANCE_TIMEZONE` | `UTC` | IANA zone of the nightly thinning window, for example `Europe/Berlin` |
| `TELEMETRY_RETENTION_COMMANDS_DAYS` | `30` | Command timeline older than this is deleted |
| `TELEMETRY_BLADE_HOURS_THRESHOLD` | `50` | Health history: remind to change the blades after this many blade hours since the last change |
| `AERIAL_WMS_URL`, `AERIAL_WMS_LAYER`, `AERIAL_XYZ_URL`, `AERIAL_ATTRIBUTION`, `AERIAL_IMAGE_FORMAT`, `AERIAL_MAX_ZOOM`, `AERIAL_CACHE_MAX_MB` | empty (off), `image/jpeg`, `19`, `256` | Optional aerial photo under the map ([ADR 0054](../../docs/adr/0054-aerial-photo-under-the-map.md), [operations](../../docs/ops/aerial-photo.md)): Telemetry fetches and caches the tiles (volume `aerial-cache`) and serves them only around an account's own maps |

Position thinning, the nightly window and the retention numbers: [Telemetry retention](../../docs/ops/telemetry-retention.md) ([ADR 0053](../../docs/adr/0053-position-retention-and-downsampling.md)). Theft tracking: [operations](../../docs/ops/theft-tracking.md) ([ADR 0058](../../docs/adr/0058-theft-tracking-mode.md)).

```bash
curl http://localhost/api/v1/telemetry/mowers/<mower_id>/state -H "Authorization: Bearer <token>"
curl "http://localhost/api/v1/telemetry/mowers/<mower_id>/history?step=5s" -H "Authorization: Bearer <token>"
curl http://localhost/api/v1/telemetry/mowers/<mower_id>/commands -H "Authorization: Bearer <token>"
curl http://localhost/api/v1/telemetry/mowers/<mower_id>/map -H "Authorization: Bearer <token>"
```

### Realtime

`realtime` starts after `authz-bootstrap`, with NATS and OpenFGA healthy, and owns no data. Clients open `ws://localhost/api/v1/realtime/ws`, send `{"type": "auth", "token": "<access token>"}` as the first message (never in the URL) and then `{"type": "subscribe", "mower_ids": [...]}`; every id is checked with `can_view` and denied ids are reported in `subscribed.denied`. Each instance subscribes to `telemetry.mower.*` and `command.result.mower.*` without a queue group, so replicas need no sticky sessions. Identity reads its module descriptor, so `realtime.view` appears in the permission catalogue.

Traefik passes the WebSocket upgrade through without extra configuration: the router is a plain `PathPrefix(/api/v1/realtime)` on the `web` entry point, Traefik forwards `Upgrade`, `Connection` and `Origin` unchanged and adds the `X-Forwarded-*` headers. Uvicorn sends protocol pings every `REALTIME_PING_SECONDS` (20), which keeps idle sockets open well below Traefik's entry point timeouts; the acceptance test holds a socket idle for 90 s through Traefik. On `docker compose stop realtime` every socket is closed with 1001 (going away).

| Variable | `.env.example` | Meaning |
| --- | --- | --- |
| `REALTIME_PING_SECONDS` | `20` | Protocol ping interval and pong timeout |
| `REALTIME_MAX_CONNECTIONS_PER_ACCOUNT` | `50` | Open sockets per account (close 4429 above) |
| `REALTIME_MAX_CONNECTIONS_PER_USER` | `10` | Open sockets per user (close 4429 above) |
| `REALTIME_DRIVE_MAX_SECONDS` | `600` | A manual drive session (joystick) ends after this long |
| `REALTIME_ALLOWED_ORIGINS` | `http://localhost` | Comma separated allowed `Origin` values (empty allows all). The web app is served at the gateway's origin: add the address you open it with when it is not `http://localhost`, such as the LAN address a phone uses (`http://localhost,http://192.168.1.20`); realtime logs each refused origin at info level |

Close codes and the protocol: `services/realtime/README.md`, [ADR 0018](../../docs/adr/0018-realtime-websocket-protocol.md).

### Notifications

`notifications-migrate` runs the Alembic migrations of the `notifications` database and exits; `notifications` starts after it, after `authz-bootstrap` and with NATS and OpenFGA healthy. It consumes `telemetry.mower.*` (durable queue `notifications-states`), Fleet's and the Device-Gateway's `mower.*` events (`notifications-mowers`; the directory is also seeded from `fleet.mowers.list`) and Identity's user events (`notifications-identity`), derives notifications with the rules of [ADR 0022](../../docs/adr/0022-notifications-rules-and-delivery.md), keeps each user's inbox and delivers through an outbox worker. Identity reads its module descriptor, so `notifications.manage` (implied by `mower.view`) appears in the permission catalogue.

VAPID keys: when `NOTIFICATIONS_VAPID_PRIVATE_KEY` is empty, the service creates a development key file in the volume `notifications-data` (`/var/lib/notifications/vapid_private_key`) on first start and logs the public key (`development VAPID key file in use`); it survives restarts and is deleted with `down -v`. Production generates a pair once and sets it explicitly (the key file is then ignored):

```bash
docker compose run --rm notifications python -m notifications.cli generate-vapid-keys
```

| Variable | `.env.example` | Meaning |
| --- | --- | --- |
| `NOTIFICATIONS_CHANNELS` | `webpush,ntfy,apns,log` | Enabled channels (`log` writes each message to the service log) |
| `NOTIFICATIONS_VAPID_PRIVATE_KEY`, `NOTIFICATIONS_VAPID_PUBLIC_KEY` | empty (development key file) | Web Push key pair (base64url) |
| `NOTIFICATIONS_VAPID_SUBJECT` | `mailto:admin@localhost.localdomain` | Operator contact for the push services |
| `NOTIFICATIONS_NTFY_ALLOWED_SERVERS` | `https://ntfy.sh` | ntfy servers users may choose (a local server: add its URL, for example `http://host.docker.internal:8091`) |
| `NOTIFICATIONS_APP_URL` | empty | Public web app URL for links in notifications |
| `APNS_KEY_FILE` | empty (APNs off) | Path of the APNs signing key inside the container, for example `/run/secrets/apns/AuthKey_ABC123DEFG.p8`; the directory `infra/compose/secrets/apns/` (git-ignored) is mounted at `/run/secrets/apns` |
| `APNS_KEY_ID`, `APNS_TEAM_ID` | empty | Key ID and Team ID (10 characters each) from the Apple Developer account |
| `APNS_BUNDLE_ID` | `app.mowdeck.mobile` | Bundle ID of the iOS app (`apns-topic`) |
| `APNS_ENVIRONMENT` | `development` | `development` (sandbox, builds from Xcode) or `production` (TestFlight, App Store) |
| `NOTIFICATIONS_OFFLINE_AFTER_SECONDS` | `120` | Offline after this long without a state |
| `NOTIFICATIONS_BATTERY_LOW_PERCENT` | `20` | Battery threshold |
| `NOTIFICATIONS_MOWING_MIN_SECONDS` | `10` (service default 120) | Shorter jobs end without `mowing_finished`; the simulated front lawn takes well under a minute of wall time at `MOWDECK_SIM_TIME_SCALE=30` |
| `NOTIFICATIONS_GEOFENCE_MARGIN_METERS` | `5` | Theft alarm: margin around the mapped areas, paths and dock before `geofence_exit` |
| `NOTIFICATIONS_ACK_SILENCE_SECONDS` | `3600` | An acknowledged alarm stays silent for that user this long |
| `NOTIFICATIONS_DEFAULT_LANGUAGE` | `de` | Message language (`de`, `en`) of users who never saved preferences |

Deploys with `./deploy.sh <version>` open a maintenance window in Notifications so that the restart does not send `mower_offline`/`mower_online` ([ADR 0055](../../docs/adr/0055-deploy-maintenance-window.md), [Deploying with the maintenance window](../../docs/ops/compose.md#deploying-with-the-maintenance-window)). Camera photos in error and theft pushes: [ADR 0056](../../docs/adr/0056-camera-photo-in-push-notifications.md).

```bash
curl -X PUT http://localhost/api/v1/notifications/preferences -H "Authorization: Bearer <token>"   -H 'Content-Type: application/json' -d '{"enabled_kinds": ["mowing_finished", "mower_stuck", "battery_low", "mower_offline", "mower_online"], "all_mowers": true, "channels": ["webpush", "ntfy", "log"]}'
curl -X POST http://localhost/api/v1/notifications/test -H "Authorization: Bearer <token>"
curl http://localhost/api/v1/notifications/inbox -H "Authorization: Bearer <token>"
docker compose logs notifications | grep "notification delivered"
```

### Scheduler

`scheduler-migrate` runs the Alembic migrations of the `scheduler` database and exits; `scheduler` starts after it, after `authz-bootstrap` and with NATS and OpenFGA healthy. It consumes `telemetry.mower.*` (durable queue `scheduler-states`, the busy check) and the map snapshots on `device.mower.>` (`scheduler-device`, areas and geo origin), ticks every `SCHEDULER_TICK_SECONDS` and fires due schedules through Fleet's internal request `fleet.commands.issue` on behalf of each schedule's owner ([ADR 0024](../../docs/adr/0024-scheduler-recurrence-rain-pause-and-command-issuing.md)). Identity reads its module descriptor, so `schedule.edit` and `scheduler.view` (implied by `mower.view`) appear in the permission catalogue.

The rain pause asks Open-Meteo by default, which needs internet access from the container (free for non-commercial use, data CC BY 4.0, "Weather data by Open-Meteo.com"). Offline installations set `SCHEDULER_WEATHER_PROVIDER=static`. The location of a mower is its site's coordinates (`PATCH /api/v1/fleet/sites/{id}` with `latitude` and `longitude`), else its map origin, else its last position.

| Variable | `.env.example` | Meaning |
| --- | --- | --- |
| `SCHEDULER_WEATHER_PROVIDER` | `open_meteo` | `open_meteo` or `static` |
| `SCHEDULER_STATIC_WEATHER` | `dry` | Answer of the static provider: `dry`, `rain` or `unknown` |
| `SCHEDULER_TICK_SECONDS` | `30` | How often due schedules are fired |
| `SCHEDULER_DEV_ENDPOINTS` | `false` | Development only: `PUT /api/v1/scheduler/dev/weather` switches the static provider |

```bash
curl -X POST http://localhost/api/v1/scheduler/schedules -H "Authorization: Bearer <token>" -H 'Content-Type: application/json'   -d '{"name": "Morning", "target_type": "mower", "target_id": "<mower_id>", "kind": "weekly", "days_of_week": ["mon", "thu"], "start_time": "09:00", "area_ids": ["front"]}'
curl http://localhost/api/v1/scheduler/schedules/<schedule_id>/preview -H "Authorization: Bearer <token>"
curl -X POST "http://localhost/api/v1/scheduler/schedules/<schedule_id>/run-now?respect_rain=true" -H "Authorization: Bearer <token>"
curl http://localhost/api/v1/scheduler/schedules/<schedule_id>/runs -H "Authorization: Bearer <token>"
```

### Web app

`web` serves the web app built from `apps/web` (SvelteKit static build, `public.ecr.aws/nginx/nginx-unprivileged:1.30.5-alpine`, uid 101, port 8080, `read_only: true` with a tmpfs at `/tmp`). Traefik routes `PathPrefix(/)` to it with priority 1, so every `/api/v1` route and `/.well-known/jwks.json` win; the app and the API share one origin, so no CORS is needed and the refresh cookie works as is. nginx sets the Content Security Policy (hashes computed at build time), `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, `X-Frame-Options: DENY` and `Cross-Origin-Opener-Policy`; hashed assets under `/_app/immutable/` are cached for a year, everything else is revalidated, unknown paths get the SPA fallback `200.html`. The healthcheck calls `/healthz`. Its runtime configuration (written at container start, no rebuild needed except for `WEB_AGORA_SDK`):

| Variable | `.env.example` | Meaning |
| --- | --- | --- |
| `WEB_MAP_TILES`, `WEB_MAP_ATTRIBUTION`, `WEB_MAP_MAX_ZOOM` | empty, empty, `19` | Optional raster tiles under the map (`{z}/{x}/{y}` template). Empty: a metric grid, no request leaves the installation; with tiles every map view sends the garden's location and the viewer's IP address to the tile server |
| `WEB_CAMERA_ENABLED` | `false` | Direct live camera of cloud mowers through Agora (the CSP then allows `*.agora.io` and `*.sd-rtn.com`; video and IP address go to Agora and Mammotion) |
| `WEB_AGORA_SDK` | `include` | Build argument: `include` keeps the proprietary Agora Web SDK in the locally built image (for your own installation only, never redistribute it), `exclude` is what releases publish |
| `WEB_LEGAL_ENABLED` | `false` | Links the legal pages from the login page (replace the placeholder pages first) |

```bash
docker compose up -d --build web     # rebuild after a change to apps/web or libs/mowdeck-ui
curl -I http://localhost/            # 200 with the security headers
```

For development run the Vite dev server against this stack instead (`pnpm dev`, proxying `/api` to `http://localhost`; see `apps/web/README.md`).

### HTTPS

Installing the PWA reliably and Web Push (Phase 2, Task 12) need HTTPS. At home use Tailscale with its HTTPS certificates (for example `https://mowdeck.<tailnet>.ts.net`) or a reverse proxy with a certificate. Then set in `.env`:

| Variable | Value |
| --- | --- |
| `IDENTITY_REFRESH_COOKIE_SECURE` | `true` |
| `REALTIME_ALLOWED_ORIGINS` | the HTTPS origin, for example `https://mowdeck.<tailnet>.ts.net` (comma separated with `http://localhost` if you keep both) |
| `IDENTITY_PUBLIC_URL` | the same HTTPS origin, for passkeys and the GitHub and Apple sign-in callbacks |

and run `docker compose up -d`.

#### Behind your own reverse proxy

When a reverse proxy you already run (Nginx Proxy Manager, Caddy, nginx, HAProxy, ...) terminates TLS and forwards to this stack's port 80, configure it and the stack as follows. The examples use `192.0.2.10` for the proxy and `https://mowdeck.example.org` for the public origin; put in your own.

- **Forward to plain HTTP** on the host's `GATEWAY_HTTP_PORT` (for example `http://192.168.1.10:80`) and pass the original `Host` header.
- **WebSocket upgrade.** The live status runs over `/api/v1/realtime/ws`; the proxy must pass `Upgrade` and `Connection` (in Nginx Proxy Manager: "Websockets Support"; in nginx: `proxy_http_version 1.1`, `proxy_set_header Upgrade $http_upgrade`, `proxy_set_header Connection "upgrade"`).
- **Long read timeout.** Keep the proxy's read timeout well above `REALTIME_PING_SECONDS` (20 s) and above the longest API call (path previews take up to `GATEWAY_PATH_PREVIEW_TIMEOUT_SECONDS`, 75 s): for nginx for example `proxy_read_timeout 3600s` and `proxy_send_timeout 3600s`.
- **Forwarded headers.** The proxy sets `X-Forwarded-For` (`$proxy_add_x_forwarded_for`) and `X-Forwarded-Proto` (`$scheme`, so `https`). Most proxies do this by default.
- **Trust the proxy.** Set `GATEWAY_TRUSTED_PROXIES` in `.env` to the proxy's address (comma separated IPs or CIDRs, for example `GATEWAY_TRUSTED_PROXIES=192.0.2.10`). Traefik then keeps the proxy's `X-Forwarded-For` and `X-Forwarded-Proto` instead of replacing them with the proxy's own address, so Identity's per-IP login throttling, `login_attempts` and the audit log see each user's real address. Without it every request appears to come from the proxy and all users share one throttling bucket. Empty (the default) trusts no one: a client's own `X-Forwarded-*` headers are discarded. List only proxies you control, never a range that ordinary clients connect from, or they can fake their address.
- **Count each client in the rate limits.** Set `GATEWAY_CLIENT_IP_DEPTH=1` (one proxy that sets or appends `X-Forwarded-For`; nginx with `$proxy_add_x_forwarded_for` and Caddy do). Trusting the proxy alone does not change Traefik's rate limits (`identity-ratelimit`, `identity-session-ratelimit`, `billing-webhook-ratelimit`): they count the TCP peer, so behind a proxy all users share the login limit of 10 a minute. With the depth they count the entry the proxy wrote, and Identity and Telemetry take the same entry as the client address, which a client cannot forge by sending its own `X-Forwarded-For`. Empty (the default) keeps the earlier behaviour. Do not set it when the proxy passes a client's `X-Forwarded-For` on unchanged. Details and the reasoning: [docs/ops/demo.md](../../docs/ops/demo.md#client-addresses-behind-a-proxy).
- **HTTPS settings.** `IDENTITY_REFRESH_COOKIE_SECURE=true` and `REALTIME_ALLOWED_ORIGINS` with the HTTPS origin (for example `https://mowdeck.example.org`), as in the table above.
- **Restrict port 80.** The stack speaks plain HTTP on `GATEWAY_HTTP_PORT`; allow it only from the proxy (and, if you want direct access, your LAN) in the host firewall, so nobody bypasses TLS. For example with ufw: `ufw allow from 192.0.2.10 to any port 80 proto tcp` and `ufw allow from 192.168.1.0/24 to any port 80 proto tcp`. Note that Docker's published ports bypass ufw's `INPUT` rules; filter in the `DOCKER-USER` chain, or bind the port to one interface with a host address in `GATEWAY_HTTP_PORT` (for example `192.168.1.10:80`).

Then `docker compose up -d` (Traefik restarts with the new flag).

### Phase 1 acceptance test

`pnpm e2e:stack up` and `pnpm e2e:stack acceptance` run `tests/e2e` (registration, sub-user with the Operator role, a simulated mower, start and dock with live status over the WebSocket, a foreign account and a Viewer rejected, an idle socket through Traefik) against a throwaway test stack, never this one. See `tests/e2e/README.md` and "Test stack" below; CI runs it in the job `e2e`.

### Phase 2 gate (web app)

`pnpm install`, `pnpm e2e:stack up` and `pnpm e2e:stack test` run the Playwright suite `tests/e2e-web` against the test stack at `http://localhost:8480`; `pnpm e2e:stack screens` writes review screenshots and `pnpm e2e:stack down` deletes the stack. CI runs it in the job `e2e-web`.

### Test stack

`docker-compose.e2e.yml` is an override for throwaway test stacks: it only adds the service `e2e-marker` (`GET /api/v1/e2e-stack` answers with the Compose project name), and both test suites refuse a stack without it. `tests/e2e-web/scripts/stack.mjs` (`pnpm e2e:stack`) starts `docker-compose.yml` plus this override as the project `mowdeck-e2e` with its own network, Traefik scope, volumes, image tag (`mowdeck/<service>:mowdeck-e2e`) and host ports from 8480 on (all on 127.0.0.1), reading only `.env.example` and `tests/e2e-web/stack.env`, never this directory's `.env`. Details and variables: `tests/e2e-web/README.md`.

### Device-Gateway

The Device-Gateway loads its mowers from Fleet (`fleet.mowers.list` on start, then the `mower.*` events); the stack seeds no mower. It reads these variables from `.env`:

| Variable | `.env.example` | Meaning |
| --- | --- | --- |
| `MOWDECK_VERSION` | `dev` | Tag of the locally built images |
| `MOWDECK_SIM_TIME_SCALE` | `30` | Simulated seconds per real second, so a lawn is mown in minutes |
| `MOWDECK_TELEMETRY_HEARTBEAT_SECONDS` | `10` | Telemetry heartbeat |

| `GATEWAY_PATH_PREVIEW_TIMEOUT_SECONDS` | `75` | Longest time a mower may take to plan a path preview; keep it below `FLEET_PATH_PREVIEW_REQUEST_TIMEOUT_SECONDS` (85) |
| `GATEWAY_RELAY_URL`, `RELAY_FRAMES_TOKEN` | empty | With the profile `relay`: `http://camera-relay:8000` and a shared secret (`openssl rand -hex 24`), so Vision's photos of cloud mowers come from the camera relay's frame tap; either empty: no relay frames |
| `GATEWAY_PHOTO_TIMEOUT_SECONDS`, `CAPTURE_TIMEOUT_SECONDS` | `30`, `20` | Longest time for a whole photo and for one relay frame (at most 30 s) |

`MOWDECK_BOOTSTRAP_MOWERS` remains a development-only setting of the service for running it without Fleet (see `services/device-gateway/README.md`); the stack does not pass it.

Databases created: `identity`, `fleet`, `telemetry`, `scheduler`, `notifications`, `entitlements`, `billing`, `planner`, `vision`, `openfga`, each owned by a role of the same name. The init scripts run only on an empty volume: on an existing one create a database added later (for example `planner`) once with `docker compose exec postgres psql -U postgres -c "CREATE ROLE planner LOGIN PASSWORD '<SERVICE_DB_PASSWORD>'" -c "CREATE DATABASE planner OWNER planner"`.

### Planner, plans, billing and AI assist

Spot mowing ([ADR 0030](../../docs/adr/0030-mowing-patterns-and-planner.md)): `planner-migrate` and `planner` serve `/api/v1/planner` (patterns, previews, plans, coverage gaps, templates). A plan is started through Fleet with the `command` the Planner returns; the simulated mower follows it (`custom_paths`), a LUBA runs only plans with a native job. `PLANNER_CUTTING_WIDTH_M` and `PLANNER_MAX_PARALLEL` tune it.

Plans ([ADR 0028](../../docs/adr/0028-entitlements-plans-and-enforcement.md)): the services `entitlements-migrate` and `entitlements` run with `ENTITLEMENTS_BILLING_ENABLED=false` by default, so every account has the plan `unlimited`. The plan file `infra/entitlements/plans.yaml` is mounted read-only into the Entitlements container. To sell plans set `ENTITLEMENTS_BILLING_ENABLED=true` (and `ENTITLEMENTS_DEFAULT_PLAN`, default `home`) in `.env` and recreate the stack; make an operator admin with `docker compose exec identity python -m identity.cli grant-platform-admin <username>`.

Billing ([ADR 0031](../../docs/adr/0031-billing-with-stripe-adapter.md)): `billing-migrate` and `billing` belong to the Compose profile `billing` and are not started without it (`docker compose config --services` lists them only with `--profile billing`); that is what `billing.enabled=false` means. To sell plans set `COMPOSE_PROFILES=billing`, `ENTITLEMENTS_BILLING_ENABLED=true` and the `BILLING_*` variables of `.env.example` (Stripe keys, price ids, `BILLING_APP_URL`) and run `docker compose up -d --wait`. The price file `infra/billing/prices.yaml` is mounted read-only into the Billing container. Traefik routes `/api/v1/billing` to it and rate limits the Stripe webhook path (`billing-webhook-ratelimit`, 120 per minute, burst 60). For a demo without Stripe use `BILLING_PROVIDER=fake` and `python -m billing.cli send-fixture` ([Billing operations](../../docs/ops/billing.md)). `WEB_LEGAL_ENABLED=true` links the legal pages from the login page.

AI assist ([ADR 0033](../../docs/adr/0033-vision-assisted-recovery-with-approval.md)): `vision-migrate` and `vision` start with the stack. Users configure their own vision model in the web app (keys encrypted with `VISION_SECRETS_KEY`); `VISION_DEFAULT_ENABLED=true` with `VISION_DEFAULT_MODEL` (any LiteLLM model string, `fake/recover` for an offline demo) and `VISION_DEFAULT_API_KEY` offers the installation's model. Details: [Compose operations, AI assist](../../docs/ops/compose.md#ai-assist-vision).

### Camera relay (profile `relay`)

`camera-relay` and `mediamtx` serve the mowers' cameras to every viewer over WebRTC (WHEP) ([ADR 0045](../../docs/adr/0045-camera-relay.md)). Signalling runs through Traefik (`/media/whep/r/...`); the media uses one ICE port for UDP and TCP that LAN clients must reach (`RELAY_ICE_HOSTS`, the addresses clients reach this host under, default `127.0.0.1`; `RELAY_ICE_PORT` 8189; `RELAY_ICE_BIND` 0.0.0.0). Agora mowers additionally need the self-built `agora-bridge` (profile `agora`). Details: [Compose operations, Camera relay](../../docs/ops/compose.md#camera-relay-profile-relay).

### Matter server (profile `matter`)

`matter-server` exposes every mower as its own Matter device ([ADR 0063](../../docs/adr/0063-matter-server.md)). It runs on the **host network** (mDNS and IPv6 link-local), so Docker publishes nothing and the host firewall decides who reaches it: allow UDP 5353 and the UDP ports from `MATTER_PORT_BASE` (5540) on, one per mower (at most `MATTER_MAX_NODES`), from the LAN only, and the admin API `MATTER_HTTP_PORT` (8790) from Docker's bridges only ([nftables example](../../docs/ops/matter.md#firewall-nftables)). Its API tokens (one per account, `mower.view` and `mower.control` only) go one per line into `secrets/matter/api-tokens` (git-ignored, mounted read-only, readable by uid 1000); set `IDENTITY_EXTRA_MODULE_URLS=http://host.docker.internal:8790` so the web app shows the Matter section. Other variables: `MATTER_API_URL` (default Traefik on the host), `MATTER_LANGUAGE`, `MATTER_MDNS_INTERFACE`. The volume `matter-data` holds the fabrics and setup codes: back it up like `.env`.

## Check health

```bash
docker compose ps
curl http://localhost:8222/healthz                       # NATS
curl http://localhost/api/v1/device-gateway/mowers       # Device-Gateway via Traefik (401 without a token)
curl http://localhost/api/v1/fleet/mowers                # Fleet via Traefik (401 without a token)
curl http://localhost/api/v1/realtime/ws                 # Realtime via Traefik (404 without a WebSocket upgrade)
curl http://localhost/.well-known/jwks.json              # Identity's public keys
curl -I http://localhost/                                # the web app (200, security headers)
curl -H "Authorization: Bearer $OPENFGA_AUTHN_PRESHARED_KEYS" http://localhost:8080/stores   # OpenFGA
```

## Reset (deletes all data)

```bash
docker compose down -v
```

The PostgreSQL init scripts only run on an empty data volume, so changes to `init/` need a reset.

## Notes

- Never commit `.env` or anything under `secrets/` (each folder carries a `.gitignore` that ignores everything but itself). Secrets never go into images: every Dockerfile has an allow-list `Dockerfile.dockerignore`, so neither `.env` nor `secrets/` reaches a build context.
- The full list of variables with comments is `.env.example`; this README covers the ones an operator usually sets.
- `docker compose config -q` needs the required variables: use `--env-file .env.example` for a dry check (`uv run poe compose-check` does this).
- `MOWDECK_NETWORK` (default `mowdeck`) names the Docker network that the services and Traefik share (`networks.mowdeck.name`, `--providers.docker.network`). To run a second stack on the same host without crossing the first, use another project name and network, and other host ports: `MOWDECK_NETWORK=other GATEWAY_HTTP_PORT=8081 GATEWAY_HTTPS_PORT=8443 docker compose -p other up -d --wait` (or `-p other` with `MOWDECK_NETWORK=other` in that stack's `.env`).
- Traefik v3.6.1 or newer is required with Docker 29 (older versions cannot talk to the Docker API).
