#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

# Writes infra/compose/.env for a Codespace (docs/dev/getting-started.md, ADR 0080).
#
#   .devcontainer/generate-env.sh [target-file]    # default: infra/compose/.env
#
# Starts from .env.example, replaces every secret with a random value of its own (nothing secret
# is committed, the file is git-ignored) and, when the script runs in a Codespace, derives the
# public origin from the two variables Codespaces sets:
#
#   https://$CODESPACE_NAME-80.$GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN
#
# An existing target file is never overwritten (delete it to start over). It never prints a
# secret, only the names of the keys it sets.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$root/infra/compose"
target="${1:-$compose_dir/.env}"

if [ -f "$target" ]; then
  echo "generate-env: $target exists, left unchanged"
  exit 0
fi
cp "$compose_dir/.env.example" "$target"
chmod 600 "$target"

# set_env KEY VALUE: replaces the line KEY=... (or appends it when the key is missing).
set_env() {
  if grep -q "^$1=" "$target"; then
    sed -i "s|^$1=.*|$1=$2|" "$target"
  else
    printf '%s=%s\n' "$1" "$2" >>"$target"
  fi
  echo "generate-env: set $1"
}

# Secrets: one per Codespace. Hex is safe in URLs and in the NATS server configuration.
set_env POSTGRES_PASSWORD "$(openssl rand -hex 24)"
set_env SERVICE_DB_PASSWORD "$(openssl rand -hex 24)"
set_env OPENFGA_AUTHN_PRESHARED_KEYS "$(openssl rand -hex 24)"
# 256-bit keys, base64.
set_env IDENTITY_KEY_ENCRYPTION_KEY "$(openssl rand -base64 32)"
set_env FLEET_CREDENTIAL_ENCRYPTION_KEY "$(openssl rand -base64 32)"
set_env VISION_SECRETS_KEY "$(openssl rand -base64 32)"
# One password per NATS user (ADR 0074); the script prints only variable names.
"$compose_dir/nats-passwords.sh" "$target"

# The public origin. Outside a Codespace the defaults (http://localhost) stay.
if [ -n "${CODESPACE_NAME:-}" ] && [ -n "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-}" ]; then
  origin="https://${CODESPACE_NAME}-80.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
  # TLS ends at GitHub's port forwarder; Traefik receives plain HTTP on port 80.
  set_env IDENTITY_REFRESH_COOKIE_SECURE true
  # Passkeys are bound to the host name of the forwarded URL.
  set_env IDENTITY_PUBLIC_URL "$origin"
  # The browser sends this Origin on the realtime WebSocket; localhost stays for curl and tests.
  set_env REALTIME_ALLOWED_ORIGINS "$origin,http://localhost"
  # Links in notifications.
  set_env NOTIFICATIONS_APP_URL "$origin"
  # Not pinned for a year on a host name that disappears with the Codespace.
  set_env WEB_HSTS false
  echo "generate-env: public origin $origin"
fi
