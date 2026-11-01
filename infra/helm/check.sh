#!/bin/sh
# Static checks of the MowDeck chart, no cluster needed (CI job "helm", and locally):
#
#   1. helm dependency build (NATS and OpenFGA charts, versions pinned in Chart.lock)
#   2. helm lint with the default values and both overlays (plus placeholder secrets)
#   3. helm template with the default values and both overlays, validated by kubeconform -strict
#      against the Kubernetes schemas of KUBE_VERSION and the CloudNativePG / Prometheus Operator
#      CRD schemas of the datreeio CRDs-catalog (pinned commit)
#   4. negative checks: a render without the required secrets must fail with a clear message,
#      the NATS users (ADR 0074): every password required and well-formed, the server includes
#      the generated permissions, every service gets its own user
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
expect_failure "GATEWAY_CLIENT_IP_DEPTH must be empty or a number" -f "$test_values" \
  --set-string config.GATEWAY_CLIENT_IP_DEPTH=-1
expect_failure "createRoleSecrets=false" --set secrets.existingSecret=mowdeck-app \
  --set openfgaSecret.create=false
# Existing Secrets (application, OpenFGA, CloudNativePG roles, NATS users) replace every secret
# value.
helm template mowdeck "$chart" --set secrets.existingSecret=mowdeck-app \
  --set openfgaSecret.create=false --set postgresql.cloudnativepg.createRoleSecrets=false \
  --set natsAuth.createSecret=false > /dev/null
echo "ok: secrets.existingSecret renders without secret values"

step "NATS users and permissions (ADR 0074)"
expect_failure "secrets.NATS_PASSWORD_BILLING is required" -f "$test_values" \
  --set secrets.NATS_PASSWORD_BILLING=
expect_failure "secrets.NATS_PASSWORD_FLEET must be 24 to 128" -f "$test_values" \
  --set secrets.NATS_PASSWORD_FLEET=0starts-with-a-digit-0123456789
expect_failure "secrets.NATS_PASSWORD_VISION must be 24 to 128" -f "$test_values" \
  --set secrets.NATS_PASSWORD_VISION=too-short
nats_out="$out/default.yaml"
# The server includes the generated users (the chart's copy of infra/compose/nats/auth.conf) and
# gets every password from the Secret; every service gets its user and only its own password.
grep -q "include ../nats-mowdeck/auth.conf;" "$nats_out"
grep -q "mountPath: /etc/nats-mowdeck" "$nats_out"
grep -q "name: NATS_PASSWORD_STREAMS" "$nats_out"
grep -q 'password: $NATS_PASSWORD_DEVICE_GATEWAY' "$nats_out"
grep -q "MOWDECK_NATS_USER: device-gateway" "$nats_out"
grep -q "value: /run/secrets/nats/password" "$nats_out"
grep -q "name: mowdeck-nats-streams-r1" "$nats_out"
cmp -s "$chart/files/nats-auth.conf" "$root/infra/compose/nats/auth.conf" ||
  { echo "files/nats-auth.conf differs from infra/compose/nats/auth.conf: uv run poe nats-auth"; exit 1; }
echo "ok: NATS authenticates every service with its own user"

step "optional agora-bridge (ADR 0045 step 7)"
# No image is published: enabling it without the operator's own image must fail.
expect_failure "agoraBridge.image.repository is required" -f "$test_values" \
  --set services.cameraRelay.agoraBridge.enabled=true
helm template mowdeck "$chart" --namespace mowdeck -f "$test_values" \
  --set services.cameraRelay.agoraBridge.enabled=true \
  --set services.cameraRelay.agoraBridge.image.repository=registry.example.org/me/agora-bridge \
  > "$out/agora-bridge.yaml"
grep -q "image: registry.example.org/me/agora-bridge:local" "$out/agora-bridge.yaml"
kubeconform -strict -summary -kubernetes-version "$KUBE_VERSION" \
  -schema-location default \
  -schema-location "$CRDS_CATALOG/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json" \
  "$out/agora-bridge.yaml"
