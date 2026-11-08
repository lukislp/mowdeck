#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

# Runs on every start of the Codespace (devcontainer.json, postStartCommand): builds the images
# on the first start (several minutes), later starts reuse the Docker cache and only recreate
# what changed, then waits until every service is healthy.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Everything this script prints also goes to a log in the workspace, because a postStartCommand's
# output only shows in the creation log, which is hard to reach once the Codespace is open.
log=".devcontainer/post-start.log"
exec > >(tee "$log") 2>&1
echo "post-start: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Docker-in-Docker starts its daemon when the container starts. A Codespace from a prebuild runs
# this script within seconds of that, often before the daemon answers, and `docker compose up`
# then failed at once ("Cannot connect to the Docker daemon"). Wait for it, up to two minutes.
for _ in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then break; fi
  sleep 2
done
if ! docker info >/dev/null 2>&1; then
  echo "post-start: the Docker daemon did not start; run .devcontainer/post-start.sh again" >&2
  exit 1
fi

docker compose -f infra/compose/docker-compose.yml up -d --build --wait --wait-timeout 900

if [ -n "${CODESPACE_NAME:-}" ]; then
  echo "MowDeck is running: https://${CODESPACE_NAME}-80.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
else
  echo "MowDeck is running: http://localhost"
fi
