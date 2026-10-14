# ADR 0029: Helm chart and release pipeline

## Status

Accepted (2026-10-10)

## Context

Phase 4 brings the cloud operating mode of the project plan ("Betriebsarten"): the same images as the Compose stack, run by a Helm chart on Kubernetes, PostgreSQL per service or managed, a NATS cluster, a gateway with a public domain, images for amd64 and arm64, non-root and read-only, configuration only through environment variables or secret files, migrations as a separate step before the start, and only standard interfaces (PostgreSQL, S3, OIDC, NATS). Until now the services carried per-service Helm value stubs (`services/*/helm/values.yaml`) that no chart rendered, and CI built amd64 images only, without publishing them. The repository is private and Actions minutes are scarce.

## Decision

**One umbrella chart, `infra/helm/mowdeck`, without MowDeck subcharts.** One template file per service calls shared named templates (`mowdeck.workload`: ConfigMap, Deployment or StatefulSet, Service, optional HorizontalPodAutoscaler and PodDisruptionBudget; `mowdeck.migrationJob`). The services are released together and share settings (NATS, OpenFGA, JWKS URL, image tag), so per-service charts would only add versions to keep in step. The per-service value stubs and the template's `helm/values.yaml` are removed. `values.yaml` uses the variable names of `infra/compose/.env.example` under `config` and `secrets`; a repository test keeps the two files, the Compose services and the rendered containers' settings in sync. Overlays `values-homelab.yaml` and `values-cloud.yaml` cover the two operating modes.

**Third-party infrastructure as optional dependencies.** NATS (official chart, JetStream with file storage) and OpenFGA (official chart, postgres datastore, migration as an init container instead of its default hook job with `k8s-wait-for`) are chart dependencies with `condition` switches, replaceable by external endpoints.

**PostgreSQL: CloudNativePG by default, external as the alternative.** With the CloudNativePG operator installed, the chart renders one `Cluster` with a managed login role per service and one `Database` resource per service owned by it, the same layout as the Compose init script. `postgresql.external.enabled` switches to an existing or managed PostgreSQL. CloudNativePG is an operator under the CNCF with declarative roles and databases, failover and backups to object storage; the Bitnami chart images are no longer freely maintained, and a hand-written StatefulSet would give no failover or backups.

**Migrations as Jobs, never in the service process; pods wait for their schema.** With an external database every service's migration Job is a `pre-install,pre-upgrade` hook ordered by `helm.sh/hook-weight`, reading its URL from a hook Secret. With the in-cluster database the release creates the database itself, which does not exist yet when pre-install hooks run, so the migrations are ordinary Jobs named per release revision. In both modes an init container (`wait-for-schema`) holds every database service until its database carries the newest migration of its image (or a newer one), so no pod runs against an old schema and `helm install --wait` converges. The authorization bootstrap is an ordinary Job per revision that waits for OpenFGA; an init container (`wait-for-model`, the bootstrap image) holds the OpenFGA users until the latest model equals the one of their release, so no pod pins an older model.

**Secrets never defaulted.** Required secrets fail the render with a message naming the key (`required`), unless an existing Secret is named (`existingSecret`). They are mounted as files and passed as the services' `MOWDECK_<NAME>_FILE` settings.

**Ingress resources instead of Traefik CRDs.** Standard `networking.k8s.io/v1` Ingress resources with the Compose path routes work with any controller (Traefik in k3s, cloud load balancers, the Tailscale operator); a second Ingress carries the login paths, so a controller-specific rate limit can be attached by annotation. Traefik's `IngressRoute` and the Gateway API remain possible later without changing the services.

**Device-Gateway as a StatefulSet.** The shard index is the pod index label, the shard count the replica count ([ADR 0013](0013-device-gateway-registry-and-sharding.md)). Autoscaling the gateway waits for a connections metric and a shard hand-over; the cloud overlay documents the intended autoscaler but leaves it disabled.

**Release pipeline: tags only, multi-arch through QEMU, OCI chart in GHCR.** `release.yml` runs for `v*` tags and by hand: every image is built once for linux/amd64 and linux/arm64 with Buildx, QEMU and the GitHub Actions cache, pushed to `ghcr.io/<owner>/mowdeck/<image>` with SBOM and provenance attestations and an SPDX file, the chart is packaged with `appVersion` = version and pushed to `oci://ghcr.io/<owner>/charts/mowdeck`, and a GitHub release gets generated notes. Pull requests run only the cheap `helm` job (lint, template, kubeconform `-strict`, drift test); the kind install test with the acceptance test runs by hand and for tags. Signing with Cosign is documented, not enabled.

## Consequences

- Kubernetes and Compose deploy the same images with the same settings; a new service needs a chart template, or the drift test fails.
- The in-cluster database requires installing the CloudNativePG operator once per cluster; the chart does not bundle it (cluster-scoped CRDs and controller).
- In job mode a failed migration does not stop `helm upgrade` by itself; the new pods stay in `wait-for-schema` (and `--wait` times out) while the old ones keep serving.
- Fixed names for the OpenFGA Secret and the NATS and OpenFGA services (subchart values cannot be templated): one MowDeck release per namespace.
- The arm64 half of the Python images is built under emulation and is slow; native arm64 runners can replace QEMU without changing the Dockerfiles. The web app's build stage runs on the build platform.
- Packages in GHCR are private like the repository until their visibility is changed.
