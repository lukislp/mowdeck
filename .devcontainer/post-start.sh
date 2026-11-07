#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

# Runs on every start of the Codespace (devcontainer.json, postStartCommand): builds the images
# on the first start (several minutes), later starts reuse the Docker cache and only recreate
# what changed, then waits until every service is healthy.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

docker compose -f infra/compose/docker-compose.yml up -d --build --wait --wait-timeout 900

if [ -n "${CODESPACE_NAME:-}" ]; then
  echo "MowDeck is running: https://${CODESPACE_NAME}-80.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
else
  echo "MowDeck is running: http://localhost"
fi
