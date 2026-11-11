# Operating the Compose stack

The stack lives in `infra/compose/` and runs the foundation infrastructure on one machine.

## Start, check, reset

```bash
cd infra/compose
cp .env.example .env            # set the passwords
./nats-passwords.sh .env        # one NATS password per service (see "NATS users" below)
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
./nats-passwords.sh --check .env                    # the release's NATS passwords (ADR 0074)
sed -i 's/^MOWDECK_VERSION=.*/MOWDECK_VERSION=<x>/' .env
docker compose -f docker-compose.yml -f docker-compose.release.yml pull
# only when the release changed nats/ (git diff --quiet <old tag> v<x> -- nats/ fails):
docker compose -f docker-compose.yml -f docker-compose.release.yml kill -s HUP nats
docker compose -f docker-compose.yml -f docker-compose.release.yml up -d --no-build --wait
```

The `kill -s HUP` reloads NATS's users and permissions ([NATS permissions after an upgrade](#nats-permissions-after-an-upgrade)); `docker compose logs nats` then shows "Reloaded server configuration".

`verify-release.sh` reads the image digests from the notes of the GitHub release `v<x>` (with `gh` when it is installed, else the public GitHub API with `curl`; no token), checks that each registry tag `<x>` points to that digest, verifies each digest's Sigstore signature with `cosign` (bound to `https://github.com/<repo>/.github/workflows/release.yml@refs/heads/main` and the issuer `https://token.actions.githubusercontent.com`; cosign from `PATH`, else the pinned cosign container) and fails closed: if one image fails, it writes nothing and exits 1. On success it writes `docker-compose.release.yml` (ignored by git), which sets every service to `<registry>/<service>:<x>@sha256:<digest>`, so `up` runs exactly the verified images even if a tag is moved later. `--print` writes the override to stdout, `--out FILE` elsewhere, `--no-pin` only verifies. The repository comes from `SOURCE_URL` (or `MOWDECK_RELEASE_REPO=<owner>/<repo>`) and must belong to the registry's owner. Instead of a full checkout, fetching `docker-compose.yml`, `verify-release.sh`, `init/` and `mediamtx/` of the tag is enough.

agora-bridge (profile `agora`) is never published: it keeps its local image `mowdeck-local/agora-bridge:local`; build it once with `docker compose --profile agora build agora-bridge` before an `up --no-build`.

