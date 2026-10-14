#!/bin/sh
# Install test of the MowDeck chart in a kind cluster (https://kind.sigs.k8s.io), locally (Docker
# Desktop or Docker Engine) and in .github/workflows/helm-kind.yml:
#
#   1. create the cluster (unless it exists) and load the MowDeck images and the pinned NATS,
#      OpenFGA and Traefik images into it (the node never pulls them)
#   2. install the CloudNativePG operator and Traefik (ClusterIP) with their official charts
#   3. install the chart with values-homelab.yaml + ci/kind-values.yaml (in-cluster PostgreSQL,
#      job mode migrations) and wait for every pod and job; then upgrade once (revision 2 jobs,
#      wait-for-schema and wait-for-model with an existing database)
#   4. optionally run the Phase 1 acceptance test (uv run poe e2e) through a port-forward of
#      Traefik
#
# The MowDeck images must exist locally as $REGISTRY/<image>:$TAG, or set SOURCE_TAG to retag the
# Compose builds mowdeck/<image>:$SOURCE_TAG (docker compose ... build), or BUILD=1 to build them.
#
#   SOURCE_TAG=dev E2E=1 sh infra/helm/kind-test.sh
#
# Environment: CLUSTER (mowdeck-test), REGISTRY (ghcr.io/lukislp/mowdeck), TAG (kind),
# SOURCE_TAG, BUILD, E2E (1: run the acceptance test), E2E_ARGS (extra pytest arguments),
# KEEP (1: keep the cluster), PORT (18080), NATS_IMAGE, OPENFGA_IMAGE, TRAEFIK_IMAGE (default:
# the pinned public images of infra/compose/.env.example).
set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)
chart="$root/infra/helm/mowdeck"
CLUSTER="${CLUSTER:-mowdeck-test}"
REGISTRY="${REGISTRY:-ghcr.io/lukislp/mowdeck}"
TAG="${TAG:-kind}"
PORT="${PORT:-18080}"
CNPG_CHART_VERSION=0.29.1
TRAEFIK_CHART_VERSION=41.7.1
images="identity fleet device-gateway telemetry realtime notifications scheduler entitlements planner vision web authz-bootstrap"

listed() { sed -n "s/^$1=//p" "$root/infra/compose/.env.example"; }
NATS_IMAGE="${NATS_IMAGE:-$(listed NATS_IMAGE)}"
OPENFGA_IMAGE="${OPENFGA_IMAGE:-$(listed OPENFGA_IMAGE)}"
TRAEFIK_IMAGE="${TRAEFIK_IMAGE:-$(listed TRAEFIK_IMAGE)}"
ns=mowdeck
step() { printf '\n== %s\n' "$*"; }

diagnose() {
  step "diagnostics"
  kubectl get pods,jobs -A -o wide || true
  kubectl -n "$ns" get cluster,database 2>/dev/null || true
  kubectl -n "$ns" get events --sort-by=.lastTimestamp | tail -40 || true
  for pod in $(kubectl -n "$ns" get pods -o name 2>/dev/null); do
    echo "--- $pod"
    kubectl -n "$ns" logs "$pod" --all-containers --tail=40 2>/dev/null || true
  done
}
pf_pid=""
cleanup() {
  status=$?
  [ -n "$pf_pid" ] && kill "$pf_pid" 2>/dev/null || true
  [ "$status" -ne 0 ] && diagnose
  if [ -z "${KEEP:-}" ]; then kind delete cluster --name "$CLUSTER" || true; fi
  exit "$status"
}
trap cleanup EXIT

step "images"
for image in $images; do
  target="$REGISTRY/$image:$TAG"
  if [ -n "${SOURCE_TAG:-}" ]; then
    docker tag "mowdeck/$image:$SOURCE_TAG" "$target"
  elif [ -n "${BUILD:-}" ]; then
    case "$image" in
      web) docker build -q -f "$root/apps/web/Dockerfile" -t "$target" "$root" ;;
      authz-bootstrap) docker build -q -f "$root/infra/openfga/bootstrap/Dockerfile" -t "$target" "$root/infra/openfga" ;;
      *) docker build -q -f "$root/services/$image/Dockerfile" -t "$target" "$root" ;;
    esac
  fi
  docker image inspect "$target" > /dev/null
