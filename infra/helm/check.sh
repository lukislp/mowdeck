#!/bin/sh
# Static checks of the MowDeck chart, no cluster needed (CI job "helm", and locally):
#
#   1. helm dependency build (NATS and OpenFGA charts, versions pinned in Chart.lock)
#   2. helm lint with the default values and both overlays (plus placeholder secrets)
#   3. helm template with the default values and both overlays, validated by kubeconform -strict
#      against the Kubernetes schemas of KUBE_VERSION and the CloudNativePG / Prometheus Operator
#      CRD schemas of the datreeio CRDs-catalog (pinned commit)
#   4. negative checks: a render without the required secrets must fail with a clear message
#   5. the drift test tools/repo-checks/tests/test_helm.py (Compose services <-> templates,
#      .env.example <-> values.yaml), when uv is available and SKIP_DRIFT is not set
#
# Needs helm (3.8+ or 4) and kubeconform on the PATH. Usage: sh infra/helm/check.sh
set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)
chart="$root/infra/helm/mowdeck"
out="${TMPDIR:-/tmp}/mowdeck-helm-check"
KUBE_VERSION="${KUBE_VERSION:-1.37.1}"
CRDS_CATALOG="${CRDS_CATALOG:-https://raw.githubusercontent.com/datreeio/CRDs-catalog/63669a570e231d4f1f8396d229a1de512bcf0a34}"
mkdir -p "$out"

step() { printf '\n== %s\n' "$*"; }

step "helm dependency build"
helm repo add nats https://nats-io.github.io/k8s/helm/charts/ --force-update > /dev/null
helm repo add openfga https://openfga.github.io/helm-charts --force-update > /dev/null
helm dependency build "$chart" > /dev/null

test_values="$chart/ci/test-values.yaml"
for overlay in "" values-homelab.yaml values-cloud.yaml; do
  name=${overlay:-default}
  set -- -f "$test_values"
  [ -n "$overlay" ] && set -- -f "$chart/$overlay" -f "$test_values"

  step "helm lint ($name)"
  helm lint --strict "$chart" "$@"

  step "helm template + kubeconform ($name)"
  helm template mowdeck "$chart" --namespace mowdeck "$@" > "$out/$name.yaml"
  # Every overlay once more with an external PostgreSQL (hook mode, no CloudNativePG resources).
  helm template mowdeck "$chart" --namespace mowdeck "$@" \
    --set postgresql.external.enabled=true --set postgresql.external.host=db.example.org \
    > "$out/$name-external.yaml"
  for file in "$out/$name.yaml" "$out/$name-external.yaml"; do
    kubeconform -strict -summary -kubernetes-version "$KUBE_VERSION" \
      -schema-location default \
      -schema-location "$CRDS_CATALOG/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json" \
      "$file"
  done
done

step "required secrets fail the render"
expect_failure() {
  message=$1; shift
  if helm template mowdeck "$chart" "$@" > /dev/null 2> "$out/error.txt"; then
    echo "expected the render to fail with: $message"; exit 1
  fi
  grep -q "$message" "$out/error.txt" || { cat "$out/error.txt"; exit 1; }
  echo "ok: $message"
}
expect_failure "secrets.OPENFGA_AUTHN_PRESHARED_KEYS is required"
expect_failure "secrets.IDENTITY_KEY_ENCRYPTION_KEY is required" \
  --set secrets.OPENFGA_AUTHN_PRESHARED_KEYS=x
expect_failure "secrets.SERVICE_DB_PASSWORD" \
  --set secrets.OPENFGA_AUTHN_PRESHARED_KEYS=x --set secrets.IDENTITY_KEY_ENCRYPTION_KEY=x
expect_failure "NOTIFICATIONS_VAPID_PRIVATE_KEY" -f "$chart/values-cloud.yaml" \
  --set secrets.OPENFGA_AUTHN_PRESHARED_KEYS=x --set secrets.IDENTITY_KEY_ENCRYPTION_KEY=x \
  --set secrets.SERVICE_DB_PASSWORD=x
expect_failure "createRoleSecrets=false" --set secrets.existingSecret=mowdeck-app \
  --set openfgaSecret.create=false
# Existing Secrets (application, OpenFGA, CloudNativePG roles) replace every secret value.
helm template mowdeck "$chart" --set secrets.existingSecret=mowdeck-app \
  --set openfgaSecret.create=false --set postgresql.cloudnativepg.createRoleSecrets=false \
  > /dev/null
echo "ok: secrets.existingSecret renders without secret values"

if [ -z "${SKIP_DRIFT:-}" ] && command -v uv > /dev/null 2>&1; then
  step "drift checks (tools/repo-checks/tests/test_helm.py)"
  (cd "$root" && uv run pytest tools/repo-checks/tests/test_helm.py -q -p no:cacheprovider)
fi
echo
echo "helm checks passed"
