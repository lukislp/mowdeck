#!/usr/bin/env bash
# (Re)creates the public demo stack (ADR 0065, docs/ops/demo.md): deletes the previous demo with
# all its volumes, starts a fresh one and lets the seed job prepare it. Nothing a visitor did
# survives a reset. Run it by hand or from the timer of docs/ops/demo.md every DEMO_RESET_HOURS.
#
#   demo/reset.sh [--dry-run] [--no-build]
#
# Safety: it only ever touches the project named in demo/demo.env (COMPOSE_PROJECT_NAME, default
# mowdeck-demo), refuses the regular project "mowdeck", any COMPOSE_PROFILES (relay, agora, matter
# and billing do not belong in a demo) and the placeholder secrets of demo.env.example. It never
# prints a secret.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
compose_dir="$(dirname "$here")"
env_file="$here/demo.env"
dry_run=0
build="--build"

die() {
  echo "demo-reset: $*" >&2
  exit 1
}

for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=1 ;;
    --no-build) build="--no-build" ;;
    *)
      echo "usage: $0 [--dry-run] [--no-build]" >&2
      exit 2
      ;;
  esac
done

[ -f "$env_file" ] || die "$env_file is missing (cp demo/demo.env.example demo/demo.env, then fill in the secrets)"

value() {
  # The last assignment of $1 in demo.env, without quotes; empty when not set.
  sed -n -E "s/^$1=//p" "$env_file" | tail -n 1 | sed -E "s/^['\"]?//; s/['\"]?$//"
}

project="$(value COMPOSE_PROJECT_NAME)"
project="${project:-mowdeck-demo}"
case "$project" in
  mowdeck) die "COMPOSE_PROJECT_NAME must not be mowdeck (the regular stack)" ;;
  *[!a-z0-9_-]* | '') die "invalid COMPOSE_PROJECT_NAME" ;;
esac
[ -z "$(value COMPOSE_PROFILES)" ] || die "COMPOSE_PROFILES must stay empty in a demo"
[ -z "${COMPOSE_PROFILES:-}" ] || die "unset COMPOSE_PROFILES: no profile belongs in a demo"
for name in POSTGRES_PASSWORD SERVICE_DB_PASSWORD OPENFGA_AUTHN_PRESHARED_KEYS IDENTITY_KEY_ENCRYPTION_KEY DEMO_SEED_TOKEN; do
  secret="$(value "$name")"
  case "$secret" in
    '' | change-me* | *dev-only*) die "set $name in demo.env (it is empty or a placeholder)" ;;
  esac
done
[ "$(value MOWDECK_NETWORK)" = "$project" ] || die "MOWDECK_NETWORK must be the project name ($project)"
[ "$(value MOWDECK_TRAEFIK_PROJECT)" = "$project" ] || die "MOWDECK_TRAEFIK_PROJECT must be the project name ($project)"

compose=(
  docker compose -p "$project"
  --project-directory "$compose_dir"
  --env-file "$compose_dir/.env.example"
  --env-file "$env_file"
  -f "$compose_dir/docker-compose.yml"
  -f "$compose_dir/docker-compose.demo.yml"
)

run() {
  echo "demo-reset: $*"
  if [ "$dry_run" -eq 0 ]; then
    "$@"
  fi
}

run "${compose[@]}" down --volumes --remove-orphans
run "${compose[@]}" up -d "$build" --wait
if [ "$dry_run" -eq 0 ]; then
  "${compose[@]}" logs --no-log-prefix demo-seed | tail -n 20
fi
echo "demo-reset: done ($project)"