echo "ok: the agora-bridge container renders only with an image of the operator's own"

step "optional GitHub sign-in (ADR 0051)"
expect_failure "services.identity.github.existingSecret is required" -f "$test_values" \
  --set services.identity.github.enabled=true --set services.identity.github.clientId=Iv1.x
expect_failure "services.identity.github.clientId is required" -f "$test_values" \
  --set services.identity.github.enabled=true \
  --set services.identity.github.existingSecret=mowdeck-github
helm template mowdeck "$chart" --namespace mowdeck -f "$test_values" \
  --set services.identity.github.enabled=true --set services.identity.github.clientId=Iv1.x \
  --set services.identity.github.existingSecret=mowdeck-github > "$out/github.yaml"
grep -q "MOWDECK_IDENTITY_GITHUB_CLIENT_SECRET_FILE: /run/secrets/github/client-secret" \
  "$out/github.yaml"
grep -q "secretName: mowdeck-github" "$out/github.yaml"
kubeconform -strict -summary -kubernetes-version "$KUBE_VERSION" \
  -schema-location default \
  -schema-location "$CRDS_CATALOG/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json" \
  "$out/github.yaml"
echo "ok: the GitHub client secret is a file from the operator's own Secret"

step "optional Apple sign-in (ADR 0052)"
expect_failure "services.identity.apple.existingSecret is required" -f "$test_values" \
  --set services.identity.apple.enabled=true --set services.identity.apple.servicesId=app.mowdeck.signin \
  --set services.identity.apple.teamId=ABCDE12345 --set services.identity.apple.keyId=KEY1234567
expect_failure "services.identity.apple.teamId is required" -f "$test_values" \
  --set services.identity.apple.enabled=true --set services.identity.apple.servicesId=app.mowdeck.signin \
  --set services.identity.apple.keyId=KEY1234567 --set services.identity.apple.existingSecret=mowdeck-apple
helm template mowdeck "$chart" --namespace mowdeck -f "$test_values" \
  --set services.identity.apple.enabled=true --set services.identity.apple.servicesId=app.mowdeck.signin \
  --set services.identity.apple.teamId=ABCDE12345 --set services.identity.apple.keyId=KEY1234567 \
  --set services.identity.apple.existingSecret=mowdeck-apple > "$out/apple.yaml"
grep -q "MOWDECK_IDENTITY_APPLE_PRIVATE_KEY_FILE: /run/secrets/apple/sign-in-key.p8" "$out/apple.yaml"
grep -q "secretName: mowdeck-apple" "$out/apple.yaml"
# GitHub and Apple together: both secrets are mounted.
helm template mowdeck "$chart" --namespace mowdeck -f "$test_values" \
  --set services.identity.github.enabled=true --set services.identity.github.clientId=Iv1.x \
  --set services.identity.github.existingSecret=mowdeck-github \
  --set services.identity.apple.enabled=true --set services.identity.apple.servicesId=app.mowdeck.signin \
  --set services.identity.apple.teamId=ABCDE12345 --set services.identity.apple.keyId=KEY1234567 \
  --set services.identity.apple.existingSecret=mowdeck-apple > "$out/providers.yaml"
grep -q "secretName: mowdeck-github" "$out/providers.yaml"
grep -q "secretName: mowdeck-apple" "$out/providers.yaml"
kubeconform -strict -summary -kubernetes-version "$KUBE_VERSION" \
  -schema-location default \
  -schema-location "$CRDS_CATALOG/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json" \
  "$out/apple.yaml" "$out/providers.yaml"
echo "ok: the Apple key is a file from the operator's own Secret"

if [ -z "${SKIP_DRIFT:-}" ] && command -v uv > /dev/null 2>&1; then
  step "drift checks (tools/repo-checks/tests/test_helm.py)"
  (cd "$root" && uv run pytest tools/repo-checks/tests/test_helm.py -q -p no:cacheprovider)
fi
echo
echo "helm checks passed"