**Rollback.** Check out the earlier tag, run `./verify-release.sh <earlier>` (it rewrites the override), set `MOWDECK_VERSION=<earlier>` and run the same `pull` and `up` commands. Migrations are backward compatible, so the older images start against the newer schema ([Rolling back](release.md#rolling-back)); a rollback across a migration that is not needs the backup ([the pre-deploy dump](#the-pre-deploy-dump) into an empty `postgres-data` volume).

## Deploying with the maintenance window

`deploy.sh` performs the procedure above in one go and wraps the restart in the deploy maintenance window ([ADR 0055](../adr/0055-deploy-maintenance-window.md)), so the Device-Gateway restart sends no "mower offline / back online" notifications:

```bash
cd infra/compose                 # of the checkout on the server
./deploy.sh <x>                  # e.g. ./deploy.sh 0.9.0; --dry-run only prints the steps
```

It runs, and stops at the first failure: `git fetch --tags`; the [pre-deploy dump](#the-pre-deploy-dump) (a gzip'd `pg_dumpall` in `${MOWDECK_BACKUP_DIR:-~/mowdeck-backups}`, refused unless it ends with "PostgreSQL database cluster dump complete", and a copy of `.env` in a separate directory; only the newest three of each are kept); `git checkout --detach v<x>` (refused with local changes); `./verify-release.sh <x>` (nothing is pulled when it fails) and `./nats-passwords.sh --check .env` (the NATS passwords of that release, see [NATS users](#nats-users-and-permissions)); `MOWDECK_VERSION=<x>` in `.env`; `pull`; **open the window** (`--window-minutes`, default 15, expires by itself); when the release changed `nats/` and NATS is running, **reload NATS** and wait up to `MOWDECK_NATS_RELOAD_SECONDS` (10) for its confirmation, stopping before `up` when it refuses the new file ([why](#nats-permissions-after-an-upgrade)); `up -d --no-build --wait`; **close the window** (also when `up` fails); a table of every service, failing when one is not running and healthy. It never prints `.env` or its values. `--no-maintenance` skips the window. A stack under another project name: `COMPOSE_PROJECT_NAME=<name> ./deploy.sh <x>`. The manual procedure above stays possible; add the window by hand with:

```bash
docker compose exec -T notifications python -m notifications.cli maintenance open --minutes 15
# ... pull / up --wait ...
docker compose exec -T notifications python -m notifications.cli maintenance close
docker compose exec -T notifications python -m notifications.cli maintenance status
```

While the window is open, `mower_offline` / `mower_online` are held back for a mower whose link broke inside it; a mower still offline two minutes after the close is reported then. Theft alarms and errors are never held. The first deploy of the release that introduces the window runs the old notifications image, which has no such command: the script warns and goes on, so that one deploy can still send the pair. Helm: no hook yet; the same CLI can run from a pre/post-upgrade Job later.

## The pre-deploy dump

Step 2 of `deploy.sh` exists for one purpose: **rolling back a failed update**. It is not a backup strategy, and the few dumps it keeps are no substitute for one.

What it writes, on every deploy (`infra/compose/backup-lib.sh`, shared by the script's steps; there is no other script that dumps or restores):

| What | Where | Mode |
| --- | --- | --- |
| `mowdeck-before-<version>-<UTC stamp>.sql.gz`: a `pg_dumpall` of every database, written to a `.partial` file first, then checked (valid gzip, not empty, ends with "cluster dump complete") and moved into place | `${MOWDECK_BACKUP_DIR:-~/mowdeck-backups}`, directory `0700` | `0600` |
| `env-before-<version>-<UTC stamp>.env`: a copy of the stack's `.env` as it was before the update | `${MOWDECK_ENV_BACKUP_DIR:-~/.mowdeck-env-backups}`, directory `0700`, **a different directory tree** | `0600` |

A failed or empty dump stops the deploy before anything changes and deletes nothing. Only after the new dump and the new `.env` copy are complete, `deploy.sh` deletes the older ones: it keeps the newest `MOWDECK_BACKUP_KEEP` (default **3**) of each, ordered by the stamp in the name. Only files named exactly like the two patterns above are ever deleted; other files and directories in those directories stay. The script sets `umask 077` itself, tightens an existing directory to `0700`, prints file names and counts and never a value of `.env`. `MOWDECK_ENV_BACKUP_DIR` must not be inside `MOWDECK_BACKUP_DIR` or the other way round; the script refuses to start otherwise.

**Why 3.** The dump is for the rollback after a failed update, so the newest one is the one that matters. The two before it cover an update that was started again after a failure (the second dump already holds the state of the failed one) and one release deliberately skipped back. Anything older is a backup, and that is not what this file is for. Change it per run or permanently, e.g. `MOWDECK_BACKUP_KEEP=1 ./deploy.sh <x>` (1 to 100). Every kept dump holds the data of the day it was written, including accounts and mowers that were deleted afterwards, until the rotation removes it; a lower number shortens that.

**Why the `.env` copy is apart.** The database holds the passwords of linked mower accounts and provider keys encrypted with keys from `.env` (`IDENTITY_KEY_ENCRYPTION_KEY`, `FLEET_CREDENTIAL_ENCRYPTION_KEY` and the like). A dump next to its keys gives whoever gets the directory both. The copy is kept at all because an older dump is only restorable together with the keys of its time: after a key rotation, or a lost or edited `.env`, the live file may no longer match. The rollback otherwise uses the live `.env` and only needs the copy for that case, and for `MOWDECK_VERSION` as it was. Keep `MOWDECK_ENV_BACKUP_DIR` out of anything you copy off the host together with the dumps.

**Rollback with the dump.** Only when the older images cannot run against the newer schema ([Rolling back](release.md#rolling-back)): stop the stack, bring up an empty `postgres-data` volume and restore into it, with the `.env` copy of the same stamp in place of the live one if the keys changed:

```bash
gzip -dc ~/mowdeck-backups/mowdeck-before-<x>-<stamp>.sql.gz | docker compose exec -T postgres psql -U postgres -d postgres
```

**A dump is only restorable together with the matching keys.** Store `IDENTITY_KEY_ENCRYPTION_KEY`, `FLEET_CREDENTIAL_ENCRYPTION_KEY` and the other keys and passwords of `.env` in a place of your own that is not the host and not next to the dumps, for example a password manager or the secret store you already use, and update that entry whenever a key changes. Without the keys, the encrypted columns of a restored database are unreadable.

**Long-term backups are yours for now.** MowDeck does not create nightly or off-host backups, does not set a retention for them and does not delete anything you copy elsewhere. If you need them, schedule your own dump, decide how long you keep it and where it is stored, and remember that deleted data survives in every backup until that backup is deleted. A retention recommendation for the project is still being decided.

**After updating an existing installation.** The first `deploy.sh` run after the update still runs the script of the release you are on; the new behaviour starts with the deploy after that. From then on `deploy.sh` moves earlier `env-before-*.env` files out of the dump directory into the `.env` directory and deletes dumps and copies beyond the newest `MOWDECK_BACKUP_KEEP`. To do the same at once, or to keep a dump first, copy the ones you want out of the directory and run:

```bash
cd ~/mowdeck-backups
mkdir -p -m 700 ~/.mowdeck-env-backups && chmod 700 ~/.mowdeck-env-backups
mv -n env-before-*.env ~/.mowdeck-env-backups/ && chmod 600 ~/.mowdeck-env-backups/*.env
chmod 700 . && chmod 600 mowdeck-before-*.sql.gz
ls -1 mowdeck-before-*.sql.gz | sed -E 's/.*-([0-9]{8}T[0-9]{6}Z)\..*/\1 &/' | sort -r |
  tail -n +4 | cut -d' ' -f2 | xargs -r rm --      # all but the newest three
```

A manual `~/mowdeck-before-<x>.sql` from the procedure above is not touched by the script; delete it when the update succeeded.

## Services and ports

Every hop inside the stack is encrypted and verified with certificates of the installation's own CA ([internal TLS](internal-tls.md)): the services, NATS, PostgreSQL and OpenFGA speak TLS on the ports below, also on the host ports.

| Service | Image | Host port |
| --- | --- | --- |
| tls-init | `mowdeck/identity:dev`, built from the repository | one-shot, no network (creates and renews the CA and the certificates of the [internal TLS](internal-tls.md)) |
| traefik | `public.ecr.aws/docker/library/traefik:v3.7.14` | 80, 443 |
| nats | `public.ecr.aws/docker/library/nats:2.15.0-alpine` | 127.0.0.1:4222 (client, TLS, a user and password per service); monitoring (`/healthz`) on the container's loopback only |
| nats-streams | `mowdeck/identity:dev`, built from the repository | one-shot, no port (creates and updates the JetStream streams) |
| postgres | `public.ecr.aws/docker/library/postgres:17.11-alpine` | 127.0.0.1:5432 (TLS required over the network) |
| openfga-migrate | `ghcr.io/openfga/openfga:v1.22.0` | one-shot, no port |
| openfga | `ghcr.io/openfga/openfga:v1.22.0` | 127.0.0.1:8080 (HTTPS), 127.0.0.1:8081 (gRPC with TLS) |
| authz-bootstrap | `mowdeck/authz-bootstrap:dev`, built from `infra/openfga/bootstrap` | one-shot, no port |
| whoami (profile `whoami`) | `ghcr.io/traefik/whoami:v1.10.3` | via Traefik at `/api/v1/whoami` |
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
| entitlements-migrate | `mowdeck/entitlements:dev`, built from the repository | one-shot, no port |
| entitlements | `mowdeck/entitlements:dev`, built from the repository | via Traefik at `/api/v1/entitlements` |
| planner-migrate | `mowdeck/planner:dev`, built from the repository | one-shot, no port |
| planner | `mowdeck/planner:dev`, built from the repository | via Traefik at `/api/v1/planner` |
| billing-migrate, billing (profile `billing`) | `mowdeck/billing:dev`, built from the repository | via Traefik at `/api/v1/billing` ([Billing](billing.md)) |
| vision-migrate | `mowdeck/vision:dev`, built from the repository | one-shot, no port |
| vision | `mowdeck/vision:dev`, built from the repository | via Traefik at `/api/v1/vision` |
| camera-relay (profile `relay`) | `mowdeck/camera-relay:dev`, built from the repository (with ffmpeg) | none (internal: NATS requests `relay.camera.*` and MediaMTX's auth callback; [ADR 0045](../adr/0045-camera-relay.md)) |
| agora-bridge (profile `agora`, with `relay`) | `mowdeck-local/agora-bridge:local`, built locally from `integrations/agora-bridge` (contains Agora's SDK, never pulled or published) | none (listens on camera-relay's loopback `127.0.0.1:7700`, shares its network namespace; [Agora source](#agora-source-profile-agora)) |
| assistant (profile `assistant`) | `mowdeck/assistant:dev`, built from the repository | via Traefik at `/api/v1/assistant`; HTTP only, no NATS user and no database (chat control, [operations](assistant.md), [ADR 0075](../adr/0075-chat-control-assistant-add-on.md)) |
| matter-server (profile `matter`) | `mowdeck/matter-server:dev`, built from the repository (Node 24, matter.js) | host network: UDP 5353 (mDNS) and one UDP port per mower from `MATTER_PORT_BASE`; admin API `MATTER_HTTP_PORT` (8790) via Traefik at `/api/v1/matter` ([Matter server](matter.md), [ADR 0063](../adr/0063-matter-server.md)) |
| mediamtx (profile `relay`) | `bluenviron/mediamtx:1.21.2` (MIT) | via Traefik at `/media/whep/r/` (WHEP only, `/media/whep` stripped); `RELAY_ICE_BIND:RELAY_ICE_PORT` UDP and TCP (ICE media, default 8189) |
| web | `mowdeck/web:dev`, built from `apps/web/Dockerfile` | via Traefik at `/` (the web app; router priority 1, behind every `/api/v1` route) |

PostgreSQL creates the databases `identity`, `fleet`, `telemetry`, `scheduler`, `notifications`, `entitlements`, `billing`, `planner`, `vision`, `openfga`, each with a role of the same name.

The complete operating notes are in the repository file `infra/compose/README.md`.

## Health and routing

Every MowDeck service answers three health endpoints ([ADR 0077](../adr/0077-liveness-readiness-and-routing.md)):

| Endpoint | Answers | Used by |
| --- | --- | --- |
| `/healthz` | 200 while the process serves requests; touches no dependency | Kubernetes liveness probe |
| `/startupz` | 503 until every readiness check passed once, then 200 like `/healthz` for the rest of the process's life | the Compose healthcheck, the image's `HEALTHCHECK`, the Kubernetes startup probe |
| `/readyz` | the readiness checks now (database, OpenFGA, NATS, subscriptions, ...); 503 with the failing check, and while shutting down; 200 with `"status": "degraded"` when only a check that is not required fails (`token-deny-list`, ADR 0082) | operators, the Kubernetes readiness probe |

So `docker compose up --wait`, `deploy.sh` and `depends_on: service_healthy` still wait until a service was ready, but once it runs, neither load nor a dependency that fails later makes its container unhealthy. That matters because Traefik's Docker provider routes only to healthy containers: under the load test a saturated Telemetry answered its old `/readyz` healthcheck too late, Docker marked it unhealthy and every Telemetry request got 404 for about 40 s at a time ([Load testing](load-testing.md)). Traefik also runs with `--providers.docker.allowEmptyServices=true`, so a service that does become unhealthy (a hung process) answers 503 instead of disappearing (404).

Dependency trouble after the start shows up in the service itself: each service evaluates its readiness checks every `MOWDECK_READINESS_INTERVAL_SECONDS` (default 30, 0 turns it off), logs `service not ready` with the failing checks (and `service ready again`) and exports `mowdeck_readiness_check_up{check}` on `/metrics`. By hand: `docker compose exec telemetry python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/readyz').read())"`; a failing check answers 503 with its name and error. The database check uses a connection of its own, never the request pool, so a busy pool does not count as a broken database.

## Database connections

PostgreSQL runs with `max_connections=200` (`command` of the `postgres` service). Every service may open at most its pool (`MOWDECK_DATABASE_POOL_SIZE`, default 5) plus overflow (`MOWDECK_DATABASE_MAX_OVERFLOW`, default 5), plus one short-lived connection per readiness check:

| Client | Connections at most |
| --- | ---: |
| Telemetry (pool 10 + overflow 5 + readiness 1; most of a mower page) | 16 |
| Identity, Fleet, Notifications, Scheduler, Entitlements, Planner, Vision (5 + 5 + 1 each) | 77 |
| Billing (profile `billing`) | 11 |
| OpenFGA (`OPENFGA_DATASTORE_MAX_OPEN_CONNS`) | 30 |
| One-shot jobs (migrations, one each, only at `up`) | 10 |
| Reserved for superusers (`psql`, backups) | 3 |
| **Together** | **147** |

That leaves about 50 for a replica or a larger pool. A service that needs more sets its two variables in its `environment`; raise `max_connections` with it (`tools/repo-checks/tests/test_compose.py` checks that the budget fits). The load test peaked at 92 connections. Kubernetes: every replica counts again; the chart sets `postgresql.cloudnativepg.maxConnections` (default 200).

## Image sources

All third-party images are pinned under "Image sources" in `.env.example` and chosen in `.env` by the same variables: `TRAEFIK_IMAGE`, `NATS_IMAGE`, `POSTGRES_IMAGE`, `OPENFGA_IMAGE`, `WHOAMI_IMAGE` for the infrastructure and `PYTHON_IMAGE`, `UV_IMAGE`, `NODE_IMAGE`, `NGINX_IMAGE`, `BUSYBOX_IMAGE`, `FGA_CLI_IMAGE`, `JQ_IMAGE` for the base images of the MowDeck builds (plus `NTFY_IMAGE` and `STRIPE_MOCK_IMAGE`, which only the CI integration tests run). The defaults are public registries that need no login (ECR Public, `ghcr.io`, Docker Hub for the OpenFGA CLI, ntfy and stripe-mock).

To use another registry at home (a pull-through cache or a mirror on the NAS), copy the images there with the same tags and set the variables in `.env`, for example `POSTGRES_IMAGE=registry.example.org/postgres:17.11-alpine`, or all of them at once with `MOWDECK_IMAGE_MIRROR=registry.example.org/mowdeck sh infra/mirror-env.sh >> infra/compose/.env`; a later line in `.env` overrides an earlier one. Log in to that registry first if it needs credentials.

CI pulls the same pinned images through the repository's GHCR mirror `ghcr.io/<owner>/mowdeck/mirror/<alias>:<tag>`, which the workflow `mirror-images.yml` fills from the same list, because anonymous pulls from the public registries are rate limited on the shared CI runners; the stack at home is not affected. Details: `infra/compose/README.md`, "Image sources".

## NATS users and permissions

Every service connects to NATS as its own user and may use only the subjects its code uses ([ADR 0074](../adr/0074-nats-authentication-and-subject-permissions.md)): a compromised container cannot command mowers, forge another service's events or read another service's replies. The users and their permissions are `nats/auth.conf`, generated from the subject map in `tools/repo-checks/scripts/nats_auth.py` (`uv run poe nats-auth`; never edit the file by hand). The one-shot job `nats-streams` creates and updates the JetStream streams as the user `streams` before the services start; no service may change a stream.

Each user has a password in `.env`, `NATS_PASSWORD_<USER>` (`NATS_PASSWORD_FLEET`, `NATS_PASSWORD_DEVICE_GATEWAY`, ..., `NATS_PASSWORD_STREAMS`). NATS gets all of them, each service only its own. `.env.example` has development placeholders; for any real installation generate them:

```bash
cd infra/compose
./nats-passwords.sh .env          # adds missing passwords, replaces placeholders, keeps good ones
./nats-passwords.sh --check .env  # only checks (deploy.sh runs this before every update)
docker compose up -d --wait       # after a change: recreates NATS and the services
```

The script never prints a value. A password is 24 to 128 letters, digits, `-` and `_` and starts with a letter (NATS reads it from its environment as a configuration value; one that starts with a digit could be taken for a number). Rotate a password by deleting its line and running the script again, then `docker compose up -d --wait`. NATS logs "Plaintext passwords detected" at start: the passwords are kept in its environment on purpose (ADR 0074, "Passwords").

**Updating an installation from a release before ADR 0074.** The new release needs the passwords; without them `docker compose pull` (and so `deploy.sh`) stops with `required variable NATS_PASSWORD_... is missing a value` before any container changes. Add them first, then update as usual:

```bash
cd infra/compose
git fetch --tags && git checkout --detach v<x>   # the release's nats/auth.conf and script
./nats-passwords.sh .env
./deploy.sh <x>
```

(If `deploy.sh` already stopped at `pull`, the checkout is done: run `./nats-passwords.sh .env` and `./deploy.sh <x>` again.) The services move their durable JetStream consumers to their own inbox on the first start, without losing their position.

A permission violation shows in the NATS log; `sh nats-violations.sh` lists them (CI runs it after the end-to-end suites). A violation after a code change means the service's subjects are missing from the map: `uv run pytest tools/repo-checks/tests/test_nats_auth.py` names them. A violation right after an update, for a subject the release's `nats/auth.conf` allows, means NATS still runs the permissions of the previous release (next section).

### NATS permissions after an upgrade

NATS reads `nats/auth.conf` when it starts and when it is told to reload (SIGHUP), never by itself. The directory is mounted into the container, so a checkout of a new release puts the new file in front of the running server, but the server keeps the users and permissions it read at its start. `docker compose up` recreates NATS only when its own service definition changed (image, command, environment), which a release that only changes permissions does not do. The services of the new release then ask for something the running NATS does not allow yet and are refused: NATS logs `Permissions Violation for Publish to "$JS.API.CONSUMER.CREATE..."`, the service logs `nats error: ... permissions violation for publish to ...` and, for a consumer it cannot work without, fails to start and restarts.

That is what happened on the update to v0.69.4 (from v0.61.7): v0.69.4 added the consumer `event.identity.access.revoked.*` (the deny list of ADR 0082) to every service that verifies tokens; NATS was not recreated, so Identity, Realtime, Fleet, Scheduler, Telemetry, Planner and Entitlements restart-looped until NATS reloaded its file. Since then:

- `deploy.sh` compares `nats/` of the running and the new release and, when it changed, reloads a running NATS before `up` and waits for "Reloaded server configuration" (a file NATS cannot parse stops the deploy before any service is restarted; NATS keeps its old configuration then).
- A refused deny list subscription no longer stops a service: the list fails open (ADR 0082), the service starts, logs `token deny list unavailable ...` once and `service degraded`, `/readyz` answers 200 with `"token-deny-list": {"status": "degraded"}`, and it subscribes by itself as soon as NATS allows it. Other consumers still stop the start, as before.

By hand, or when an update stopped like this: `docker compose kill -s HUP nats` (or `docker kill -s HUP <project>-nats-1`), check `docker compose logs nats` for "Reloaded server configuration", then `docker compose -f docker-compose.yml -f docker-compose.release.yml up -d --no-build --wait`. Which file a running NATS has loaded: its log names the configuration's SHA-256 at the start ("Using configuration file ... (sha256:...)") and at every reload ("Reloaded server configuration (sha256:...)"); the hash covers the included `auth.conf`. Helm restarts NATS instead (the checksum of `files/nats-auth.conf` on its pods, [Helm](helm.md#nats-and-openfga)).

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

Fleet owns sites, groups and mowers, writes their place in the hierarchy to OpenFGA and sends every command to the Device-Gateway after checking the right the command kind needs (`can_control`, `can_drive`, or `can_emergency_stop` for an emergency stop). `fleet-migrate` runs the database migrations first. Register a simulated mower with `POST /api/v1/fleet/mowers` (`{"name": "Front lawn", "connection_kind": "simulated"}`), start it with `POST /api/v1/fleet/mowers/<id>/commands` (`{"command": {"kind": "start_mow", "area_ids": ["front"]}}`) and follow the command at `GET /api/v1/fleet/commands/<command_id>`. `FLEET_ALLOWED_CONNECTION_KINDS` in `.env` lists the accepted connection kinds (the example enables `simulated,cloud,ble_bridge`; the web app offers exactly these, from `GET /api/v1/fleet/connection-kinds`). Cloud mowers come from a linked Mammotion account ([ADR 0034](../adr/0034-linked-cloud-accounts-and-device-discovery.md)): link it once in the web app (add mower, or Settings, Linked accounts), then pick its mowers; the password is stored encrypted with `FLEET_CREDENTIAL_ENCRYPTION_KEY` (the example value is a marked development key: generate your own and keep a backup). `GATEWAY_FAKE_CLOUDS=true` (development only; the former name `GATEWAY_FAKE_MAMMOTION_CLOUD` still works) replaces the vendor clouds of the enabled providers with fake ones (Mammotion: `*@fake.mowdeck.test`, password `fake-cloud-password`; `shared-*@fake.mowdeck.test` plays a shared account that may not drive). Cloud providers other than Mammotion are off until listed in `GATEWAY_CLOUD_PROVIDERS_ENABLE` ([ADR 0066](../adr/0066-cloud-provider-plugins-and-more-vendors.md), [Cloud providers](../dev/cloud-providers.md)). Manual driving over the cloud ([ADR 0049](../adr/0049-manual-driving-over-the-mammotion-cloud.md): joystick, nudges, AI assist moves and spot runs without a Bluetooth bridge, forwards and turns only, firmware 1.30.31.19 or newer) needs the linked account to be the account that **owns** the mower; a shared account sees and runs everything else, and its drive attempts are refused with a message that says so. Running the same Mammotion account in the phone app or in a script at the same time can drop MowDeck's MQTT session (and the other client's); never run test scripts with MowDeck's account. The cloud mower's option `cloud_drive` (default on) switches cloud driving off. Details: `services/fleet/README.md` and [ADR 0016](../adr/0016-fleet-commands-and-authorization-per-command-kind.md).

## Telemetry

Telemetry stores what the Device-Gateway publishes: the latest state of every mower, a history in which unchanged heartbeats are kept only once per `TELEMETRY_HEARTBEAT_HISTORY_SECONDS`, the position track and the timeline of command results. Read it with `GET /api/v1/telemetry/mowers/<id>/state`, `.../history?step=5s`, `.../positions` and `.../commands`, or the states of all visible mowers with `GET /api/v1/telemetry/accounts/me/mowers/states`; a mower you cannot see answers 404 like an unknown one, and a visible mower that has not reported yet answers 404 "no telemetry yet". The mower's map and capabilities, as the Device-Gateway last read them, are at `.../map` (with an ETag) and `.../capabilities` (404 "no map yet" or "no capabilities yet" before the first report). A retention job (history 30 days, positions 730 with every row kept for the first 90 and older ones thinned in a nightly window, command timeline 30; `TELEMETRY_RETENTION_*_DAYS` and `TELEMETRY_POSITIONS_DETAIL_DAYS` in `.env`, [Telemetry retention](telemetry-retention.md)) deletes older rows, on one replica at a time. Details: `services/telemetry/README.md` and [ADR 0017](../adr/0017-telemetry-storage-and-retention.md).

## Realtime

Realtime pushes the live state and the command results of the mowers a client may view over a WebSocket at `ws://<host>/api/v1/realtime/ws`. The client sends its access token in the first message (`{"type": "auth", "token": "..."}`), then subscribes to mower ids; every id is checked in OpenFGA and checked again when Identity changes grants, every minute and on every token refresh. Traefik needs no extra configuration for the upgrade; protocol pings every `REALTIME_PING_SECONDS` (20) keep idle sockets open. Limits per instance: `REALTIME_MAX_CONNECTIONS_PER_ACCOUNT` (50) and `REALTIME_MAX_CONNECTIONS_PER_USER` (10). `REALTIME_ALLOWED_ORIGINS` (default `http://localhost`) lists the origins allowed to open a socket: add the address you open the web app with when it is not `http://localhost`, including the LAN address a phone on your network uses (for example `REALTIME_ALLOWED_ORIGINS=http://localhost,http://192.168.1.20`). A refused socket closes before the app can say why; realtime logs it at info level with the origin (`websocket refused: origin not allowed`), so `docker compose logs realtime` shows which address to add. Restarting the service closes every socket with 1001; clients reconnect. Realtime is ready when NATS and its two subscriptions are active; an OpenFGA outage keeps open sockets running but denies new subscriptions. Details: `services/realtime/README.md` and [ADR 0018](../adr/0018-realtime-websocket-protocol.md).

## Web app

The service `web` serves the web app (a static SvelteKit PWA in an unprivileged, read-only nginx, port 8080 inside the network) at `/`: open `http://localhost`, register and add a simulated mower. It runs on the same origin as the API, so no CORS is needed. nginx sends a strict Content Security Policy and the other security headers; `/healthz` is its healthcheck, and Traefik waits for it. After a change to the app, `docker compose up -d --build web`; for development use the Vite dev server instead (`pnpm dev`, see `apps/web/README.md`).

**Map background.** By default the map draws a metric grid in the mower's own frame and sends nothing to third parties. `WEB_MAP_TILES` (empty by default) sets a raster tile URL template with `{z}`, `{x}`, `{y}` (and optionally `{s}`), `WEB_MAP_ATTRIBUTION` the attribution text the provider requires (filled in automatically for `openstreetmap.org` URLs), `WEB_MAP_MAX_ZOOM` the highest zoom it delivers (default 19). The `web` container renders them into `/config.json` and adds the tile host to the CSP `img-src` at start (`docker compose up -d web` applies a change); an invalid template is logged and ignored. **Privacy:** with tiles, every map view tells the tile server which tiles are shown, that is where the garden is, together with the viewer's IP address. Respect the provider's usage policy (the public OpenStreetMap tile servers are not meant for apps: https://operations.osmfoundation.org/policies/tiles/); an API key in the template is visible to every user of the app.

**Street map under the theft tracking track.** `BASEMAP_URL` (empty by default) is fetched by Telemetry, not the browser, only around a tracked mower, cached and with the identifying `User-Agent` and cache handling the OpenStreetMap tile usage policy asks for; the recommended value is `https://tile.openstreetmap.org/{z}/{x}/{y}.png` with `BASEMAP_ATTRIBUTION=© OpenStreetMap contributors`. `BASEMAP_COVERAGE` and `AERIAL_COVERAGE` (`south,west,north,east`) say where a source has data (Bavarian aerial photo: `47.27,8.97,50.57,13.84`). Details: [Theft tracking, base map](theft-tracking.md#base-map).

**Live camera.** `WEB_CAMERA_ENABLED=false` by default. With `true` the browser may join the live video of cloud mowers on Agora's servers: the `web` container adds `*.agora.io` and `*.sd-rtn.com` (on any port) to the CSP `connect-src` (and `blob:` to `media-src` and `worker-src`) and sets `cameraEnabled` in `/config.json` at start ([ADR 0032](../adr/0032-live-camera-over-agora.md)). The live video needs HTTPS (or `localhost`). **Privacy:** the video of the garden and the viewer's IP address go to Agora and Mammotion; over 4G the stream counts against the mower's video allowance. The simulated mower's test pattern works without the switch.

**Source code link.** The sign-in page and Settings, About show the build's version and link its source code, and every service names the same address in the `X-Source-Code` response header (AGPL-3.0 section 13). `SOURCE_URL` (default the official repository) sets the repository for every service and the web app; a fork that runs modified code must set its own. Locally built images show `dev` and link the repository root; see [Source code offer](source-code.md).

**HTTPS.** Installing the PWA reliably and Web Push (Task 12) need HTTPS. At home the intended path is Tailscale with its HTTPS certificates (for example `https://mowdeck.<tailnet>.ts.net`) or a reverse proxy with a certificate in front of Traefik. Behind HTTPS set `IDENTITY_REFRESH_COOKIE_SECURE=true` (the refresh cookie gets the `Secure` flag) and `REALTIME_ALLOWED_ORIGINS` to the HTTPS origin, then `docker compose up -d`. Over HTTPS the web app sends `Strict-Transport-Security` (one year, with subdomains, no `preload`; `WEB_HSTS=false` turns it off), never over plain HTTP. Behind your own reverse proxy also set `GATEWAY_TRUSTED_PROXIES` to the proxy's address (comma separated IPs or CIDRs, empty by default): Traefik then keeps the proxy's `X-Forwarded-For` and `X-Forwarded-Proto`, so Identity's login throttling and audit log see each user's real IP instead of the proxy's. Without it Traefik discards incoming `X-Forwarded-*` headers, which also keeps clients from faking their address. Add `GATEWAY_CLIENT_IP_DEPTH=1` (one proxy that sets or appends `X-Forwarded-For`) so Traefik's login and session rate limits count each client instead of the proxy and Identity and Telemetry take the same, unforgeable entry; empty keeps the earlier behaviour ([details](demo.md#client-addresses-behind-a-proxy)). The proxy needs WebSocket upgrade and a long read timeout, and port 80 should only be reachable from the proxy and the LAN; the checklist is in [infra/compose/README.md](https://github.com/lukislp/mowdeck/blob/main/infra/compose/README.md#behind-your-own-reverse-proxy).

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

The service runs as non-root with a read-only root file system (tmpfs at `/tmp`); its healthcheck uses `/startupz` ([Health and routing](#health-and-routing)). Details: `services/device-gateway/README.md`.

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
- **The installation provides one** for users without their own: `VISION_DEFAULT_ENABLED=true`, `VISION_DEFAULT_MODEL`, `VISION_DEFAULT_API_BASE`, `VISION_DEFAULT_LOCAL`, `VISION_DEFAULT_API_KEY` (plan feature `ai_recover_hosted`). Without `VISION_DEFAULT_API_KEY` LiteLLM reads the family's usual variable (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `MISTRAL_API_KEY`), which you can add to the `vision` service's environment.

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

**Local models.** Run Ollama next to the stack (`docker run -d --name ollama --network mowdeck -v ollama:/root/.ollama ollama/ollama`, then `docker exec ollama ollama pull llava`) and use `ollama/llava` with the base URL `http://ollama:11434`; images then never leave the installation, and as the installation model (`VISION_DEFAULT_MODEL=ollama/llava`, `VISION_DEFAULT_API_BASE=http://ollama:11434`) it still needs `VISION_DEFAULT_LOCAL=true` to be treated as local; the setting is the only declaration the service accepts. Small local models answer less reliably: answers that do not match the schema are retried once and otherwise fail, low confidence is never proposed.

**Costs.** One analysis sends one or two images (about 1,000 to 1,600 input tokens each for Claude) and the scene context (about 1,000 tokens) and gets a few hundred output tokens back (plus thinking tokens on current Claude models). With `anthropic/claude-sonnet-5-5` ($2 input and $10 output per million tokens) that is about 1 to 4 US cents per analysis; every request is recorded with its tokens and LiteLLM's estimate (`provider_requests`). Caps: `VISION_USER_DAILY_REQUESTS` per user and day, `VISION_HOSTED_ACCOUNT_DAILY_REQUESTS` per account and day on the installation model.

**Privacy and consent** ([ADR 0081](../adr/0081-explicit-consent-before-cloud-ai.md)). With a cloud model the snapshots (which can show people and the neighbours' property), a map sketch with the user's area names and the mower status go to that provider, and with the chat assistant the typed text and the mower and area names. Every user therefore agrees once, in the app, before AI assist, the automatic analysis or the chat assistant sends anything to a cloud model; the service refuses the request (`403 vision-consent-required`) before a picture is taken without it. The consent is for the provider and model shown, so a user is asked again after changing either, and it can be withdrawn under Settings, AI provider (which also switches the automatic analysis off). No consent is needed for a local model, the offline demo (`fake/...`) or the provider test. A user's own provider is classified by its model and base URL, because the user chose it (Ollama, or an OpenAI-compatible endpoint on a private network, is local; everything else, and everything unknown, is cloud). The installation model is never guessed: it is local only with `VISION_DEFAULT_LOCAL=true`, which you set when it runs on your own hardware (an Ollama or vLLM next to the stack), whatever its prefix or base URL; otherwise it is cloud, because a gateway on your network may forward to a cloud provider and the user cannot tell. The consent also names the endpoint: a user who points the same model at another host is asked again. After an upgrade nobody has agreed yet: users are asked on first use, and automatic analyses pause until their owners agree. The privacy policy template has an "AI features" section to fill in. Images are deleted with the proposal after `VISION_PROPOSAL_TTL_SECONDS` (default 300) unless `VISION_KEEP_IMAGES_DAYS` is set; they are never logged.

**Offline demo with the simulator.** Set `VISION_DEFAULT_ENABLED=true` and `VISION_DEFAULT_MODEL=fake/recover`, register a simulated mower with the connection option `{"stuck_after_seconds": 20}` (development only: the mower gets stuck after 20 s of mowing), start mowing, and use AI assist on the mower page when it reports the error. `VISION_ALLOW_FAKE_PROVIDER=true` also offers the fake to users on the settings page (e2e stack).

**A second stack next to this one.** `MOWDECK_NETWORK` names the Docker network (default `mowdeck`). Every Traefik with the Docker provider sees the labels of all containers on the host, so a second stack (own project name with `docker compose -p`, own network, no host ports besides its gateway) must not carry the same router labels: remove them in an override file (`labels: !reset []`) and route with a Traefik file provider, or pin each Traefik to its own project.

### Photos of a real camera and automatic analyses

A Mammotion mower has no photo command, so without an open browser Vision has no picture of it. The camera relay provides it ([ADR 0045](../adr/0045-camera-relay.md), step 8): with the relay running (profile `relay`, for Mammotion also `agora`, see above), set `GATEWAY_RELAY_URL=http://camera-relay:8000` and a shared `RELAY_FRAMES_TOKEN` (for example `openssl rand -hex 24`) in `infra/compose/.env`, then `docker compose up -d camera-relay device-gateway`. Each photo holds a Vision lease for that one request: while someone watches the camera it is a frame of that stream (no second vendor session); otherwise the relay starts it: AI assist and the automatic analysis are error analyses, which may wake a sleeping mower and are not bound by the Vision budget (refused only when the mower has no connection); routine Vision demand keeps every limit (never wakes a sleeping mower, not below 15 % battery unless charging or below 10 % of the video budget, at most 20 Vision minutes per mower and day) and releases it 15 s after the photo. Vision always gets both front cameras of the same moment (the relay's frame set, analysed as a stereo pair); a camera that does not deliver is left out and the analysis goes on with the other. The relay's log shows `vision frame set served` per photo (with the cameras, any `missing` camera and the `skew_ms` between the two frames). Update the camera relay and the Device-Gateway together: a gateway of this release still gets a single frame from an older relay, and an older gateway keeps asking for single frames.

**The capture worker is removed** (in the first release after v0.20.0; [ADR 0035](../adr/0035-server-side-camera-frames-and-automatic-analysis.md), deprecated by ADR 0045). The relay above is the only source of camera photos for mowers without an own photo function; without it they answer `unsupported` (AI assist then uses the camera panel's frame, automatic analyses record that no photo arrived). An installation that still has the worker's settings can drop them; nothing breaks while they stay, because the Compose file no longer has a `vision-capture` service and the Device-Gateway ignores `MOWDECK_CAPTURE_URL` and `MOWDECK_CAPTURE_TOKEN`:

1. Before the update, check a photo through the relay: start AI assist on the mower page without the camera panel open (or wait for an automatic analysis); the relay's log has `vision frame set served` for the mower, and `docker compose logs device-gateway` shows `camera frame captured` with `source=RelaySnapshotter`.
2. Remove `capture` from `COMPOSE_PROFILES` (and `--profile capture` from your commands), and delete `GATEWAY_CAPTURE_URL`, `CAPTURE_TOKEN`, `CAPTURE_MAX_CONCURRENCY` and `CAPTURE_BROWSER_CHANNEL` from `infra/compose/.env`. `CAPTURE_TIMEOUT_SECONDS` stays: it bounds the relay's frame.
3. A worker container left from an earlier release is an orphan of the project now: the `up` of the update with `--remove-orphans` (and the same `-f` files) removes it; optionally `docker image rm mowdeck/vision-capture:dev`.

Users switch on "Automatische KI-Analyse bei Fehlern" on the AI provider page; when a mower gets stuck, blocked, leaves its area or its wheels slip, Vision takes a photo, asks the user's model and sends "KI-Vorschlag für <Mäher>" as a notification. Limits: `VISION_AUTO_COOLDOWN_MINUTES` (10) per mower, `VISION_AUTO_DAILY_PER_MOWER` (20); `VISION_AUTO_ENABLED=false` switches it off. Nothing moves without an approval.