done
for image in "$NATS_IMAGE" "$OPENFGA_IMAGE" "$TRAEFIK_IMAGE"; do
  docker image inspect "$image" > /dev/null 2>&1 || docker pull -q "$image"
done

step "kind cluster $CLUSTER"
kind get clusters 2>/dev/null | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER" --wait 120s
kubectl config use-context "kind-$CLUSTER"
# shellcheck disable=SC2046
kind load docker-image --name "$CLUSTER" $(for i in $images; do echo "$REGISTRY/$i:$TAG"; done)
# Pulled images are multi-platform indexes whose other platforms are not stored locally (Docker's
# containerd image store), which `kind load docker-image` cannot import: save the node's platform.
arch=$(docker version --format '{{.Server.Arch}}')
archive=$(mktemp -d)
for image in "$NATS_IMAGE" "$OPENFGA_IMAGE" "$TRAEFIK_IMAGE"; do
  docker save --platform "linux/$arch" -o "$archive/image.tar" "$image"
  kind load image-archive --name "$CLUSTER" "$archive/image.tar"
done
rm -rf "$archive"

step "CloudNativePG operator $CNPG_CHART_VERSION and Traefik $TRAEFIK_CHART_VERSION"
helm repo add cnpg https://cloudnative-pg.github.io/charts --force-update > /dev/null
helm repo add traefik https://traefik.github.io/charts --force-update > /dev/null
helm repo add nats https://nats-io.github.io/k8s/helm/charts/ --force-update > /dev/null
helm repo add openfga https://openfga.github.io/helm-charts --force-update > /dev/null
helm upgrade --install cnpg cnpg/cloudnative-pg --version "$CNPG_CHART_VERSION" \
  -n cnpg-system --create-namespace --wait --timeout 5m
traefik_registry=${TRAEFIK_IMAGE%%/*}
traefik_repository=${TRAEFIK_IMAGE#*/}
helm upgrade --install traefik traefik/traefik --version "$TRAEFIK_CHART_VERSION" \
  -n traefik --create-namespace --wait --timeout 5m \
  --set service.type=ClusterIP \
  --set image.registry="$traefik_registry" \
  --set image.repository="${traefik_repository%:*}" \
  --set image.tag="${TRAEFIK_IMAGE##*:}" \
  --set image.pullPolicy=Never

step "install the MowDeck chart"
helm dependency build "$chart" > /dev/null
install() {
  helm upgrade --install mowdeck "$chart" -n "$ns" --create-namespace \
    -f "$chart/values-homelab.yaml" -f "$chart/ci/kind-values.yaml" \
    --set image.registry="$REGISTRY" --set image.tag="$TAG" \
    --set nats.container.image.fullImageName="$NATS_IMAGE" \
    --set openfga.image.repository="${OPENFGA_IMAGE%:*}" \
    --set openfga.image.tag="${OPENFGA_IMAGE##*:}" \
    --wait --wait-for-jobs --timeout 15m
}
start=$(date +%s)
install
echo "installed in $(( $(date +%s) - start ))s"
kubectl -n "$ns" get pods,jobs

step "upgrade (revision 2)"
start=$(date +%s)
install
echo "upgraded in $(( $(date +%s) - start ))s"
kubectl -n "$ns" get pods,jobs

if [ -n "${E2E:-}" ]; then
  step "Phase 1 acceptance test through Traefik (port-forward :$PORT)"
  kubectl -n traefik port-forward svc/traefik "$PORT:80" > /dev/null 2>&1 &
  pf_pid=$!
  for _ in $(seq 1 30); do
    curl -sf "http://localhost:$PORT/.well-known/jwks.json" > /dev/null && break
    sleep 1
  done
  curl -sf "http://localhost:$PORT/.well-known/jwks.json" > /dev/null
  curl -sf "http://localhost:$PORT/healthz" > /dev/null
  # shellcheck disable=SC2086
  (cd "$root" && MOWDECK_E2E_BASE_URL="http://localhost:$PORT" uv run poe e2e ${E2E_ARGS:-})
fi
step "kind test passed"
