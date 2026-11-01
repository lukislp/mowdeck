# Operating MowDeck on Kubernetes (Helm)

The chart `infra/helm/mowdeck` runs the same images as the Compose stack ([Compose operations](compose.md)) on Kubernetes: every MowDeck service, the web app, the migration and authorization bootstrap jobs and an Ingress, plus NATS, OpenFGA and an in-cluster PostgreSQL when you want them. The decisions behind it are in [ADR 0029](../adr/0029-helm-chart-and-release-pipeline.md); how releases are built and published is in [Releases](release.md).

## Prerequisites

| Tool | Version | Notes |
| --- | --- | --- |
| Kubernetes | 1.29 or newer (tested with 1.37) | the Device-Gateway reads its shard from the pod index label ([StatefulSet pod index label](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/#pod-index-label)) |
| Helm | 3.8 or newer, or 4 (CI uses [4.3.0](https://github.com/helm/helm/releases/tag/v4.3.0)) | OCI charts need 3.8+ ([Helm registries](https://helm.sh/docs/topics/registries/)) |
| Ingress controller | any; examples for [Traefik](https://github.com/traefik/traefik-helm-chart) (default in k3s) | the chart renders standard `networking.k8s.io/v1` Ingress resources |
| [CloudNativePG](https://cloudnative-pg.io/documentation/current/) operator | chart 0.29.1 (operator 1.30.1) | only for the in-cluster PostgreSQL (the default) |
| A default StorageClass | | NATS JetStream and PostgreSQL use PersistentVolumeClaims |

The images are `ghcr.io/lukislp/mowdeck/<service>:<version>` for linux/amd64 and linux/arm64, so a Raspberry Pi 5 runs the same chart. The packages are private like the repository: either make them public ([Releases](release.md#making-the-packages-public)) or create a pull secret and set `imagePullSecrets`:

```bash
kubectl -n mowdeck create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io --docker-username=<user> --docker-password=<token with read:packages>
# values: imagePullSecrets: [{name: ghcr-pull}]
```

## What the chart deploys

| Compose service | Kubernetes | Notes |
| --- | --- | --- |
| traefik | your Ingress controller, two Ingress resources (three with the camera relay) | path routes identical to the Traefik labels |
| nats | [NATS chart](https://github.com/nats-io/k8s/tree/main/helm/charts/nats) 2.15.0 (`nats.enabled`) | JetStream with file storage on a PVC, `nats://mowdeck-nats:4222` |
| postgres | CloudNativePG `Cluster` + one `Database` per service, or an external PostgreSQL | see [PostgreSQL](#postgresql) |
| openfga, openfga-migrate | [OpenFGA chart](https://github.com/openfga/helm-charts) 0.3.16 (`openfga.enabled`) | postgres datastore, migration as an init container, `http://mowdeck-openfga:8080` |
| authz-bootstrap | Job `mowdeck-authz-bootstrap-r<revision>` | writes the model when it changed (idempotent) |
| `<service>-migrate` | Job per service (hook or per revision, see [Upgrades](#upgrades-and-migrations)) | `python -m <package>.adapters.db.migrate` |
| identity, fleet, telemetry, realtime, notifications, scheduler, entitlements, planner, vision | Deployment + Service + ConfigMap | probes on `/healthz` (startup, liveness) and `/readyz` (readiness) |
| device-gateway | StatefulSet + Service + headless Service | one shard per replica ([ADR 0013](../adr/0013-device-gateway-registry-and-sharding.md)) |
| web | Deployment (nginx, uid 101, port 8080) | `/healthz`, runtime config from `WEB_MAP_*` |
| camera-relay, mediamtx | Deployment each (`services.cameraRelay.enabled`) + a third Ingress `mowdeck-media` | off by default, see [Camera relay](#camera-relay-media-plane) |
| agora-bridge | a second container of the camera-relay pod (`services.cameraRelay.agoraBridge`, your own image) | off by default, see [Agora source](#agora-source) |
| matter-server | Deployment with `hostNetwork` and the `Recreate` strategy, a PersistentVolumeClaim (`services.matterServer.enabled`) | off by default; `/api/v1/matter` through the Ingress, API tokens from `secrets.MATTER_API_TOKENS`; pin it to a node in the hub's LAN and firewall that node's ports, see [Matter server](matter.md#helm) |

Every MowDeck pod runs as a non-root user with a read-only root file system, `allowPrivilegeEscalation: false`, all capabilities dropped, the `RuntimeDefault` seccomp profile, a memory-backed `/tmp` and no service account token. Settings come from a ConfigMap per service; secrets are mounted as files under `/run/secrets/mowdeck` and passed as the services' `MOWDECK_<NAME>_FILE` settings (database URL, OpenFGA token, Identity's key encryption key, the VAPID private key), so they never appear in the environment. Database services wait in an init container (`wait-for-schema`) until their database carries the newest migration of their image; services using OpenFGA wait (`wait-for-model`) until the store's latest model is the one of their release.

## Configuration

The Entitlements plan file is a ConfigMap built from `infra/helm/mowdeck/files/plans.yaml`, an identical copy of `infra/entitlements/plans.yaml` (a repository test fails when they differ); change plans there and in the copy, then upgrade the release. Billing ([ADR 0031](../adr/0031-billing-with-stripe-adapter.md)) is off by default (`services.billing.enabled: false`: nothing of it is rendered); enabled, it gets its database URL and the Stripe keys (`secrets.BILLING_STRIPE_SECRET_KEY`, required with `config.BILLING_PROVIDER=stripe`, and `secrets.BILLING_STRIPE_WEBHOOK_SECRET`, or the keys `billing-stripe-secret-key` and `billing-stripe-webhook-secret` of an `existingSecret`) as files, its price file as a ConfigMap built from `files/prices.yaml` (an identical copy of `infra/billing/prices.yaml`), and the Stripe webhook path joins the rate limited `-auth` Ingress; see [Billing operations](billing.md). The CI render (`ci/test-values.yaml`) enables it. The camera relay ([ADR 0045](../adr/0045-camera-relay.md)) is off by default as well (`services.cameraRelay.enabled: false`) and brings its media server along when enabled ([Camera relay](#camera-relay-media-plane)); the CI render enables it too. Vision ([ADR 0033](../adr/0033-vision-assisted-recovery-with-approval.md)) is on by default; it gets `secrets.VISION_SECRETS_KEY` (encrypts the users' provider keys) and `secrets.VISION_DEFAULT_API_KEY` (the installation model, `config.VISION_DEFAULT_*`) as files, both may stay empty ([Compose operations, AI assist](compose.md#ai-assist-vision)). Fleet gets `secrets.FLEET_CREDENTIAL_ENCRYPTION_KEY` (the passwords of linked Mammotion accounts, [ADR 0034](../adr/0034-linked-cloud-accounts-and-device-discovery.md)) as a file; empty means no linked accounts, and an `existingSecret` must hold the keys `fleet-credential-encryption-key` and `fleet-credential-encryption-key-previous` (both may be empty; the second holds earlier keys during a rotation). Keep a backup of it like `IDENTITY_KEY_ENCRYPTION_KEY`. `values.yaml` is fully commented. `config` and `secrets` use the variable names of `infra/compose/.env.example`, so a Compose `.env` translates line by line; a repository test (`tools/repo-checks/tests/test_helm.py`) fails when a variable exists on one side only, when a Compose service has no chart template, or when a Compose service receives a `MOWDECK_*` setting the chart does not pass. The chart's defaults are production defaults (`MOWDECK_ENVIRONMENT=prod`, `MOWDECK_SIM_TIME_SCALE=1`, `NOTIFICATIONS_MOWING_MIN_SECONDS=120`, `IDENTITY_REFRESH_COOKIE_SECURE=true`); the Compose example file carries development values.

| Key | Purpose |
| --- | --- |
| `image.registry`, `image.tag` | MowDeck images; the tag defaults to the chart's `appVersion` (Compose: `MOWDECK_VERSION`) |
| `config.*` | plain settings (`IDENTITY_REGISTRATION`, `FLEET_ALLOWED_CONNECTION_KINDS`, `REALTIME_*`, `NOTIFICATIONS_*`, `SCHEDULER_*`, `ENTITLEMENTS_*`, `WEB_MAP_*`, ...) |
| `secrets.*`, `secrets.existingSecret` | see [Secrets](#secrets) |
| `postgresql.*` | in-cluster CloudNativePG or `external` |
| `ingress.*` | class, host, TLS, annotations; `ingress.auth` for the login paths |
| `services.<name>` | `enabled`, `replicas`, `resources`, `autoscaling`, `pdb`, `extraEnv`, `extraVolumes`, scheduling |
| `migrations.*`, `authzBootstrap.*` | job mode, retries, the wait init containers |
| `config.SOURCE_URL` | repository of the running code for the source code link and `X-Source-Code` (AGPL-3.0 section 13); empty keeps the images' own value, see [Source code offer](source-code.md) |
| `metrics.*` | scrape annotations, ServiceMonitor |
| `nats.*`, `openfga.*` | values of the subcharts |

Two overlays ship with the chart: `values-homelab.yaml` (one replica each, small requests, `owasp_min` Argon2, one PostgreSQL instance, one NATS server) and `values-cloud.yaml` (two replicas of every stateless service with a PodDisruptionBudget and a CPU autoscaler, topology spread, a three node NATS cluster, three OpenFGA replicas, a three instance PostgreSQL cluster, closed registration, TLS through cert-manager and a rate-limit middleware on the login paths).

## Secrets

Nothing secret has a default. A render without the required values fails with a message naming the key:

```text
Error: execution error at (mowdeck/templates/secrets.yaml): secrets.IDENTITY_KEY_ENCRYPTION_KEY is required: generate one (docs/ops/helm.md) and give it with --set, or name an existing Secret in secrets.existingSecret
```

| Value | Required | Generate |
| --- | --- | --- |
| `secrets.OPENFGA_AUTHN_PRESHARED_KEYS` | yes (also for the OpenFGA subchart) | `openssl rand -hex 32` |
| `secrets.OPENFGA_API_TOKEN` | only with several pre-shared keys | one of the keys |
| `secrets.IDENTITY_KEY_ENCRYPTION_KEY` | yes | `python -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"` |
| `secrets.SERVICE_DB_PASSWORD` or `secrets.databasePasswords.<role>` | yes, unless every database URL comes from `existingSecret` | `openssl rand -hex 24` (one per role in production) |
| `secrets.NOTIFICATIONS_VAPID_PRIVATE_KEY` | for more than one Notifications replica (the chart refuses otherwise) | `python -m notifications.cli generate-vapid-keys`; put the public key into `config.NOTIFICATIONS_VAPID_PUBLIC_KEY` |
| `secrets.NATS_PASSWORD_<USER>`, one per NATS user: `BILLING`, `CAMERA_RELAY`, `DEVICE_GATEWAY`, `ENTITLEMENTS`, `FLEET`, `IDENTITY`, `NOTIFICATIONS`, `PLANNER`, `REALTIME`, `SCHEDULER`, `STREAMS`, `TELEMETRY`, `VISION` | yes, all of them (also of disabled services), unless `natsAuth.createSecret: false` | `printf 'n%s\n' "$(openssl rand -hex 24)"` each (24 to 128 letters, digits, `-`, `_`, starting with a letter; the render refuses others) |

Keep them in a values file outside the repository (or encrypted with [SOPS](https://github.com/getsops/sops)) and pass it with `-f`. Alternatively create the Secrets yourself (for example with External Secrets or Sealed Secrets) and reference them:

```yaml
secrets:
  existingSecret: mowdeck-app          # keys: openfga-api-token, identity-key-encryption-key,
  existingSecretHasVapidKey: true      #   database-url-<identity|fleet|telemetry|notifications|scheduler|entitlements|planner>,
                                       #   notifications-vapid-private-key
openfgaSecret:
  create: false                        # Secret "mowdeck-openfga" with the keys "keys" and "uri"
natsAuth:
  createSecret: false                  # Secret "mowdeck-nats-auth", one key per NATS user:
                                       #   billing, camera-relay, device-gateway, entitlements, fleet,
                                       #   identity, notifications, planner, realtime, scheduler,
                                       #   streams, telemetry, vision
postgresql:
  cloudnativepg:
    createRoleSecrets: false           # basic-auth Secrets mowdeck-db-<role> (username, password)
```

The OpenFGA subchart reads its Secret by a fixed name (`openfgaSecret.name`, default `mowdeck-openfga`, which `openfga.authn.preshared.keysSecret` and `openfga.datastore.uriSecret` must repeat), and NATS and OpenFGA have fixed service names (`fullnameOverride`), so install one MowDeck release per namespace.

## PostgreSQL

One database and one login role per service (identity, fleet, telemetry, notifications, scheduler, entitlements, billing, planner, vision, openfga), as in the Compose stack.

**In-cluster with CloudNativePG (default).** Install the operator once per cluster, then the chart renders a `Cluster` (`mowdeck-pg`, `postgresql.cloudnativepg.*`: instances, storage, resources, `extraSpec` for backups and parameters), the roles as [managed roles](https://cloudnative-pg.io/documentation/current/declarative_role_management/) with passwords from basic-auth Secrets, and one [`Database` resource](https://cloudnative-pg.io/documentation/current/declarative_database_management/) per service with `databaseReclaimPolicy: retain`. Services connect to `mowdeck-pg-rw:5432`.

```bash
helm repo add cnpg https://cloudnative-pg.github.io/charts
helm upgrade --install cnpg cnpg/cloudnative-pg --version 0.29.1 -n cnpg-system --create-namespace --wait
```

The superuser is disabled (`enableSuperuserAccess: false`); the services never need it. Unlike the Compose init script, CloudNativePG does not revoke `CONNECT` from `PUBLIC` on the databases; every database is owned by its role and no role has privileges on another service's tables. Use separate clusters (install the chart's Cluster settings per service via `extraSpec`, or an external PostgreSQL per service) if you need strict isolation.

**External or managed PostgreSQL** (a cloud provider's database, or a self-run cluster):

```yaml
postgresql:
  external:
    enabled: true
    host: db.example.org
    port: 5432
    serviceUrlParams: ssl=require      # asyncpg query parameters of the service URLs
  openfgaUriParams: sslmode=require    # pgx parameters of the OpenFGA URI
```

Create the databases and roles first, like `infra/compose/init/01-databases.sql`. With an external database the migrations run as `pre-install`/`pre-upgrade` hooks before any pod changes.

## NATS and OpenFGA

NATS runs as a StatefulSet with JetStream on a PVC (`nats.config.jetstream.fileStore.pvc.size`); `values-cloud.yaml` enables a three node cluster (`nats.config.cluster`), which JetStream needs for replicated streams. The config reloader and nats-box are disabled. To use an existing NATS cluster with JetStream set `nats.enabled: false` and `natsUrl`.

**Users and permissions** ([ADR 0074](../adr/0074-nats-authentication-and-subject-permissions.md)). Every service connects as its own user (`MOWDECK_NATS_USER`, its password as the file `/run/secrets/nats/password` from the Secret `natsAuth.secretName`, only its own key) and may use only the subjects its code uses. The users and permissions are `files/nats-auth.conf` (generated, identical to the Compose stack's `infra/compose/nats/auth.conf`); the chart puts it into the ConfigMap `natsAuth.configMapName`, which the NATS server includes (`nats.config.merge`, `nats.podTemplate.patch`), and gives the server every password from the Secret (`nats.container.env`). The names in those `nats.*` values are fixed: change `natsAuth.secretName` or `natsAuth.configMapName` only together with them. The Job `mowdeck-nats-streams-r<revision>` (identity image) creates and updates the JetStream streams as the user `streams`; the services wait for the streams on start (up to two minutes, then they restart). With an external NATS, configure the users of `files/nats-auth.conf` there (the passwords as the environment variables `NATS_PASSWORD_<USER>` of the server).

Rotating a password: change `secrets.NATS_PASSWORD_<USER>` (or your Secret), `helm upgrade` (the service pods restart through their secrets checksum), then restart NATS so it reads the new value: `kubectl -n mowdeck rollout restart statefulset mowdeck-nats`.

OpenFGA uses the postgres datastore in the `openfga` database; its migration runs as an init container of the OpenFGA pods (`openfga.datastore.migrationType: initContainer`), which retries until the database exists and needs neither the chart's default `k8s-wait-for` image nor extra RBAC. The playground is off and the pods run read-only as uid 65532. For an external OpenFGA set `openfga.enabled: false` and `config.OPENFGA_API_URL`.

The bootstrap Job (`mowdeck-authz-bootstrap-r<revision>`) waits for OpenFGA, creates the store `config.OPENFGA_STORE_NAME` and writes `infra/openfga/model.fga` when it differs from the latest model, exactly like the Compose job ([ADR 0014](../adr/0014-authorization-model-and-tuple-ownership.md)).

## Install at home (k3s or kind)

[k3s](https://docs.k3s.io/) is a good single-node choice (Traefik and a local-path StorageClass are built in):

```bash
curl -sfL https://get.k3s.io | sh -
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
helm upgrade --install cnpg cnpg/cloudnative-pg --version 0.29.1 -n cnpg-system --create-namespace --wait
helm install mowdeck oci://ghcr.io/lukislp/charts/mowdeck --version <version> \
  -n mowdeck --create-namespace -f values-homelab.yaml -f my-secrets.yaml --wait --timeout 15m
```

`values-homelab.yaml` comes from the chart (`helm pull oci://ghcr.io/lukislp/charts/mowdeck --version <version> --untar`). The first install takes a few minutes: the PostgreSQL cluster starts, the migration Jobs and the bootstrap run, and the services wait in their init containers until the schema and the model are there. `kubectl -n mowdeck get cluster,database,jobs,pods` shows the progress.

[kind](https://kind.sigs.k8s.io) on Docker Desktop or Docker Engine is the quickest way to try the chart from the repository: `infra/helm/kind-test.sh` creates a cluster, loads the images, installs the CloudNativePG operator, Traefik and the chart (homelab overlay plus `ci/kind-values.yaml`), upgrades once and optionally runs the Phase 1 acceptance test through Traefik:

```bash
docker compose -f infra/compose/docker-compose.yml build      # images mowdeck/<service>:dev
docker build -t mowdeck/authz-bootstrap:dev -f infra/openfga/bootstrap/Dockerfile infra/openfga
SOURCE_TAG=dev E2E=1 KEEP=1 sh infra/helm/kind-test.sh        # KEEP: leave the cluster running
kind delete cluster --name mowdeck-test
```

With `E2E=1` the script first deploys the e2e marker of `infra/compose/docker-compose.e2e.yml` into the test cluster (a whoami pod behind an Ingress at `/api/v1/e2e-stack`, not part of the chart): the acceptance test refuses any stack without it. The CI workflow `helm-kind.yml` runs the same script (by hand, for release tags and for pull requests that change `infra/helm/` or the workflow; not a required check).

## Install in the cloud

```bash
helm install mowdeck oci://ghcr.io/lukislp/charts/mowdeck --version <version> \
  -n mowdeck --create-namespace -f values-cloud.yaml -f my-cloud.yaml --wait --timeout 15m
```

`my-cloud.yaml` sets at least `ingress.host` and its TLS entry, the secrets (or `existingSecret`), `config.NOTIFICATIONS_VAPID_PUBLIC_KEY`, and either the CloudNativePG sizing or `postgresql.external`. Registration is `closed` in the overlay: the first account registers, further owners are created with `kubectl -n mowdeck exec deploy/mowdeck-identity -- python -m identity.cli create-user`.

**Scaling.** Identity, Fleet, Telemetry, Realtime, Notifications, Scheduler, Entitlements, the Planner (CPU-bound: scale it on CPU) and the web app are stateless and scale horizontally (`autoscaling` renders a `autoscaling/v2` HorizontalPodAutoscaler, by CPU unless `metrics` is given). Realtime needs no sticky sessions; Notifications and the Scheduler coordinate through the database (`SKIP LOCKED`). The Device-Gateway is sharded: `MOWDECK_SHARD_COUNT` is its replica count and fixed per rollout, so changing `services.deviceGateway.replicas` moves mowers between pods (they reconnect on their new owner, [ADR 0013](../adr/0013-device-gateway-registry-and-sharding.md)). An autoscaler would change the replica count without updating the shard count, so `values-cloud.yaml` keeps the gateway's HPA disabled and only shows the intended one: a Pods metric `mowdeck_device_gateway_connections` (open mower connections per pod, served by the [Prometheus Adapter](https://github.com/kubernetes-sigs/prometheus-adapter)). The service does not export that metric yet and the shard hand-over is planned; until then scale the gateway by changing `replicas`.

## Ingress, TLS and WebSockets

The main Ingress routes `/api/v1/<service>` (Prefix) to each API service, `/.well-known/jwks.json` (Exact) to Identity and `/` to the web app; Kubernetes picks the longest matching path, which gives the same precedence as the Traefik priorities of the Compose stack. The second Ingress (`mowdeck-auth`, `ingress.auth`) carries `/api/v1/identity/session`, `/register`, `/token` (Exact) and `/api/v1/identity/session/` (Prefix), so a rate limit can be attached to these paths alone. With Traefik, create the middlewares and reference them ([Traefik RateLimit](https://doc.traefik.io/traefik/middlewares/http/ratelimit/), [Kubernetes Ingress provider](https://doc.traefik.io/traefik/providers/kubernetes-ingress/)):

```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: identity-ratelimit
  namespace: mowdeck
spec:
  rateLimit:
    average: 10        # per period, as in the Compose stack (identity-ratelimit)
    period: 1m
    burst: 20
    sourceCriterion:
      ipStrategy:
        depth: 0       # config.GATEWAY_CLIENT_IP_DEPTH: 1 behind one proxy in front of Traefik
        ipv6Subnet: 64 # an IPv6 client counts by its /64, not per address (Traefik v3, checked with v3.7)
# values: ingress.auth.annotations:
#   traefik.ingress.kubernetes.io/router.middlewares: mowdeck-identity-ratelimit@kubernetescrd
```

The Compose stack uses a looser limit for the refresh path (`identity-session-ratelimit`, 60/min); with Traefik, split it into its own Ingress through `ingress.auth.annotations` per path if you need the difference. Identity trusts `X-Forwarded-For` (`MOWDECK_IDENTITY_TRUST_FORWARDED_FOR=true`), so only the ingress controller may reach it; do not expose the Services otherwise. The chart has no counterpart of the Compose variable `GATEWAY_TRUSTED_PROXIES`: when another proxy or load balancer sits in front of the ingress controller, tell the controller to trust its forwarded headers (for Traefik `--entrypoints.<name>.forwardedHeaders.trustedIPs`, or `proxyProtocol.trustedIPs` for a TCP load balancer), otherwise Identity sees that proxy's address for every user. Then also set `config.GATEWAY_CLIENT_IP_DEPTH` to the number of those proxies (usually `1`): Identity and Telemetry take the `X-Forwarded-For` entry they wrote as the client address (the leftmost entry stays the rule while it is empty), and a rate limit must count the same entry (`sourceCriterion.ipStrategy.depth`), or every user behind the proxy shares one bucket. `ingress.auth.traefikRateLimit.enabled: true` renders that Middleware (`<fullname>-auth-ratelimit`, 10 a minute, burst 20, IPv6 per /64) with the depth and attaches it to the `-auth` Ingress. Set a depth only when the controller trusts just those proxies' forwarded headers and they set or append `X-Forwarded-For`; see [Client addresses behind a proxy](demo.md#client-addresses-behind-a-proxy).

**WebSockets.** The Realtime socket `/api/v1/realtime/ws` needs no annotation with Traefik. Controllers with short idle timeouts need a read timeout above `REALTIME_PING_SECONDS` (20 s); for example HAProxy or nginx-based controllers default to 60 s, which is enough. (The community ingress-nginx controller is retired; see the [Kubernetes announcement](https://kubernetes.io/blog/2025/11/11/ingress-nginx-retirement/).)

**TLS.** With [cert-manager](https://cert-manager.io/docs/usage/ingress/) add `cert-manager.io/cluster-issuer: <issuer>` to `ingress.annotations` and a `tls` entry; cert-manager fills the Secret. Behind HTTPS keep `IDENTITY_REFRESH_COOKIE_SECURE=true`; `REALTIME_ALLOWED_ORIGINS` and `NOTIFICATIONS_APP_URL` default to `https://<ingress.host>`.

**Tailscale at home.** With the [Tailscale Kubernetes operator](https://tailscale.com/kb/1236/kubernetes-operator) set `ingress.className: tailscale` and `ingress.host` to the machine name; the operator exposes the Ingress in the tailnet with a certificate for `<name>.<tailnet>.ts.net` ([cluster ingress](https://tailscale.com/kb/1439/kubernetes-operator-cluster-ingress)). Check the operator's documentation for what its Ingress class supports; the alternative is to keep your normal controller for the path routes and expose that controller's Service in the tailnet (annotation `tailscale.com/expose: "true"`, [exposing a Service](https://tailscale.com/kb/1439/kubernetes-operator-cluster-ingress)).

## Camera relay (media plane)

The camera relay ([ADR 0045](../adr/0045-camera-relay.md)) shares one camera session per account and mower among every viewer and serves it to browsers over WebRTC ([WHEP](https://datatracker.ietf.org/doc/draft-ietf-wish-whep/)). `services.cameraRelay.enabled: true` renders two Deployments, like the Compose profile `relay`:

- **camera-relay** (one replica, its sessions live in memory): answers `relay.camera.*` on NATS, runs the source pipeline (ffmpeg: the simulator's fake source, the agora-bridge muxer and the transcoder for vendor sources), publishes into MediaMTX over RTSP (`MOWDECK_RELAY_MEDIAMTX_RTSP_URL=rtsp://mowdeck-mediamtx:8554`) and drives it through its API (`MOWDECK_RELAY_MEDIAMTX_API_URL=http://mowdeck-mediamtx:9997`). Sources without an available driver are answered with `unsupported_provider` (Agora without the optional [agora-bridge](#agora-source)). It has no Ingress route.
- **mediamtx** ([MediaMTX](https://github.com/bluenviron/mediamtx), MIT, `services.cameraRelay.mediamtx.image`, the `MEDIAMTX_IMAGE` of the Compose stack): one replica with the `Recreate` strategy, uid 65534, read-only root file system, all capabilities dropped, no service account token, TCP probes on the WHEP port. Its config is the ConfigMap built from `files/mediamtx.yml`, an identical copy of `infra/compose/mediamtx/mediamtx.yml` (a repository test fails when they differ). Every publish, read and API call is authorized by camera-relay (`MTX_AUTHHTTPADDRESS=http://mowdeck-camera-relay:8000/internal/mediamtx/auth`): a viewer needs a ticket of a live lease. The Service `mowdeck-mediamtx` exposes WHEP (8889), the API (9997) and RTSP (8554) inside the cluster only; metrics, playback, pprof, RTMP, HLS and SRT are off.

**The ICE port.** The signalling goes through the Ingress, the media does not: MediaMTX sends it on one ICE port for UDP and TCP (`config.RELAY_ICE_PORT`, default 8189), which the chart publishes on the node as a `hostPort` (`services.cameraRelay.mediamtx.hostPort: true`; `hostIP` binds it to one node address like the Compose variable `RELAY_ICE_BIND`). ICE candidates carry the listener port, so the container listens on the same port. MediaMTX announces the addresses of `config.RELAY_ICE_HOSTS` (comma separated, the IPs clients reach the node under, for example its LAN IP); the chart default is empty and must be set, otherwise no candidate reaches the browser. There is no STUN or TURN: open UDP **and** TCP of the ICE port in the node's firewall for the clients in the LAN, and pin the pod to the node whose address you announce (`services.cameraRelay.mediamtx.nodeSelector`). A `hostPort` needs a CNI with the portmap plugin (k3s has it; kind needs an `extraPortMappings` entry); with `hostPort: false` expose the port yourself, for example with a `LoadBalancer` Service whose port equals `RELAY_ICE_PORT`.

```yaml
services:
  cameraRelay:
    enabled: true
    mediamtx:
      nodeSelector: {kubernetes.io/hostname: pi5}
config:
  RELAY_ICE_HOSTS: 192.168.1.20
  RELAY_ICE_PORT: "8189"
```

**The `-media` Ingress.** A third Ingress (`mowdeck-media`, `ingress.media`) routes `/media/whep/r/` (Prefix) to the port `webrtc` (8889) of `mowdeck-mediamtx`; nothing else of MediaMTX is reachable from outside. MediaMTX serves `/r/<relay>/<camera>/whep`, so the controller must strip `/media/whep`, like the Compose middleware `mediamtx-whep-strip`. `ingress.media.annotations` are merged over `ingress.annotations`. With Traefik set `ingress.media.traefikStripPrefix: true`: the chart renders the Middleware `mowdeck-media-strip` (`traefik.io/v1alpha1`, needs Traefik's CRDs) and references it. Without the CRDs in the chart, create it yourself and reference it ([StripPrefix](https://doc.traefik.io/traefik/middlewares/http/stripprefix/)); if you set your own `router.middlewares` annotation, list the strip middleware in it:

```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: media-strip
  namespace: mowdeck
spec:
  stripPrefix:
    prefixes: [/media/whep]
# values: ingress.media.annotations:
#   traefik.ingress.kubernetes.io/router.middlewares: mowdeck-media-strip@kubernetescrd
```

NGINX-based controllers rewrite with a regex path instead ([rewrite](https://kubernetes.github.io/ingress-nginx/examples/rewrite/)):

```yaml
ingress:
  media:
    path: /media/whep(/r/.*)
    pathType: ImplementationSpecific
    annotations:
      nginx.ingress.kubernetes.io/use-regex: "true"
      nginx.ingress.kubernetes.io/rewrite-target: /$1
```

### Agora source

The camera of a Mammotion mower comes from Agora's media cloud; the relay plays it with the optional agora-bridge ([ADR 0045](../adr/0045-camera-relay.md) step 7, `integrations/agora-bridge/README.md`). `services.cameraRelay.agoraBridge.enabled: true` adds it as a second container (`agora-bridge`) of the camera-relay pod: it listens on the pod's loopback (`127.0.0.1:7700`, which the relay always names in `MOWDECK_RELAY_AGORA_BRIDGE_URL`), so the vendor token never crosses the pod network. It runs with the shared security context (uid 10001, read-only root file system, no capabilities), an in-memory `/tmp` of its own and TCP probes on 7700. Without it Agora mowers are answered with `unsupported_provider` and the web app keeps the direct path.

The image contains Agora's proprietary SDK, so **MowDeck publishes none**: build it yourself (`docker build --platform linux/amd64 -t <your registry>/agora-bridge:<tag> integrations/agora-bridge`; building it means accepting Agora's licence), push it to a registry you control (private) and name it:

```yaml
services:
  cameraRelay:
    enabled: true
    agoraBridge:
      enabled: true
      image:
        repository: registry.home.example/mowdeck/agora-bridge   # your own, private
        tag: "2026-10-18"
      maxSessions: 4
```

`enabled: true` with an empty `image.repository` fails the render with a message saying so. The image is x86_64 only: schedule the relay on an amd64 node.

## Upgrades and migrations

```bash
helm upgrade mowdeck oci://ghcr.io/lukislp/charts/mowdeck --version <new> -n mowdeck \
  -f values-homelab.yaml -f my-secrets.yaml --wait --timeout 15m
```

Migrations never run inside a service process. How they run depends on where the database comes from (`migrations.mode: auto`):

- **External PostgreSQL (hook mode):** every service's migration Job is a `pre-install,pre-upgrade` hook ordered by `helm.sh/hook-weight` (identity 1, fleet 2, telemetry 3, notifications 4, scheduler 5, entitlements 6, billing 7, planner 8, vision 9; their database URLs come from a hook Secret with weight -10). Helm waits for all of them before it changes any Deployment, and a failed migration stops the upgrade.
- **In-cluster CloudNativePG (job mode):** the database is created by the release itself and does not exist yet when `pre-install` hooks run, so the migrations are ordinary Jobs named per revision (`mowdeck-<service>-migrate-r<revision>`; Helm removes them with the next upgrade, Kubernetes after `migrations.ttlSecondsAfterFinished`). Each Job first waits in its init container `wait-for-database` (at most `migrations.waitTimeoutSeconds`) until the operator has started PostgreSQL and created the service's role and database, so on a fresh install the migration runs once instead of failing and being retried until the database exists. The new pods wait in `wait-for-schema` until their Job has migrated the database, the old pods keep serving meanwhile.

**Upgrading from a release before ADR 0074** (NATS without authentication): the render fails until every `secrets.NATS_PASSWORD_*` is given (or `natsAuth.createSecret: false` with your own Secret); add them to your secrets values file first. During the rolling update the old pods lose their NATS connection once the server restarts with the users (they have no credentials) until they are replaced; the new pods move their durable JetStream consumers to their own inbox without losing their position.

Migrations are backward compatible (the template's rule), so old pods run against a migrated database during the rolling update, and `wait-for-schema` lets an older image start against a newer schema. Configuration changes roll the pods (checksum annotations). The authorization model follows the same pattern: the bootstrap Job of the new revision writes the model, and the new pods start only once the latest model equals the one in their release (`wait-for-model`), so no pod pins an older model. Rolling back: [Releases](release.md#rolling-back).

## Backups

**PostgreSQL.** One logical dump per service database is the simplest and portable backup (also the way to move between Compose and Kubernetes):

```bash
for db in identity fleet telemetry notifications scheduler entitlements billing planner openfga; do
  kubectl -n mowdeck exec mowdeck-pg-1 -c postgres -- pg_dump -Fc -d "$db" > "$db.dump"
done
# restore into an empty database of the same name:
kubectl -n mowdeck exec -i mowdeck-pg-1 -c postgres -- pg_restore -d identity --clean --if-exists < identity.dump
```

For point-in-time recovery use CloudNativePG's continuous backup to object storage (S3, GCS, Azure Blob) with WAL archiving and `ScheduledBackup`, configured through `postgresql.cloudnativepg.extraSpec` and the Barman Cloud plugin ([CloudNativePG backup](https://cloudnative-pg.io/documentation/current/backup/)); managed databases use the provider's backups.

**NATS JetStream.** The streams hold in-flight commands and events, not the system of record (that is PostgreSQL), so a lost JetStream volume costs only undelivered messages. To keep them across a migration, back up the streams with the NATS CLI (`nats stream backup <stream> <dir>` / `nats stream restore`, [JetStream disaster recovery](https://docs.nats.io/running-a-nats-service/nats_admin/jetstream_admin/disaster_recovery)) or snapshot the `mowdeck-nats-js-*` PVCs with your storage's VolumeSnapshots.

**Secrets.** Back up the values file with the secrets (or the Secret objects) separately; without `IDENTITY_KEY_ENCRYPTION_KEY` the stored signing keys cannot be decrypted.

## Monitoring

Every service serves Prometheus metrics at `/metrics` on port 8000 (HTTP requests, bus messages, authorization checks, service specific counters). The pods carry `prometheus.io/scrape`, `prometheus.io/port` and `prometheus.io/path` annotations (`metrics.podAnnotations`) for annotation based scraping; with the Prometheus Operator set `metrics.serviceMonitor.enabled: true` (one ServiceMonitor per service, `metrics.serviceMonitor.labels` for the operator's selector). OpenFGA exports metrics on port 2112 (annotated by its chart), NATS on its monitoring port 8222 (`nats.promExporter` adds an exporter), CloudNativePG through `postgresql.cloudnativepg.enablePodMonitor`. Logs are JSON on stdout. Useful alerts: pods not ready, restarts, migration or bootstrap Jobs failed, `/readyz` failures, PVC usage of NATS and PostgreSQL.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `helm install` fails with `... is required` | a required secret is missing: see [Secrets](#secrets) |
| `no matches for kind "Cluster" in version "postgresql.cnpg.io/v1"` | the CloudNativePG operator is not installed, or use `postgresql.external` |
| migration Job pod in `Init:0/1`, log `wait-for-database: database not reachable yet` | PostgreSQL still starting or the operator has not created the database yet (`kubectl get cluster,database`) |
| pods stay in `Init:0/2`, log `wait-for-schema: database not reachable yet` | PostgreSQL still starting (`kubectl get cluster`), wrong external host or password; check the migration Job's logs |
| `wait-for-schema: database at [...], waiting for [...]` | the migration Job failed or is still running: `kubectl logs job/mowdeck-<service>-migrate-r<revision>` |
| `wait-for-model: waiting for the authorization model` | the bootstrap Job has not finished: `kubectl logs job/mowdeck-authz-bootstrap-r<revision>`; OpenFGA not ready (`kubectl get pods -l app.kubernetes.io/name=openfga`) |
| OpenFGA pod in `Init:CrashLoopBackOff` | its migration waits for the `openfga` database or has a wrong URI (`mowdeck-openfga` Secret, key `uri`) |
| `ImagePullBackOff` on `ghcr.io/lukislp/mowdeck/...` | private packages: create a pull secret or make the packages public |
| 401 from every service | Identity unreachable for JWKS (`MOWDECK_AUTH_JWKS_URL`) or clocks out of sync |
| login works but the session is lost on reload | plain HTTP with `IDENTITY_REFRESH_COOKIE_SECURE=true`: use HTTPS (or set it to `"false"` for tests) |
| WebSocket closes with 4403 or the handshake is refused | `REALTIME_ALLOWED_ORIGINS` does not contain the origin you open the app with |
| several Notifications replicas, push fails | `NOTIFICATIONS_VAPID_PRIVATE_KEY` not set (the chart refuses this unless the key is given) |

`kubectl -n mowdeck get pods,jobs`, `kubectl -n mowdeck describe pod <pod>` and `kubectl -n mowdeck logs <pod> -c <container>` (containers `wait-for-schema`, `wait-for-model`, the service) answer most questions.

## Checks in CI

`sh infra/helm/check.sh` (CI job `helm`, about a minute) runs `helm dependency build`, `helm lint --strict` and `helm template` for the default values and both overlays, each also with an external PostgreSQL, validates every rendered manifest with [kubeconform](https://github.com/yannh/kubeconform) `-strict` against the Kubernetes 1.37 schemas and the CloudNativePG and Prometheus Operator schemas of the [CRDs-catalog](https://github.com/datreeio/CRDs-catalog), checks that missing secrets fail with their message, and runs the drift test. Locally it needs `helm` and `kubeconform` on the `PATH`. The kind install test (`helm-kind.yml`, [helm/kind-action](https://github.com/helm/kind-action)) runs by hand, for release tags and for pull requests that change `infra/helm/` or the workflow.
