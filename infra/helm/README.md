# helm

| Path | Content |
| --- | --- |
| `mowdeck/` | The MowDeck umbrella chart: every service, the web app, migration and authorization bootstrap Jobs, Ingress, and the optional NATS and OpenFGA subcharts and CloudNativePG PostgreSQL |
| `mowdeck/values.yaml` | All settings, commented; `config` and `secrets` use the names of `infra/compose/.env.example` |
| `mowdeck/values-homelab.yaml`, `mowdeck/values-cloud.yaml` | Overlays for one node at home and for a cloud cluster |
| `mowdeck/ci/` | Placeholder values for the CI checks and the kind test (not packaged) |
| `check.sh` | Lint, template, kubeconform `-strict`, required-secret checks and the drift test (CI job `helm`) |
| `kind-test.sh` | Install test in kind with the Phase 1 acceptance test (workflow `helm-kind.yml`) |

```bash
sh infra/helm/check.sh                          # needs helm and kubeconform
SOURCE_TAG=dev E2E=1 sh infra/helm/kind-test.sh   # needs kind, the Compose images and uv
```

Operations manual: [docs/ops/helm.md](../../docs/ops/helm.md); releases (images and the chart in GHCR): [docs/ops/release.md](../../docs/ops/release.md); decision: [ADR 0029](../../docs/adr/0029-helm-chart-and-release-pipeline.md).
